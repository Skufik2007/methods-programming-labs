"""Находка №4: в CI контейнер дашборда падал с «Directory .../vitals/dashboard/static does not
exist». Статические файлы не входили в колесо, а локально это не было видно из-за установки
в режиме editable. Тест собирает настоящее колесо и проверяет, что в нём есть веб-ресурсы."""

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ["index.html", "app.js", "chart.js", "style.css"]


def test_wheel_contains_dashboard_static(tmp_path: Path) -> None:
    subprocess.run(  # noqa: S603 — фиксированная команда текущего интерпретатора
        [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "-q", "-w", str(tmp_path)],
        check=True,
        timeout=300,
    )
    wheel = next(tmp_path.glob("vitals-*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [f for f in REQUIRED if f"vitals/dashboard/static/{f}" not in names]
    assert not missing, f"в колесе нет статики дашборда: {missing}"
