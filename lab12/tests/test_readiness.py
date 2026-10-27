"""Проверка готовности /api/health/ready."""

import httpx
from sqlalchemy.exc import OperationalError

from app.db import get_session
from app.main import app


async def test_ready_when_database_answers(client: httpx.AsyncClient):
    response = await client.get("/api/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_not_ready_without_database_and_no_driver_details(client: httpx.AsyncClient):
    class BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise OperationalError("SELECT 1", {}, Exception("connection to server at 10.0.0.5 failed"))

    async def broken_session():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken_session
    response = await client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "error"}
    assert "10.0.0.5" not in response.text
