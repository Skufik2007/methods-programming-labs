# Ответ G1: unit-тесты (scheduling, analytics, schemas, config, security)

Тесты проверены локально: все файлы проходят (scheduling 153, analytics 179, schemas 358, config+security 186 тестов). Код приложения и conftest.py не менялись. Таблицы классов эквивалентности и границ приведены в комментариях над группами тестов в каждом файле.

### tests/test_gen_scheduling.py

```python
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


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(0), timedelta(days=-30)], ids=["minus1s", "now", "far_past"])
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


@pytest.mark.parametrize("delta", [timedelta(days=90, seconds=1), timedelta(days=91), timedelta(days=365)], ids=["90d_plus1s", "91d", "year"])
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
    kwargs = {"starts_at": naive, "now": NOW} if which == "starts_at" else {"starts_at": aware, "now": NOW.replace(tzinfo=None)}
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
    ids=["start_1min_early", "start_30min_early", "end_1min_late", "end_30min_late", "wider_than_shift", "after_shift", "before_shift"],
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
    validate_within_shift(datetime(2026, 10, 12, 9, 0, tzinfo=UTC), datetime(2026, 10, 12, 9, 30, tzinfo=UTC), SCHEDULE, MSK)
    # 06:00Z = 09:00 МСК - начало смены; 05:30Z = 08:30 МСК - до смены
    with pytest.raises(SchedulingError, match="shift"):
        validate_within_shift(datetime(2026, 10, 12, 5, 30, tzinfo=UTC), datetime(2026, 10, 12, 6, 0, tzinfo=UTC), SCHEDULE, MSK)


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
    [timedelta(0), timedelta(minutes=-30), timedelta(minutes=29), timedelta(minutes=31), timedelta(minutes=45),
     timedelta(minutes=150), timedelta(minutes=121), timedelta(minutes=30, seconds=1), timedelta(hours=8)],
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
    ids=["equal", "touch_right", "touch_left", "overlap_1s_right", "overlap_1s_left", "contains", "contained",
         "disjoint", "overlap_last_second", "shares_start"],
)
def test_intervals_overlap_cases(a, b, expected):
    assert intervals_overlap(*a, *b) is expected


@pytest.mark.parametrize(
    ("a", "b"),
    [((_t(10), _t(10)), (_t(10), _t(11))), ((_t(11), _t(10)), (_t(10), _t(11))),
     ((_t(10), _t(11)), (_t(10), _t(10))), ((_t(10), _t(11)), (_t(12), _t(11)))],
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
        (date(2026, 10, 25), ZoneInfo("Europe/Berlin"), datetime(2026, 10, 24, 22, tzinfo=UTC), datetime(2026, 10, 25, 23, tzinfo=UTC)),
        (date(2026, 3, 29), ZoneInfo("Europe/Berlin"), datetime(2026, 3, 28, 23, tzinfo=UTC), datetime(2026, 3, 29, 22, tzinfo=UTC)),
        (date(2026, 7, 1), ZoneInfo("America/New_York"), datetime(2026, 7, 1, 4, tzinfo=UTC), datetime(2026, 7, 2, 4, tzinfo=UTC)),
        (date(2026, 10, 12), ZoneInfo("UTC"), datetime(2026, 10, 12, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC)),
        (date(2026, 10, 12), ZoneInfo("Asia/Kolkata"), datetime(2026, 10, 11, 18, 30, tzinfo=UTC), datetime(2026, 10, 12, 18, 30, tzinfo=UTC)),
    ],
    ids=["msk", "month_end", "year_end", "new_year", "leap_day", "berlin_25h", "berlin_23h", "new_york", "utc", "kolkata_half_hour"],
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
    ids=["offgrid_inside", "1s_each_side", "1s_inside_slot", "straddles_two", "starts_before_shift", "ends_after_shift"],
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
```

### tests/test_gen_analytics_pure.py

```python
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
    ids=["mon", "fri", "sat", "sun", "weekend", "sat_to_mon", "mon_fri", "exact_week", "wed_to_tue",
         "week_plus_1", "13_days", "two_weeks_from_thu", "fri_to_sun_next", "new_year", "leap_day", "month_boundary"],
)
def test_available_minutes_counts_working_days_times_shift(d_from, d_to, working_days):
    assert available_minutes(SCHEDULE, d_from, d_to) == working_days * SHIFT


@pytest.mark.parametrize("start", [date(2026, 10, 12) + timedelta(days=i) for i in range(7)], ids=lambda d: d.strftime("%a"))
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
    assert _segments(date(2026, 10, 12), date(2026, 10, 13), ZoneInfo("Asia/Kolkata")) == [(utc(2026, 10, 11, 18, 30), 19800)]


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
        (0, 0, 0.0), (5, 0, 0.0), (0, 5, 0.0), (5, 5, 1.0), (1, 2, 0.5), (1, 3, 0.3333), (2, 3, 0.6667),
        (1, 8, 0.125), (1, 7, 0.1429), (1, 30000, 0.0), (9999, 10000, 0.9999), (3, 2, 1.5), (1, 1, 1.0),
    ],
    ids=["zero_zero", "num_over_zero", "zero_num", "equal", "half", "third", "two_thirds", "eighth", "seventh",
         "tiny_rounds_to_zero", "four_digits", "above_one", "one_one"],
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
    [("2026-10-12", date(2026, 10, 12)), ("2028-02-29", date(2028, 2, 29)), ("2026-12-31", date(2026, 12, 31)),
     ("0001-01-01", date(1, 1, 1))],
    ids=["regular", "leap_day", "year_end", "min_date"],
)
def test_as_date_iso_string(text, expected):
    assert _as_date(text) == expected


@pytest.mark.parametrize("bad", ["", "not-a-date", "2027-02-29", "2026-13-01", "2026-10-32", None, "12.10.2026"],
                         ids=["empty", "garbage", "non_leap_feb_29", "month_13", "day_32", "none", "dotted"])
def test_as_date_invalid_value_raises_value_error(bad):
    with pytest.raises(ValueError):
        _as_date(bad)
```

### tests/test_gen_schemas.py

