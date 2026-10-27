//! Задание 8 (Rust): Python-модуль на Rust, собирается Maturin и импортируется в Python.
//!
//! Вычислительная логика вынесена в обычные Rust-функции (их проверяет `cargo test`),
//! а `#[pyfunction]`-обёртки только переводят типы и ошибки в Python.
//! Тяжёлые функции отпускают GIL, чтобы не блокировать другие потоки Python.

use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;

/// Верхняя граница для решета. Решето хранит 1 бит на нечётное число,
/// поэтому для 1e9 нужно около 60 МБ памяти.
pub const MAX_SIEVE_LIMIT: usize = 1_000_000_000;

/// Сумма квадратов с проверкой переполнения i64.
pub fn sum_squares_checked(numbers: &[i64]) -> Option<i64> {
    numbers.iter().try_fold(0i64, |acc, &x| {
        x.checked_mul(x).and_then(|sq| acc.checked_add(sq))
    })
}

/// Решето Эратосфена только по нечётным числам с битовой упаковкой:
/// бит k отвечает за число 2k + 1 и установлен, если число составное.
struct Sieve {
    limit: usize,
    composite: Vec<u64>,
}

impl Sieve {
    fn new(limit: usize) -> Self {
        // Нечётных чисел в 0..=limit ровно (limit + 1) / 2; при limit = 0 массив пуст.
        let odd_count = limit.div_ceil(2);
        let mut composite = vec![0u64; odd_count.div_ceil(64)];
        let mut p = 3;
        while p * p <= limit {
            if composite[p / 2 / 64] >> (p / 2 % 64) & 1 == 0 {
                // Начинаем с p², шагаем по 2p — чётные кратные в решете не хранятся.
                let mut j = p * p;
                while j <= limit {
                    composite[j / 2 / 64] |= 1 << (j / 2 % 64);
                    j += 2 * p;
                }
            }
            p += 2;
        }
        Sieve { limit, composite }
    }

    fn is_prime(&self, n: usize) -> bool {
        match n {
            _ if n > self.limit => panic!("{n} вне решета до {}", self.limit),
            0 | 1 => false,
            2 => true,
            _ if n.is_multiple_of(2) => false,
            _ => self.composite[n / 2 / 64] >> (n / 2 % 64) & 1 == 0,
        }
    }

    fn primes(&self) -> impl Iterator<Item = usize> + '_ {
        let odd = (3..=self.limit).step_by(2).filter(|&n| self.is_prime(n));
        (self.limit >= 2).then_some(2).into_iter().chain(odd)
    }
}

pub fn count_primes_upto(limit: usize) -> usize {
    if limit < 2 {
        return 0;
    }
    // Простые = 2 + все нечётные, кроме 1 и составных. Составные считаем через
    // popcount по словам — это быстрее, чем проверять каждое число отдельно.
    // Хвостовые биты последнего слова (за пределами решета) всегда нулевые.
    let sieve = Sieve::new(limit);
    let odd_count = limit.div_ceil(2);
    let composites: usize = sieve
        .composite
        .iter()
        .map(|w| w.count_ones() as usize)
        .sum();
    1 + (odd_count - 1) - composites
}

pub fn primes_upto(limit: usize) -> Vec<u64> {
    Sieve::new(limit).primes().map(|n| n as u64).collect()
}

/// Детерминированный тест Миллера — Рабина, точен для всех u64.
pub fn is_prime_u64(n: u64) -> bool {
    if n < 2 {
        return false;
    }
    const BASES: [u64; 12] = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37];
    for &p in &BASES {
        if n.is_multiple_of(p) {
            return n == p;
        }
    }
    let mut d = n - 1;
    let mut s = 0;
    while d.is_multiple_of(2) {
        d /= 2;
        s += 1;
    }
    'witness: for &a in &BASES {
        let mut x = pow_mod(a, d, n);
        if x == 1 || x == n - 1 {
            continue;
        }
        for _ in 1..s {
            x = mul_mod(x, x, n);
            if x == n - 1 {
                continue 'witness;
            }
        }
        return false;
    }
    true
}

