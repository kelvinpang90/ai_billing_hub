"""The request path remains available while billing dependencies are down."""

from __future__ import annotations

import pytest
from test_ai_messages_api import _build_world

from app.ai_api.outbox import AiUsageOutboxStatus
from app.core.celery_app import RedisNotConfigured


def _unavailable(*args, **kwargs) -> None:
    raise RuntimeError("billing dependency unavailable")


@pytest.mark.parametrize("outage", ["ingest", "billing_worker", "broker"])
def test_ai_request_succeeds_and_leaves_pending_outbox_when_billing_is_down(
    outage: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if outage == "ingest":
        monkeypatch.setattr("app.services.usage_ingest.ingest_one", _unavailable)
    elif outage == "billing_worker":
        monkeypatch.setattr("app.services.usage_billing.bill_pending_events", _unavailable)
    else:
        monkeypatch.setattr(
            "app.main.create_celery_app",
            lambda settings: (_ for _ in ()).throw(RedisNotConfigured("broker unavailable")),
        )

    with _build_world("sqlite", tmp_path, monkeypatch) as world:
        response = world.post()

        assert response.status_code == 200
        assert [row.status for row in world.rows()] == [AiUsageOutboxStatus.PENDING]
