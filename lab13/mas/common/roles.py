"""Разбор файла роли (раздел 8 контракта): разделы Markdown + YAML после `## Параметры`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROMPT_SECTIONS = ("Роль", "Правила", "Стиль общения", "Специализация")


class RoleError(ValueError):
    """Файл роли нечитаем или не соответствует формату."""


@dataclass(frozen=True)
class Role:
    title: str
    sections: dict[str, str]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return str(self.params["kind"])

    @property
    def step(self) -> str:
        return str(self.params["step"])

    def system_prompt(self) -> str:
        parts = [f"## {name}\n{self.sections[name]}" for name in PROMPT_SECTIONS if self.sections.get(name)]
        return "\n\n".join(parts)


def _split_sections(text: str) -> tuple[str, dict[str, str]]:
    title = ""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif not in_fence and line.startswith("# ") and not title and current is None:
            title = line[2:].strip()
        elif current is not None:
            sections[current].append(line)
    return title, {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _extract_yaml(params_section: str) -> str:
    lines = params_section.splitlines()
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("```yaml"):
            body: list[str] = []
            for inner in lines[i + 1 :]:
                if inner.strip().startswith("```"):
                    return "\n".join(body)
                body.append(inner)
            raise RoleError("блок yaml в разделе «Параметры» не закрыт")
    raise RoleError("в разделе «Параметры» нет блока ```yaml")


def parse_role(text: str) -> Role:
    title, sections = _split_sections(text)
    if "Параметры" not in sections:
        raise RoleError("нет раздела «## Параметры»")
    try:
        params = yaml.safe_load(_extract_yaml(sections["Параметры"]))
    except yaml.YAMLError as exc:
        raise RoleError(f"некорректный YAML в роли: {exc}") from exc
    if not isinstance(params, dict):
        raise RoleError("параметры роли должны быть отображением YAML")
    for key in ("kind", "step"):
        if not params.get(key):
            raise RoleError(f"в параметрах роли нет обязательного поля {key!r}")
    title = title.removeprefix("Роль:").strip() or title
    return Role(title=title, sections=sections, params=params)


def load_role(path: str | Path) -> Role:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleError(f"не удалось прочитать файл роли {path}: {exc}") from exc
    return parse_role(text)
