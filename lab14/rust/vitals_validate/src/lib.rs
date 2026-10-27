//! Валидация показаний носимых датчиков (контракт: docs/ARCHITECTURE.md, разделы 2 и 10).
//! Таблица ниже должна совпадать с `_RANGES` в python/vitals/validation.py
//! (совпадение проверяет тест).

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;
use pyo3::types::PyDict;

/// Коды причин (раздел 10).
pub const OK: u8 = 0;
pub const MISSING: u8 = 1;
pub const OUT_OF_RANGE: u8 = 2;
pub const UNKNOWN_METRIC: u8 = 3;
pub const BAD_UNIT: u8 = 4;

/// (метрика, нижняя граница, верхняя граница, единица). Границы включительно.
pub const RANGES: [(&str, f64, f64, &str); 7] = [
    ("heart_rate", 25.0, 250.0, "bpm"),
    ("spo2", 50.0, 100.0, "%"),
    ("temperature", 30.0, 43.0, "C"),
    ("systolic_bp", 50.0, 260.0, "mmHg"),
    ("diastolic_bp", 30.0, 160.0, "mmHg"),
    ("resp_rate", 4.0, 60.0, "breaths/min"),
    ("steps", 0.0, 400.0, "count"),
];

fn lookup(metric: &str) -> Option<&'static (&'static str, f64, f64, &'static str)> {
    // 7 записей — линейный поиск быстрее хэш-таблицы
    RANGES.iter().find(|r| r.0 == metric)
}

/// Чистая функция валидации одной строки.
/// Приоритет при нескольких нарушениях: 3 (метрика) → 1 (пропуск) → 2 (диапазон) → 4 (единица).
/// `None` в метрике считается неизвестной метрикой, в единице — неверной единицей.
pub fn validate_row(metric: Option<&str>, value: f64, unit: Option<&str>) -> u8 {
    let Some(&(_, lo, hi, expected_unit)) = metric.and_then(lookup) else {
        return UNKNOWN_METRIC;
    };
    if value.is_nan() {
        return MISSING;
    }
    // inf тоже вне диапазона: сравнение ниже его отсекает
    if !(value >= lo && value <= hi) {
        return OUT_OF_RANGE;
    }
    // точное совпадение, без нормализации регистра ("BPM" — ошибка)
    if unit != Some(expected_unit) {
        return BAD_UNIT;
    }
    OK
}

fn validate_slices(
    metric: &[Option<PyBackedStr>],
    value: &[f64],
    unit: &[Option<PyBackedStr>],
) -> Vec<u8> {
    metric
        .iter()
        .zip(value)
        .zip(unit)
        .map(|((m, &v), u)| {
            validate_row(
                m.as_ref().map(|s| s.as_ref() as &str),
                v,
                u.as_ref().map(|s| s.as_ref() as &str),
            )
        })
        .collect()
}

/// validate_columns(metric, value, unit) -> numpy.ndarray[uint8]
#[pyfunction]
fn validate_columns<'py>(
    py: Python<'py>,
    metric: Vec<Option<PyBackedStr>>,
    value: PyReadonlyArray1<'py, f64>,
    unit: Vec<Option<PyBackedStr>>,
) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let n = value.len()?;
    if metric.len() != n || unit.len() != n {
        return Err(PyValueError::new_err(format!(
            "длины входов не совпадают: metric={}, value={}, unit={}",
            metric.len(),
            n,
            unit.len()
        )));
    }
    // readonly view без копии; если массив не непрерывный — as_slice вернёт ошибку
    let value_slice = value.as_slice().map_err(|_| {
        PyValueError::new_err("value должен быть непрерывным одномерным массивом float64")
    })?;
    // PyBackedStr держит ссылку на строки Python и не требует GIL для чтения
    let codes = py.allow_threads(|| validate_slices(&metric, value_slice, &unit));
    Ok(PyArray1::from_vec(py, codes))
}

/// Код, которым Python-обёртка обозначает null в массиве кодов словаря.
pub const NULL_CODE: i32 = -1;

