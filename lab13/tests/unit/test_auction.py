"""Аукцион записи (раздел 6): формула стоимости, выбор победителя, разбор ставок."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mas.common.models import Bid
from mas.orchestrator.auction import bid_cost, choose_winner, parse_bids

AUCTION = "auc-1"


def _bid(agent: str, cost: float, *, slot: str = "2026-10-14T06:00:00Z", eligible: bool = True,
         auction: str = AUCTION) -> Bid:
    return Bid(auction_id=auction, agent_id=agent, doctor_id=f"d-{agent}", slot_start=slot, cost=cost,
               eligible=eligible)


def _raw(**fields: Any) -> bytes:
    payload = {"auction_id": AUCTION, "agent_id": "booking-1", "doctor_id": "d-1",
               "slot_start": "2026-10-14T06:00:00Z", "skill": 1.0, "availability": 0.9, "load": 0.1,
               "cost": 0.1, "eligible": True, "reason": ""}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False).encode()


# ---- формула стоимости (раздел 6, п. 2) --------------------------------------------------------------


def test_cost_of_ideal_bid_is_zero() -> None:
    assert bid_cost(skill=1.0, availability=1.0, load=0.0) == pytest.approx(0.0)


def test_cost_of_worst_bid_is_one() -> None:
    assert bid_cost(skill=0.0, availability=0.0, load=1.0) == pytest.approx(0.5 + 0.3 + 0.2)


def test_cost_follows_contract_formula() -> None:
    # 0.5·(1-0.83) + 0.3·0.25 + 0.2·(1-0.5) = 0.085 + 0.075 + 0.1
    assert bid_cost(skill=0.5, availability=0.83, load=0.25) == pytest.approx(0.26)


def test_lower_availability_and_higher_load_increase_cost() -> None:
    base = bid_cost(skill=1.0, availability=0.8, load=0.2)
    assert bid_cost(skill=1.0, availability=0.5, load=0.2) > base
    assert bid_cost(skill=1.0, availability=0.8, load=0.6) > base


def test_partial_skill_match_costs_more_than_full_match() -> None:
    assert bid_cost(skill=0.5, availability=1.0, load=0.0) > bid_cost(skill=1.0, availability=1.0, load=0.0)


# ---- выбор победителя ---------------------------------------------------------------------------------


def test_no_bids_means_no_winner() -> None:
    assert choose_winner([]) is None


def test_ineligible_bids_are_never_chosen_even_if_cheapest() -> None:
    bids = [_bid("booking-a-1", 0.01, eligible=False), _bid("booking-b-1", 0.9)]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-b-1"


def test_all_ineligible_means_no_winner() -> None:
    assert choose_winner([_bid("booking-a-1", 0.1, eligible=False)]) is None


def test_minimal_cost_wins() -> None:
    bids = [_bid("booking-a-1", 0.41), _bid("booking-b-1", 0.2), _bid("booking-c-1", 0.3)]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-b-1"


def test_tie_is_broken_by_earlier_slot_start() -> None:
    bids = [
        _bid("booking-a-1", 0.2, slot="2026-10-14T09:00:00Z"),
        _bid("booking-b-1", 0.2, slot="2026-10-14T06:30:00Z"),
    ]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-b-1"


def test_slot_tie_is_broken_by_agent_id_alphabetically() -> None:
    bids = [_bid("booking-b-1", 0.2), _bid("booking-a-1", 0.2)]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-a-1"


def test_slot_comparison_is_by_instant_not_by_text() -> None:
    # 06:30+03:00 — это 03:30 UTC, раньше 05:00 UTC; по тексту было бы наоборот.
    bids = [
        _bid("booking-a-1", 0.2, slot="2026-10-14T06:30:00+03:00"),
        _bid("booking-b-1", 0.2, slot="2026-10-14T05:00:00Z"),
    ]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-a-1"


def test_float_noise_does_not_break_a_tie() -> None:
    # 0.1 + 0.2 != 0.3 в float, но по контракту это равная стоимость: решает время слота.
    bids = [
        _bid("booking-a-1", 0.1 + 0.2, slot="2026-10-14T09:00:00Z"),
        _bid("booking-b-1", 0.3, slot="2026-10-14T06:00:00Z"),
    ]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-b-1"


# ---- разбор ставок ------------------------------------------------------------------------------------


def test_valid_bids_are_parsed() -> None:
    bids = parse_bids([_raw(agent_id="booking-a-1"), _raw(agent_id="booking-b-1")], AUCTION)
    assert sorted(b.agent_id for b in bids) == ["booking-a-1", "booking-b-1"]


def test_broken_bids_are_skipped() -> None:
    raw = [
        b"{not json",
        b"[1, 2, 3]",
        b'{"auction_id": "auc-1"}',  # нет agent_id — не ставка
        b"\xff\xfe",
        _raw(agent_id="booking-a-1"),
    ]
    bids = parse_bids(raw, AUCTION)
    assert [b.agent_id for b in bids] == ["booking-a-1"]


def test_bids_of_another_auction_are_ignored() -> None:
    raw = [_raw(agent_id="booking-a-1", auction_id="auc-OTHER"), _raw(agent_id="booking-b-1")]
    bids = parse_bids(raw, AUCTION)
    assert [b.agent_id for b in bids] == ["booking-b-1"]


def test_repeated_bid_from_one_agent_counts_once() -> None:
    raw = [_raw(agent_id="booking-a-1", cost=0.5), _raw(agent_id="booking-a-1", cost=0.2)]
    bids = parse_bids(raw, AUCTION)
    assert len(bids) == 1
    assert bids[0].agent_id == "booking-a-1"


def test_repeated_bids_do_not_duplicate_agents_in_auction() -> None:
    raw = [
        _raw(agent_id="booking-a-1", cost=0.9),
        _raw(agent_id="booking-b-1", cost=0.5),
        _raw(agent_id="booking-a-1", cost=0.1),
    ]
    bids = parse_bids(raw, AUCTION)
    assert sorted(b.agent_id for b in bids) == ["booking-a-1", "booking-b-1"]


def test_naive_slot_time_does_not_crash_auction() -> None:
    # НАХОДКА: slot_start без смещения (не RFC 3339, раздел 4) при равной стоимости роняет выбор победителя:
    # _slot_key сравнивает aware- и naive-datetime и падает с TypeError. По контракту такая ставка
    # некорректна и должна пропускаться; корректная ставка с той же стоимостью должна выиграть.
    bids = [
        _bid("booking-a-1", 0.2, slot="2026-10-14T07:00:00"),
        _bid("booking-b-1", 0.2, slot="2026-10-14T06:00:00Z"),
    ]
    winner = choose_winner(bids)
    assert winner is not None
    assert winner.agent_id == "booking-b-1"
