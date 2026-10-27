"""Находка №2: на Windows при перенаправлении stderr в файл логи Python-компонентов
писались в кодовой странице ANSI (cp1251), а логи Go-агентов — в UTF-8: общий журнал
не читался. Проверяется в отдельном процессе без PYTHONIOENCODING."""

import os
import subprocess
import sys

SCRIPT = "from mas.common.logging import setup_logging; setup_logging().info('агент запущен')"


def test_console_log_is_utf8_when_redirected() -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    proc = subprocess.run(  # noqa: S603 — фиксированная команда текущего интерпретатора
        [sys.executable, "-c", SCRIPT], capture_output=True, env=env, check=True, timeout=30
    )
    assert "агент запущен" in proc.stderr.decode("utf-8")
