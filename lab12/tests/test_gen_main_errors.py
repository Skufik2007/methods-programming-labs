"""Тесты app/main.py (заголовки безопасности, index.html, статика) и app/errors.py."""

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from app import models
from app.errors import (
    BusinessRuleError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    is_unique_violation,
    register_exception_handlers,
)
from app.main import STATIC_DIR
from app.models import AppointmentStatus, UserRole

_seq = itertools.count(1)

CSP_STRICT_EXPECTED = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
)
APP_DIR = Path(STATIC_DIR).parent


# --------------------------------------------------------------------------- #
# Заголовки безопасности
# --------------------------------------------------------------------------- #

STRICT_PATHS = [
    "/",
    "/static/app.js",
    "/static/app.css",
    "/static/missing.js",
    "/api/health",
    "/api/doctors",  # 401 без токена
    "/api/analytics/summary",  # 422/401
    "/openapi.json",
    "/docsx",
    "/api/docs",
    "/nonexistent",
]


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", STRICT_PATHS)
    async def test_strict_csp_everywhere_except_docs(self, client, path):
        r = await client.get(path)
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    @pytest.mark.parametrize("path", STRICT_PATHS + ["/docs", "/redoc"])
    async def test_nosniff_and_referrer_policy_on_every_response(self, client, path):
        r = await client.get(path)
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"

    @pytest.mark.parametrize("path", ["/docs", "/redoc"])
    async def test_docs_pages_get_relaxed_csp(self, client, path):
        r = await client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in csp
        assert "'unsafe-inline'" in csp
        assert "frame-ancestors 'none'" in csp
        assert csp != CSP_STRICT_EXPECTED

    async def test_docs_with_query_string_still_relaxed(self, client):
        r = await client.get("/docs?x=1")
        assert "'unsafe-inline'" in r.headers["content-security-policy"]

    async def test_strict_csp_has_no_unsafe_directives_or_external_hosts(self, client):
        csp = (await client.get("/")).headers["content-security-policy"]
        assert "unsafe" not in csp
        assert "http" not in csp
        assert "*" not in csp

    async def test_openapi_json_is_not_relaxed(self, client):
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        assert "'unsafe-inline'" not in r.headers["content-security-policy"]

    async def test_headers_on_401_response(self, client):
        r = await client.get("/api/doctors")
        assert r.status_code == 401
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED
        assert r.headers["x-content-type-options"] == "nosniff"

    async def test_headers_on_422_response(self, client, make_user, auth):
        admin = await make_user(UserRole.ADMIN)
        r = await client.get("/api/analytics/summary", headers=auth(admin))
        assert r.status_code == 422
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    async def test_headers_on_405_response(self, client):
        r = await client.post("/")
        assert r.status_code == 405
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED

    async def test_headers_on_domain_error_response(self, client, make_user, auth):
        doctor_user = await make_user(UserRole.DOCTOR)
        r = await client.get("/api/patients/1/medical-records", headers=auth(doctor_user))
        assert r.status_code == 404
        assert r.headers["content-security-policy"] == CSP_STRICT_EXPECTED
        assert r.headers["referrer-policy"] == "no-referrer"


# --------------------------------------------------------------------------- #
# index.html и статика
# --------------------------------------------------------------------------- #


