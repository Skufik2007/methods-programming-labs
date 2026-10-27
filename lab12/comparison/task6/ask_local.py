"""Задача C1 для локальной модели через Ollama (тот же текст промпта, что у Claude).

python comparison/task6/ask_local.py qwen2.5-coder:3b 1 [файл-с-доп-сообщением]
"""

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT = re.search(
    r"```text\n(.*?)\n```", (ROOT / "prompts/task6-models/C1-prompt.md").read_text(encoding="utf-8"), re.S
).group(1)


def main() -> None:
    model, iteration = sys.argv[1], sys.argv[2]
    messages = [{"role": "user", "content": PROMPT}]
    if len(sys.argv) > 3:  # следующая итерация: прошлый ответ и список упавших тестов
        history = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        messages = history
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.2,
                "num_ctx": 8192,
                "num_predict": 4096,
                # Итерация 3 для локальной модели: штатная защита Ollama от зацикливания.
                **({"repeat_penalty": float(os.environ["REPEAT_PENALTY"])} if "REPEAT_PENALTY" in os.environ else {}),
            },
        }
    ).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310 - локальный адрес
        data = json.load(response)
    elapsed = time.perf_counter() - started
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", model)
    out = ROOT / "prompts/task6-models/runs" / f"{name}-{iteration}-response.md"
    out.write_text(data["message"]["content"], encoding="utf-8", newline="\n")
    print(f"{out.name}: {elapsed:.1f} s, {data.get('eval_count')} токенов, done_reason={data.get('done_reason')}")


if __name__ == "__main__":
    main()
