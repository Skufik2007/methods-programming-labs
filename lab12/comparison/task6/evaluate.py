"""Извлекает решение модели из ответа и прогоняет скрытые тесты.

    python comparison/task6/evaluate.py prompts/task6-models/runs/opus-1-response.md opus 1

Код берётся из первого блока ```python ответа без правок и сохраняется в
comparison/task6/solutions/<модель>/iter<N>/slots.py; результат прогона —
рядом, в pytest.txt (строки упавших тестов затем уходят модели в следующей итерации).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    response, model, iteration = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    text = response.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)\n```", text, re.S)
    if not blocks:
        print("в ответе нет блока ```python", file=sys.stderr)
        return 1
    code = max(blocks, key=len)  # модуль — самый длинный блок (в пояснениях бывают короткие примеры)
    out_dir = ROOT / "comparison" / "task6" / "solutions" / model / f"iter{iteration}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "slots.py").write_text(code + "\n", encoding="utf-8", newline="\n")
    env = {**os.environ, "SLOTS_IMPL": str(out_dir / "slots.py"), "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(  # noqa: S603 - фиксированная команда
        [sys.executable, "-m", "pytest", "comparison/task6", "-q", "-p", "no:cacheprovider", "--tb=line", "-rf"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    report = result.stdout + result.stderr
    (out_dir / "pytest.txt").write_text(report, encoding="utf-8", newline="\n")
    print(report.strip().splitlines()[-1] if report.strip() else "нет вывода")
    return 0


if __name__ == "__main__":
    sys.exit(main())
