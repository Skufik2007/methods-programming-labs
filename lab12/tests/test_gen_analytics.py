"""HTTP-тесты аналитики /api/analytics/* на данных с заранее известными числами."""

import itertools
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app import models
from app.models import AppointmentStatus as S
from app.models import UserRole

MSK = timezone(timedelta(hours=3))
_seq = itertools.count(1)

ENDPOINTS = ["summary", "doctors-load", "appointments-per-day", "top-diagnoses"]
WEEK = {"date_from": "2026-10-12", "date_to": "2026-10-18"}  # пн-вс


def msk(day: int, hour: int = 0, minute: int = 0, month: int = 10) -> datetime:
    """Местное время клиники (МСК) 2026 года как aware UTC."""
    return datetime(2026, month, day, hour, minute, tzinfo=MSK).astimezone(UTC)


async def _user_of(session, doctor):
    return await session.get(models.User, doctor.user_id)


async def _patient(session):
    p = models.Patient(full_name="Пациент", birth_date=date(1990, 1, 1), policy_number=f"{next(_seq):016d}")
    session.add(p)
    await session.commit()
    return p


async def _appt(session, doctor, patient, creator, start, minutes=30, status=S.COMPLETED):
    a = models.Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        status=status,
        created_by=creator.id,
    )
    session.add(a)
    await session.commit()
    return a


async def _record(session, patient, doctor, created_at, code):
    session.add(
        models.MedicalRecord(
            patient_id=patient.id, doctor_id=doctor.id, diagnosis_code=code, notes="секрет", created_at=created_at
        )
    )
    await session.commit()


@pytest.fixture
async def staff(make_user):
    return {"admin": await make_user(UserRole.ADMIN), "registrar": await make_user(UserRole.REGISTRAR)}


@pytest.fixture
async def reg(staff):
    return staff["registrar"]


@pytest.fixture
async def hdr(staff, auth):
    return auth(staff["admin"])


# --------------------------------------------------------------------------- #
# Матрица доступа
# --------------------------------------------------------------------------- #


class TestAccessMatrix:
    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_without_token_returns_401(self, client, endpoint):
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK)
        assert r.status_code == 401
        assert r.json() == {"detail": "Not authenticated"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize("role", [UserRole.DOCTOR, UserRole.PATIENT])
    async def test_medical_roles_get_403(self, client, make_user, make_doctor, auth, endpoint, role):
        user = await make_user(role)
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK, headers=auth(user))
        assert r.status_code == 403
        assert r.json() == {"detail": "Insufficient permissions"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize("role", ["admin", "registrar"])
    async def test_staff_roles_get_200(self, client, staff, auth, endpoint, role):
        r = await client.get(f"/api/analytics/{endpoint}", params=WEEK, headers=auth(staff[role]))
        assert r.status_code == 200

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_forbidden_role_is_checked_before_validation(self, client, make_user, auth, endpoint):
        patient = await make_user(UserRole.PATIENT)
        r = await client.get(f"/api/analytics/{endpoint}", headers=auth(patient))  # без дат
        assert r.status_code == 403


# --------------------------------------------------------------------------- #
# Валидация параметров
# --------------------------------------------------------------------------- #


class TestValidation:
    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"date_from": "2026-10-12"},
            {"date_to": "2026-10-18"},
            {"date_from": "12.10.2026", "date_to": "18.10.2026"},
            {"date_from": "abc", "date_to": "2026-10-18"},
            {"date_from": "2026-02-30", "date_to": "2026-10-18"},
            {"date_from": "2026-10-12", "date_to": ""},
            {"date_from": "1969-12-31", "date_to": "1970-01-05"},
            {"date_from": "2100-12-25", "date_to": "2101-01-01"},
        ],
        ids=["none", "no-to", "no-from", "dotted", "word", "feb30", "empty-to", "before-1970", "after-2100"],
    )
    async def test_bad_dates_return_422(self, client, hdr, endpoint, params):
        r = await client.get(f"/api/analytics/{endpoint}", params=params, headers=hdr)
        assert r.status_code == 422

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_date_from_after_date_to_returns_422_with_detail(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-10-13", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "date_from must not be later than date_to"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_period_of_367_days_returns_422(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-01-01", "date_to": "2027-01-02"}, headers=hdr
        )
        assert r.status_code == 422
        assert r.json() == {"detail": "The period must not be longer than 366 days"}

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_period_of_366_days_is_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-01-01", "date_to": "2027-01-01"}, headers=hdr
        )
        assert r.status_code == 200

    async def test_leap_year_366_day_period_is_accepted_and_367_rejected(self, client, hdr):
        ok = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2028-01-01", "date_to": "2028-12-31"},
            headers=hdr,
        )
        assert ok.status_code == 200
        assert len(ok.json()) == 366
        bad = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2028-01-01", "date_to": "2029-01-01"},
            headers=hdr,
        )
        assert bad.status_code == 422

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_single_day_period_is_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.status_code == 200

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    async def test_boundary_dates_1970_and_2100_are_accepted(self, client, hdr, endpoint):
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "1970-01-01", "date_to": "1970-01-02"}, headers=hdr
        )
        assert r.status_code == 200
        r = await client.get(
            f"/api/analytics/{endpoint}", params={"date_from": "2100-12-30", "date_to": "2100-12-31"}, headers=hdr
        )
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "limit,expected", [(1, 200), (10, 200), (50, 200), (0, 422), (51, 422), (-1, 422), ("x", 422)]
    )
    async def test_top_diagnoses_limit_boundaries(self, client, hdr, limit, expected):
        r = await client.get("/api/analytics/top-diagnoses", params={**WEEK, "limit": limit}, headers=hdr)
        assert r.status_code == expected


