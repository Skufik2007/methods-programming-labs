"""Запасные правила без модели: классификатор по ключевым словам и шаблонный ответ на отзыв."""

from __future__ import annotations

from mas.common.models import FeedbackReplyOutput, IntakeOutput
from mas.common.specialties import DEFAULT_SPECIALTY

# Ключевые основы слов (нижний регистр) -> красный флаг.
RED_FLAGS: tuple[tuple[str, str], ...] = (
    ("боль в груди", "боль в груди"),
    ("боли в груди", "боль в груди"),
    ("давит в груди", "давящая боль в груди"),
    ("одышк", "одышка"),
    ("тяжело дыша", "затруднённое дыхание"),
    ("трудно дыша", "затруднённое дыхание"),
    ("не могу дыша", "затруднённое дыхание"),
    ("потеря сознани", "потеря сознания"),
    ("потерял сознани", "потеря сознания"),
    ("потеряла сознани", "потеря сознания"),
    ("без сознания", "потеря сознания"),
    ("судорог", "судороги"),
    ("сильное кровотечени", "сильное кровотечение"),
    ("кровь в рвот", "кровь в рвоте"),
    ("перекос", "перекос лица"),
    ("нарушение речи", "нарушение речи"),
    ("не могу говорить", "нарушение речи"),
    ("парализ", "паралич"),
    ("внезапная потеря зрени", "внезапная потеря зрения"),
    ("резкая головная боль", "внезапная сильная головная боль"),
)

SPECIALTY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cardiologist": ("сердц", "давлени", "пульс", "аритми", "перебои", "грудин", "тахикард"),
    "neurologist": ("головн", "головокруж", "мигрен", "онемени", "невралг", "тремор", "бессонниц"),
    "ophthalmologist": ("глаз", "зрени", "слезит", "веко", "веки", "очки"),
    "dermatologist": ("сыпь", "сыпи", "зуд", "кож", "прыщ", "экзем", "родинк", "псориаз"),
    "ent": ("ухо", "уха", "уши", "ушах", "слух", "слышу", "заложен", "гайморит", "синусит", "носов"),
    "traumatologist": ("подвернул", "вывих", "перелом", "ушиб", "растяжени", "лодыж", "колен", "сустав"),
    "therapist": ("температур", "кашел", "горл", "простуд", "орви", "слабост", "ломота", "насморк"),
}

SYMPTOMS: tuple[tuple[str, str], ...] = (
    ("температур", "температура"),
    ("кашел", "кашель"),
    ("горл", "боль в горле"),
    ("насморк", "насморк"),
    ("заложен", "заложенность носа"),
    ("головн", "головная боль"),
    ("головокруж", "головокружение"),
    ("слабост", "слабость"),
    ("ломота", "ломота в теле"),
    ("сыпь", "сыпь"),
    ("зуд", "зуд"),
    ("глаз", "жалобы на глаза"),
    ("зрени", "снижение зрения"),
    ("ухо", "боль в ухе"),
    ("слышу", "снижение слуха"),
    ("давлени", "изменение давления"),
    ("перебои", "перебои в сердце"),
    ("опухл", "отёк"),
    ("подвернул", "травма"),
    ("боль", "боль"),
    ("болит", "боль"),
)


def find_red_flags(text: str) -> list[str]:
    low = text.lower()
    found: list[str] = []
    for needle, label in RED_FLAGS:
        if needle in low and label not in found:
            found.append(label)
    return found


def fallback_classify(complaint: str, preferred_specialty: str = "") -> IntakeOutput:
    """Разбор по ключевым словам. Специальность — с наибольшим числом совпадений, иначе
    указанная пациентом (если из справочника), иначе терапевт."""
    low = complaint.lower()
    scores = {spec: sum(1 for kw in kws if kw in low) for spec, kws in SPECIALTY_KEYWORDS.items()}
    best = max(scores.values(), default=0)
    if best > 0:
        # При равенстве порядок словаря: узкие специальности раньше терапевта.
        specialty = next(spec for spec, score in scores.items() if score == best)
    else:
        specialty = preferred_specialty or DEFAULT_SPECIALTY
    symptoms: list[str] = []
    for needle, label in SYMPTOMS:
        if needle in low and label not in symptoms:
            symptoms.append(label)
    summary = " ".join(complaint.split())[:200]
    return IntakeOutput(
        symptoms=symptoms[:10],
        specialty=specialty,
        red_flags=find_red_flags(complaint),
        summary=summary,
        source="fallback",
    )


def fallback_reply(rating: int | None, topics: list[str] | None = None) -> FeedbackReplyOutput:
    """Шаблонный вежливый ответ на негативный отзыв без медицинских назначений."""
    about = f" Мы учтём ваши замечания ({', '.join(topics[:3])})." if topics else ""
    text = (
        "Здравствуйте! Спасибо, что поделились мнением, и приносим извинения за доставленные неудобства."
        f"{about} Администратор клиники свяжется с вами, чтобы разобраться в ситуации. "
        "По вопросам лечения, пожалуйста, обратитесь к вашему врачу на приёме."
    )
    return FeedbackReplyOutput(reply=text, source="fallback")
