"""Contract-level validation tests for the admin usage analytics query."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.schemas.usage_analytics import parse_analytics_query, ratio_text
from app.schemas.usage_events import InvalidUsageEventQuery

_FROM = "2026-01-01T00:00:00Z"
_TO = "2026-01-02T00:00:00Z"


@pytest.mark.parametrize("group_by", ["provider", "model", "customer", "project", "feature"])
def test_analytics_accepts_each_grouping(group_by: str) -> None:
    parsed, start, end, feature = parse_analytics_query(
        group_by=group_by,
        occurred_from=_FROM,
        occurred_to=_TO,
        feature=None,
        page=1,
        page_size=20,
        max_page=10_000,
        max_page_size=100,
    )

    assert (parsed, start, end, feature) == (
        group_by,
        dt.datetime(2026, 1, 1),
        dt.datetime(2026, 1, 2),
        None,
    )


@pytest.mark.parametrize(
    ("occurred_from", "occurred_to"),
    [
        ("2026-01-01T00:00:00Z", "2027-01-02T00:00:01Z"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
    ],
)
def test_analytics_rejects_invalid_period(occurred_from: str, occurred_to: str) -> None:
    with pytest.raises(InvalidUsageEventQuery):
        parse_analytics_query(
            group_by="provider",
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            feature=None,
            page=1,
            page_size=20,
            max_page=10_000,
            max_page_size=100,
        )


def test_analytics_decimal_ratios_never_use_float() -> None:
    assert ratio_text(Decimal(1) / Decimal(3)) == "0.3333333333333333333333333333"