# --------------------------------------------------------------------------- #
# Сводка
# --------------------------------------------------------------------------- #


@pytest.fixture
async def dataset(session, make_doctor, reg):
    """D1 (пн-пт 09-18, 540 мин/день) и D2 (пн-ср 10-14, 240 мин/день); неделя 12-18.10.2026.

    D1: пн 09:00 completed 30, 09:30 completed 30, 10:00 no_show 30, 10:30 cancelled 30,
        ср 09:00 scheduled 60.
    D2: вт 10:00 completed 120, вт 12:00 cancelled 30.
    Вне периода: вс 11.10 23:30 и пн 19.10 00:00 (местное время).
    """
    d1 = await make_doctor()
    d2 = await make_doctor(
        work_start=datetime(2000, 1, 1, 10).time(), work_end=datetime(2000, 1, 1, 14).time(), work_days=[1, 2, 3]
    )
    p = await _patient(session)
    rows = [
        (d1, msk(12, 9, 0), 30, S.COMPLETED),
        (d1, msk(12, 9, 30), 30, S.COMPLETED),
        (d1, msk(12, 10, 0), 30, S.NO_SHOW),
        (d1, msk(12, 10, 30), 30, S.CANCELLED),
        (d1, msk(14, 9, 0), 60, S.SCHEDULED),
        (d2, msk(13, 10, 0), 120, S.COMPLETED),
        (d2, msk(13, 12, 0), 30, S.CANCELLED),
        (d1, msk(11, 23, 30), 30, S.COMPLETED),  # до периода
        (d1, msk(19, 0, 0), 30, S.NO_SHOW),  # после периода
    ]
    for doctor, start, minutes, status in rows:
        await _appt(session, doctor, p, reg, start, minutes, status)
    return d1, d2