```python
"""Unit-тесты валидаторов app/schemas.py (без HTTP и БД)."""

from datetime import UTC, date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from app import schemas
from app.models import AppointmentStatus, UserRole
from app.schemas import (
    AppointmentCreate,
    AppointmentUpdate,
    Cabinet,
    ClinicTime,
    Dosage,
    DoctorCreate,
    DoctorUpdate,
    Email,
    EntityId,
    FullName,
    Icd10Code,
    Instructions,
    LoginIn,
    LoginPassword,
    MedicalRecordCreate,
    MedicalRecordUpdate,
    Medication,
    Notes,
    Password,
    PatientCreate,
    PatientUpdate,
    PhoneE164,
    PolicyNumber,
    PrescriptionCreate,
    PrescriptionUpdate,
    Reason,
    SlotMinutes,
    Specialty,
    TokenOut,
    UserCreate,
    UserOut,
    UserUpdate,
    UTCDatetime,
    WorkDays,
)

TODAY = date(2026, 10, 12)


def check(annotation, value):
    return TypeAdapter(annotation).validate_python(value)


def fails(annotation, value):
    with pytest.raises(ValidationError):
        TypeAdapter(annotation).validate_python(value)


def error_of(exc_info) -> dict:
    return exc_info.value.errors()[0]


@pytest.fixture
def frozen_today(monkeypatch):
    """Подменяет «сегодня» внутри валидатора даты рождения (12.10.2026 по UTC)."""

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 12, 6, 0, tzinfo=UTC)

    monkeypatch.setattr(schemas, "datetime", FakeDatetime)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
# нормализация: strip + lower; длина 6 (мин) / 5; 254 (макс) / 255; метки домена 63 / 64; TLD 2..63;
# недопустимые: нет @, два @, пробелы, точки по краям/двойные, дефис по краям метки, нет TLD, юникод.

_LONG_DOMAIN = ".".join(["x" * 63] * 3 + ["com"])  # 195 символов


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user@example.com", "user@example.com"),
        ("  User@Example.COM  ", "user@example.com"),
        ("\tA.B+tag@Sub.Example.Org\n", "a.b+tag@sub.example.org"),
        ("a_b%c-d@ex-ample.co", "a_b%c-d@ex-ample.co"),
        ("a@b.co", "a@b.co"),
        ("a" * (254 - len(_LONG_DOMAIN) - 1) + "@" + _LONG_DOMAIN, "a" * (254 - len(_LONG_DOMAIN) - 1) + "@" + _LONG_DOMAIN),
        ("a@" + "x" * 63 + ".com", "a@" + "x" * 63 + ".com"),
        ("a@example." + "c" * 63, "a@example." + "c" * 63),
        ("1@2.io", "1@2.io"),
    ],
    ids=["plain", "strip_and_lower", "whitespace_chars_and_subdomain", "special_local_chars", "min_length_6",
         "max_length_254", "label_63", "tld_63", "digits"],
)
def test_email_valid_is_normalized(raw, expected):
    assert check(Email, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "", "   ", "plainaddress", "a@b", "a@@b.com", "@b.com", "a@.com", "a b@example.com", "a@exa mple.com",
        ".a@example.com", "a.@example.com", "a..b@example.com", "a@-b.com", "a@b-.com", "a@b.c", "a@b.c0m",
        "a@example.123", "a@exa_mple.com", "тест@example.com", "a@пример.рф", "a@example.com,b@example.com",
        "a@" + "x" * 64 + ".com", "a@example." + "c" * 64, "a" * 244 + "@example.com",
        "a@b.c" "om" + "­", "a@example.com\x00",
    ],
    ids=["empty", "blank", "no_at", "no_tld", "double_at", "no_local", "empty_label", "space_local", "space_domain",
         "leading_dot", "trailing_dot", "double_dot", "label_starts_dash", "label_ends_dash", "tld_1_char",
         "tld_with_digit", "numeric_tld", "underscore_domain", "cyrillic_local", "cyrillic_domain", "two_addresses",
         "label_64", "tld_64", "length_255", "soft_hyphen", "nul_byte"],
)
def test_email_invalid_raises(raw):
    fails(Email, raw)


def test_email_five_characters_is_too_short():
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(Email).validate_python("a@b.c")
    assert error_of(exc)["type"] == "string_too_short"


def test_email_255_characters_reports_too_long():
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(Email).validate_python("a" * (255 - len(_LONG_DOMAIN) - 1) + "@" + _LONG_DOMAIN)
    assert error_of(exc)["type"] == "string_too_long"


@pytest.mark.parametrize("bad", [None, 123, ["a@b.com"]], ids=["none", "int", "list"])
def test_email_non_string_raises(bad):
    fails(Email, bad)


# --------------------------------------------------------------------------- #
# Password / LoginPassword
# --------------------------------------------------------------------------- #
# длина в символах: 7 -> нет, 8 -> да; в байтах UTF-8: 72 -> да, 73 -> нет;
# вход: 1 -> да, 0 -> нет, 1024 -> да, 1025 -> нет (без ограничения 72 байт)


@pytest.mark.parametrize(
    "raw",
    ["abcdefgh", "Password123", "a" * 72, "я" * 36, "я" * 8, "a" * 71, "😀" * 18, "пароль" + "!" * 30],
    ids=["8_chars", "typical", "72_bytes_ascii", "72_bytes_cyrillic", "8_cyrillic_chars", "71_ascii", "72_bytes_emoji", "mixed_cyr_ascii"],
)
def test_password_valid_returns_secret_unchanged(raw):
    secret = check(Password, raw)
    assert secret.get_secret_value() == raw
    assert raw not in repr(secret)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "at least 8 characters"),
        ("1234567", "at least 8 characters"),
        ("я" * 7, "at least 8 characters"),
        ("a" * 73, "72 bytes"),
        ("я" * 37, "72 bytes"),
        ("a" * 71 + "я", "72 bytes"),
        ("😀" * 19, "72 bytes"),
        ("a" * 1000, "72 bytes"),
    ],
    ids=["empty", "7_chars", "7_cyrillic", "73_bytes_ascii", "74_bytes_cyrillic", "73_bytes_mixed", "76_bytes_emoji", "1000"],
)
def test_password_invalid_raises_with_reason_and_does_not_leak_value(raw, message):
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(Password).validate_python(raw)
    assert message in error_of(exc)["msg"]
    if len(raw) >= 8:
        assert raw not in str(exc.value)


@pytest.mark.parametrize("bad", [None, 12345678, ["password123"]], ids=["none", "int", "list"])
def test_password_non_string_raises(bad):
    fails(Password, bad)


@pytest.mark.parametrize("raw", ["a", "x" * 72, "x" * 73, "я" * 100, "x" * 1024], ids=["1", "72", "73_bytes_ok_for_login", "cyrillic", "1024"])
def test_login_password_valid_lengths_ok(raw):
    assert check(LoginPassword, raw).get_secret_value() == raw


@pytest.mark.parametrize("raw", ["", "x" * 1025], ids=["empty", "1025"])
def test_login_password_invalid_lengths_raise(raw):
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(LoginPassword).validate_python(raw)
    assert "invalid password length" in error_of(exc)["msg"]


def test_login_in_normalizes_email_and_keeps_password_secret():
    data = LoginIn(email="  Doc@Clinic.RU ", password="x")
    assert data.email == "doc@clinic.ru"
    assert data.password.get_secret_value() == "x"
    assert "**" in repr(data)


# --------------------------------------------------------------------------- #
# PolicyNumber (ОМС): ровно 16 ASCII-цифр
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1234567890123456", "1234567890123456"), ("0000000000000000", "0000000000000000"),
     ("  1234567890123456\n", "1234567890123456")],
    ids=["regular", "leading_zeros_kept", "stripped"],
)
def test_policy_number_valid(raw, expected):
    assert check(PolicyNumber, raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "123456789012345", "12345678901234567", "123456789012345a", "1234 5678 9012 3456", "1234-5678-9012-3456",
     "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦", "１２３４５６７８９０１２３４５６", "12345678901234.6", "+234567890123456", "1234567890123456\n1"],
    ids=["empty", "15_digits", "17_digits", "letter", "spaces", "dashes", "arabic_indic_digits", "fullwidth_digits",
         "dot", "plus", "newline_inside"],
)
def test_policy_number_invalid_raises(raw):
    fails(PolicyNumber, raw)


def test_policy_number_int_input_rejected():
    fails(PolicyNumber, 1234567890123456)


# --------------------------------------------------------------------------- #
# PhoneE164: "+" + код страны 1-9 + всего 7..15 цифр
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("+79991234567", "+79991234567"), ("+1234567", "+1234567"), ("+123456789012345", "+123456789012345"),
     ("  +79991234567 ", "+79991234567"), ("+9999999", "+9999999")],
    ids=["russian", "min_7_digits", "max_15_digits", "stripped", "nines"],
)
def test_phone_valid(raw, expected):
    assert check(PhoneE164, raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "+", "+123456", "+1234567890123456", "+0123456789", "+00123456789", "79991234567", "89991234567",
     "+7 999 123 45 67", "+7(999)1234567", "+7-999-123-45-67", "++79991234567", "+7999123456a", "+٧٩٩٩١٢٣٤٥٦٧",
     "+79991234567 ext", "+7999123456\n7"],
    ids=["empty", "plus_only", "6_digits", "16_digits", "starts_with_0", "double_zero", "no_plus", "8_prefix",
         "spaces", "brackets", "dashes", "double_plus", "letter", "arabic_digits", "extension", "newline_inside"],
)
def test_phone_invalid_raises(raw):
    fails(PhoneE164, raw)


# --------------------------------------------------------------------------- #
# Icd10Code: буква + 2 цифры + необязательно ".d" / ".dd"; верхний регистр
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("J06.9", "J06.9"), ("j06.9", "J06.9"), ("I10", "I10"), ("s72.00", "S72.00"), (" a00 ", "A00"),
     ("Z99.99", "Z99.99"), ("A00", "A00"), ("u07.1", "U07.1"), ("C34.1", "C34.1")],
    ids=["upper", "lower_is_uppercased", "no_subcode", "two_digit_subcode", "stripped", "max_chars", "min_code",
         "u_code", "c_code"],
)
def test_icd10_valid_is_normalized(raw, expected):
    assert check(Icd10Code, raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "J6.9", "J06.", "J06.999", "J06,9", "06.9", "JJ06", "J0", "J06.9.1", "J06 .9", "Ж06.9", "J０６", "J06.٩",
     "J", "123", "J06.9*", "J06.9†", "J06-9"],
    ids=["empty", "one_digit", "dot_only", "three_digit_subcode", "comma", "no_letter", "two_letters", "one_digit_code",
         "two_subcodes", "space", "cyrillic_letter", "fullwidth_digits", "arabic_subcode", "letter_only",
         "digits_only", "asterisk", "dagger", "dash"],
)
def test_icd10_invalid_raises(raw):
    fails(Icd10Code, raw)


# --------------------------------------------------------------------------- #
# Строковые типы: (min, max) длины после strip
# --------------------------------------------------------------------------- #

STRING_TYPES = [
    pytest.param(FullName, 2, 255, id="FullName"),
    pytest.param(Specialty, 2, 100, id="Specialty"),
    pytest.param(Cabinet, 1, 20, id="Cabinet"),
    pytest.param(Medication, 1, 255, id="Medication"),
    pytest.param(Dosage, 1, 100, id="Dosage"),
    pytest.param(Reason, 1, 500, id="Reason"),
    pytest.param(Instructions, 1, 2000, id="Instructions"),
    pytest.param(Notes, 1, 10_000, id="Notes"),
]


@pytest.mark.parametrize(("annotation", "lo", "hi"), STRING_TYPES)
def test_string_type_length_boundaries_ok(annotation, lo, hi):
    assert check(annotation, "x" * lo) == "x" * lo
    assert check(annotation, "x" * hi) == "x" * hi
    assert check(annotation, "я" * hi) == "я" * hi  # символы, а не байты


@pytest.mark.parametrize(("annotation", "lo", "hi"), STRING_TYPES)
def test_string_type_length_out_of_bounds_raises(annotation, lo, hi):
    fails(annotation, "x" * (lo - 1))
    fails(annotation, "x" * (hi + 1))


@pytest.mark.parametrize(("annotation", "lo", "hi"), STRING_TYPES)
def test_string_type_whitespace_is_stripped_before_length_check(annotation, lo, hi):
    assert check(annotation, f"  {'x' * hi}\n") == "x" * hi
    fails(annotation, f" {'x' * (lo - 1)} ")
    fails(annotation, "   \t\n ")


@pytest.mark.parametrize(("annotation", "lo", "hi"), STRING_TYPES)
def test_string_type_none_and_non_string_raise(annotation, lo, hi):
    fails(annotation, None)
    fails(annotation, 123)


# --------------------------------------------------------------------------- #
# EntityId, SlotMinutes, WorkDays, UTCDatetime, ClinicTime
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [1, 2, 2_147_483_647])
def test_entity_id_valid(value):
    assert check(EntityId, value) == value


@pytest.mark.parametrize("value", [0, -1, 2_147_483_648, None, "abc", 1.5], ids=["zero", "negative", "over_int32", "none", "text", "float"])
def test_entity_id_invalid_raises(value):
    fails(EntityId, value)


@pytest.mark.parametrize("value", [5, 6, 30, 239, 240])
def test_slot_minutes_valid(value):
    assert check(SlotMinutes, value) == value


@pytest.mark.parametrize("value", [4, 0, -5, 241, 1000, None])
def test_slot_minutes_invalid_raises(value):
    fails(SlotMinutes, value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [([1], [1]), ([7, 1, 3], [1, 3, 7]), ([5, 4, 3, 2, 1], [1, 2, 3, 4, 5]), ([1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7]),
     ([7], [7])],
    ids=["single", "unsorted", "reversed", "all_week", "sunday"],
)
def test_work_days_valid_is_sorted(raw, expected):
    assert check(WorkDays, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [[], [0], [8], [-1], [1, 1], [1, 2, 2], [1, 2, 3, 4, 5, 6, 7, 1], None, "12345", [1, "x"]],
    ids=["empty", "zero", "eight", "negative", "duplicate", "duplicate_later", "eight_items", "none", "string", "non_int"],
)
def test_work_days_invalid_raises(raw):
    fails(WorkDays, raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-12T12:00:00+03:00", datetime(2026, 10, 12, 9, tzinfo=UTC)),
        ("2026-10-12T12:00:00Z", datetime(2026, 10, 12, 12, tzinfo=UTC)),
        ("2026-10-13T00:30:00+03:00", datetime(2026, 10, 12, 21, 30, tzinfo=UTC)),
        ("2027-01-01T01:00:00+03:00", datetime(2026, 12, 31, 22, tzinfo=UTC)),
        ("2026-10-12T00:00:00-05:00", datetime(2026, 10, 12, 5, tzinfo=UTC)),
        ("2026-10-12T05:30:00+05:30", datetime(2026, 10, 12, 0, tzinfo=UTC)),
        ("2028-02-29T23:59:59-12:00", datetime(2028, 3, 1, 11, 59, 59, tzinfo=UTC)),
    ],
    ids=["msk", "utc_z", "after_local_midnight", "year_end", "negative_offset", "half_hour", "leap_day_far_west"],
)
def test_utc_datetime_converted_to_utc(raw, expected):
    result = check(UTCDatetime, raw)
    assert result == expected
    assert result.utcoffset() == timedelta(0)
    assert result.tzinfo is UTC or result.tzinfo == UTC


def test_utc_datetime_accepts_aware_datetime_object():
    value = datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=3)))
    assert check(UTCDatetime, value) == datetime(2026, 10, 12, 9, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["2026-10-12T12:00:00", datetime(2026, 10, 12, 12), "2026-10-12", "garbage", "", None],
                         ids=["naive_str", "naive_obj", "date_only", "garbage", "empty", "none"])
def test_utc_datetime_naive_or_invalid_raises(raw):
    fails(UTCDatetime, raw)


@pytest.mark.parametrize("raw", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"], ids=["min_edge", "max_edge"])
def test_utc_datetime_overflow_edge_gives_validation_error_not_overflow(raw):
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(UTCDatetime).validate_python(raw)
    assert "out of the supported range" in error_of(exc)["msg"]


@pytest.mark.parametrize(("raw", "expected"), [("09:00", time(9)), (time(23, 59, 59), time(23, 59, 59)), ("00:00", time(0))])
def test_clinic_time_naive_ok(raw, expected):
    assert check(ClinicTime, raw) == expected


@pytest.mark.parametrize("raw", ["09:00+03:00", "09:00Z", time(9, tzinfo=UTC)], ids=["offset", "z", "aware_obj"])
def test_clinic_time_with_offset_raises(raw):
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(ClinicTime).validate_python(raw)
    assert "must not contain a UTC offset" in error_of(exc)["msg"]


@pytest.mark.parametrize("raw", ["25:00", "09:60", "nine", None], ids=["hour_25", "minute_60", "text", "none"])
def test_clinic_time_invalid_raises(raw):
    fails(ClinicTime, raw)


# --------------------------------------------------------------------------- #
# Дата рождения (PatientCreate.birth_date), «сегодня» = 12.10.2026
# --------------------------------------------------------------------------- #
# сегодня, завтра (допуск на пояса) -> ok; послезавтра -> нет; год рождения 1896 (= год - 130) ok, 1895 нет


def make_patient(**override):
    data = {"full_name": "Иван Иванов", "birth_date": date(1990, 5, 17), "policy_number": "1234567890123456"}
    data.update(override)
    return PatientCreate(**data)


@pytest.mark.parametrize(
    "value",
    [TODAY, TODAY + timedelta(days=1), date(1990, 5, 17), date(2000, 2, 29), date(1896, 1, 1), date(1896, 12, 31), date(2026, 10, 11)],
    ids=["today", "tomorrow", "regular", "leap_day", "oldest_allowed_first_day", "oldest_allowed_last_day", "yesterday"],
)
def test_birth_date_valid(frozen_today, value):
    assert make_patient(birth_date=value).birth_date == value


@pytest.mark.parametrize("value", [TODAY + timedelta(days=2), date(2030, 1, 1), date(9999, 12, 31)], ids=["day_after_tomorrow", "future_year", "max"])
def test_birth_date_in_future_raises(frozen_today, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(birth_date=value)
    assert error_of(exc)["loc"] == ("birth_date",)
    assert "must not be in the future" in error_of(exc)["msg"]


@pytest.mark.parametrize("value", [date(1895, 12, 31), date(1800, 1, 1), date(1, 1, 1)], ids=["one_day_too_old", "1800", "min"])
def test_birth_date_too_old_raises(frozen_today, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(birth_date=value)
    assert "implausibly far in the past" in error_of(exc)["msg"]


def test_birth_date_year_boundary_uses_first_of_january(frozen_today):
    monkey_today = date(2026, 1, 1)
    assert make_patient(birth_date=date(monkey_today.year - 130, 1, 1)).birth_date == date(1896, 1, 1)


@pytest.mark.parametrize("value", ["1990-02-30", "1990-13-01", "garbage", "", None, 123], ids=["feb_30", "month_13", "text", "empty", "none", "int"])
def test_birth_date_unparsable_raises(frozen_today, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(birth_date=value)
    assert error_of(exc)["loc"] == ("birth_date",)


def test_birth_date_iso_string_is_parsed(frozen_today):
    assert make_patient(birth_date="1990-05-17").birth_date == date(1990, 5, 17)


# --------------------------------------------------------------------------- #
# Create-схемы
# --------------------------------------------------------------------------- #


def test_user_create_normalizes_fields():
    user = UserCreate(email=" Ann@Clinic.RU ", password="Password123", full_name="  Анна Петрова ", role="doctor")
    assert user.email == "ann@clinic.ru"
    assert user.full_name == "Анна Петрова"
    assert user.role is UserRole.DOCTOR
    assert user.password.get_secret_value() == "Password123"


@pytest.mark.parametrize("role", [r.value for r in UserRole])
def test_user_create_accepts_every_role(role):
    assert UserCreate(email="a@b.co", password="Password123", full_name="Ан", role=role).role.value == role


@pytest.mark.parametrize("role", ["superuser", "ADMIN", "", None, 1], ids=["unknown", "uppercase", "empty", "none", "int"])
def test_user_create_invalid_role_raises(role):
    with pytest.raises(ValidationError) as exc:
        UserCreate(email="a@b.co", password="Password123", full_name="Ан", role=role)
    assert error_of(exc)["loc"] == ("role",)


@pytest.mark.parametrize("extra", ["is_active", "id", "password_hash", "token_version"])
def test_user_create_extra_field_forbidden(extra):
    with pytest.raises(ValidationError) as exc:
        UserCreate(email="a@b.co", password="Password123", full_name="Ан", role="admin", **{extra: 1})
    assert error_of(exc)["type"] == "extra_forbidden"
    assert error_of(exc)["loc"] == (extra,)


@pytest.mark.parametrize("missing", ["email", "password", "full_name", "role"])
def test_user_create_missing_required_field_raises(missing):
    data = {"email": "a@b.co", "password": "Password123", "full_name": "Ан", "role": "admin"}
    del data[missing]
    with pytest.raises(ValidationError) as exc:
        UserCreate(**data)
    assert error_of(exc)["type"] == "missing"
    assert error_of(exc)["loc"] == (missing,)


def doctor_data(**override):
    data = {"user_id": 1, "specialty": "Терапевт", "cabinet": "101", "work_start": "09:00", "work_end": "18:00"}
    data.update(override)
    return data


def test_doctor_create_defaults():
    doctor = DoctorCreate(**doctor_data())
    assert doctor.slot_minutes == 30
    assert doctor.work_days == [1, 2, 3, 4, 5]
    assert doctor.work_start == time(9)
    assert doctor.work_end == time(18)


def test_doctor_create_default_work_days_not_shared_between_instances():
    first = DoctorCreate(**doctor_data())
    first.work_days.append(6)
    assert DoctorCreate(**doctor_data()).work_days == [1, 2, 3, 4, 5]


def test_doctor_create_work_days_sorted():
    assert DoctorCreate(**doctor_data(work_days=[5, 1, 3])).work_days == [1, 3, 5]


@pytest.mark.parametrize(
    ("start", "end"),
    [("09:00", "09:01"), ("00:00", "23:59:59"), ("17:59", "18:00")],
    ids=["one_minute", "full_day", "last_minute"],
)
def test_doctor_create_shift_start_before_end_ok(start, end):
    DoctorCreate(**doctor_data(work_start=start, work_end=end))


@pytest.mark.parametrize(
    ("start", "end"),
    [("09:00", "09:00"), ("18:00", "09:00"), ("09:01", "09:00"), ("23:00", "01:00")],
    ids=["equal", "reversed", "reversed_by_one_minute", "overnight"],
)
def test_doctor_create_shift_start_not_before_end_raises(start, end):
    with pytest.raises(ValidationError) as exc:
        DoctorCreate(**doctor_data(work_start=start, work_end=end))
    assert "work_start must be earlier than work_end" in error_of(exc)["msg"]


@pytest.mark.parametrize("field", ["work_start", "work_end"])
def test_doctor_create_shift_with_offset_raises(field):
    with pytest.raises(ValidationError) as exc:
        DoctorCreate(**doctor_data(**{field: "10:00+03:00"}))
    assert error_of(exc)["loc"] == (field,)


@pytest.mark.parametrize(
    ("field", "value"),
    [("user_id", 0), ("user_id", 2_147_483_648), ("specialty", "A"), ("cabinet", ""), ("slot_minutes", 4),
     ("slot_minutes", 241), ("work_days", []), ("work_days", [1, 1])],
    ids=["user_id_0", "user_id_over", "specialty_short", "cabinet_empty", "slot_4", "slot_241", "days_empty", "days_dup"],
)
def test_doctor_create_invalid_field_reports_field(field, value):
    with pytest.raises(ValidationError) as exc:
        DoctorCreate(**doctor_data(**{field: value}))
    assert error_of(exc)["loc"][0] == field


def test_doctor_create_extra_field_forbidden():
    with pytest.raises(ValidationError) as exc:
        DoctorCreate(**doctor_data(id=5))
    assert error_of(exc)["type"] == "extra_forbidden"


def test_patient_create_minimal(frozen_today):
    patient = make_patient()
    assert patient.user_id is None
    assert patient.phone is None


def test_patient_create_normalizes_phone_and_name(frozen_today):
    patient = make_patient(full_name="  Иван Иванов ", phone=" +79991234567", user_id=7)
    assert (patient.full_name, patient.phone, patient.user_id) == ("Иван Иванов", "+79991234567", 7)


@pytest.mark.parametrize(
    ("field", "value"),
    [("policy_number", "123"), ("phone", "89991234567"), ("user_id", 0), ("full_name", "И"), ("policy_number", None)],
    ids=["policy", "phone", "user_id", "name", "policy_none"],
)
def test_patient_create_invalid_field_reports_field(frozen_today, field, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(**{field: value})
    assert error_of(exc)["loc"] == (field,)


def appointment_data(**override):
    data = {"doctor_id": 1, "patient_id": 2, "starts_at": "2026-10-13T10:00:00+03:00", "ends_at": "2026-10-13T10:30:00+03:00"}
    data.update(override)
    return data


def test_appointment_create_converts_times_to_utc():
    appointment = AppointmentCreate(**appointment_data(reason="  Боль  "))
    assert appointment.starts_at == datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
    assert appointment.ends_at == datetime(2026, 10, 13, 7, 30, tzinfo=UTC)
    assert appointment.starts_at.utcoffset() == timedelta(0)
    assert appointment.reason == "Боль"


def test_appointment_create_reason_defaults_to_none():
    assert AppointmentCreate(**appointment_data()).reason is None


@pytest.mark.parametrize(
    "ends",
    ["2026-10-13T10:00:00+03:00", "2026-10-13T09:59:59+03:00", "2026-10-13T07:00:00Z", "2026-10-12T10:30:00+03:00"],
    ids=["equal", "one_second_earlier", "equal_other_offset", "day_earlier"],
)
def test_appointment_create_end_not_after_start_raises(ends):
    with pytest.raises(ValidationError) as exc:
        AppointmentCreate(**appointment_data(ends_at=ends))
    assert "ends_at must be later than starts_at" in error_of(exc)["msg"]


def test_appointment_create_end_one_second_after_start_ok():
    AppointmentCreate(**appointment_data(ends_at="2026-10-13T10:00:01+03:00"))


def test_appointment_create_same_instant_in_different_offsets_is_rejected():
    with pytest.raises(ValidationError):
        AppointmentCreate(**appointment_data(starts_at="2026-10-13T10:00:00+03:00", ends_at="2026-10-13T12:00:00+05:00"))


@pytest.mark.parametrize("field", ["starts_at", "ends_at"])
def test_appointment_create_naive_datetime_raises(field):
    with pytest.raises(ValidationError) as exc:
        AppointmentCreate(**appointment_data(**{field: "2026-10-13T11:00:00"}))
    assert error_of(exc)["loc"] == (field,)


@pytest.mark.parametrize("extra", ["status", "created_by", "created_at", "id"])
def test_appointment_create_server_fields_forbidden(extra):
    with pytest.raises(ValidationError) as exc:
        AppointmentCreate(**appointment_data(**{extra: "x"}))
    assert error_of(exc)["type"] == "extra_forbidden"


def test_appointment_create_empty_reason_raises():
    with pytest.raises(ValidationError) as exc:
        AppointmentCreate(**appointment_data(reason="   "))
    assert error_of(exc)["loc"] == ("reason",)


def test_prescription_create_optional_instructions():
    prescription = PrescriptionCreate(appointment_id=1, medication=" Аспирин ", dosage="1 таб.")
    assert prescription.instructions is None
    assert prescription.medication == "Аспирин"


def test_prescription_create_issued_at_forbidden():
    with pytest.raises(ValidationError) as exc:
        PrescriptionCreate(appointment_id=1, medication="A", dosage="1", issued_at="2026-10-12T00:00:00Z")
    assert error_of(exc)["type"] == "extra_forbidden"


def test_medical_record_create_normalizes_code_and_defaults():
    record = MedicalRecordCreate(patient_id=1, doctor_id=2, diagnosis_code=" j06.9 ")
    assert record.diagnosis_code == "J06.9"
    assert record.appointment_id is None
    assert record.notes is None


def test_medical_record_create_invalid_code_reports_field():
    with pytest.raises(ValidationError) as exc:
        MedicalRecordCreate(patient_id=1, doctor_id=2, diagnosis_code="J06.999")
    assert error_of(exc)["loc"] == ("diagnosis_code",)


# --------------------------------------------------------------------------- #
# PATCH-схемы: отсутствие поля / явный null
# --------------------------------------------------------------------------- #
# для каждого поля, не входящего в nullable_fields, null -> ValidationError «must not be null»;
# для nullable_fields null допустим и остаётся в model_fields_set / model_dump(exclude_unset=True).

NON_NULLABLE = [
    (UserUpdate, "email"), (UserUpdate, "password"), (UserUpdate, "full_name"), (UserUpdate, "role"), (UserUpdate, "is_active"),
    (DoctorUpdate, "specialty"), (DoctorUpdate, "cabinet"), (DoctorUpdate, "slot_minutes"), (DoctorUpdate, "work_start"),
    (DoctorUpdate, "work_end"), (DoctorUpdate, "work_days"),
    (PatientUpdate, "full_name"), (PatientUpdate, "birth_date"), (PatientUpdate, "policy_number"),
    (AppointmentUpdate, "starts_at"), (AppointmentUpdate, "ends_at"), (AppointmentUpdate, "status"),
    (PrescriptionUpdate, "medication"), (PrescriptionUpdate, "dosage"),
    (MedicalRecordUpdate, "diagnosis_code"),
]
NULLABLE = [
    (PatientUpdate, "user_id"), (PatientUpdate, "phone"), (AppointmentUpdate, "reason"),
    (PrescriptionUpdate, "instructions"), (MedicalRecordUpdate, "notes"),
]


@pytest.mark.parametrize(("model", "field"), NON_NULLABLE, ids=lambda x: x if isinstance(x, str) else x.__name__)
def test_update_explicit_null_for_non_nullable_field_raises(frozen_today, model, field):
    with pytest.raises(ValidationError) as exc:
        model(**{field: None})
    assert f"field '{field}' must not be null" in error_of(exc)["msg"]


@pytest.mark.parametrize(("model", "field"), NULLABLE, ids=lambda x: x if isinstance(x, str) else x.__name__)
def test_update_explicit_null_for_nullable_field_is_kept(model, field):
    update = model(**{field: None})
    assert field in update.model_fields_set
    assert update.model_dump(exclude_unset=True) == {field: None}


@pytest.mark.parametrize("model", [UserUpdate, DoctorUpdate, PatientUpdate, AppointmentUpdate, PrescriptionUpdate, MedicalRecordUpdate],
                         ids=lambda m: m.__name__)
def test_update_empty_body_changes_nothing(model):
    update = model()
    assert update.model_fields_set == set()
    assert update.model_dump(exclude_unset=True) == {}


def test_update_dump_exclude_unset_contains_only_passed_fields():
    update = UserUpdate(full_name="  Новое Имя ", is_active=False)
    assert update.model_dump(exclude_unset=True) == {"full_name": "Новое Имя", "is_active": False}


def test_update_false_value_is_not_treated_as_null():
    assert UserUpdate(is_active=False).is_active is False


def test_update_one_null_among_valid_fields_still_raises(frozen_today):
    with pytest.raises(ValidationError):
        PatientUpdate(full_name="Иван Иванов", policy_number=None)


def test_update_extra_field_forbidden():
    with pytest.raises(ValidationError) as exc:
        UserUpdate(token_version=5)
    assert error_of(exc)["type"] == "extra_forbidden"


def test_user_update_normalizes_email_and_validates_password():
    assert UserUpdate(email=" X@Y.CO ").email == "x@y.co"
    with pytest.raises(ValidationError):
        UserUpdate(password="short")
    with pytest.raises(ValidationError):
        UserUpdate(password="a" * 73)


def test_patient_update_other_validators_apply(frozen_today):
    assert PatientUpdate(phone=" +79991234567 ").phone == "+79991234567"
    with pytest.raises(ValidationError):
        PatientUpdate(phone="123")
    with pytest.raises(ValidationError):
        PatientUpdate(birth_date=TODAY + timedelta(days=2))
    with pytest.raises(ValidationError):
        PatientUpdate(policy_number="1")


def test_doctor_update_single_shift_bound_is_accepted():
    assert DoctorUpdate(work_start="08:00").work_end is None
    assert DoctorUpdate(work_end="20:00").work_start is None


@pytest.mark.parametrize("start,end", [("09:00", "09:00"), ("10:00", "09:00")], ids=["equal", "reversed"])
def test_doctor_update_both_bounds_reversed_raises(start, end):
    with pytest.raises(ValidationError) as exc:
        DoctorUpdate(work_start=start, work_end=end)
    assert "work_start must be earlier than work_end" in error_of(exc)["msg"]


def test_doctor_update_one_minute_shift_ok_and_work_days_sorted():
    update = DoctorUpdate(work_start="09:00", work_end="09:01", work_days=[3, 1])
    assert update.work_days == [1, 3]


def test_appointment_update_interval_checks():
    update = AppointmentUpdate(starts_at="2026-10-13T10:00:00+03:00", ends_at="2026-10-13T10:00:01+03:00")
    assert update.starts_at == datetime(2026, 10, 13, 7, tzinfo=UTC)
    with pytest.raises(ValidationError):
        AppointmentUpdate(starts_at="2026-10-13T10:00:00+03:00", ends_at="2026-10-13T10:00:00+03:00")
    with pytest.raises(ValidationError):
        AppointmentUpdate(starts_at="2026-10-13T10:00:00+03:00", ends_at="2026-10-13T09:00:00+03:00")


def test_appointment_update_single_bound_and_status():
    update = AppointmentUpdate(ends_at="2026-10-13T10:00:00Z", status="cancelled")
    assert update.starts_at is None
    assert update.status is AppointmentStatus.CANCELLED


def test_appointment_update_invalid_status_raises():
    with pytest.raises(ValidationError) as exc:
        AppointmentUpdate(status="done")
    assert error_of(exc)["loc"] == ("status",)


def test_appointment_update_reason_null_then_value():
    assert AppointmentUpdate(reason=None).reason is None
    assert AppointmentUpdate(reason=" ok ").reason == "ok"
    with pytest.raises(ValidationError):
        AppointmentUpdate(reason="")


def test_medical_record_update_normalizes_code_and_allows_notes_null():
    update = MedicalRecordUpdate(diagnosis_code="i10", notes=None)
    assert update.model_dump(exclude_unset=True) == {"diagnosis_code": "I10", "notes": None}


# --------------------------------------------------------------------------- #
# Out-схемы
# --------------------------------------------------------------------------- #


def test_user_out_has_no_password_hash_and_converts_created_at_to_utc():
    orm_like = SimpleNamespace(
        id=1, email="a@b.co", full_name="Ан", role=UserRole.ADMIN, is_active=True,
        created_at=datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=3))), password_hash="$2b$12$secret",
    )
    out = UserOut.model_validate(orm_like)
    dumped = out.model_dump()
    assert "password_hash" not in dumped
    assert "secret" not in out.model_dump_json()
    assert out.created_at == datetime(2026, 10, 12, 9, tzinfo=UTC)
    assert out.created_at.utcoffset() == timedelta(0)


def test_user_out_naive_created_at_raises():
    orm_like = SimpleNamespace(id=1, email="a@b.co", full_name="Ан", role=UserRole.ADMIN, is_active=True, created_at=datetime(2026, 10, 12))
    with pytest.raises(ValidationError):
        UserOut.model_validate(orm_like)


def test_token_out_default_type_and_literal():
    assert TokenOut(access_token="t", expires_in=3600).token_type == "bearer"
    with pytest.raises(ValidationError):
        TokenOut(access_token="t", expires_in=3600, token_type="basic")
```

