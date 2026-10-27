"""Тесты vitals.dashboard: HTTP и WebSocket через фейковую шину, CSP и заголовки, Hub с медленным клиентом."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from vitals.common import Settings
from vitals.dashboard.app import create_app, csp_for
from vitals.dashboard.hub import Hub

STATIC = Path(__file__).resolve().parents[2] / "python" / "vitals" / "dashboard" / "static"


class FakeBus:
    """Заглушка шины: сохраняет обработчик и не ходит в NATS."""

    def __init__(self) -> None:
        self.handler: Any = None
        self.started = False
        self.closed = False
        self.connected = True

    async def start(self, on_message: Any) -> None:
        self.handler = on_message
        self.started = True

    async def close(self) -> None:
        self.closed = True


def make_app(bus: FakeBus | None = None) -> tuple[Any, FakeBus]:
    bus = bus or FakeBus()
    app = create_app(settings=Settings(), bus=bus)
    return app, bus


def alert(i: int) -> dict[str, Any]:
    return {"type": "alert", "event": "raised", "alert": "tachycardia", "patient_id": f"p-{i}", "ts": "t"}


def asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


# --- HTTP -------------------------------------------------------------------------------------------------------


def test_healthz_reports_status_nats_and_clients_under_lifespan():
    app, bus = make_app()
    with TestClient(app) as client:
        body = client.get("/healthz").json()
    assert body == {"status": "ok", "nats": True, "clients": 0}
    assert bus.started is True and bus.closed is True  # lifespan поднял и закрыл шину


async def test_healthz_over_asgi_transport_without_lifespan():
    app, _ = make_app()
    async with asgi_client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_snapshot_is_empty_placeholder_until_first_message():
    app, _ = make_app()
    async with asgi_client(app) as c:
        r = await c.get("/api/snapshot")
    assert r.json() == {"type": "snapshot", "ts": None, "patients": [], "counters": {}}


async def test_snapshot_returns_last_published_snapshot():
    app, _ = make_app()
    snap = {"type": "snapshot", "ts": "2026-10-13T09:30:00.000Z", "patients": [{"patient_id": "p-1"}], "counters": {}}
    app.state.on_message(snap)
    app.state.on_message(dict(snap, ts="2026-10-13T09:30:02.000Z"))
    async with asgi_client(app) as c:
        r = await c.get("/api/snapshot")
    assert r.json()["ts"] == "2026-10-13T09:30:02.000Z"


async def test_alerts_pagination_is_newest_first_with_total():
    app, _ = make_app()
    for i in range(5):
        app.state.on_message(alert(i))  # p-0 старейшая, p-4 новейшая
    async with asgi_client(app) as c:
        page = (await c.get("/api/alerts", params={"limit": 2, "offset": 1})).json()
        last = (await c.get("/api/alerts", params={"limit": 10, "offset": 4})).json()
        beyond = (await c.get("/api/alerts", params={"limit": 2, "offset": 9})).json()
    assert page["total"] == 5
    assert [i["patient_id"] for i in page["items"]] == ["p-3", "p-2"]
    assert [i["patient_id"] for i in last["items"]] == ["p-0"]
    assert beyond["items"] == []


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
async def test_alerts_rejects_out_of_range_paging(query):
    app, _ = make_app()
    async with asgi_client(app) as c:
        r = await c.get("/api/alerts", params=query)
    assert r.status_code == 422


async def test_security_headers_csp_without_unsafe_inline_and_referrer_policy():
    app, _ = make_app()
    async with asgi_client(app) as c:
        r = await c.get("/healthz")
    csp = r.headers["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert "'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_csp_adds_ws_only_for_valid_host_and_cannot_be_injected():
    assert "ws://localhost:8000" in csp_for("localhost:8000")
    assert "wss://localhost:8000" in csp_for("localhost:8000")
    injected = csp_for("evil.example; script-src 'unsafe-inline'")
    assert "unsafe-inline" not in injected
    assert "script-src 'self';" in injected
    assert csp_for(None).startswith("default-src 'self'")


def test_index_page_is_served_without_inline_script():
    app, _ = make_app()
    with TestClient(app) as client:
        html = client.get("/").text
    assert "<script>" not in html.lower()
    assert "<script src=" in html.lower()


def test_static_js_never_uses_innerhtml():
    files = sorted(STATIC.glob("*.js"))
    assert files, "нет static/*.js"
    for f in files:
        # комментарии не считаем: в app.js есть пояснение «без innerHTML»
        code = re.sub(r"/\*.*?\*/|//[^\n]*", "", f.read_text(encoding="utf-8"), flags=re.S)
        assert "innerHTML" not in code, f.name
        assert "insertAdjacentHTML" not in code, f.name
        assert "outerHTML" not in code, f.name


# --- WebSocket --------------------------------------------------------------------------------------------------


def test_websocket_with_foreign_origin_is_rejected():
    app, _ = make_app()
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
            pass
    assert exc.value.code == 1008


def test_websocket_with_same_origin_gets_init_message_first():
    app, _ = make_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
            first = ws.receive_json()
    assert first == {"type": "init", "snapshot": None, "alerts": []}


def test_websocket_without_origin_is_accepted_and_init_contains_recent_alerts():
    app, _ = make_app()
    app.state.on_message(alert(7))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
    assert first["type"] == "init"
    assert first["alerts"][-1]["patient_id"] == "p-7"


# --- Hub: медленный клиент не блокирует остальных ---------------------------------------------------------------


def test_slow_client_does_not_block_fast_one_and_is_marked_for_resync():
    hub = Hub(client_queue=2)
    slow = hub.connect()  # никогда не читает
    fast = hub.connect()
    received: list[dict[str, Any]] = []
    for i in range(10):
        hub.publish_alert(alert(i))  # publish не ждёт клиентов
        received.append(fast.queue.get_nowait())  # быстрый успевает читать
    assert [m["patient_id"] for m in received] == [f"p-{i}" for i in range(10)]
    assert fast.dropped == 0 and fast.resync is False
    assert slow.dropped == 8 and slow.resync is True
    assert slow.queue.qsize() == 2  # в очереди самые свежие сообщения


def test_resync_flag_makes_snapshot_plus_recent_alerts_available():
    hub = Hub(client_queue=1)
    hub.connect()
    hub.publish_snapshot({"type": "snapshot", "ts": "x", "patients": [], "counters": {}})
    for i in range(3):
        hub.publish_alert(alert(i))
    hello = hub.hello()
    assert hello["snapshot"]["ts"] == "x"
    assert [a["patient_id"] for a in hello["alerts"]] == ["p-0", "p-1", "p-2"]


def test_alerts_page_handles_offset_past_end_and_history_limit():
    hub = Hub(alert_history=3)
    for i in range(5):
        hub.publish_alert(alert(i))
    page = hub.alerts_page(limit=10, offset=0)
    assert page["total"] == 3  # история ограничена
    assert hub.alerts_total == 5
    assert hub.alerts_page(limit=2, offset=5)["items"] == []


def test_disconnect_removes_client():
    hub = Hub()
    c = hub.connect()
    assert hub.client_count == 1
    hub.disconnect(c)
    hub.disconnect(c)  # повторное отключение безопасно
    assert hub.client_count == 0
