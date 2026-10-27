"""Единый формат ошибок, совместимый с Go-сервисом:

    {"error": {"code": "...", "message": "...", "details": [{"field", "rule", "message"}]}}

Коды HTTP тоже выбраны как в Go: ошибки разбора JSON и типов — 400,
нарушения правил — 422, слишком большое тело — 413.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

# Тип ошибки Pydantic -> имя правила (как теги validator в Go).
_RULES = {
    "missing": "required",
    "string_too_short": "min",
    "too_short": "min",
    "string_too_long": "max",
    "too_long": "max",
    "greater_than": "gt",
    "greater_than_equal": "min",
    "less_than_equal": "max",
}
# Для несовпадения с регулярным выражением правило определяется по полю.
_PATTERN_RULES = {"sku": "sku", "email": "email", "phone": "e164"}


def _message(err: dict[str, Any]) -> str:
    ctx = err.get("ctx") or {}
    kind = err["type"]
    if kind == "missing":
        return "обязательное поле"
    if kind == "string_too_short":
        return f"минимальная длина {ctx.get('min_length')}"
    if kind == "string_too_long":
        return f"максимальная длина {ctx.get('max_length')}"
    if kind == "too_short":
        return f"минимум элементов: {ctx.get('min_length')}"
    if kind == "too_long":
        return f"максимум элементов: {ctx.get('max_length')}"
    if kind == "greater_than":
        return f"значение должно быть больше {ctx.get('gt')}"
    if kind == "greater_than_equal":
        return f"значение должно быть не меньше {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"значение должно быть не больше {ctx.get('le')}"
    if kind == "string_pattern_mismatch":
        return f"не соответствует формату {ctx.get('pattern')}"
    return str(err.get("msg", "некорректное значение"))


def field_path(loc: tuple[Any, ...]) -> str:
    """('body', 'items', 0, 'sku') -> 'items[0].sku'."""
    parts = list(loc[1:] if loc and loc[0] == "body" else loc)
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out


def describe(errors: list[dict[str, Any]]) -> list[dict[str, str]]:
    details = []
    for err in errors:
        # Валидатор уровня списка может указать точное поле в контексте ошибки (см. unique_skus).
        path = (err.get("ctx") or {}).get("field") or field_path(tuple(err["loc"]))
        kind = err["type"]
        if kind == "string_pattern_mismatch":
            rule = _PATTERN_RULES.get(path.rsplit(".", 1)[-1], "pattern")
        else:
            rule = _RULES.get(kind, kind)
        details.append({"field": path, "rule": rule, "message": _message(err)})
    return details


def error_body(code: str, message: str, details: list[dict[str, str]] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


def _is_type_error(kind: str) -> bool:
    return kind.endswith("_type") or kind.endswith("_parsing") or kind in {"model_attributes_type", "dict_type"}


async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = list(exc.errors())
    kinds = {e["type"] for e in errors}

    if "json_invalid" in kinds:
        return JSONResponse(error_body("invalid_json", "синтаксическая ошибка JSON"), status_code=400)
    if any(e["type"] == "missing" and tuple(e["loc"]) == ("body",) for e in errors):
        return JSONResponse(error_body("empty_body", "пустое тело запроса"), status_code=400)
    if "extra_forbidden" in kinds:
        extra = [field_path(tuple(e["loc"])) for e in errors if e["type"] == "extra_forbidden"]
        return JSONResponse(error_body("unknown_field", "неизвестное поле " + ", ".join(extra)), status_code=400)
    typed = [e for e in errors if _is_type_error(e["type"])]
    if typed:
        first = typed[0]
        return JSONResponse(
            error_body("invalid_type", f"поле {field_path(tuple(first['loc']))}: {first['msg']}"),
            status_code=400,
        )
    return JSONResponse(
        error_body("validation_failed", "данные не прошли проверку", describe(errors)),
        status_code=422,
    )


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    # Starlette-исключение — базовое для FastAPI HTTPException и 404/405 роутера.
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        body = {"error": exc.detail}
    else:
        body = error_body("http_error", str(exc.detail))
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