### tests/test_gen_config.py

```python
"""Unit-тесты app/config.py: Settings и его валидаторы, get_settings (без HTTP и БД)."""

from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.config import ALLOWED_DB_DRIVERS, JWT_SECRET_MIN_LENGTH, Settings, get_settings

GOOD_URL = "sqlite+aiosqlite:///test.db"
GOOD_SECRET = "s" * 32


def make(**override):
    """Settings только из переданных значений: .env не читается."""
    data = {"database_url": GOOD_URL, "jwt_secret": GOOD_SECRET}
    data.update(override)
    return Settings(_env_file=None, **data)


def fields_of(exc_info) -> list[str]:
    return [e["loc"][0] for e in exc_info.value.errors()]


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("DATABASE_URL", "JWT_SECRET", "JWT_TTL_MINUTES", "CLINIC_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def fresh_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# database_url
# --------------------------------------------------------------------------- #
# допустимо: postgresql+asyncpg, sqlite+aiosqlite; недопустимо: синхронные драйверы, другие СУБД, не URL, пусто


@pytest.mark.parametrize(
    "url",
    ["postgresql+asyncpg://user:pw@localhost:5432/clinic", "sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///C:/tmp/x.db",
     "postgresql+asyncpg://localhost/db"],
    ids=["postgres", "sqlite_memory", "sqlite_file", "postgres_no_credentials"],
)
def test_database_url_async_driver_accepted(url):
    assert make(database_url=url).database_url.get_secret_value() == url


@pytest.mark.parametrize(
    "url",
    ["postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db", "postgresql+psycopg://u:p@h/db", "sqlite://",
     "sqlite:///x.db", "mysql+aiomysql://u:p@h/db", "not a url", "", "postgresql+asyncpg", "http://example.com"],
    ids=["plain_postgres", "psycopg2", "psycopg3_sync", "sqlite_sync_memory", "sqlite_sync_file", "mysql", "garbage",
         "empty", "no_separator", "http"],
)
def test_database_url_invalid_or_sync_driver_raises(url):
    with pytest.raises(ValidationError) as exc:
        make(database_url=url)
    assert fields_of(exc) == ["database_url"]


def test_database_url_error_message_for_sync_driver():
    with pytest.raises(ValidationError) as exc:
        make(database_url="postgresql://u:p@h/db")
    assert "must use an async driver" in exc.value.errors()[0]["msg"]


def test_database_url_error_message_for_malformed_url():
    with pytest.raises(ValidationError) as exc:
        make(database_url="not a url")
    assert "not a valid SQLAlchemy URL" in exc.value.errors()[0]["msg"]


def test_database_url_password_not_in_error_text():
    with pytest.raises(ValidationError) as exc:
        make(database_url="postgresql://user:SuperSecretPw@host/db")
    assert "SuperSecretPw" not in str(exc.value)


def test_allowed_drivers_constant():
    assert ALLOWED_DB_DRIVERS == {"postgresql+asyncpg", "sqlite+aiosqlite"}


# --------------------------------------------------------------------------- #
# jwt_secret: длина >= 32
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("secret", ["s" * 32, "s" * 33, "s" * 200, "ключ" * 8], ids=["32", "33", "200", "cyrillic_32_chars"])
def test_jwt_secret_long_enough_accepted(secret):
    assert make(jwt_secret=secret).jwt_secret.get_secret_value() == secret


@pytest.mark.parametrize("secret", ["", "s", "s" * 31], ids=["empty", "one", "31"])
def test_jwt_secret_too_short_raises(secret):
    with pytest.raises(ValidationError) as exc:
        make(jwt_secret=secret)
    assert fields_of(exc) == ["jwt_secret"]
    assert f"at least {JWT_SECRET_MIN_LENGTH} characters" in exc.value.errors()[0]["msg"]


def test_jwt_secret_value_not_in_error_text_or_repr():
    weak = "weak-secret-value"
    with pytest.raises(ValidationError) as exc:
        make(jwt_secret=weak)
    assert weak not in str(exc.value)
    settings = make()
    assert GOOD_SECRET not in repr(settings)
    assert GOOD_SECRET not in str(settings)


# --------------------------------------------------------------------------- #
# jwt_ttl_minutes: 1..1440, по умолчанию 60
# --------------------------------------------------------------------------- #


def test_jwt_ttl_default_is_60(clean_env):
    assert make().jwt_ttl_minutes == 60


@pytest.mark.parametrize("ttl", [1, 2, 60, 1439, 1440])
def test_jwt_ttl_in_range_accepted(ttl):
    assert make(jwt_ttl_minutes=ttl).jwt_ttl_minutes == ttl


@pytest.mark.parametrize("ttl", [0, -1, 1441, 100_000, "abc", "60.5", None], ids=["0", "-1", "1441", "huge", "text", "float_text", "none"])
def test_jwt_ttl_out_of_range_or_invalid_raises(ttl):
    with pytest.raises(ValidationError) as exc:
        make(jwt_ttl_minutes=ttl)
    assert fields_of(exc) == ["jwt_ttl_minutes"]


# --------------------------------------------------------------------------- #
# clinic_timezone
# --------------------------------------------------------------------------- #


def test_clinic_timezone_default_is_moscow(clean_env):
    settings = make()
    assert settings.clinic_timezone == "Europe/Moscow"
    assert settings.clinic_tz == ZoneInfo("Europe/Moscow")


@pytest.mark.parametrize("name", ["Europe/Moscow", "UTC", "Asia/Kolkata", "America/New_York", "Europe/Berlin", "Asia/Vladivostok"])
def test_clinic_timezone_known_zone_accepted(name):
    settings = make(clinic_timezone=name)
    assert settings.clinic_timezone == name
    assert settings.clinic_tz.key == name


@pytest.mark.parametrize(
    "name",
    ["Mars/Phobos", "", "../etc/passwd", "/etc/localtime", "Europe/Moscow\n", "Moscow", "Europe/Moskva", " "],
    ids=["unknown", "empty", "path_traversal", "absolute_path", "trailing_newline", "city_only", "typo", "blank"],
)
def test_clinic_timezone_unknown_raises(name):
    with pytest.raises(ValidationError) as exc:
        make(clinic_timezone=name)
    assert fields_of(exc) == ["clinic_timezone"]
    assert "unknown IANA time zone" in exc.value.errors()[0]["msg"]


# --------------------------------------------------------------------------- #
# обязательные поля, окружение, .env, extra
# --------------------------------------------------------------------------- #


def test_missing_database_url_raises(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, jwt_secret=GOOD_SECRET)
    assert fields_of(exc) == ["database_url"]
    assert exc.value.errors()[0]["type"] == "missing"


def test_missing_jwt_secret_raises(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url=GOOD_URL)
    assert fields_of(exc) == ["jwt_secret"]
    assert exc.value.errors()[0]["type"] == "missing"


def test_missing_both_required_fields_reports_both(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert sorted(fields_of(exc)) == ["database_url", "jwt_secret"]


def test_all_errors_reported_together(clean_env):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url="mysql://x", jwt_secret="x", jwt_ttl_minutes=0, clinic_timezone="Nowhere/Land")
    assert sorted(fields_of(exc)) == ["clinic_timezone", "database_url", "jwt_secret", "jwt_ttl_minutes"]


def test_values_are_read_from_environment(clean_env):
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    clean_env.setenv("JWT_SECRET", "e" * 40)
    clean_env.setenv("JWT_TTL_MINUTES", "15")
    clean_env.setenv("CLINIC_TIMEZONE", "Asia/Kolkata")
    settings = Settings(_env_file=None)
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://u:p@h/db"
    assert settings.jwt_secret.get_secret_value() == "e" * 40
    assert settings.jwt_ttl_minutes == 15
    assert settings.clinic_timezone == "Asia/Kolkata"


def test_environment_value_is_validated(clean_env):
    clean_env.setenv("DATABASE_URL", GOOD_URL)
    clean_env.setenv("JWT_SECRET", "short")
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert fields_of(exc) == ["jwt_secret"]


def test_values_are_read_from_env_file(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={GOOD_URL}\nJWT_SECRET={'f' * 36}\nJWT_TTL_MINUTES=30\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.jwt_secret.get_secret_value() == "f" * 36
    assert settings.jwt_ttl_minutes == 30


def test_environment_overrides_env_file(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={GOOD_URL}\nJWT_SECRET={'f' * 36}\nJWT_TTL_MINUTES=30\n", encoding="utf-8")
    clean_env.setenv("JWT_TTL_MINUTES", "45")
    assert Settings(_env_file=env_file).jwt_ttl_minutes == 45


def test_explicit_arguments_override_environment(clean_env):
    clean_env.setenv("JWT_TTL_MINUTES", "45")
    assert make(jwt_ttl_minutes=10).jwt_ttl_minutes == 10


def test_unknown_settings_are_ignored(clean_env):
    clean_env.setenv("SOMETHING_ELSE", "1")
    assert make(unexpected_option="x").jwt_ttl_minutes == 60


def test_clinic_tz_returns_zoneinfo_object():
    tz = make(clinic_timezone="Europe/Berlin").clinic_tz
    assert isinstance(tz, ZoneInfo)
    assert tz.key == "Europe/Berlin"


# --------------------------------------------------------------------------- #
# get_settings
# --------------------------------------------------------------------------- #


def test_get_settings_is_cached(fresh_cache):
    assert get_settings() is get_settings()


def test_get_settings_cache_clear_picks_up_new_environment(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_TTL_MINUTES", "11")
    get_settings.cache_clear()
    assert get_settings().jwt_ttl_minutes == 11
    monkeypatch.setenv("JWT_TTL_MINUTES", "12")
    assert get_settings().jwt_ttl_minutes == 11  # из кеша
    get_settings.cache_clear()
    assert get_settings().jwt_ttl_minutes == 12


def test_get_settings_invalid_environment_raises(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValidationError) as exc:
        get_settings()
    assert fields_of(exc) == ["jwt_secret"]


def test_get_settings_failure_is_not_cached(fresh_cache, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "too-short")
    with pytest.raises(ValidationError):
        get_settings()
    monkeypatch.setenv("JWT_SECRET", "z" * 40)
    assert get_settings().jwt_secret.get_secret_value() == "z" * 40
```