class TestSummary:
    async def test_summary_counts_and_rates(self, client, hdr, dataset):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == {
            "date_from": "2026-10-12",
            "date_to": "2026-10-18",
            "total": 7,
            "by_status": {"scheduled": 1, "completed": 3, "cancelled": 2, "no_show": 1},
            "no_show_rate": 0.25,  # 1 / (3 + 1)
            "cancellation_rate": 0.2857,  # 2 / 7
        }

    async def test_summary_excludes_appointments_outside_period_by_local_date(self, client, hdr, dataset):
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-11", "date_to": "2026-10-11"}, headers=hdr
        )
        assert r.json()["total"] == 1
        assert r.json()["by_status"]["completed"] == 1

    async def test_summary_period_end_is_inclusive_and_next_midnight_excluded(self, client, hdr, dataset):
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-19", "date_to": "2026-10-19"}, headers=hdr
        )
        assert r.json()["total"] == 1
        assert r.json()["by_status"]["no_show"] == 1
        r = await client.get(
            "/api/analytics/summary", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()["total"] == 4

    async def test_summary_empty_period_returns_zeros(self, client, hdr):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json() == {
            "date_from": "2026-10-12",
            "date_to": "2026-10-18",
            "total": 0,
            "by_status": {"scheduled": 0, "completed": 0, "cancelled": 0, "no_show": 0},
            "no_show_rate": 0.0,
            "cancellation_rate": 0.0,
        }

    async def test_summary_only_scheduled_gives_zero_no_show_rate(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.SCHEDULED)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["total"] == 1
        assert r.json()["no_show_rate"] == 0.0
        assert r.json()["cancellation_rate"] == 0.0

    async def test_summary_only_no_show_gives_rate_one(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.NO_SHOW)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["no_show_rate"] == 1.0

    async def test_summary_only_cancelled_gives_cancellation_rate_one_and_no_show_zero(
        self, client, hdr, session, make_doctor, reg
    ):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.CANCELLED)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["cancellation_rate"] == 1.0
        assert r.json()["no_show_rate"] == 0.0

    async def test_summary_rate_rounds_to_four_digits(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        for i, st in enumerate([S.COMPLETED, S.COMPLETED, S.NO_SHOW]):
            await _appt(session, d, p, reg, msk(12, 9 + i), status=st)
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert r.json()["no_show_rate"] == 0.3333  # 1 / 3

    async def test_summary_contains_no_personal_data(self, client, hdr, dataset):
        r = await client.get("/api/analytics/summary", params=WEEK, headers=hdr)
        assert set(r.json()) == {"date_from", "date_to", "total", "by_status", "no_show_rate", "cancellation_rate"}


# --------------------------------------------------------------------------- #
# Загрузка врачей
# --------------------------------------------------------------------------- #


class TestDoctorsLoad:
    async def test_load_values_for_known_data(self, client, hdr, dataset):
        d1, d2 = dataset
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {
                "doctor_id": d1.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 2700,  # 5 дней * 540
                "busy_minutes": 150,  # 30 + 30 + 30 + 60, отменённая не считается
                "load_percent": 5.56,  # 150 / 2700 * 100
                "completed": 2,
                "no_show": 1,
            },
            {
                "doctor_id": d2.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 720,  # 3 дня * 240
                "busy_minutes": 120,
                "load_percent": 16.67,
                "completed": 1,
                "no_show": 0,
            },
        ]

    async def test_load_available_minutes_for_two_week_period(self, client, hdr, dataset):
        d1, d2 = dataset
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-25"}, headers=hdr
        )
        by_id = {x["doctor_id"]: x for x in r.json()}
        assert by_id[d1.id]["available_minutes"] == 5400
        assert by_id[d2.id]["available_minutes"] == 1440

    async def test_load_available_minutes_for_weekend_only_is_zero(self, client, hdr, dataset):
        d1, _ = dataset
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-17", "date_to": "2026-10-18"}, headers=hdr
        )
        assert r.json()[0]["available_minutes"] == 0
        assert r.json()[0]["load_percent"] == 0.0

    async def test_load_available_minutes_partial_week_counts_only_working_weekdays(self, client, hdr, dataset):
        d1, d2 = dataset
        # чт 15.10 - вс 18.10: у D1 два рабочих дня (чт, пт), у D2 - ни одного
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-15", "date_to": "2026-10-18"}, headers=hdr
        )
        by_id = {x["doctor_id"]: x for x in r.json()}
        assert by_id[d1.id]["available_minutes"] == 1080
        assert by_id[d2.id]["available_minutes"] == 0

    async def test_load_over_100_percent_is_not_capped(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=600, status=S.COMPLETED)
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()[0]["busy_minutes"] == 600
        assert r.json()[0]["available_minutes"] == 540
        assert r.json()[0]["load_percent"] == 111.11

    async def test_load_exactly_full_day_is_100_percent(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=540, status=S.SCHEDULED)
        r = await client.get(
            "/api/analytics/doctors-load", params={"date_from": "2026-10-12", "date_to": "2026-10-12"}, headers=hdr
        )
        assert r.json()[0]["load_percent"] == 100.0

    async def test_load_doctor_without_workdays_has_zero_percent_not_error(
        self, client, hdr, session, make_doctor, reg
    ):
        d = await make_doctor(work_days=[])
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), status=S.COMPLETED)
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json()[0]["available_minutes"] == 0
        assert r.json()[0]["busy_minutes"] == 30
        assert r.json()[0]["load_percent"] == 0.0

    async def test_load_doctor_without_appointments_has_zeros(self, client, hdr, make_doctor):
        d = await make_doctor()
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.json() == [
            {
                "doctor_id": d.id,
                "specialty": "Терапевт",
                "is_active": True,
                "available_minutes": 2700,
                "busy_minutes": 0,
                "load_percent": 0.0,
                "completed": 0,
                "no_show": 0,
            }
        ]

    async def test_load_includes_deactivated_doctor_with_flag(self, client, hdr, session, make_doctor):
        d = await make_doctor()
        user = await _user_of(session, d)
        user.is_active = False
        await session.commit()
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert [x["is_active"] for x in r.json()] == [False]

    async def test_load_cancelled_does_not_add_busy_minutes(self, client, hdr, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        await _appt(session, d, p, reg, msk(12, 9), minutes=60, status=S.CANCELLED)
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.json()[0]["busy_minutes"] == 0

    async def test_load_empty_without_doctors_returns_empty_list(self, client, hdr):
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == []

    async def test_load_response_has_no_names(self, client, hdr, dataset):
        r = await client.get("/api/analytics/doctors-load", params=WEEK, headers=hdr)
        for item in r.json():
            assert "full_name" not in item
            assert set(item) == {
                "doctor_id",
                "specialty",
                "is_active",
                "available_minutes",
                "busy_minutes",
                "load_percent",
                "completed",
                "no_show",
            }


# --------------------------------------------------------------------------- #
# Записи по дням
# --------------------------------------------------------------------------- #


class TestPerDay:
    @pytest.fixture
    async def edge_data(self, session, make_doctor, reg):
        d = await make_doctor()
        p = await _patient(session)
        rows = [
            (msk(11, 23, 30), S.COMPLETED),  # 11.10 - вне периода
            (msk(12, 0, 0), S.CANCELLED),  # 12.10 00:00 местное (= 11.10 21:00 UTC) - первый день
            (msk(12, 9, 0), S.SCHEDULED),
            (msk(14, 9, 0), S.NO_SHOW),
            (msk(18, 23, 30), S.COMPLETED),  # последний день, 20:30 UTC
            (msk(19, 0, 0), S.COMPLETED),  # 19.10 00:00 - вне периода (граница не включается)
        ]
        for start, st in rows:
            await _appt(session, d, p, reg, start, status=st)

    async def test_per_day_counts_all_statuses_by_local_date(self, client, hdr, edge_data):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {"day": "2026-10-12", "count": 2},
            {"day": "2026-10-13", "count": 0},
            {"day": "2026-10-14", "count": 1},
            {"day": "2026-10-15", "count": 0},
            {"day": "2026-10-16", "count": 0},
            {"day": "2026-10-17", "count": 0},
            {"day": "2026-10-18", "count": 1},
        ]

    async def test_per_day_days_without_appointments_are_present_with_zero(self, client, hdr, edge_data):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert [x["day"] for x in r.json()] == [f"2026-10-{d}" for d in range(12, 19)]
        assert sum(1 for x in r.json() if x["count"] == 0) == 4

    async def test_per_day_single_day_midnight_belongs_to_local_date(self, client, hdr, edge_data):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-10-12", "date_to": "2026-10-12"},
            headers=hdr,
        )
        assert r.json() == [{"day": "2026-10-12", "count": 2}]

    async def test_per_day_late_evening_stays_on_same_local_date(self, client, hdr, edge_data):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-10-11", "date_to": "2026-10-11"},
            headers=hdr,
        )
        assert r.json() == [{"day": "2026-10-11", "count": 1}]

    async def test_per_day_empty_period_returns_zero_for_each_day(self, client, hdr):
        r = await client.get("/api/analytics/appointments-per-day", params=WEEK, headers=hdr)
        assert len(r.json()) == 7
        assert all(x["count"] == 0 for x in r.json())

    async def test_per_day_366_days_returns_366_items_in_order(self, client, hdr):
        r = await client.get(
            "/api/analytics/appointments-per-day",
            params={"date_from": "2026-01-01", "date_to": "2027-01-01"},
            headers=hdr,
        )
        days = [x["day"] for x in r.json()]
        assert len(days) == 366
        assert days[0] == "2026-01-01"
        assert days[-1] == "2027-01-01"
        assert days == sorted(days)