class TestIndexAndStatic:
    async def test_index_returns_html_with_no_cache(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.headers["cache-control"] == "no-cache"
        assert r.content == (STATIC_DIR / "index.html").read_bytes()
        assert "<title>Система управления клиникой</title>" in r.text

    async def test_index_is_served_without_authentication(self, client):
        assert (await client.get("/")).status_code == 200

    async def test_index_not_in_openapi_schema(self, client):
        schema = (await client.get("/openapi.json")).json()
        assert "/" not in schema["paths"]

    async def test_index_has_no_inline_scripts_or_styles(self, client):
        import re

        html = (await client.get("/")).text
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag
        assert "<style" not in html
        assert " style=" not in html
        assert not re.search(r"\son[a-z]+=", html)  # onclick= и т.п. запрещены строгой CSP

    async def test_index_references_only_local_resources(self, client):
        import re

        html = (await client.get("/")).text
        for url in re.findall(r'(?:src|href)="([^"]*)"', html):
            assert url.startswith(("/static/", "#", "data:")), url

    @pytest.mark.parametrize(
        "name,content_type_part",
        [("app.js", "javascript"), ("app.css", "text/css")],
    )
    async def test_static_files_are_served_with_content(self, client, name, content_type_part):
        r = await client.get(f"/static/{name}")
        assert r.status_code == 200
        assert content_type_part in r.headers["content-type"]
        assert r.content == (STATIC_DIR / name).read_bytes()

    async def test_static_index_is_reachable_under_static_too(self, client):
        r = await client.get("/static/index.html")
        assert r.status_code == 200
        assert r.content == (STATIC_DIR / "index.html").read_bytes()

    async def test_missing_static_file_returns_404_json(self, client):
        r = await client.get("/static/nope.js")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}

    async def test_static_directory_listing_is_not_exposed(self, client):
        r = await client.get("/static/")
        assert r.status_code == 404

    @pytest.mark.parametrize(
        "path",
        ["/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py", "/static/%2e%2e%2fconfig.py"],
        ids=["dotdot", "enc-dots", "enc-slash", "enc-both"],
    )
    async def test_static_path_traversal_does_not_leak_sources(self, client, path):
        r = await client.get(path)
        assert r.status_code == 404
        assert "get_settings" not in r.text and "create_app" not in r.text

    async def test_health_returns_ok_without_authentication(self, client):
        r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    async def test_unknown_route_returns_404_json(self, client):
        r = await client.get("/api/unknown")
        assert r.status_code == 404
        assert r.json() == {"detail": "Not Found"}


# --------------------------------------------------------------------------- #
# errors.py: классы и обработчик
# --------------------------------------------------------------------------- #


def _error_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/{kind}")
    async def raise_it(kind: str) -> None:
        cls = {
            "notfound": NotFoundError,
            "conflict": ConflictError,
            "denied": PermissionDeniedError,
            "rule": BusinessRuleError,
            "base": DomainError,
        }[kind]
        raise cls("текст ошибки")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("unexpected")

    return app


@pytest.fixture
async def error_client():
    transport = httpx.ASGITransport(app=_error_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


class TestDomainErrorHandler:
    @pytest.mark.parametrize(
        "kind,status",
        [("notfound", 404), ("conflict", 409), ("denied", 403), ("rule", 422), ("base", 500)],
    )
    async def test_exception_maps_to_status_and_detail(self, error_client, kind, status):
        r = await error_client.get(f"/raise/{kind}")
        assert r.status_code == status
        assert r.json() == {"detail": "текст ошибки"}
        assert r.headers["content-type"] == "application/json"

    @pytest.mark.parametrize(
        "cls,status",
        [(NotFoundError, 404), (ConflictError, 409), (PermissionDeniedError, 403), (BusinessRuleError, 422)],
    )
    def test_status_code_and_detail_attributes(self, cls, status):
        exc = cls("msg")
        assert exc.status_code == status
        assert exc.detail == "msg"
        assert str(exc) == "msg"
        assert isinstance(exc, DomainError)

    async def test_non_domain_exception_is_not_swallowed_as_domain_error(self, error_client):
        r = await error_client.get("/boom")
        assert r.status_code == 500
        assert "unexpected" not in r.text  # детали исключения не утекают клиенту

    async def test_real_api_returns_404_domain_error(self, client, make_user, auth):
        doctor_user = await make_user(UserRole.DOCTOR)
        r = await client.get("/api/appointments/999/prescriptions", headers=auth(doctor_user))
        assert (r.status_code, r.json()) == (404, {"detail": "Appointment not found"})

    async def test_real_api_returns_403_domain_error(self, client, make_user, auth):
        ghost = await make_user(UserRole.DOCTOR)  # врач без профиля
        r = await client.post(
            "/api/prescriptions", json={"appointment_id": 1, "medication": "A", "dosage": "1"}, headers=auth(ghost)
        )
        assert (r.status_code, r.json()) == (403, {"detail": "Doctor profile not found"})

    async def test_real_api_returns_422_domain_error(self, client, make_user, auth):
        admin = await make_user(UserRole.ADMIN)
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-02", "date_to": "2026-10-01"}, headers=auth(admin)
        )
        assert (r.status_code, r.json()) == (422, {"detail": "date_from must not be later than date_to"})

    async def test_real_api_returns_409_domain_error(self, client, session, make_user, make_doctor, auth):
        doctor = await make_doctor()
        doctor_user = await session.get(models.User, doctor.user_id)
        registrar = await make_user(UserRole.REGISTRAR)
        patient = models.Patient(full_name="П", birth_date=date(1990, 1, 1), policy_number=f"{next(_seq):016d}")
        session.add(patient)
        await session.commit()
        start = datetime(2026, 10, 14, 6, tzinfo=UTC)
        appt = models.Appointment(
            doctor_id=doctor.id,
            patient_id=patient.id,
            starts_at=start,
            ends_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            created_by=registrar.id,
        )
        session.add(appt)
        await session.commit()
        r = await client.post(
            "/api/prescriptions",
            json={"appointment_id": appt.id, "medication": "A", "dosage": "1"},
            headers=auth(doctor_user),
        )
        assert (r.status_code, r.json()) == (
            409,
            {"detail": "Prescriptions can only be issued for completed appointments"},
        )


