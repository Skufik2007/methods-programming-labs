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
    DoctorCreate,
    DoctorUpdate,
    Dosage,
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
        (
            "a" * (254 - len(_LONG_DOMAIN) - 1) + "@" + _LONG_DOMAIN,
            "a" * (254 - len(_LONG_DOMAIN) - 1) + "@" + _LONG_DOMAIN,
        ),
        ("a@" + "x" * 63 + ".com", "a@" + "x" * 63 + ".com"),
        ("a@example." + "c" * 63, "a@example." + "c" * 63),
        ("1@2.io", "1@2.io"),
    ],
    ids=[
        "plain",
        "strip_and_lower",
        "whitespace_chars_and_subdomain",
        "special_local_chars",
        "min_length_6",
        "max_length_254",
        "label_63",
        "tld_63",
        "digits",
    ],
)
def test_email_valid_is_normalized(raw, expected):
    assert check(Email, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "plainaddress",
        "a@b",
        "a@@b.com",
        "@b.com",
        "a@.com",
        "a b@example.com",
        "a@exa mple.com",
        ".a@example.com",
        "a.@example.com",
        "a..b@example.com",
        "a@-b.com",
        "a@b-.com",
        "a@b.c",
        "a@b.c0m",
        "a@example.123",
        "a@exa_mple.com",
        "тест@example.com",
        "a@пример.рф",
        "a@example.com,b@example.com",
        "a@" + "x" * 64 + ".com",
        "a@example." + "c" * 64,
        "a" * 244 + "@example.com",
        "a@b.com" + "­",
        "a@example.com\x00",
    ],
    ids=[
        "empty",
        "blank",
        "no_at",
        "no_tld",
        "double_at",
        "no_local",
        "empty_label",
        "space_local",
        "space_domain",
        "leading_dot",
        "trailing_dot",
        "double_dot",
        "label_starts_dash",
        "label_ends_dash",
        "tld_1_char",
        "tld_with_digit",
        "numeric_tld",
        "underscore_domain",
        "cyrillic_local",
        "cyrillic_domain",
        "two_addresses",
        "label_64",
        "tld_64",
        "length_255",
        "soft_hyphen",
        "nul_byte",
    ],
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
    ids=[
        "8_chars",
        "typical",
        "72_bytes_ascii",
        "72_bytes_cyrillic",
        "8_cyrillic_chars",
        "71_ascii",
        "72_bytes_emoji",
        "mixed_cyr_ascii",
    ],
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
    ids=[
        "empty",
        "7_chars",
        "7_cyrillic",
        "73_bytes_ascii",
        "74_bytes_cyrillic",
        "73_bytes_mixed",
        "76_bytes_emoji",
        "1000",
    ],
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


@pytest.mark.parametrize(
    "raw",
    ["a", "x" * 72, "x" * 73, "я" * 100, "x" * 1024],
    ids=["1", "72", "73_bytes_ok_for_login", "cyrillic", "1024"],
)
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
    [
        ("1234567890123456", "1234567890123456"),
        ("0000000000000000", "0000000000000000"),
        ("  1234567890123456\n", "1234567890123456"),
    ],
    ids=["regular", "leading_zeros_kept", "stripped"],
)
def test_policy_number_valid(raw, expected):
    assert check(PolicyNumber, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "123456789012345",
        "12345678901234567",
        "123456789012345a",
        "1234 5678 9012 3456",
        "1234-5678-9012-3456",
        "١٢٣٤٥٦٧٨٩٠١٢٣٤٥٦",
        "１２３４５６７８９０１２３４５６",
        "12345678901234.6",
        "+234567890123456",
        "1234567890123456\n1",
    ],
    ids=[
        "empty",
        "15_digits",
        "17_digits",
        "letter",
        "spaces",
        "dashes",
        "arabic_indic_digits",
        "fullwidth_digits",
        "dot",
        "plus",
        "newline_inside",
    ],
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
    [
        ("+79991234567", "+79991234567"),
        ("+1234567", "+1234567"),
        ("+123456789012345", "+123456789012345"),
        ("  +79991234567 ", "+79991234567"),
        ("+9999999", "+9999999"),
    ],
    ids=["russian", "min_7_digits", "max_15_digits", "stripped", "nines"],
)
def test_phone_valid(raw, expected):
    assert check(PhoneE164, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "+",
        "+123456",
        "+1234567890123456",
        "+0123456789",
        "+00123456789",
        "79991234567",
        "89991234567",
        "+7 999 123 45 67",
        "+7(999)1234567",
        "+7-999-123-45-67",
        "++79991234567",
        "+7999123456a",
        "+٧٩٩٩١٢٣٤٥٦٧",
        "+79991234567 ext",
        "+7999123456\n7",
    ],
    ids=[
        "empty",
        "plus_only",
        "6_digits",
        "16_digits",
        "starts_with_0",
        "double_zero",
        "no_plus",
        "8_prefix",
        "spaces",
        "brackets",
        "dashes",
        "double_plus",
        "letter",
        "arabic_digits",
        "extension",
        "newline_inside",
    ],
)
def test_phone_invalid_raises(raw):
    fails(PhoneE164, raw)


