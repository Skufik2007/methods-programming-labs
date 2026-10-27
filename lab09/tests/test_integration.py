"""Интеграционные тесты Python <-> Go <-> Rust.

Запуск из корня репозитория:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import shutil
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from common import free_port, start_server, stop_server  # noqa: E402

HAS_GO = shutil.which("go") is not None

try:
    import fastmath
except ImportError:
    fastmath = None


@unittest.skipUnless(HAS_GO, "Go не установлен")
class GoSubprocessTest(unittest.TestCase):
    def test_stats(self):
        from go_subprocess import calculate

        result = calculate([1, 2, 3, 4, 5])
        self.assertEqual(result["sum_squares"], 55)
        self.assertEqual(result["sum"], 15)
        self.assertEqual(result["min"], 1)
        self.assertEqual(result["max"], 5)
        self.assertAlmostEqual(result["mean"], 3.0)

    def test_errors(self):
        from go_subprocess import CalculatorError, calculate

        with self.assertRaises(CalculatorError):
            calculate([])
        with self.assertRaises(CalculatorError):
            calculate([2**40, 2**40])  # переполнение суммы квадратов


@unittest.skipUnless(HAS_GO, "Go не установлен")
class TcpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = start_server("tcpserver", f"127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.server)

    def client(self):
        from tcp_client import TcpClient

        return TcpClient("127.0.0.1", self.port)

    def test_commands(self):
        with self.client() as c:
            self.assertEqual(c.request("ping"), "pong")
            self.assertEqual(c.request("upper", text="абв"), "АБВ")
            self.assertEqual(c.request("reverse", text="abc"), "cba")
            self.assertEqual(c.request("sum", numbers=[1, 2, 3]), 6)

    def test_unknown_command(self):
        from tcp_client import ServerError

        with self.client() as c:
            with self.assertRaises(ServerError):
                c.request("nope")
            # после ошибки соединение продолжает работать
            self.assertEqual(c.request("ping"), "pong")

    def test_many_clients(self):
        def work(i):
            with self.client() as c:
                return c.request("sum", numbers=[i, i])

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, range(20)))
        self.assertEqual(results, [2 * i for i in range(20)])


@unittest.skipUnless(HAS_GO, "Go не установлен")
class MicroserviceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from microservice_client import ComputeClient

        port = free_port()
        cls.server = start_server("microservice", f"127.0.0.1:{port}")
        cls.client = ComputeClient(f"http://127.0.0.1:{port}")

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.server)

    def test_health(self):
        self.assertEqual(self.client.health()["status"], "ok")

    def test_primes(self):
        self.assertEqual(self.client.count_primes(1_000_000)["count"], 78498)
        self.assertEqual(self.client.count_primes(1_000_000, workers=1)["count"], 78498)

    def test_matmul_deterministic(self):
        a = self.client.matmul(100, seed=7)["trace"]
        b = self.client.matmul(100, seed=7)["trace"]
        self.assertEqual(a, b)

    def test_validation(self):
        from microservice_client import ServiceError

        with self.assertRaises(ServiceError) as ctx:
            self.client.matmul(0)
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ServiceError):
            self.client.count_primes(-1)


@unittest.skipIf(fastmath is None, "модуль fastmath не собран (maturin develop)")
class RustModuleTest(unittest.TestCase):
    def test_sum_squares(self):
        self.assertEqual(fastmath.sum_squares([1, 2, 3, 4, 5]), 55)
        self.assertEqual(fastmath.sum_squares([]), 0)
        with self.assertRaises(OverflowError):
            fastmath.sum_squares([2**62, 2**62])

    def test_primes(self):
        from rust_module import py_count_primes

        self.assertEqual(fastmath.count_primes(1_000_000), 78498)
        self.assertEqual(fastmath.count_primes(100_000), py_count_primes(100_000))
        self.assertEqual(fastmath.primes(20), [2, 3, 5, 7, 11, 13, 17, 19])
        self.assertTrue(fastmath.is_prime(1_000_000_007))
        self.assertFalse(fastmath.is_prime(1_000_000_008))

    def test_limit_validation(self):
        with self.assertRaises(ValueError):
            fastmath.count_primes(fastmath.MAX_SIEVE_LIMIT + 1)
        with self.assertRaises(OverflowError):
            fastmath.count_primes(-1)


if __name__ == "__main__":
    unittest.main()