# --------------------------------------------------------------------------- #
# errors.py: is_unique_violation
# --------------------------------------------------------------------------- #


def _integrity(orig) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, orig)


class _Orig(Exception):
    def __init__(self, text="", **attrs):
        super().__init__(text)
        for k, v in attrs.items():
            setattr(self, k, v)


class TestIsUniqueViolation:
    @pytest.mark.parametrize(
        "orig,expected",
        [
            (_Orig(sqlstate="23505"), True),
            (_Orig(sqlstate="23P01"), True),
            (_Orig(pgcode="23505"), True),
            (_Orig(pgcode="23P01"), True),
            (_Orig(sqlstate="23503"), False),  # foreign_key_violation
            (_Orig(sqlstate="23514"), False),  # check_violation
            (_Orig(sqlstate="23502"), False),  # not_null_violation
            (_Orig(sqlite_errorcode=2067), True),
            (_Orig(sqlite_errorcode=1555), True),
            (_Orig(sqlite_errorcode=787), False),  # FOREIGN KEY
            (_Orig(sqlite_errorcode=275), False),  # CHECK
            (_Orig("UNIQUE constraint failed: users.email"), True),
            (_Orig("NOT NULL constraint failed: users.email"), False),
            (_Orig("CHECK constraint failed: x"), False),
            (_Orig(""), False),
        ],
        ids=[
            "pg-unique",
            "pg-exclusion",
            "pgcode-unique",
            "pgcode-exclusion",
            "pg-fk",
            "pg-check",
            "pg-notnull",
            "sqlite-2067",
            "sqlite-1555",
            "sqlite-fk",
            "sqlite-check",
            "text-unique",
            "text-notnull",
            "text-check",
            "empty",
        ],
    )
    def test_classification(self, orig, expected):
        assert is_unique_violation(_integrity(orig)) is expected

    def test_sqlstate_is_found_on_cause_of_wrapper(self):
        wrapper = _Orig("wrapped")
        wrapper.__cause__ = _Orig(sqlstate="23505")
        assert is_unique_violation(_integrity(wrapper)) is True

    def test_cause_with_foreign_key_state_is_not_unique(self):
        wrapper = _Orig("wrapped")
        wrapper.__cause__ = _Orig(sqlstate="23503")
        assert is_unique_violation(_integrity(wrapper)) is False

    def test_orig_none_is_not_unique(self):
        assert is_unique_violation(IntegrityError("x", {}, None)) is False

    async def test_real_duplicate_email_is_unique_violation(self, session, make_user):
        await make_user(UserRole.ADMIN, email="dup@clinic.test")
        session.add(models.User(email="dup@clinic.test", password_hash="x", full_name="Дубль", role=UserRole.ADMIN))
        with pytest.raises(IntegrityError) as info:
            await session.commit()
        await session.rollback()
        assert is_unique_violation(info.value) is True

    async def test_real_check_violation_is_not_unique(self, session):
        session.add(models.Patient(full_name="П", birth_date=date(1990, 1, 1), policy_number="123"))
        with pytest.raises(IntegrityError) as info:
            await session.commit()
        await session.rollback()
        assert is_unique_violation(info.value) is False