# --------------------------------------------------------------------------- #
# Icd10Code: буква + 2 цифры + необязательно ".d" / ".dd"; верхний регистр
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("J06.9", "J06.9"),
        ("j06.9", "J06.9"),
        ("I10", "I10"),
        ("s72.00", "S72.00"),
        (" a00 ", "A00"),
        ("Z99.99", "Z99.99"),
        ("A00", "A00"),
        ("u07.1", "U07.1"),
        ("C34.1", "C34.1"),
    ],
    ids=[
        "upper",
        "lower_is_uppercased",
        "no_subcode",
        "two_digit_subcode",
        "stripped",
        "max_chars",
        "min_code",
        "u_code",
        "c_code",
    ],
)
def test_icd10_valid_is_normalized(raw, expected):
    assert check(Icd10Code, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "J6.9",
        "J06.",
        "J06.999",
        "J06,9",
        "06.9",
        "JJ06",
        "J0",
        "J06.9.1",
        "J06 .9",
        "Ж06.9",
        "J０６",
        "J06.٩",
        "J",
        "123",
        "J06.9*",
        "J06.9†",
        "J06-9",
    ],
    ids=[
        "empty",
        "one_digit",
        "dot_only",
        "three_digit_subcode",
        "comma",
        "no_letter",
        "two_letters",
        "one_digit_code",
        "two_subcodes",
        "space",
        "cyrillic_letter",
        "fullwidth_digits",
        "arabic_subcode",
        "letter_only",
        "digits_only",
        "asterisk",
        "dagger",
        "dash",
    ],
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


@pytest.mark.parametrize(
    "value", [0, -1, 2_147_483_648, None, "abc", 1.5], ids=["zero", "negative", "over_int32", "none", "text", "float"]
)
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
    [
        ([1], [1]),
        ([7, 1, 3], [1, 3, 7]),
        ([5, 4, 3, 2, 1], [1, 2, 3, 4, 5]),
        ([1, 2, 3, 4, 5, 6, 7], [1, 2, 3, 4, 5, 6, 7]),
        ([7], [7]),
    ],
    ids=["single", "unsorted", "reversed", "all_week", "sunday"],
)
def test_work_days_valid_is_sorted(raw, expected):
    assert check(WorkDays, raw) == expected


@pytest.mark.parametrize(
    "raw",
    [[], [0], [8], [-1], [1, 1], [1, 2, 2], [1, 2, 3, 4, 5, 6, 7, 1], None, "12345", [1, "x"]],
    ids=[
        "empty",
        "zero",
        "eight",
        "negative",
        "duplicate",
        "duplicate_later",
        "eight_items",
        "none",
        "string",
        "non_int",
    ],
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


@pytest.mark.parametrize(
    "raw",
    ["2026-10-12T12:00:00", datetime(2026, 10, 12, 12), "2026-10-12", "garbage", "", None],
    ids=["naive_str", "naive_obj", "date_only", "garbage", "empty", "none"],
)
def test_utc_datetime_naive_or_invalid_raises(raw):
    fails(UTCDatetime, raw)


@pytest.mark.parametrize(
    "raw", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"], ids=["min_edge", "max_edge"]
)
def test_utc_datetime_overflow_edge_gives_validation_error_not_overflow(raw):
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(UTCDatetime).validate_python(raw)
    assert "out of the supported range" in error_of(exc)["msg"]


@pytest.mark.parametrize(
    ("raw", "expected"), [("09:00", time(9)), (time(23, 59, 59), time(23, 59, 59)), ("00:00", time(0))]
)
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
    [
        TODAY,
        TODAY + timedelta(days=1),
        date(1990, 5, 17),
        date(2000, 2, 29),
        date(1896, 1, 1),
        date(1896, 12, 31),
        date(2026, 10, 11),
    ],
    ids=[
        "today",
        "tomorrow",
        "regular",
        "leap_day",
        "oldest_allowed_first_day",
        "oldest_allowed_last_day",
        "yesterday",
    ],
)
def test_birth_date_valid(frozen_today, value):
    assert make_patient(birth_date=value).birth_date == value