### tests/test_gen_security.py

```python
"""Unit-тесты app/security.py без HTTP: пароли (bcrypt), JWT, require_roles."""

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from app import security
from app.config import get_settings
from app.models import UserRole
from app.security import (
    JWT_ALGORITHM,
    TokenError,
    TokenPayload,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_password_async,
    require_roles,
    verify_password,
    verify_password_async,
)

SECRET = get_settings().jwt_secret.get_secret_value()


@pytest.fixture(scope="module")
def pw_hash() -> str:
    """Один дорогой bcrypt-хэш на модуль для пароля 'Password123'."""
    return hash_password("Password123")


def exp_in(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def claims(**override):
    data = {"sub": "5", "role": "doctor", "ver": 0, "exp": exp_in()}
    data.update(override)
    return {k: v for k, v in data.items() if v is not ...}


def forge(payload: dict, key: str = SECRET, alg: str = JWT_ALGORITHM) -> str:
    return jwt.encode(payload, key, algorithm=alg)


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# --------------------------------------------------------------------------- #
# hash_password / verify_password
# --------------------------------------------------------------------------- #
# длина в байтах UTF-8: 72 -> ok, 73 -> ValueError; пустой пароль; юникод;
# verify: верный / неверный / регистр / пробел / битый хэш / не-ASCII хэш / пароль > 72 байт.


def test_hash_password_format(pw_hash):
    assert pw_hash.startswith("$2b$12$")
    assert len(pw_hash) == 60
    assert pw_hash.isascii()


def test_hash_password_is_salted(pw_hash):
    assert hash_password("Password123") != pw_hash


def test_verify_password_correct(pw_hash):
    assert verify_password("Password123", pw_hash) is True


@pytest.mark.parametrize("wrong", ["password123", "Password123 ", " Password123", "Password124", "", "Password12", "Password1234"],
                         ids=["case", "trailing_space", "leading_space", "last_char", "empty", "prefix", "extra_char"])
def test_verify_password_wrong_password_false(pw_hash, wrong):
    assert verify_password(wrong, pw_hash) is False


@pytest.mark.parametrize(
    "password",
    ["a" * 72, "я" * 36, "😀" * 18, "пароль-Ж-123", ""],
    ids=["72_ascii_bytes", "72_cyrillic_bytes", "72_emoji_bytes", "unicode", "empty"],
)
def test_hash_password_roundtrip_including_limit_and_unicode(password):
    hashed = hash_password(password)
    assert verify_password(password, hashed) is True
    if len(password.encode()) < 72:
        assert verify_password(password + "x", hashed) is False


@pytest.mark.parametrize("password", ["a" * 73, "я" * 37, "a" * 71 + "я", "😀" * 19, "a" * 1000],
                         ids=["73_bytes", "74_bytes_cyrillic", "73_bytes_mixed", "76_bytes_emoji", "1000"])
def test_hash_password_over_72_bytes_raises(password):
    with pytest.raises(ValueError, match="72 bytes"):
        hash_password(password)


def test_verify_password_differs_in_last_byte_of_72_bytes():
    hashed = hash_password("a" * 72)
    assert verify_password("a" * 71 + "b", hashed) is False


def test_verify_password_over_72_bytes_is_false_not_truncated_match():
    hashed = hash_password("a" * 72)
    assert verify_password("a" * 73, hashed) is False
    assert verify_password("a" * 500, hashed) is False


@pytest.mark.parametrize(
    "bad_hash",
    ["", "not-a-hash", "$2b$12$short", "$2b$12$" + "x" * 53, "$1$salt$abcdefghijklmnopqrstuv", "хэш", "$2b$99$" + "a" * 53],
    ids=["empty", "garbage", "truncated", "invalid_chars_body", "md5_crypt", "non_ascii", "bad_rounds"],
)
def test_verify_password_malformed_hash_returns_false(bad_hash):
    assert verify_password("Password123", bad_hash) is False


def test_verify_password_does_not_accept_hash_as_password(pw_hash):
    assert verify_password(pw_hash, pw_hash) is False


async def test_async_wrappers_roundtrip(pw_hash):
    hashed = await hash_password_async("Password123")
    assert hashed.startswith("$2b$12$")
    assert await verify_password_async("Password123", hashed) is True
    assert await verify_password_async("wrong", hashed) is False
    assert await verify_password_async("Password123", pw_hash) is True


async def test_hash_password_async_over_limit_raises():
    with pytest.raises(ValueError, match="72 bytes"):
        await hash_password_async("a" * 73)


async def test_verify_password_async_malformed_hash_false():
    assert await verify_password_async("x", "broken") is False


# --------------------------------------------------------------------------- #
# create_access_token / decode_access_token: корректные токены
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_token_roundtrip_for_every_role(role):
    payload = decode_access_token(create_access_token(42, role, version=3))
    assert isinstance(payload, TokenPayload)
    assert (payload.user_id, payload.role, payload.version) == (42, role, 3)


@pytest.mark.parametrize(("user_id", "version"), [(1, 0), (2_147_483_647, 0), (1, 1), (7, 2_147_483_647)])
def test_token_roundtrip_boundary_ids_and_versions(user_id, version):
    payload = decode_access_token(create_access_token(user_id, UserRole.ADMIN, version=version))
    assert (payload.user_id, payload.version) == (user_id, version)


def test_token_raw_claims_and_header():
    token = create_access_token(42, UserRole.DOCTOR, version=3)
    raw = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
    assert raw["sub"] == "42"
    assert raw["role"] == "doctor"
    assert raw["ver"] == 3
    assert isinstance(raw["exp"], int)
    assert set(raw) == {"sub", "role", "ver", "exp"}
    assert jwt.get_unverified_header(token)["alg"] == "HS256"


def test_token_default_ttl_comes_from_settings():
    before = datetime.now(UTC)
    payload = decode_access_token(create_access_token(1, UserRole.PATIENT, version=0))
    expected = before + timedelta(minutes=get_settings().jwt_ttl_minutes)
    assert expected - timedelta(seconds=2) <= payload.expires_at <= expected + timedelta(seconds=2)
    assert payload.expires_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("delta", [timedelta(seconds=30), timedelta(days=7)], ids=["30s", "7d"])
def test_token_custom_ttl_is_applied(delta):
    before = datetime.now(UTC)
    payload = decode_access_token(create_access_token(1, UserRole.PATIENT, version=0, expires_delta=delta))
    assert before + delta - timedelta(seconds=2) <= payload.expires_at <= before + delta + timedelta(seconds=2)


def test_token_zero_ttl_is_not_replaced_by_default():
    # timedelta(0) - ложное значение, но это явный срок: токен сразу просрочен
    with pytest.raises(TokenError):
        decode_access_token(create_access_token(1, UserRole.PATIENT, version=0, expires_delta=timedelta(0)))


@pytest.mark.parametrize("delta", [timedelta(seconds=-1), timedelta(seconds=-3600), timedelta(days=-365)], ids=["1s", "1h", "1y"])
def test_expired_token_raises(delta):
    token = create_access_token(1, UserRole.ADMIN, version=0, expires_delta=delta)
    with pytest.raises(TokenError, match="invalid token"):
        decode_access_token(token)


def test_token_valid_for_next_minute_and_extra_claims_are_ignored():
    token = forge(claims(exp=exp_in(60), extra="x", iat=int(datetime.now(UTC).timestamp()) - 5))
    assert decode_access_token(token).user_id == 5


def test_token_error_has_cause_from_library():
    with pytest.raises(TokenError) as exc:
        decode_access_token(create_access_token(1, UserRole.ADMIN, version=0, expires_delta=timedelta(seconds=-1)))
    assert isinstance(exc.value.__cause__, jwt.ExpiredSignatureError)


# --------------------------------------------------------------------------- #
# decode_access_token: подделки и некорректные токены
# --------------------------------------------------------------------------- #


def test_token_signed_with_other_key_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), key="another-secret-0123456789abcdef0123"))


def test_token_signed_with_key_differing_in_one_char_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), key=SECRET[:-1] + ("x" if SECRET[-1] != "x" else "y")))


def test_token_alg_none_rejected():
    with pytest.raises(TokenError):
        decode_access_token(jwt.encode(claims(), None, algorithm="none"))


def test_token_alg_none_handmade_rejected():
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'role': 'admin', 'ver': 0, 'exp': 4102444800})}."
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_token_with_stripped_signature_rejected():
    token = create_access_token(1, UserRole.ADMIN, version=0)
    with pytest.raises(TokenError):
        decode_access_token(token.rsplit(".", 1)[0] + ".")
    with pytest.raises(TokenError):
        decode_access_token(token.rsplit(".", 1)[0])


@pytest.mark.parametrize("alg", ["HS384", "HS512"])
def test_token_other_hmac_algorithm_with_correct_key_rejected(alg):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(), alg=alg))


def test_token_with_tampered_payload_rejected():
    token = create_access_token(5, UserRole.PATIENT, version=0)
    header, _, signature = token.split(".")
    evil = b64({"sub": "5", "role": "admin", "ver": 0, "exp": 4102444800})
    with pytest.raises(TokenError):
        decode_access_token(f"{header}.{evil}.{signature}")


def test_token_with_flipped_signature_character_rejected():
    token = create_access_token(5, UserRole.PATIENT, version=0)
    head, signature = token[:-4], token[-4:]
    flipped = head + ("AAAA" if signature != "AAAA" else "BBBB")
    with pytest.raises(TokenError):
        decode_access_token(flipped)


@pytest.mark.parametrize("missing", ["sub", "role", "ver", "exp"])
def test_token_without_required_claim_rejected(missing):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(**{missing: ...})))


@pytest.mark.parametrize("sub", ["abc", "", "1.5", "5x", " ", "null"], ids=["text", "empty", "float", "suffix", "blank", "null_str"])
def test_token_with_non_numeric_sub_rejected(sub):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(sub=sub)))


@pytest.mark.parametrize("sub", [5, None, ["5"]], ids=["int", "none", "list"])
def test_token_with_non_string_sub_rejected(sub):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(sub=sub)))


@pytest.mark.parametrize("role", ["superuser", "ADMIN", "", None, 5, ["admin"]], ids=["unknown", "uppercase", "empty", "none", "int", "list"])
def test_token_with_invalid_role_rejected(role):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(role=role)))


@pytest.mark.parametrize("ver", ["1", "0", 1.0, 1.5, True, False, None, [1], {"v": 1}],
                         ids=["str_1", "str_0", "float_1", "float_1.5", "true", "false", "none", "list", "dict"])
def test_token_with_non_int_ver_rejected(ver):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(ver=ver)))


@pytest.mark.parametrize("exp", ["tomorrow", "9999999999", None, [1], True], ids=["text", "numeric_str", "none", "list", "bool"])
def test_token_with_invalid_exp_rejected(exp):
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(exp=exp)))


def test_token_not_yet_valid_nbf_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(nbf=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()))))


def test_token_with_unexpected_audience_rejected():
    with pytest.raises(TokenError):
        decode_access_token(forge(claims(aud="someone-else")))


@pytest.mark.parametrize(
    "token",
    ["", " ", "abc", "a.b", "a.b.c", "...", "Bearer x.y.z", "eyJhbGciOiJIUzI1NiJ9..", "\x00", "тест.тест.тест"],
    ids=["empty", "blank", "one_part", "two_parts", "garbage_three_parts", "dots_only", "bearer_prefix", "empty_payload", "nul", "cyrillic"],
)
def test_garbage_token_rejected(token):
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_token_with_surrounding_whitespace_rejected():
    token = create_access_token(1, UserRole.ADMIN, version=0)
    with pytest.raises(TokenError):
        decode_access_token(f" {token}")


def test_token_error_is_not_leaking_library_exceptions():
    for token in ("", "a.b.c", forge(claims(ver="1"))):
        with pytest.raises(TokenError):
            decode_access_token(token)


def test_module_constants():
    assert JWT_ALGORITHM == "HS256"
    assert security.BCRYPT_ROUNDS == 12


# --------------------------------------------------------------------------- #
# require_roles (без HTTP: зависимость вызывается напрямую)
# --------------------------------------------------------------------------- #


def test_require_roles_without_roles_raises():
    with pytest.raises(ValueError, match="at least one role"):
        require_roles()


@pytest.mark.parametrize(
    ("allowed", "actual"),
    [((UserRole.ADMIN,), UserRole.ADMIN), ((UserRole.ADMIN, UserRole.REGISTRAR), UserRole.REGISTRAR),
     ((UserRole.DOCTOR, UserRole.PATIENT), UserRole.PATIENT), (tuple(UserRole), UserRole.DOCTOR)],
    ids=["single", "second_of_two", "patient", "all_roles"],
)
async def test_require_roles_allows_listed_role(allowed, actual):
    user = SimpleNamespace(role=actual)
    assert await require_roles(*allowed)(user=user) is user


@pytest.mark.parametrize(
    ("allowed", "actual"),
    [((UserRole.DOCTOR,), UserRole.ADMIN), ((UserRole.ADMIN,), UserRole.REGISTRAR), ((UserRole.ADMIN, UserRole.REGISTRAR), UserRole.PATIENT),
     ((UserRole.PATIENT,), UserRole.DOCTOR)],
    ids=["admin_not_implicitly_allowed", "registrar_vs_admin", "patient_vs_staff", "doctor_vs_patient"],
)
async def test_require_roles_rejects_other_role_with_403(allowed, actual):
    with pytest.raises(HTTPException) as exc:
        await require_roles(*allowed)(user=SimpleNamespace(role=actual))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Insufficient permissions"
```

