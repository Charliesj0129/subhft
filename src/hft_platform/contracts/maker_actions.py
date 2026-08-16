"""Canonical maker-action DTOs shared by platform and research backtests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostQuote:
    side: str
    price: int
    qty: int = 1


@dataclass(frozen=True)
class CancelQuote:
    side: str


@dataclass(frozen=True)
class Hold:
    pass


__all__ = [
    "CancelQuote",
    "Hold",
    "PostQuote",
]