fn mul_mod(a: u64, b: u64, m: u64) -> u64 {
    ((a as u128 * b as u128) % m as u128) as u64
}

fn pow_mod(mut base: u64, mut exp: u64, m: u64) -> u64 {
    let mut result = 1;
    base %= m;
    while exp > 0 {
        if exp & 1 == 1 {
            result = mul_mod(result, base, m);
        }
        base = mul_mod(base, base, m);
        exp >>= 1;
    }
    result
}

fn check_limit(limit: usize) -> PyResult<()> {
    if limit > MAX_SIEVE_LIMIT {
        return Err(PyValueError::new_err(format!(
            "limit слишком большой (максимум {MAX_SIEVE_LIMIT})"
        )));
    }
    Ok(())
}

// ---------------------------------------------------------------- Python API

/// Сумма квадратов списка целых чисел.
#[pyfunction]
fn sum_squares(numbers: Vec<i64>) -> PyResult<i64> {
    sum_squares_checked(&numbers).ok_or_else(|| {
        PyOverflowError::new_err("переполнение int64 при вычислении суммы квадратов")
    })
}

/// Количество простых чисел, не превосходящих limit.
#[pyfunction]
fn count_primes(py: Python<'_>, limit: usize) -> PyResult<usize> {
    check_limit(limit)?;
    Ok(py.allow_threads(|| count_primes_upto(limit)))
}

/// Список простых чисел, не превосходящих limit.
#[pyfunction]
fn primes(py: Python<'_>, limit: usize) -> PyResult<Vec<u64>> {
    check_limit(limit)?;
    Ok(py.allow_threads(|| primes_upto(limit)))
}

/// Проверка числа на простоту (тест Миллера — Рабина).
#[pyfunction]
fn is_prime(n: u64) -> bool {
    is_prime_u64(n)
}

#[pymodule]
fn fastmath(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sum_squares, m)?)?;
    m.add_function(wrap_pyfunction!(count_primes, m)?)?;
    m.add_function(wrap_pyfunction!(primes, m)?)?;
    m.add_function(wrap_pyfunction!(is_prime, m)?)?;
    m.add("MAX_SIEVE_LIMIT", MAX_SIEVE_LIMIT)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sum_squares_works() {
        assert_eq!(sum_squares_checked(&[1, 2, 3, 4, 5]), Some(55));
        assert_eq!(sum_squares_checked(&[]), Some(0));
        assert_eq!(sum_squares_checked(&[-3]), Some(9));
        assert_eq!(sum_squares_checked(&[i64::MAX]), None);
        assert_eq!(sum_squares_checked(&[3_000_000_000, 3_000_000_000]), None);
    }

    #[test]
    fn prime_counts() {
        // Граничные случаи: пустое решето (0), только 1, только 2, нечётная и чётная граница.
        for (limit, want) in [
            (0, 0),
            (1, 0),
            (2, 1),
            (3, 2),
            (4, 2),
            (9, 4),
            (10, 4),
            (100, 25),
            (1_000_000, 78_498),
        ] {
            assert_eq!(count_primes_upto(limit), want, "limit = {limit}");
        }
        assert_eq!(primes_upto(20), vec![2, 3, 5, 7, 11, 13, 17, 19]);
    }

    #[test]
    fn miller_rabin_matches_sieve() {
        let sieve = Sieve::new(10_000);
        for n in 0..=10_000u64 {
            assert_eq!(is_prime_u64(n), sieve.is_prime(n as usize), "n = {n}");
        }
        assert!(is_prime_u64(1_000_000_007));
        assert!(is_prime_u64(18_446_744_073_709_551_557)); // наибольшее простое < 2^64
        assert!(!is_prime_u64(3_215_031_751)); // сильное псевдопростое по основаниям 2, 3, 5, 7
    }
}