/// Валидация по кодам словаря: строковые столбцы приходят как индексы (int32) в маленький
/// словарь уникальных значений. Строки сравниваются один раз на элемент словаря, а не на
/// строку данных, и массивы кодов передаются из Arrow без копии — перевод миллиона строк в
/// список Python (validate_columns) стоил дороже самой проверки.
pub fn validate_coded_slices(
    metric_codes: &[i32],
    metric_dict: &[Option<String>],
    value: &[f64],
    unit_codes: &[i32],
    unit_dict: &[Option<String>],
) -> Vec<u8> {
    // индекс в RANGES для каждого элемента словаря метрик
    let metric_idx: Vec<Option<usize>> = metric_dict
        .iter()
        .map(|m| m.as_deref().and_then(|m| RANGES.iter().position(|r| r.0 == m)))
        .collect();
    // unit_ok[код единицы][индекс метрики] — единица подходит этой метрике
    let unit_ok: Vec<[bool; RANGES.len()]> = unit_dict
        .iter()
        .map(|u| {
            let mut row = [false; RANGES.len()];
            for (i, r) in RANGES.iter().enumerate() {
                row[i] = u.as_deref() == Some(r.3);
            }
            row
        })
        .collect();
    let lookup_code = |codes: &[i32], i: usize, len: usize| -> Option<usize> {
        let c = codes[i];
        (c >= 0 && (c as usize) < len).then_some(c as usize)
    };
    (0..value.len())
        .map(|i| {
            let Some(mi) = lookup_code(metric_codes, i, metric_idx.len()).and_then(|c| metric_idx[c]) else {
                return UNKNOWN_METRIC;
            };
            let v = value[i];
            if v.is_nan() {
                return MISSING;
            }
            let (_, lo, hi, _) = RANGES[mi];
            if !(v >= lo && v <= hi) {
                return OUT_OF_RANGE;
            }
            match lookup_code(unit_codes, i, unit_ok.len()) {
                Some(u) if unit_ok[u][mi] => OK,
                _ => BAD_UNIT,
            }
        })
        .collect()
}

/// validate_coded(metric_codes, metric_dict, value, unit_codes, unit_dict) -> numpy.ndarray[uint8]
#[pyfunction]
fn validate_coded<'py>(
    py: Python<'py>,
    metric_codes: PyReadonlyArray1<'py, i32>,
    metric_dict: Vec<Option<String>>,
    value: PyReadonlyArray1<'py, f64>,
    unit_codes: PyReadonlyArray1<'py, i32>,
    unit_dict: Vec<Option<String>>,
) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let n = value.len()?;
    if metric_codes.len()? != n || unit_codes.len()? != n {
        return Err(PyValueError::new_err(format!(
            "длины входов не совпадают: metric={}, value={}, unit={}",
            metric_codes.len()?,
            n,
            unit_codes.len()?
        )));
    }
    let contiguous = |name: &str| PyValueError::new_err(format!("{name} должен быть непрерывным одномерным массивом"));
    let (m, v, u) = (
        metric_codes.as_slice().map_err(|_| contiguous("metric_codes"))?,
        value.as_slice().map_err(|_| contiguous("value"))?,
        unit_codes.as_slice().map_err(|_| contiguous("unit_codes"))?,
    );
    let codes = py.allow_threads(|| validate_coded_slices(m, &metric_dict, v, u, &unit_dict));
    Ok(PyArray1::from_vec(py, codes))
}

/// ranges() -> dict[str, (low, high, unit)]
#[pyfunction]
fn ranges(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let d = PyDict::new(py);
    for (m, lo, hi, u) in RANGES {
        d.set_item(m, (lo, hi, u))?;
    }
    Ok(d)
}