## Что покрыто

| Файл / функция | Сценарии |
|---|---|
| scheduling.Schedule | slot_minutes -1/0/1; start >/==/< end (на 1 мин); work_days {0},{8},{1,8},{-1}, {1},{7}, пусто; неизменяемость |
| validate_booking_window | now-1с, now, +1с, +90 сут (включительно), +90 сут+1с; свой лимит 1 и 0; другие пояса входа; граница года; naive start/now |
| validate_within_shift | начало/конец смены, на минуту/30 минут за границей, пустой и перевёрнутый интервал, переход через полночь (в т.ч. ровно до 00:00), сб/вс, день недели по местному времени (21:30Z), пояс Владивостока, naive |
| validate_slot_grid | начало вне сетки (в т.ч. +1 с, до work_start), длительность 0/отрицательная/29/31/45/150/121 мин, 1-4 слота, шаг 20 мин, work_start 09:10, местное время, naive |
| intervals_overlap | равные, касание с обеих сторон, пересечение на 1 с, вложенные, разделённые, нулевой/перевёрнутый интервал, разные смещения |
| local_day_bounds | МСК, конец месяца, года, 29 февраля, Берлин 23/25 часов, Нью-Йорк, Калькутта (+5:30), UTC, date.min/date.max (OverflowError) |
| free_slots | now около начала смены и внутри дня, слот ровно в now, выходные, прошлый день, пустой work_days, занятость (касание, 1 с, многослотовая, неупорядоченная, генератор, вне смены), неполный последний слот, смена из одного слота, окно 90 сут (граница с двух сторон), свой max_days_ahead, пояс Владивостока, слоты около местной полуночи, конец месяца/года, naive now |
| analytics.make_period | перевёрнутый период, 365/366/367 дней, конец месяца/года, високосный февраль, DST-пояс, OverflowError на date.max, неизменяемость |
| analytics.available_minutes | 16 явных таблиц + сверка с посуточным подсчётом (7 стартовых дней x 14 длин), неполные недели, пустой график, смена в 1 минуту, високосный год |
| analytics._utc_offset_segments | МСК (1 отрезок), осенний и весенний переход, оба перехода (3 отрезка), конец периода в день перехода, отрицательное и дробное смещение |
| analytics._ratio | знаменатель 0, 0/n, n/n, округление до 4 знаков, значения больше 1 |
| analytics._as_date | date, datetime (naive/aware, возвращает именно date), ISO-строка, мусор/None/29.02 невисокосного |
| schemas: Email | strip+lower, длина 6/5/254/255, метки 63/64, TLD 63/64, 26 некорректных форм, не-строки |
| schemas: Password / LoginPassword | 7/8 символов, 72/73 байта (ASCII, кириллица, эмодзи, смесь), отсутствие утечки пароля в ошибке; вход 1/0/1024/1025 |
| schemas: PolicyNumber, PhoneE164, Icd10Code | 15/16/17 цифр, юникодные цифры; 6/7/15/16 цифр, +0, пробелы; регистр, подкоды 0/1/2/3 цифры, кириллица |
| schemas: строковые типы | границы min/max для 8 типов, strip до проверки длины, None и не-строки |
| schemas: EntityId, SlotMinutes, WorkDays, UTCDatetime, ClinicTime | границы int32, 4/5/240/241, дубли и сортировка, перевод в UTC около полуночи и конца года, naive, переполнение у границ datetime, время со смещением |
| schemas: BirthDate | сегодня, завтра, послезавтра, 1896 и 1895, 29 февраля, невалидные строки (с подменой «сегодня») |
| schemas: Create/Update/Out | нормализация, extra=forbid (серверные поля), обязательные поля, смена графика, явный null по каждому полю (20 не-nullable, 5 nullable), пустое тело PATCH, model_dump(exclude_unset), password_hash не попадает в UserOut |
| config.Settings / get_settings | драйверы БД, длина секрета 31/32, TTL 0/1/1440/1441, часовые пояса, обязательные поля, окружение/.env/приоритеты, отсутствие секретов в ошибках и repr, кеш |
| security: пароли | формат хэша, соль, 72/73 байта, юникод, битые хэши, пароль > 72 байт даёт False, async-обёртки |
| security: JWT | roundtrip для всех ролей, TTL, просрочка, чужой ключ, alg=none, HS384/512, подмена payload/подписи, отсутствие sub/role/ver/exp, нечисловой sub, неверная роль, ver не int (str/float/bool/None/list), exp, nbf, aud, мусор |
| security.require_roles | пустой список, разрешённые роли, 403 для чужой (admin не неявно) |

## Найденные ошибки приложения

Ошибок, на которые нужен xfail, не обнаружено: все тесты на правильное поведение проходят. Наблюдения (не ошибки, зафиксированы тестами как текущее поведение):

- `local_day_bounds` / `make_period` на крайних датах (`date.max`, `date.min`) бросают `OverflowError`; это задокументировано, защита должна быть на уровне роутов.
- `decode_access_token` принимает `sub` вида `" 7 "` (`int()` терпим к пробелам); подпись защищает, практического риска нет.
- Email-regex не ограничивает длину локальной части 64 символами (RFC 5321), только общую длину 254.
- Тесты `validate_slot_grid` не покрывают переходы на летнее время: модуль явно их не поддерживает.
