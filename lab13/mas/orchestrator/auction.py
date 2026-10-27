"""Чистая логика аукциона (раздел 6): выбор победителя и сбор ставок."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from mas.common.models import Bid, parse_ts

log = logging.getLogger("mas.auction")


def bid_cost(skill: float, availability: float, load: float) -> float:
    """Формула контракта: 0.5·(1-availability) + 0.3·load + 0.2·(1-skill)."""
    return 0.5 * (1 - availability) + 0.3 * load + 0.2 * (1 - skill)


def _slot_time(slot_start: str) -> datetime | None:
    """Время слота по RFC 3339 (раздел 4: с часовым поясом); иначе None — ставка некорректна."""
    try:
        moment = parse_ts(slot_start)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def choose_winner(bids: list[Bid]) -> Bid | None:
    """Минимальный cost среди eligible; при равенстве — раньше slot_start, затем agent_id.

    Ставки со слотом не по RFC 3339 (в том числе без часового пояса) пропускаются: их нельзя
    сравнить с остальными, а сравнение naive и aware datetime роняло весь раунд аукциона.
    Стоимость сравнивается с округлением до 9 знаков, чтобы шум float не ломал ничью.
    """
    ranked = [(b, slot) for b in bids if b.eligible and (slot := _slot_time(b.slot_start)) is not None]
    if not ranked:
        return None
    return min(ranked, key=lambda item: (round(item[0].cost, 9), item[1], item[0].agent_id))[0]


def parse_bids(raw: list[bytes], auction_id: str) -> list[Bid]:
    """Разбирает ответы; битые и чужого аукциона пропускает, от одного агента берёт последнюю ставку."""
    by_agent: dict[str, Bid] = {}
    for item in raw:
        try:
            bid = Bid.model_validate(json.loads(item))
        except (ValueError, TypeError):
            log.warning("пропущена некорректная ставка")
            continue
        if bid.auction_id != auction_id:
            continue
        by_agent[bid.agent_id] = bid
    return list(by_agent.values())
