"""Извлекает файлы из ответа модели (Markdown) в проект без ручных правок.

Ожидаемый формат ответа: заголовок `### путь/к/файлу` (путь может быть в обратных
кавычках), за ним — блок кода с полным содержимым файла. Между заголовком и блоком
допускается поясняющий текст. Путь — ASCII, с точкой или косой чертой (`app/main.py`,
`.dockerignore`), либо одно из имён без расширения (`Dockerfile`, `Makefile`).

    python prompts/tools/extract.py prompts/task1-webapp/P1-response.md
    python prompts/tools/extract.py <ответ.md> --dry-run      # только показать список

Так в репозитории видно, какой код написала модель: файлы коммитятся сразу после
извлечения, а все правки идут отдельными коммитами.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADING = re.compile(r"^#{2,4}\s+`?(?P<path>[A-Za-z0-9_./-]+)`?\s*$")
BARE_NAMES = frozenset({"Dockerfile", "Makefile"})
FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})")


def extract(markdown: str) -> dict[str, str]:
    files: dict[str, str] = {}
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        match = HEADING.match(lines[i])
        if not match or not ("." in match["path"] or "/" in match["path"] or match["path"] in BARE_NAMES):
            i += 1
            continue
        path = match["path"]
        # Ищем открывающий блок кода после заголовка; пояснения до него пропускаются,
        # но не дальше следующего заголовка.
        j = i + 1
        while j < len(lines) and not FENCE.match(lines[j]) and not lines[j].startswith("#"):
            j += 1
        fence = FENCE.match(lines[j]) if j < len(lines) else None
        if not fence:
            i += 1
            continue
        closing = fence["fence"]
        body: list[str] = []
        k = j + 1
        while k < len(lines) and not lines[k].startswith(closing):
            body.append(lines[k])
            k += 1
        files[path] = "\n".join(body) + "\n"
        i = k + 1
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("response", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    files = extract(args.response.read_text(encoding="utf-8"))
    if not files:
        print("в ответе не найдено ни одного файла", file=sys.stderr)
        return 1
    for path, content in files.items():
        target = (ROOT / path).resolve()
        if ROOT not in target.parents:
            print(f"пропущен путь вне репозитория: {path}", file=sys.stderr)
            continue
        print(f"{path} ({content.count(chr(10))} строк)")
        if not args.dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
