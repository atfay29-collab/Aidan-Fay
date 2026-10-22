"""Turn a model probability and Kalshi quotes into a trade (or no trade)."""

from __future__ import annotations

import math
from dataclasses import dataclass


def taker_fee_cents(price_cents: int, count: int = 1) -> int:
    """Kalshi taker fee: round_up(0.07 * C * P * (1 - P)) to the cent, P in dollars."""
    p = price_cents / 100.0
    return math.ceil(round(0.07 * count * p * (1.0 - p) * 100.0, 6))


@dataclass(frozen=True)
class Params:
    min_edge: float = 0.05        # required EV per contract after fees, in dollars
    min_minutes_left: float = 2.0  # skip the last minutes (settlement is a 60s average)
    max_minutes_left: float = 14.0
    min_price: int = 10            # cents; skip extreme prices where model error dominates
    max_price: int = 90
    contracts: int = 10


@dataclass(frozen=True)
class Decision:
    side: str          # "yes" or "no"
    price: int         # cents paid per contract
    p_win: float       # model probability this side wins
    edge: float        # expected profit per contract after fees, dollars


def decide(p_up: float, yes_ask: int | None, no_ask: int | None, minutes_left: float,
           params: Params = Params()) -> Decision | None:
    if not (params.min_minutes_left <= minutes_left <= params.max_minutes_left):
        return None
    best = None
    for side, ask, p_win in (("yes", yes_ask, p_up), ("no", no_ask, 1.0 - p_up)):
        if ask is None or not (params.min_price <= ask <= params.max_price):
            continue
        fee = taker_fee_cents(ask, params.contracts) / params.contracts
        edge = p_win - (ask + fee) / 100.0
        if edge >= params.min_edge and (best is None or edge > best.edge):
            best = Decision(side, ask, p_win, edge)
    return best


def settle_pnl_cents(decision: Decision, result: str, contracts: int) -> int:
    """Net P&L in cents including the taker fee."""
    fee = taker_fee_cents(decision.price, contracts)
    payout = 100 * contracts if result == decision.side else 0
    return payout - decision.price * contracts - fee
