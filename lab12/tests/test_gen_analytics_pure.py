"""Unit-тесты чистых функций app/services/analytics.py: make_period, available_minutes,
_utc_offset_segments, _ratio, _as_date (без БД и HTTP)."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.errors import BusinessRuleError
from app.services.analytics import (
    MAX_PERIOD_DAYS,
    Period,
    _as_date,
    _ratio,
    _utc_offset_segments,
    available_minutes,
    make_period,
)
from app.services.scheduling import Schedule

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")
WEEKDAYS = frozenset({1, 2, 3, 4, 5})
SCHEDULE = Schedule(WEEKDAYS, time(9), time(18), 30)  # смена 540 минут
SHIFT = 540


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# make_period
# --------------------------------------------------------------------------- #
# date_from > date_to (на 1 день) -> BusinessRuleError; равны -> 1 день;
# длина 365, 366 -> ok; 367 -> BusinessRuleError; границы переводятся в UTC полуоткрытым интервалом.


@pytest.mark.parametrize(
    ("d_from", "d_to", "start", "end", "days"),
    [
        (date(2026, 10, 12), date(2026, 10, 12), utc(2026, 10, 11, 21), utc(2026, 10, 12, 21), 1),
        (date(2026, 10, 12), date(2026, 10, 13), utc(2026, 10, 11, 21), utc(2026, 10, 13, 21), 2),
        (date(2026, 10, 31), date(2026, 11, 1), utc(2026, 10, 30, 21), utc(2026, 11, 1, 21), 2),
        (date(2026, 12, 31), date(2027, 1, 1), utc(2026, 12, 30, 21), utc(2027, 1, 1, 21), 2),
        (date(2028, 2, 28), date(2028, 3, 1), utc(2028, 2, 27, 21), utc(2028, 3, 1, 21), 3),
        (date(2027, 2, 28), date(2027, 3, 1), utc(2027, 2, 27, 21), utc(2027, 3, 1, 21), 2),
    ],
    ids=["one_day", "two_days", "month_end", "year_end", "leap_february", "non_leap_february"],
)
def test_make_period_converts_local_dates_to_half_open_utc_interval(d_from, d_to, start, end, days):
    period = make_period(d_from, d_to, MSK)
    assert period == Period(date_from=d_from, date_to=d_to, start=start, end=end)
    assert period.days == days


def test_make_period_dates_reversed_by_one_day_raises():
    with pytest.raises(BusinessRuleError) as exc:
        make_period(date(2026, 10, 13), date(2026, 10, 12), MSK)
    assert exc.value.detail == "date_from must not be later than date_to"
    assert exc.value.status_code == 422


@pytest.mark.parametrize(
    ("d_from", "d_to", "days"),
    [
        (date(2027, 1, 1), date(2027, 12, 31), 365),
        (date(2028, 1, 1), date(2028, 12, 31), 366),
        (date(2026, 1, 1), date(2026, 12, 31), 365),
    ],
    ids=["365_days", "366_days_leap_year", "full_2026"],
)
def test_make_period_up_to_max_length_ok(d_from, d_to, days):
    assert make_period(d_from, d_to, MSK).days == days


@pytest.mark.parametrize(
    ("d_from", "d_to"),
    [(date(2028, 1, 1), date(2029, 1, 1)), (date(2026, 1, 1), date(2027, 1, 2)), (date(2020, 1, 1), date(2026, 1, 1))],
    ids=["367_days", "367_days_non_leap_start", "six_years"],
)
def test_make_period_longer_than_max_raises(d_from, d_to):
    with pytest.raises(BusinessRuleError) as exc:
        make_period(d_from, d_to, MSK)
    assert exc.value.detail == f"The period must not be longer than {MAX_PERIOD_DAYS} days"


def test_make_period_dst_zone_uses_actual_offsets_at_each_end():
    period = make_period(date(2026, 10, 24), date(2026, 10, 26), BERLIN)
    assert period.start == utc(2026, 10, 23, 22)  # CEST
    assert period.end == utc(2026, 10, 26, 23)  # CET


def test_make_period_overflowing_edge_dates_raise_overflow():
    with pytest.raises(OverflowError):
        make_period(date(9999, 12, 31), date(9999, 12, 31), MSK)


def test_make_period_is_immutable():
    period = make_period(date(2026, 10, 12), date(2026, 10, 12), MSK)
    with pytest.raises(AttributeError):
        period.date_from = date(2020, 1, 1)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# available_minutes
# --------------------------------------------------------------------------- #
# date_from > date_to -> ValueError; 1 день рабочий/нерабочий; полная неделя; неполные недели
# (старт с разных дней недели, остаток 1..6 дней); граница года; високосный день; пустой график.


def test_available_minutes_reversed_dates_raises():
    with pytest.raises(ValueError, match="date_from must not be later than date_to"):
        available_minutes(SCHEDULE, date(2026, 10, 13), date(2026, 10, 12))


@pytest.mark.parametrize(
    ("d_from", "d_to", "working_days"),
    [
        (date(2026, 10, 12), date(2026, 10, 12), 1),  # пн
        (date(2026, 10, 16), date(2026, 10, 16), 1),  # пт
        (date(2026, 10, 17), date(2026, 10, 17), 0),  # сб
        (date(2026, 10, 18), date(2026, 10, 18), 0),  # вс
        (date(2026, 10, 17), date(2026, 10, 18), 0),  # выходные
        (date(2026, 10, 17), date(2026, 10, 19), 1),  # сб-пн
        (date(2026, 10, 12), date(2026, 10, 16), 5),  # пн-пт
        (date(2026, 10, 12), date(2026, 10, 18), 5),  # ровно неделя
        (date(2026, 10, 14), date(2026, 10, 20), 5),  # ср-вт: неделя с середины
        (date(2026, 10, 12), date(2026, 10, 19), 6),  # неделя + 1 день
        (date(2026, 10, 14), date(2026, 10, 26), 9),  # 13 дней: неделя + остаток 6
        (date(2026, 10, 15), date(2026, 10, 28), 10),  # ровно две недели
        (date(2026, 10, 16), date(2026, 10, 25), 6),  # пт-вс: 10 дней, остаток 3
        (date(2026, 12, 31), date(2027, 1, 1), 2),  # граница года
        (date(2028, 2, 29), date(2028, 2, 29), 1),  # 29 февраля (вторник)
        (date(2026, 10, 31), date(2026, 11, 2), 1),  # сб-пн через границу месяца
    ],
    ids=[
        "mon",
        "fri",
        "sat",
        "sun",
        "weekend",
        "sat_to_mon",
        "mon_fri",
        "exact_week",
        "wed_to_tue",
        "week_plus_1",
        "13_days",
        "two_weeks_from_thu",
        "fri_to_sun_next",
        "new_year",
        "leap_day",
        "month_boundary",
    ],
)
def test_available_minutes_counts_working_days_times_shift(d_from, d_to, working_days):
    assert available_minutes(SCHEDULE, d_from, d_to) == working_days * SHIFT


@pytest.mark.parametrize(
    "start", [date(2026, 10, 12) + timedelta(days=i) for i in range(7)], ids=lambda d: d.strftime("%a")
)
@pytest.mark.parametrize("length", [1, 2, 3, 6, 7, 8, 13, 14, 15, 29, 30, 31, 100, 366])
def test_available_minutes_matches_day_by_day_count(start, length):
    schedule = Schedule(frozenset({1, 3, 6, 7}), time(8, 30), time(12, 15), 15)
    end = start + timedelta(days=length - 1)
    expected_days = sum(1 for i in range(length) if (start + timedelta(days=i)).isoweekday() in schedule.work_days)
    assert available_minutes(schedule, start, end) == expected_days * 225


@pytest.mark.parametrize("weekday", range(1, 8))
def test_available_minutes_single_day_only_for_its_weekday(weekday):
    schedule = Schedule(frozenset({3}), time(9), time(10), 30)
    day = date(2026, 10, 11) + timedelta(days=weekday)  # 12.10 = ISO 1
    assert available_minutes(schedule, day, day) == (60 if weekday == 3 else 0)


def test_available_minutes_all_days_schedule_two_weeks():
    schedule = Schedule(frozenset(range(1, 8)), time(9), time(10), 30)
    assert available_minutes(schedule, date(2026, 10, 12), date(2026, 10, 25)) == 14 * 60


def test_available_minutes_empty_work_days_is_zero():
    schedule = Schedule(frozenset(), time(9), time(18), 30)
    assert available_minutes(schedule, date(2026, 10, 12), date(2026, 12, 31)) == 0


def test_available_minutes_shift_of_one_minute():
    schedule = Schedule(WEEKDAYS, time(9), time(9, 1), 5)
    assert available_minutes(schedule, date(2026, 10, 12), date(2026, 10, 16)) == 5


def test_available_minutes_leap_year_total():
    # 2028: 366 дней, 260 рабочих (год начинается в субботу: 52 недели + сб + вс)
    assert available_minutes(SCHEDULE, date(2028, 1, 1), date(2028, 12, 31)) == 260 * SHIFT


# --------------------------------------------------------------------------- #
# _utc_offset_segments
# --------------------------------------------------------------------------- #
# Без перевода часов -> один отрезок; осенний/весенний перевод -> два отрезка; период
# с обоими переводами -> три; отрицательное и дробное смещение; один день.


def _segments(d_from: date, d_to: date, tz: ZoneInfo):
    return _utc_offset_segments(make_period(d_from, d_to, tz), tz)


def test_utc_offset_segments_moscow_is_single_segment():
    assert _segments(date(2026, 10, 1), date(2026, 10, 31), MSK) == [(utc(2026, 9, 30, 21), 10800)]


def test_utc_offset_segments_single_day_is_single_segment():
    assert _segments(date(2026, 10, 12), date(2026, 10, 12), MSK) == [(utc(2026, 10, 11, 21), 10800)]


def test_utc_offset_segments_autumn_transition_splits_in_two():
    assert _segments(date(2026, 10, 23), date(2026, 10, 27), BERLIN) == [
        (utc(2026, 10, 22, 22), 7200),
        (utc(2026, 10, 25, 23), 3600),
    ]


def test_utc_offset_segments_spring_transition_splits_in_two():
    assert _segments(date(2026, 3, 27), date(2026, 3, 31), BERLIN) == [
        (utc(2026, 3, 26, 23), 3600),
        (utc(2026, 3, 29, 22), 7200),
    ]


def test_utc_offset_segments_period_with_both_transitions_has_three_segments():
    segments = _segments(date(2026, 3, 1), date(2026, 12, 31), BERLIN)
    assert [offset for _, offset in segments] == [3600, 7200, 3600]
    assert segments[0][0] == utc(2026, 2, 28, 23)
    assert segments[1][0] == utc(2026, 3, 29, 22)
    assert segments[2][0] == utc(2026, 10, 25, 23)


def test_utc_offset_segments_period_ending_on_transition_day_is_one_segment():
    # 25.10.2026: полночь ещё по летнему времени; смещение меняется только к полуночи 26.10
    assert _segments(date(2026, 10, 20), date(2026, 10, 25), BERLIN) == [(utc(2026, 10, 19, 22), 7200)]


def test_utc_offset_segments_negative_offset():
    assert _segments(date(2026, 7, 1), date(2026, 7, 2), ZoneInfo("America/New_York")) == [(utc(2026, 7, 1, 4), -14400)]


def test_utc_offset_segments_half_hour_offset():
    assert _segments(date(2026, 10, 12), date(2026, 10, 13), ZoneInfo("Asia/Kolkata")) == [
        (utc(2026, 10, 11, 18, 30), 19800)
    ]


def test_utc_offset_segments_first_segment_starts_at_period_start():
    period = make_period(date(2026, 10, 23), date(2026, 10, 27), BERLIN)
    assert _utc_offset_segments(period, BERLIN)[0][0] == period.start


# --------------------------------------------------------------------------- #
# _ratio
# --------------------------------------------------------------------------- #
# знаменатель 0 -> 0.0 (при любом числителе); числитель 0; равны -> 1.0; округление до 4 знаков;
# числитель больше знаменателя (> 1.0 допустимо)


@pytest.mark.parametrize(
    ("num", "den", "expected"),
    [
        (0, 0, 0.0),
        (5, 0, 0.0),
        (0, 5, 0.0),
        (5, 5, 1.0),
        (1, 2, 0.5),
        (1, 3, 0.3333),
        (2, 3, 0.6667),
        (1, 8, 0.125),
        (1, 7, 0.1429),
        (1, 30000, 0.0),
        (9999, 10000, 0.9999),
        (3, 2, 1.5),
        (1, 1, 1.0),
    ],
    ids=[
        "zero_zero",
        "num_over_zero",
        "zero_num",
        "equal",
        "half",
        "third",
        "two_thirds",
        "eighth",
        "seventh",
        "tiny_rounds_to_zero",
        "four_digits",
        "above_one",
        "one_one",
    ],
)
def test_ratio_values(num, den, expected):
    result = _ratio(num, den)
    assert result == expected
    assert isinstance(result, float)


# --------------------------------------------------------------------------- #
# _as_date
# --------------------------------------------------------------------------- #
# date -> без изменений; datetime -> date (aware и naive); строка ISO -> date; мусор -> ValueError


def test_as_date_date_returned_as_is():
    value = date(2026, 10, 12)
    assert _as_date(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime(2026, 10, 12, 23, 59, 59), date(2026, 10, 12)),
        (datetime(2026, 12, 31, 0, 0), date(2026, 12, 31)),
        (datetime(2026, 10, 12, 23, 30, tzinfo=UTC), date(2026, 10, 12)),
    ],
    ids=["naive_end_of_day", "naive_midnight", "aware"],
)
def test_as_date_datetime_becomes_plain_date(value, expected):
    result = _as_date(value)
    assert result == expected
    assert type(result) is date


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-12", date(2026, 10, 12)),
        ("2028-02-29", date(2028, 2, 29)),
        ("2026-12-31", date(2026, 12, 31)),
        ("0001-01-01", date(1, 1, 1)),
    ],
    ids=["regular", "leap_day", "year_end", "min_date"],
)
def test_as_date_iso_string(text, expected):
    assert _as_date(text) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-date", "2027-02-29", "2026-13-01", "2026-10-32", None, "12.10.2026"],
    ids=["empty", "garbage", "non_leap_feb_29", "month_13", "day_32", "none", "dotted"],
)
def test_as_date_invalid_value_raises_value_error(bad):
    with pytest.raises(ValueError):
        _as_date(bad)
