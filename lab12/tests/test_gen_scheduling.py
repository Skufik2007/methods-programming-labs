"""Unit-тесты app/services/scheduling.py (чистая логика расписания, без БД и HTTP).

Таблицы классов эквивалентности и границ приведены над каждой группой тестов.
Расписание по умолчанию: пн-пт 09:00-18:00 МСК, слот 30 минут. Понедельник 12.10.2026.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import scheduling
from app.services.scheduling import (
    Schedule,
    SchedulingError,
    Slot,
    free_slots,
    intervals_overlap,
    local_day_bounds,
    validate_booking_window,
    validate_slot_grid,
    validate_within_shift,
)

MSK = ZoneInfo("Europe/Moscow")
VLAT = ZoneInfo("Asia/Vladivostok")  # UTC+10, без перевода часов
NOW = datetime(2026, 10, 12, 6, 0, tzinfo=UTC)  # пн 09:00 МСК
WEEKDAYS = frozenset({1, 2, 3, 4, 5})
SCHEDULE = Schedule(work_days=WEEKDAYS, work_start=time(9), work_end=time(18), slot_minutes=30)
ALL_DAYS = frozenset(range(1, 8))
MONDAY = date(2026, 10, 12)


def msk(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 10, year: int = 2026) -> datetime:
    """Момент по московскому времени как aware UTC."""
    return datetime(year, month, day, hour, minute, second, tzinfo=MSK).astimezone(UTC)


# --------------------------------------------------------------------------- #
# Schedule.__post_init__
# --------------------------------------------------------------------------- #
# slot_minutes: -1 -> ValueError, 0 -> ValueError, 1 -> ok
# work_start vs work_end: start > end -> ValueError, start == end -> ValueError, start < end (на 1 мин) -> ok
# work_days: {0} -> ValueError, {8} -> ValueError, {1} и {7} -> ok, пустое множество -> ok


@pytest.mark.parametrize("minutes", [-1, 0], ids=["negative", "zero"])
def test_schedule_slot_minutes_not_positive_raises(minutes):
    with pytest.raises(ValueError, match="slot_minutes must be positive"):
        Schedule(WEEKDAYS, time(9), time(18), minutes)


def test_schedule_slot_minutes_one_is_valid():
    assert Schedule(WEEKDAYS, time(9), time(18), 1).slot_minutes == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [(time(18), time(9)), (time(9), time(9)), (time(9, 1), time(9))],
    ids=["reversed", "equal", "reversed_by_one_minute"],
)
def test_schedule_start_not_before_end_raises(start, end):
    with pytest.raises(ValueError, match="work_start must be earlier than work_end"):
        Schedule(WEEKDAYS, start, end, 30)


def test_schedule_one_minute_shift_is_valid():
    assert Schedule(WEEKDAYS, time(9), time(9, 1), 30).work_end == time(9, 1)


@pytest.mark.parametrize("days", [{0}, {8}, {1, 8}, {-1}], ids=["zero", "eight", "valid_plus_eight", "negative"])
def test_schedule_work_days_out_of_range_raises(days):
    with pytest.raises(ValueError, match="ISO weekday numbers"):
        Schedule(frozenset(days), time(9), time(18), 30)


@pytest.mark.parametrize("days", [{1}, {7}, set(range(1, 8)), set()], ids=["monday", "sunday", "all", "empty"])
def test_schedule_work_days_in_range_is_valid(days):
    assert Schedule(frozenset(days), time(9), time(18), 30).work_days == frozenset(days)


def test_schedule_is_immutable():
    with pytest.raises(AttributeError):
        SCHEDULE.slot_minutes = 10  # type: ignore[misc]


def test_scheduling_error_is_value_error():
    assert issubclass(SchedulingError, ValueError)


# --------------------------------------------------------------------------- #
# validate_booking_window (П1)
# --------------------------------------------------------------------------- #
# starts_at - now: -1 c, 0 -> "future"; +1 c -> ok; +90 сут -> ok; +90 сут +1 c -> "at most 90 days"
# naive starts_at / naive now -> ValueError; другой часовой пояс входа -> сравнение по моменту
# max_days_ahead: 1 (граница 1 сут), 0 (любое будущее -> слишком далеко)


@pytest.mark.parametrize(
    "delta", [timedelta(seconds=-1), timedelta(0), timedelta(days=-30)], ids=["minus1s", "now", "far_past"]
)
def test_booking_window_not_in_future_raises(delta):
    with pytest.raises(SchedulingError, match="Appointment must start in the future"):
        validate_booking_window(NOW + delta, now=NOW)


@pytest.mark.parametrize(
    "delta",
    [timedelta(seconds=1), timedelta(days=1), timedelta(days=89, hours=23, minutes=59), timedelta(days=90)],
    ids=["plus1s", "plus1d", "just_under_90d", "exactly_90d"],
)
def test_booking_window_inside_window_ok(delta):
    validate_booking_window(NOW + delta, now=NOW)


@pytest.mark.parametrize(
    "delta", [timedelta(days=90, seconds=1), timedelta(days=91), timedelta(days=365)], ids=["90d_plus1s", "91d", "year"]
)
def test_booking_window_too_far_raises(delta):
    with pytest.raises(SchedulingError, match="at most 90 days in advance"):
        validate_booking_window(NOW + delta, now=NOW)


def test_booking_window_custom_limit_boundaries():
    validate_booking_window(NOW + timedelta(days=1), now=NOW, max_days_ahead=1)
    with pytest.raises(SchedulingError, match="at most 1 days in advance"):
        validate_booking_window(NOW + timedelta(days=1, seconds=1), now=NOW, max_days_ahead=1)


def test_booking_window_zero_limit_rejects_any_future():
    with pytest.raises(SchedulingError, match="at most 0 days"):
        validate_booking_window(NOW + timedelta(seconds=1), now=NOW, max_days_ahead=0)


def test_booking_window_compares_instants_across_time_zones():
    plus5 = datetime(2026, 10, 12, 11, 0, 1, tzinfo=ZoneInfo("Asia/Yekaterinburg"))  # 06:00:01Z
    validate_booking_window(plus5, now=NOW)
    equal_to_now = datetime(2026, 10, 12, 11, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))  # 06:00:00Z
    with pytest.raises(SchedulingError, match="future"):
        validate_booking_window(equal_to_now, now=NOW)


def test_booking_window_year_end_rollover():
    now = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    validate_booking_window(datetime(2027, 1, 1, 0, 0, tzinfo=UTC), now=now)
    with pytest.raises(SchedulingError, match="future"):
        validate_booking_window(datetime(2026, 12, 31, 23, 59, tzinfo=UTC), now=now)


@pytest.mark.parametrize("which", ["starts_at", "now"])
def test_booking_window_naive_datetime_raises(which):
    aware = NOW + timedelta(days=1)
    naive = aware.replace(tzinfo=None)
    kwargs = (
        {"starts_at": naive, "now": NOW}
        if which == "starts_at"
        else {"starts_at": aware, "now": NOW.replace(tzinfo=None)}
    )
    with pytest.raises(ValueError, match="naive datetime"):
        validate_booking_window(kwargs["starts_at"], now=kwargs["now"])


# --------------------------------------------------------------------------- #
# validate_within_shift
# --------------------------------------------------------------------------- #
# пн 09:00-09:30 (начало смены)  -> ok          | пн 17:30-18:00 (конец смены) -> ok
# пн 08:59-09:29 (на минуту раньше)    -> shift | пн 17:31-18:01 (на минуту позже) -> shift
# пн 08:30-09:30 (начало до смены)     -> shift | пн 17:30-18:30 (конец после смены) -> shift
# ends == starts / ends < starts -> "later"     | пн 23:30 - вт 00:30 -> midnight
# сб, вс -> weekday | локальный день vs день UTC (00:30 МСК, 21:30Z предыдущего дня)
# naive -> ValueError


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (msk(12, 9, 0), msk(12, 9, 30)),
        (msk(12, 17, 30), msk(12, 18, 0)),
        (msk(12, 9, 0), msk(12, 18, 0)),
        (msk(16, 12, 0), msk(16, 12, 1)),
    ],
    ids=["shift_start", "shift_end", "whole_shift", "friday_one_minute"],
)
def test_within_shift_inside_ok(start, end):
    validate_within_shift(start, end, SCHEDULE, MSK)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (msk(12, 8, 59), msk(12, 9, 29)),
        (msk(12, 8, 30), msk(12, 9, 30)),
        (msk(12, 17, 31), msk(12, 18, 1)),
        (msk(12, 17, 30), msk(12, 18, 30)),
        (msk(12, 8, 0), msk(12, 19, 0)),
        (msk(12, 18, 0), msk(12, 18, 30)),
        (msk(12, 6, 0), msk(12, 7, 0)),
    ],
    ids=[
        "start_1min_early",
        "start_30min_early",
        "end_1min_late",
        "end_30min_late",
        "wider_than_shift",
        "after_shift",
        "before_shift",
    ],
)
def test_within_shift_outside_shift_raises(start, end):
    with pytest.raises(SchedulingError, match=r"fit into the doctor's shift 09:00-18:00"):
        validate_within_shift(start, end, SCHEDULE, MSK)


@pytest.mark.parametrize(
    ("start", "end"),
    [(msk(12, 10), msk(12, 10)), (msk(12, 10), msk(12, 9, 59))],
    ids=["empty", "reversed"],
)
def test_within_shift_empty_or_reversed_raises(start, end):
    with pytest.raises(SchedulingError, match="ends_at must be later than starts_at"):
        validate_within_shift(start, end, SCHEDULE, MSK)


def test_within_shift_crossing_midnight_raises_even_for_all_day_shift():
    schedule = Schedule(ALL_DAYS, time(0), time(23, 59, 59), 30)
    with pytest.raises(SchedulingError, match="must not cross midnight"):
        validate_within_shift(msk(12, 23, 30), msk(13, 0, 30), schedule, MSK)


def test_within_shift_ending_exactly_at_local_midnight_counts_as_crossing():
    schedule = Schedule(ALL_DAYS, time(0), time(23, 59, 59), 30)
    with pytest.raises(SchedulingError, match="must not cross midnight"):
        validate_within_shift(msk(12, 23, 30), msk(13, 0, 0), schedule, MSK)


@pytest.mark.parametrize("day", [17, 18], ids=["saturday", "sunday"])
def test_within_shift_weekend_raises(day):
    with pytest.raises(SchedulingError, match="does not work on this day of the week"):
        validate_within_shift(msk(day, 10), msk(day, 10, 30), SCHEDULE, MSK)


def test_within_shift_weekday_is_taken_in_local_time_not_utc():
    monday_only = Schedule(frozenset({1}), time(0), time(12), 30)
    # 00:30 МСК понедельника = 21:30Z воскресенья
    start = datetime(2026, 10, 11, 21, 30, tzinfo=UTC)
    validate_within_shift(start, start + timedelta(minutes=30), monday_only, MSK)
    # 00:30 МСК вторника = 21:30Z понедельника
    start = datetime(2026, 10, 12, 21, 30, tzinfo=UTC)
    with pytest.raises(SchedulingError, match="day of the week"):
        validate_within_shift(start, start + timedelta(minutes=30), monday_only, MSK)


def test_within_shift_shift_is_local_not_utc():
    # 09:00Z = 12:00 МСК: внутри смены по местному времени, хотя в UTC это было бы «в смене» по-другому
    validate_within_shift(
        datetime(2026, 10, 12, 9, 0, tzinfo=UTC), datetime(2026, 10, 12, 9, 30, tzinfo=UTC), SCHEDULE, MSK
    )
    # 06:00Z = 09:00 МСК - начало смены; 05:30Z = 08:30 МСК - до смены
    with pytest.raises(SchedulingError, match="shift"):
        validate_within_shift(
            datetime(2026, 10, 12, 5, 30, tzinfo=UTC), datetime(2026, 10, 12, 6, 0, tzinfo=UTC), SCHEDULE, MSK
        )


def test_within_shift_input_in_other_offset_is_converted():
    start = datetime(2026, 10, 12, 11, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))  # 09:00 МСК
    validate_within_shift(start, start + timedelta(minutes=30), SCHEDULE, MSK)


def test_within_shift_other_clinic_time_zone():
    # Владивосток UTC+10: 09:00 местного = 23:00Z предыдущих суток (воскресенье UTC, понедельник локально)
    start = datetime(2026, 10, 11, 23, 0, tzinfo=UTC)
    validate_within_shift(start, start + timedelta(minutes=30), SCHEDULE, VLAT)
    with pytest.raises(SchedulingError, match="shift"):
        validate_within_shift(start - timedelta(minutes=30), start, SCHEDULE, VLAT)


@pytest.mark.parametrize("naive_end", [True, False], ids=["end_naive", "start_naive"])
def test_within_shift_naive_datetime_raises(naive_end):
    aware = msk(12, 10)
    naive = datetime(2026, 10, 12, 10, 30)
    args = (aware, naive) if naive_end else (naive.replace(hour=10, minute=0), aware + timedelta(minutes=30))
    with pytest.raises(ValueError, match="naive datetime"):
        validate_within_shift(*args, SCHEDULE, MSK)


# --------------------------------------------------------------------------- #
# validate_slot_grid
# --------------------------------------------------------------------------- #
# начало: 09:00, 09:30 ok; 09:15 нет; 08:30 (до work_start) нет; 09:00:01 нет; 09:29 нет
# длительность в слотах: 0 / -1 / 0.5 -> нет; 1 и 4 -> ok; 5 -> нет; 1.5 -> нет
# нестандартная сетка: slot 20 мин от 09:00; work_start 09:10


@pytest.mark.parametrize("hour,minute,slots", [(9, 0, 1), (9, 30, 1), (10, 0, 2), (15, 0, 4), (17, 30, 1)])
def test_slot_grid_valid_aligned_ok(hour, minute, slots):
    start = msk(12, hour, minute)
    validate_slot_grid(start, start + timedelta(minutes=30 * slots), SCHEDULE, MSK)


@pytest.mark.parametrize(
    ("hour", "minute", "second"),
    [(9, 15, 0), (9, 29, 0), (9, 31, 0), (8, 30, 0), (8, 0, 0), (9, 0, 1), (0, 0, 0)],
    ids=["09:15", "09:29", "09:31", "before_start_aligned", "before_start_far", "plus_1s", "midnight"],
)
def test_slot_grid_start_off_grid_raises(hour, minute, second):
    start = msk(12, hour, minute, second)
    with pytest.raises(SchedulingError, match="must start at a slot boundary: every 30 minutes from 09:00"):
        validate_slot_grid(start, start + timedelta(minutes=30), SCHEDULE, MSK)


@pytest.mark.parametrize(
    "duration",
    [
        timedelta(0),
        timedelta(minutes=-30),
        timedelta(minutes=29),
        timedelta(minutes=31),
        timedelta(minutes=45),
        timedelta(minutes=150),
        timedelta(minutes=121),
        timedelta(minutes=30, seconds=1),
        timedelta(hours=8),
    ],
    ids=["zero", "negative", "29min", "31min", "1.5_slots", "5_slots", "4_slots_plus_1min", "30min_1s", "8h"],
)
def test_slot_grid_wrong_duration_raises(duration):
    start = msk(12, 10)
    with pytest.raises(SchedulingError, match="duration must be from 1 to 4 slots of 30 minutes"):
        validate_slot_grid(start, start + duration, SCHEDULE, MSK)


@pytest.mark.parametrize("slots", [1, 2, 3, 4])
def test_slot_grid_duration_from_one_to_four_slots_ok(slots):
    start = msk(12, 10)
    validate_slot_grid(start, start + timedelta(minutes=30 * slots), SCHEDULE, MSK)


def test_slot_grid_custom_step_20_minutes():
    schedule = Schedule(WEEKDAYS, time(9), time(18), 20)
    validate_slot_grid(msk(12, 9, 20), msk(12, 9, 40), schedule, MSK)
    validate_slot_grid(msk(12, 10, 0), msk(12, 11, 20), schedule, MSK)  # 4 слота
    with pytest.raises(SchedulingError, match="slot boundary: every 20 minutes"):
        validate_slot_grid(msk(12, 9, 30), msk(12, 9, 50), schedule, MSK)
    with pytest.raises(SchedulingError, match="1 to 4 slots of 20 minutes"):
        validate_slot_grid(msk(12, 9, 0), msk(12, 9, 30), schedule, MSK)


def test_slot_grid_is_anchored_to_work_start_not_to_the_hour():
    schedule = Schedule(WEEKDAYS, time(9, 10), time(18), 30)
    validate_slot_grid(msk(12, 9, 40), msk(12, 10, 10), schedule, MSK)
    with pytest.raises(SchedulingError, match="slot boundary"):
        validate_slot_grid(msk(12, 9, 30), msk(12, 10, 0), schedule, MSK)


def test_slot_grid_uses_local_time_for_other_zone():
    # 09:30 по Владивостоку = 23:30Z. По UTC это тоже «:30», поэтому проверяем сдвиг 09:15 (= 23:15Z)
    start = datetime(2026, 10, 11, 23, 15, tzinfo=UTC)
    with pytest.raises(SchedulingError, match="slot boundary"):
        validate_slot_grid(start, start + timedelta(minutes=30), SCHEDULE, VLAT)
    ok = datetime(2026, 10, 11, 23, 30, tzinfo=UTC)
    validate_slot_grid(ok, ok + timedelta(minutes=30), SCHEDULE, VLAT)


def test_slot_grid_naive_datetime_raises():
    with pytest.raises(ValueError, match="naive datetime"):
        validate_slot_grid(datetime(2026, 10, 12, 10), msk(12, 10, 30), SCHEDULE, MSK)


# --------------------------------------------------------------------------- #
# intervals_overlap (полуоткрытые интервалы)
# --------------------------------------------------------------------------- #
# равные -> True; касаются (a_end == b_start и наоборот) -> False; пересечение на 1 с -> True;
# вложенные -> True; разделённые -> False; нулевой/перевёрнутый интервал -> ValueError


def _t(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 10, 12, h, m, s, tzinfo=UTC)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((_t(10), _t(11)), (_t(10), _t(11)), True),
        ((_t(10), _t(11)), (_t(11), _t(12)), False),
        ((_t(11), _t(12)), (_t(10), _t(11)), False),
        ((_t(10), _t(11, 0, 1)), (_t(11), _t(12)), True),
        ((_t(11), _t(12)), (_t(10), _t(11, 0, 1)), True),
        ((_t(10), _t(12)), (_t(10, 30), _t(11)), True),
        ((_t(10, 30), _t(11)), (_t(10), _t(12)), True),
        ((_t(10), _t(11)), (_t(12), _t(13)), False),
        ((_t(10), _t(11)), (_t(10, 59, 59), _t(12)), True),
        ((_t(10), _t(11)), (_t(10), _t(10, 0, 1)), True),
    ],
    ids=[
        "equal",
        "touch_right",
        "touch_left",
        "overlap_1s_right",
        "overlap_1s_left",
        "contains",
        "contained",
        "disjoint",
        "overlap_last_second",
        "shares_start",
    ],
)
def test_intervals_overlap_cases(a, b, expected):
    assert intervals_overlap(*a, *b) is expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ((_t(10), _t(10)), (_t(10), _t(11))),
        ((_t(11), _t(10)), (_t(10), _t(11))),
        ((_t(10), _t(11)), (_t(10), _t(10))),
        ((_t(10), _t(11)), (_t(12), _t(11))),
    ],
    ids=["a_empty", "a_reversed", "b_empty", "b_reversed"],
)
def test_intervals_overlap_non_positive_interval_raises(a, b):
    with pytest.raises(ValueError, match="positive length"):
        intervals_overlap(*a, *b)


def test_intervals_overlap_compares_instants_across_offsets():
    a = (datetime(2026, 10, 12, 13, 0, tzinfo=MSK), datetime(2026, 10, 12, 14, 0, tzinfo=MSK))  # 10:00-11:00Z
    b = (_t(11), _t(12))
    assert intervals_overlap(*a, *b) is False
    assert intervals_overlap(*a, _t(10, 59), _t(12)) is True


# --------------------------------------------------------------------------- #
# local_day_bounds
# --------------------------------------------------------------------------- #
# обычный день МСК; конец месяца, конец года, 29 февраля; пояса с летним временем (23 и 25 часов);
# отрицательное смещение; date.max / date.min -> OverflowError


@pytest.mark.parametrize(
    ("day", "tz", "start", "end"),
    [
        (date(2026, 10, 12), MSK, datetime(2026, 10, 11, 21, tzinfo=UTC), datetime(2026, 10, 12, 21, tzinfo=UTC)),
        (date(2026, 10, 31), MSK, datetime(2026, 10, 30, 21, tzinfo=UTC), datetime(2026, 10, 31, 21, tzinfo=UTC)),
        (date(2026, 12, 31), MSK, datetime(2026, 12, 30, 21, tzinfo=UTC), datetime(2026, 12, 31, 21, tzinfo=UTC)),
        (date(2027, 1, 1), MSK, datetime(2026, 12, 31, 21, tzinfo=UTC), datetime(2027, 1, 1, 21, tzinfo=UTC)),
        (date(2028, 2, 29), MSK, datetime(2028, 2, 28, 21, tzinfo=UTC), datetime(2028, 2, 29, 21, tzinfo=UTC)),
        (
            date(2026, 10, 25),
            ZoneInfo("Europe/Berlin"),
            datetime(2026, 10, 24, 22, tzinfo=UTC),
            datetime(2026, 10, 25, 23, tzinfo=UTC),
        ),
        (
            date(2026, 3, 29),
            ZoneInfo("Europe/Berlin"),
            datetime(2026, 3, 28, 23, tzinfo=UTC),
            datetime(2026, 3, 29, 22, tzinfo=UTC),
        ),
        (
            date(2026, 7, 1),
            ZoneInfo("America/New_York"),
            datetime(2026, 7, 1, 4, tzinfo=UTC),
            datetime(2026, 7, 2, 4, tzinfo=UTC),
        ),
        (date(2026, 10, 12), ZoneInfo("UTC"), datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC)),
        (
            date(2026, 10, 12),
            ZoneInfo("Asia/Kolkata"),
            datetime(2026, 10, 11, 18, 30, tzinfo=UTC),
            datetime(2026, 10, 12, 18, 30, tzinfo=UTC),
        ),
    ],
    ids=[
        "msk",
        "month_end",
        "year_end",
        "new_year",
        "leap_day",
        "berlin_25h",
        "berlin_23h",
        "new_york",
        "utc",
        "kolkata_half_hour",
    ],
)
def test_local_day_bounds_values(day, tz, start, end):
    assert local_day_bounds(day, tz) == (start, end)


def test_local_day_bounds_result_is_aware_utc():
    start, end = local_day_bounds(MONDAY, MSK)
    assert start.utcoffset() == timedelta(0)
    assert end.utcoffset() == timedelta(0)


def test_local_day_bounds_consecutive_days_are_contiguous():
    _, end = local_day_bounds(date(2026, 12, 31), MSK)
    start_next, _ = local_day_bounds(date(2027, 1, 1), MSK)
    assert end == start_next


@pytest.mark.parametrize("day", [date.max, date.min], ids=["date_max", "date_min"])
def test_local_day_bounds_at_date_limits_raises_overflow(day):
    with pytest.raises(OverflowError):
        local_day_bounds(day, MSK)


# --------------------------------------------------------------------------- #
# free_slots
# --------------------------------------------------------------------------- #
# now: 08:59:59 -> 18 слотов; 09:00:00 -> 17 (слот 09:00 не строго после now); 09:29:59 -> 17; 09:30 -> 16;
#      17:30 -> 0; позже -> 0
# дни: рабочий / сб / вс / прошедший / не рабочий для пустого work_days
# окно 90 сут: день now+90 сут (1 слот), следующий (0), накануне (все)
# занятость: касание границы не блокирует; перекрытие на 1 с блокирует; многослотовая запись; порядок и генератор
# неполный последний слот; смена в один слот; нестандартный пояс; naive now -> ValueError


def _starts(slots: list[Slot]) -> list[datetime]:
    return [s.starts_at for s in slots]


def test_free_slots_full_day_before_shift_returns_all_slots():
    now = msk(12, 8, 59, 59)
    slots = free_slots(SCHEDULE, MONDAY, MSK, [], now=now)
    assert len(slots) == 18
    assert slots[0] == Slot(msk(12, 9), msk(12, 9, 30))
    assert slots[-1] == Slot(msk(12, 17, 30), msk(12, 18))


def test_free_slots_slot_starting_exactly_now_is_excluded():
    slots = free_slots(SCHEDULE, MONDAY, MSK, [], now=NOW)
    assert len(slots) == 17
    assert slots[0].starts_at == msk(12, 9, 30)


@pytest.mark.parametrize(
    ("now", "count", "first"),
    [
        (msk(12, 9, 29, 59), 17, msk(12, 9, 30)),
        (msk(12, 9, 30), 16, msk(12, 10)),
        (msk(12, 17, 29, 59), 1, msk(12, 17, 30)),
        (msk(12, 17, 30), 0, None),
        (msk(12, 23, 59), 0, None),
    ],
    ids=["1s_before_0930", "exactly_0930", "1s_before_last_slot", "exactly_last_slot", "evening"],
)
def test_free_slots_now_inside_day_cuts_past_and_running_slots(now, count, first):
    slots = free_slots(SCHEDULE, MONDAY, MSK, [], now=now)
    assert len(slots) == count
    if first is not None:
        assert slots[0].starts_at == first


def test_free_slots_slots_are_sorted_contiguous_aware_utc():
    slots = free_slots(SCHEDULE, MONDAY, MSK, [], now=msk(12, 8))
    assert _starts(slots) == sorted(_starts(slots))
    assert all(a.ends_at == b.starts_at for a, b in zip(slots, slots[1:], strict=False))
    assert all(s.ends_at - s.starts_at == timedelta(minutes=30) for s in slots)
    assert all(s.starts_at.utcoffset() == timedelta(0) for s in slots)
    assert slots[0].starts_at == datetime(2026, 10, 12, 6, 0, tzinfo=UTC)


@pytest.mark.parametrize("day", [date(2026, 10, 17), date(2026, 10, 18)], ids=["saturday", "sunday"])
def test_free_slots_non_working_weekday_returns_empty(day):
    assert free_slots(SCHEDULE, day, MSK, [], now=NOW) == []


def test_free_slots_past_day_returns_empty():
    assert free_slots(SCHEDULE, date(2026, 10, 9), MSK, [], now=NOW) == []


def test_free_slots_empty_work_days_returns_empty():
    schedule = Schedule(frozenset(), time(9), time(18), 30)
    assert free_slots(schedule, MONDAY, MSK, [], now=msk(12, 8)) == []


def test_free_slots_busy_interval_removes_covered_slots_only():
    busy = [(msk(12, 10), msk(12, 11))]
    starts = _starts(free_slots(SCHEDULE, MONDAY, MSK, busy, now=msk(12, 8)))
    assert len(starts) == 16
    assert msk(12, 10) not in starts
    assert msk(12, 10, 30) not in starts
    assert msk(12, 9, 30) in starts
    assert msk(12, 11) in starts


def test_free_slots_busy_touching_neighbours_do_not_block_them():
    busy = [(msk(12, 9, 30), msk(12, 10))]
    starts = _starts(free_slots(SCHEDULE, MONDAY, MSK, busy, now=msk(12, 8)))
    assert msk(12, 9) in starts
    assert msk(12, 9, 30) not in starts
    assert msk(12, 10) in starts


@pytest.mark.parametrize(
    ("busy", "blocked"),
    [
        ((msk(12, 10, 15), msk(12, 10, 45)), [msk(12, 10), msk(12, 10, 30)]),
        ((msk(12, 9, 59, 59), msk(12, 10, 0, 1)), [msk(12, 9, 30), msk(12, 10)]),
        ((msk(12, 10), msk(12, 10, 0, 1)), [msk(12, 10)]),
        ((msk(12, 10, 29, 59), msk(12, 10, 30, 1)), [msk(12, 10), msk(12, 10, 30)]),
        ((msk(12, 8), msk(12, 9, 0, 1)), [msk(12, 9)]),
        ((msk(12, 17, 59, 59), msk(12, 19)), [msk(12, 17, 30)]),
    ],
    ids=[
        "offgrid_inside",
        "1s_each_side",
        "1s_inside_slot",
        "straddles_two",
        "starts_before_shift",
        "ends_after_shift",
    ],
)
def test_free_slots_busy_partial_overlap_blocks_exactly_overlapped_slots(busy, blocked):
    all_starts = set(_starts(free_slots(SCHEDULE, MONDAY, MSK, [], now=msk(12, 8))))
    starts = set(_starts(free_slots(SCHEDULE, MONDAY, MSK, [busy], now=msk(12, 8))))
    assert all_starts - starts == set(blocked)


def test_free_slots_busy_unordered_and_generator_accepted():
    busy = ((msk(12, 15), msk(12, 16)), (msk(12, 9), msk(12, 10)), (msk(12, 12), msk(12, 12, 30)))
    starts = _starts(free_slots(SCHEDULE, MONDAY, MSK, (b for b in busy), now=msk(12, 8)))
    assert len(starts) == 18 - 2 - 2 - 1
    for blocked in (msk(12, 9), msk(12, 9, 30), msk(12, 12), msk(12, 15), msk(12, 15, 30)):
        assert blocked not in starts


def test_free_slots_all_busy_returns_empty():
    assert free_slots(SCHEDULE, MONDAY, MSK, [(msk(12, 0), msk(13, 0))], now=msk(12, 8)) == []


def test_free_slots_busy_on_another_day_is_ignored():
    busy = [(msk(13, 9), msk(13, 18))]
    assert len(free_slots(SCHEDULE, MONDAY, MSK, busy, now=msk(12, 8))) == 18


def test_free_slots_incomplete_last_slot_is_dropped():
    schedule = Schedule(WEEKDAYS, time(9), time(17, 45), 30)
    starts = _starts(free_slots(schedule, MONDAY, MSK, [], now=msk(12, 8)))
    assert len(starts) == 17
    assert starts[-1] == msk(12, 17)


def test_free_slots_shift_shorter_than_slot_returns_empty():
    schedule = Schedule(WEEKDAYS, time(9), time(9, 29), 30)
    assert free_slots(schedule, MONDAY, MSK, [], now=msk(12, 8)) == []


def test_free_slots_shift_of_exactly_one_slot_returns_one():
    schedule = Schedule(WEEKDAYS, time(9), time(9, 30), 30)
    assert free_slots(schedule, MONDAY, MSK, [], now=msk(12, 8)) == [Slot(msk(12, 9), msk(12, 9, 30))]


# окно записи 90 суток: now = пн 12.10.2026 09:00 МСК, now+90 сут = вс 10.01.2027 09:00 МСК
ALL_DAYS_SCHEDULE = Schedule(ALL_DAYS, time(9), time(18), 30)


def test_free_slots_last_day_of_window_offers_only_slot_not_after_limit():
    slots = free_slots(ALL_DAYS_SCHEDULE, date(2027, 1, 10), MSK, [], now=NOW)
    assert slots == [Slot(msk(10, 9, month=1, year=2027), msk(10, 9, 30, month=1, year=2027))]


def test_free_slots_day_after_window_is_empty():
    assert free_slots(ALL_DAYS_SCHEDULE, date(2027, 1, 11), MSK, [], now=NOW) == []


def test_free_slots_day_before_window_end_is_full():
    assert len(free_slots(ALL_DAYS_SCHEDULE, date(2027, 1, 9), MSK, [], now=NOW)) == 18


def test_free_slots_window_matches_validate_booking_window():
    slots = free_slots(ALL_DAYS_SCHEDULE, date(2027, 1, 10), MSK, [], now=NOW)
    for slot in slots:
        validate_booking_window(slot.starts_at, now=NOW)
    with pytest.raises(SchedulingError):
        validate_booking_window(slots[-1].starts_at + timedelta(minutes=30), now=NOW)


def test_free_slots_custom_window_of_one_day():
    day = date(2026, 10, 13)
    slots = free_slots(SCHEDULE, day, MSK, [], now=NOW, max_days_ahead=1)
    assert [s.starts_at for s in slots] == [msk(13, 9)]  # 13.10 09:00 = now + 1 сут ровно
    assert free_slots(SCHEDULE, date(2026, 10, 14), MSK, [], now=NOW, max_days_ahead=1) == []


def test_free_slots_zero_window_returns_empty():
    assert free_slots(SCHEDULE, MONDAY, MSK, [], now=msk(12, 8), max_days_ahead=0) == []


def test_free_slots_every_returned_slot_passes_the_validators():
    for slot in free_slots(SCHEDULE, date(2026, 10, 14), MSK, [], now=NOW):
        validate_booking_window(slot.starts_at, now=NOW)
        validate_within_shift(slot.starts_at, slot.ends_at, SCHEDULE, MSK)
        validate_slot_grid(slot.starts_at, slot.ends_at, SCHEDULE, MSK)


def test_free_slots_other_time_zone_gives_utc_of_previous_day():
    now = datetime(2026, 10, 10, tzinfo=UTC)
    slots = free_slots(SCHEDULE, MONDAY, VLAT, [], now=now)
    assert slots[0].starts_at == datetime(2026, 10, 11, 23, 0, tzinfo=UTC)  # 09:00 местного
    assert slots[-1].ends_at == datetime(2026, 10, 12, 8, 0, tzinfo=UTC)  # 18:00 местного


def test_free_slots_late_evening_shift_stays_on_local_day():
    schedule = Schedule(ALL_DAYS, time(23, 0), time(23, 59, 59), 30)
    slots = free_slots(schedule, MONDAY, MSK, [], now=msk(12, 8))
    assert slots == [Slot(msk(12, 23), msk(12, 23, 30))]  # 23:30-24:00 не помещается в смену


def test_free_slots_night_shift_start_belongs_to_previous_utc_day():
    schedule = Schedule(ALL_DAYS, time(0), time(1), 30)
    slots = free_slots(schedule, MONDAY, MSK, [], now=datetime(2026, 10, 11, tzinfo=UTC))
    assert _starts(slots) == [datetime(2026, 10, 11, 21, 0, tzinfo=UTC), datetime(2026, 10, 11, 21, 30, tzinfo=UTC)]


def test_free_slots_month_and_year_boundary():
    now = datetime(2026, 12, 30, tzinfo=UTC)
    slots = free_slots(ALL_DAYS_SCHEDULE, date(2026, 12, 31), MSK, [], now=now)
    assert len(slots) == 18
    assert slots[-1].ends_at == datetime(2026, 12, 31, 15, 0, tzinfo=UTC)
    slots = free_slots(ALL_DAYS_SCHEDULE, date(2027, 1, 1), MSK, [], now=now)
    assert slots[0].starts_at == datetime(2027, 1, 1, 6, 0, tzinfo=UTC)


def test_free_slots_naive_now_raises():
    with pytest.raises(ValueError, match="naive datetime"):
        free_slots(SCHEDULE, MONDAY, MSK, [], now=datetime(2026, 10, 12, 6, 0))


def test_module_constants():
    assert scheduling.MAX_BOOKING_DAYS_AHEAD == 90
    assert scheduling.MAX_SLOTS_PER_APPOINTMENT == 4