@pytest.mark.parametrize(
    "value",
    [TODAY + timedelta(days=2), date(2030, 1, 1), date(9999, 12, 31)],
    ids=["day_after_tomorrow", "future_year", "max"],
)
def test_birth_date_in_future_raises(frozen_today, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(birth_date=value)
    assert error_of(exc)["loc"] == ("birth_date",)
    assert "must not be in the future" in error_of(exc)["msg"]


@pytest.mark.parametrize(
    "value", [date(1895, 12, 31), date(1800, 1, 1), date(1, 1, 1)], ids=["one_day_too_old", "1800", "min"]
)
def test_birth_date_too_old_raises(frozen_today, value):
    with pytest.raises(ValidationError) as exc:
        make_patient(birth_date=value)
    assert "implausibly far in the past" in error_of(exc)["msg"]


def test_birth_date_year_boundary_uses_first_of_january(frozen_today):
    monkey_today = date(2026, 1, 1)
    assert make_patient(birth_date=date(monkey_today.year - 130, 1, 1)).birth_date == date(1896, 1, 1)


@pytest.mark.parametrize(
    "value",
    ["1990-02-30", "1990-13-01", "garbage", "", None, 123],
    ids=["feb_30", "month_13", "text", "empty", "none", "int"],
)
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


@pytest.mark.parametrize(
    "role", ["superuser", "ADMIN", "", None, 1], ids=["unknown", "uppercase", "empty", "none", "int"]
)
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
    [
        ("user_id", 0),
        ("user_id", 2_147_483_648),
        ("specialty", "A"),
        ("cabinet", ""),
        ("slot_minutes", 4),
        ("slot_minutes", 241),
        ("work_days", []),
        ("work_days", [1, 1]),
    ],
    ids=[
        "user_id_0",
        "user_id_over",
        "specialty_short",
        "cabinet_empty",
        "slot_4",
        "slot_241",
        "days_empty",
        "days_dup",
    ],
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
    data = {
        "doctor_id": 1,
        "patient_id": 2,
        "starts_at": "2026-10-13T10:00:00+03:00",
        "ends_at": "2026-10-13T10:30:00+03:00",
    }
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
        AppointmentCreate(
            **appointment_data(starts_at="2026-10-13T10:00:00+03:00", ends_at="2026-10-13T12:00:00+05:00")
        )


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
    (UserUpdate, "email"),
    (UserUpdate, "password"),
    (UserUpdate, "full_name"),
    (UserUpdate, "role"),
    (UserUpdate, "is_active"),
    (DoctorUpdate, "specialty"),
    (DoctorUpdate, "cabinet"),
    (DoctorUpdate, "slot_minutes"),
    (DoctorUpdate, "work_start"),
    (DoctorUpdate, "work_end"),
    (DoctorUpdate, "work_days"),
    (PatientUpdate, "full_name"),
    (PatientUpdate, "birth_date"),
    (PatientUpdate, "policy_number"),
    (AppointmentUpdate, "starts_at"),
    (AppointmentUpdate, "ends_at"),
    (AppointmentUpdate, "status"),
    (PrescriptionUpdate, "medication"),
    (PrescriptionUpdate, "dosage"),
    (MedicalRecordUpdate, "diagnosis_code"),
]
NULLABLE = [
    (PatientUpdate, "user_id"),
    (PatientUpdate, "phone"),
    (AppointmentUpdate, "reason"),
    (PrescriptionUpdate, "instructions"),
    (MedicalRecordUpdate, "notes"),
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


@pytest.mark.parametrize(
    "model",
    [UserUpdate, DoctorUpdate, PatientUpdate, AppointmentUpdate, PrescriptionUpdate, MedicalRecordUpdate],
    ids=lambda m: m.__name__,
)
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
        id=1,
        email="a@b.co",
        full_name="Ан",
        role=UserRole.ADMIN,
        is_active=True,
        created_at=datetime(2026, 10, 12, 12, tzinfo=timezone(timedelta(hours=3))),
        password_hash="$2b$12$secret",
    )
    out = UserOut.model_validate(orm_like)
    dumped = out.model_dump()
    assert "password_hash" not in dumped
    assert "secret" not in out.model_dump_json()
    assert out.created_at == datetime(2026, 10, 12, 9, tzinfo=UTC)
    assert out.created_at.utcoffset() == timedelta(0)


def test_user_out_naive_created_at_raises():
    orm_like = SimpleNamespace(
        id=1, email="a@b.co", full_name="Ан", role=UserRole.ADMIN, is_active=True, created_at=datetime(2026, 10, 12)
    )
    with pytest.raises(ValidationError):
        UserOut.model_validate(orm_like)


def test_token_out_default_type_and_literal():
    assert TokenOut(access_token="t", expires_in=3600).token_type == "bearer"
    with pytest.raises(ValidationError):
        TokenOut(access_token="t", expires_in=3600, token_type="basic")
