# fastmath

Python-модуль на Rust (PyO3 + Maturin), учебный проект для ЛР №9 «Методы программирования».

```python
import fastmath

fastmath.sum_squares([1, 2, 3, 4, 5])   # 55
fastmath.count_primes(1_000_000)        # 78498
fastmath.is_prime(1_000_000_007)        # True
```

Сборка из исходников:

```bash
pip install maturin
maturin develop --release
```
