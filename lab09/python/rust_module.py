"""Задание 8: импорт Rust-модуля, собранного Maturin, и его использование из Python.

Перед запуском модуль нужно собрать и установить:
    cd rust/fastmath && maturin develop --release
"""

from __future__ import annotations

import time

from common import configure_stdout

try:
    import fastmath
except ImportError as exc:  # pragma: no cover - подсказка пользователю
    raise SystemExit(
        "Модуль fastmath не установлен. Соберите его: cd rust/fastmath && maturin develop --release"
    ) from exc


def py_count_primes(limit: int) -> int:
    """То же решето Эратосфена на чистом Python — для сравнения скорости."""
    if limit < 2:
        return 0
    flags = bytearray([1]) * (limit + 1)
    flags[0] = flags[1] = 0
    i = 2
    while i * i <= limit:
        if flags[i]:
            flags[i * i :: i] = bytes(len(range(i * i, limit + 1, i)))
        i += 1
    return sum(flags)


def timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


def main() -> None:
    configure_stdout()
    print("Модуль:", fastmath.__file__)
    print("sum_squares([1..5]) =", fastmath.sum_squares([1, 2, 3, 4, 5]))
    print("is_prime(1_000_000_007) =", fastmath.is_prime(1_000_000_007))
    print("primes(30) =", fastmath.primes(30))

    limit = 10_000_000
    rust_res, rust_t = timed(fastmath.count_primes, limit)
    py_res, py_t = timed(py_count_primes, limit)
    assert rust_res == py_res, (rust_res, py_res)
    print(f"\ncount_primes({limit:_}) = {rust_res}")
    print(f"  Rust:   {rust_t * 1000:8.1f} мс")
    print(f"  Python: {py_t * 1000:8.1f} мс  (ускорение x{py_t / rust_t:.1f})")

    try:
        fastmath.sum_squares([2**62, 2**62])
    except OverflowError as exc:
        print("\nПереполнение перехвачено:", exc)


if __name__ == "__main__":
    main()