# --------------------------------------------------------------------------- #
# Топ диагнозов
# --------------------------------------------------------------------------- #


class TestTopDiagnoses:
    @pytest.fixture
    async def records(self, session, make_doctor):
        d = await make_doctor()
        p = await _patient(session)
        plan = [
            ("J06.9", msk(12, 9)),
            ("J06.9", msk(13, 9)),
            ("J06.9", msk(18, 23, 59)),
            ("I10", msk(12, 0, 0)),  # ровно начало периода
            ("I10", msk(15, 9)),
            ("A09", msk(14, 9)),
            ("A09", msk(16, 9)),
            ("K21", msk(17, 9)),
            ("J06.9", msk(11, 23, 59)),  # вне периода
            ("Z00", msk(19, 0, 0)),  # вне периода (следующая полночь)
        ]
        for code, when in plan:
            await _record(session, p, d, when, code)

    async def test_top_diagnoses_values_and_tie_order_by_code(self, client, hdr, records):
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        assert r.status_code == 200
        assert r.json() == [
            {"code": "J06.9", "count": 3},
            {"code": "A09", "count": 2},  # при равенстве - по коду: A09 раньше I10
            {"code": "I10", "count": 2},
            {"code": "K21", "count": 1},
        ]

    @pytest.mark.parametrize(
        "limit,expected_codes",
        [
            (1, ["J06.9"]),
            (2, ["J06.9", "A09"]),
            (3, ["J06.9", "A09", "I10"]),
            (4, ["J06.9", "A09", "I10", "K21"]),
            (50, ["J06.9", "A09", "I10", "K21"]),
        ],
    )
    async def test_top_diagnoses_limit(self, client, hdr, records, limit, expected_codes):
        r = await client.get("/api/analytics/top-diagnoses", params={**WEEK, "limit": limit}, headers=hdr)
        assert [x["code"] for x in r.json()] == expected_codes

    async def test_top_diagnoses_default_limit_is_ten(self, client, hdr, session, make_doctor):
        d = await make_doctor()
        p = await _patient(session)
        for i in range(12):
            await _record(session, p, d, msk(12, 9), f"B{i:02d}")
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        assert len(r.json()) == 10

    async def test_top_diagnoses_exposes_only_code_and_count(self, client, hdr, records):
        r = await client.get("/api/analytics/top-diagnoses", params=WEEK, headers=hdr)
        for item in r.json():
            assert set(item) == {"code", "count"}
        assert "секрет" not in r.text

    async def test_top_diagnoses_empty_period_returns_empty_list(self, client, hdr, records):
        r = await client.get(
            "/api/analytics/top-diagnoses", params={"date_from": "2026-11-01", "date_to": "2026-11-02"}, headers=hdr
        )
        assert r.status_code == 200
        assert r.json() == []

    async def test_top_diagnoses_period_boundaries_are_by_created_at_local_date(self, client, hdr, records):
        r = await client.get(
            "/api/analytics/top-diagnoses", params={"date_from": "2026-10-19", "date_to": "2026-10-19"}, headers=hdr
        )
        assert r.json() == [{"code": "Z00", "count": 1}]