#[pymodule]
fn vitals_validate(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(validate_columns, m)?)?;
    m.add_function(wrap_pyfunction!(validate_coded, m)?)?;
    m.add_function(wrap_pyfunction!(ranges, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ok_row() {
        assert_eq!(validate_row(Some("heart_rate"), 72.0, Some("bpm")), OK);
        assert_eq!(validate_row(Some("resp_rate"), 15.0, Some("breaths/min")), OK);
    }

    #[test]
    fn unknown_metric() {
        assert_eq!(validate_row(Some("glucose"), 5.0, Some("mmol")), UNKNOWN_METRIC);
        assert_eq!(validate_row(None, 5.0, Some("bpm")), UNKNOWN_METRIC);
        assert_eq!(validate_row(Some("HEART_RATE"), 72.0, Some("bpm")), UNKNOWN_METRIC);
    }

    #[test]
    fn missing_value() {
        assert_eq!(validate_row(Some("spo2"), f64::NAN, Some("%")), MISSING);
    }

    #[test]
    fn bad_unit() {
        assert_eq!(validate_row(Some("heart_rate"), 72.0, Some("BPM")), BAD_UNIT);
        assert_eq!(validate_row(Some("heart_rate"), 72.0, Some("")), BAD_UNIT);
        assert_eq!(validate_row(Some("heart_rate"), 72.0, None), BAD_UNIT);
        assert_eq!(validate_row(Some("heart_rate"), 72.0, Some("bpm ")), BAD_UNIT);
    }

    #[test]
    fn priority_order() {
        // 3 важнее всего
        assert_eq!(validate_row(Some("x"), f64::NAN, Some("")), UNKNOWN_METRIC);
        // 1 важнее 2 и 4
        assert_eq!(validate_row(Some("spo2"), f64::NAN, Some("BAD")), MISSING);
        // 2 важнее 4
        assert_eq!(validate_row(Some("spo2"), -1.0, Some("BAD")), OUT_OF_RANGE);
    }

    #[test]
    fn bounds_are_inclusive() {
        for (m, lo, hi, u) in RANGES {
            assert_eq!(validate_row(Some(m), lo, Some(u)), OK, "{m} нижняя граница");
            assert_eq!(validate_row(Some(m), hi, Some(u)), OK, "{m} верхняя граница");
            assert_eq!(validate_row(Some(m), lo - 1e-9, Some(u)), OUT_OF_RANGE, "{m} ниже");
            assert_eq!(validate_row(Some(m), hi + 1e-9, Some(u)), OUT_OF_RANGE, "{m} выше");
        }
    }

    #[test]
    fn infinities_out_of_range() {
        assert_eq!(validate_row(Some("steps"), f64::INFINITY, Some("count")), OUT_OF_RANGE);
        assert_eq!(validate_row(Some("steps"), f64::NEG_INFINITY, Some("count")), OUT_OF_RANGE);
    }

    #[test]
    fn coded_matches_row_validation() {
        let metric_dict = vec![Some("heart_rate".to_string()), Some("spo2".to_string()), Some("x".to_string()), None];
        let unit_dict = vec![Some("bpm".to_string()), Some("%".to_string()), Some("BPM".to_string()), None];
        let rows: Vec<(i32, f64, i32)> = vec![
            (0, 72.0, 0), (0, 999.0, 0), (0, f64::NAN, 0), (0, 72.0, 2), (0, 72.0, 1),
            (1, 97.0, 1), (2, 1.0, 0), (3, 1.0, 0), (NULL_CODE, 1.0, 0), (0, 72.0, NULL_CODE),
        ];
        let (mc, v, uc): (Vec<i32>, Vec<f64>, Vec<i32>) =
            rows.iter().fold((vec![], vec![], vec![]), |(mut a, mut b, mut c), &(m, x, u)| {
                a.push(m);
                b.push(x);
                c.push(u);
                (a, b, c)
            });
        let got = validate_coded_slices(&mc, &metric_dict, &v, &uc, &unit_dict);
        let name = |d: &[Option<String>], c: i32| if c < 0 { None } else { d[c as usize].clone() };
        let want: Vec<u8> = rows
            .iter()
            .map(|&(m, x, u)| validate_row(name(&metric_dict, m).as_deref(), x, name(&unit_dict, u).as_deref()))
            .collect();
        assert_eq!(got, want);
    }

    #[test]
    fn spec_examples() {
        assert_eq!(validate_row(Some("heart_rate"), 999.0, Some("bpm")), OUT_OF_RANGE);
        assert_eq!(validate_row(Some("spo2"), -1.0, Some("%")), OUT_OF_RANGE);
        assert_eq!(validate_row(Some("steps"), 0.0, Some("count")), OK);
        assert_eq!(validate_row(Some("temperature"), 43.0, Some("C")), OK);
    }
}
