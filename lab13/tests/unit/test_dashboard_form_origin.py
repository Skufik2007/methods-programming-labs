"""Находка №6: форма «Новое обращение» в браузере получала 403 «Запрос с чужого origin».
Дашборд отдавал Referrer-Policy: no-referrer, а при ней Chromium отправляет POST формы
с заголовком Origin: null — проверка CSRF считала свой же запрос чужим.
Политика same-origin сохраняет Origin для своих запросов и не раскрывает адрес чужим сайтам."""

import httpx

from mas.dashboard.app import create_app


async def test_referrer_policy_keeps_origin_for_own_forms() -> None:
    app = create_app(orchestrator=object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/healthz")
    assert response.headers["Referrer-Policy"] == "same-origin"
