# vitals_validate

Rust-валидатор показаний датчиков (PyO3 + numpy, abi3 для Python 3.12).
Контракт — `docs/ARCHITECTURE.md`, разделы 2 и 10.

## Сборка

PowerShell, из корня репозитория (`.venv` уже создан и содержит `maturin`, `numpy`, `polars`):

```powershell
$env:RUSTUP_HOME = "D:\dev\rustup"
$env:CARGO_HOME  = "D:\dev\cargo"
$env:PATH        = "D:\dev\cargo\bin;$env:PATH"
$env:PYO3_PYTHON = (Resolve-Path .venv\Scripts\python.exe)
.venv\Scripts\Activate.ps1
cd rust\vitals_validate
maturin develop --release   # при кириллице в пути нужен $env:PYTHONUTF8 = "1"
```

Модуль устанавливается в `.venv` как `vitals_validate`.

## Тесты

```powershell
cd rust\vitals_validate
cargo test
```

## Использование

```python
import numpy as np, vitals_validate as vv
vv.validate_columns(["heart_rate", "spo2"], np.array([72.0, -1.0]), ["bpm", "%"])  # [0, 2]
vv.ranges()  # {'heart_rate': (25.0, 250.0, 'bpm'), ...}
```

Коды: 0 ок, 1 пропуск/NaN, 2 вне диапазона, 3 неизвестная метрика, 4 неверная единица
(приоритет 3 → 1 → 2 → 4). Разные длины входов -> `ValueError`.
Python-обёртка: `python/vitals/validation.py` (с запасной реализацией на Polars).
