"""Persistent billing delivery worker. Run separately from the API process."""

from __future__ import annotations

import argparse
import logging
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update

from app.billing import BillingClient, DeliveryResult
from app.config import Settings
from app.db import UsageOutbox, create_session_factory, utcnow
from app.ids import new_ulid

logger = logging.getLogger(__name__)

# Touched after every loop; the container health check requires it to be fresh (docker-compose.yml).
HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "acuven_ai_api_worker_heartbeat"


def claim_one(session_factory) -> UsageOutbox | None:
    now = utcnow()
    eligible = or_(
        and_(UsageOutbox.status == "PENDING", UsageOutbox.next_attempt_at <= now),
        and_(UsageOutbox.status == "SENDING", UsageOutbox.next_attempt_at <= now),
    )
    with session_factory.begin() as session:
        ids = session.scalars(select(UsageOutbox.id).where(eligible).order_by(UsageOutbox.id).limit(10)).all()
        for row_id in ids:
            claim_id = new_ulid()
            changed = session.execute(
                update(UsageOutbox)
                .where(UsageOutbox.id == row_id, eligible)
                .values(
                    status="SENDING",
                    claim_id=claim_id,
                    next_attempt_at=now + timedelta(seconds=60),
                    attempts=UsageOutbox.attempts + 1,
                )
            )
            if changed.rowcount == 1:
                row = session.get(UsageOutbox, row_id)
                session.expunge(row)
                return row
    return None


def deliver_one(session_factory, settings: Settings, billing: BillingClient) -> bool:
    row = claim_one(session_factory)
    if row is None:
        return False
    binding = settings.clients.get(row.client_id)
    if binding is None:
        outcome = DeliveryResult(False, False, "CLIENT_BINDING_MISSING")
    else:
        outcome = billing.send(binding, row.payload_json)
    if outcome.delivered:
        new_status = "DELIVERED"
    elif outcome.retryable:
        new_status = "PENDING"
    else:
        new_status = "DEAD"
    delay = min(3600, 2 ** min(row.attempts, 12))
    with session_factory.begin() as session:
        session.execute(
            update(UsageOutbox)
            .where(
                UsageOutbox.id == row.id,
                UsageOutbox.status == "SENDING",
                UsageOutbox.claim_id == row.claim_id,
            )
            .values(
                status=new_status,
                claim_id=None,
                last_error=outcome.error_code,
                next_attempt_at=utcnow() + timedelta(seconds=delay),
            )
        )
    if new_status == "DEAD":
        logger.error("Billing usage event requires intervention: event_id=%s code=%s", row.event_id, outcome.error_code)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    if not settings.billing_base_url:
        raise SystemExit("AI_API_BILLING_BASE_URL is required")
    session_factory = create_session_factory(settings.database_url)
    billing = BillingClient(settings.billing_base_url)
    while True:
        worked = deliver_one(session_factory, settings, billing)
        HEARTBEAT_PATH.touch()
        if args.once:
            return
        if not worked:
            time.sleep(2)


if __name__ == "__main__":
    main()
