"""The payment criteria merged into `wallet_negative_balance` (design gate #580 v3 §2「告警」,
AIH-TASK-175; updated by design gate #585 v3 §2「告警」, AIH-TASK-181).

两份设计 §7「告警判据」：告警表每一行与 24 小时窗口边界
（`now − 24 小时 < 时刻 ≤ now`，晚于 now 的不计）、最老未处理事件 900 秒的边界
（`max(0, ⌊now − received_at⌋)`，严格大于才算）、配置判据的每一种情形；#585 的新表：
未结案的复核不论多久都报（结案即消失）、争议与退款的 24 小时窗口（`IGNORED` 的不计、
不入账结案后的退款不计而争议照计）、`expires_at` 前后 3600 秒的过期 `PENDING`；以及
**没有支付数据时摘要与原来逐字相同**。

每个用例先放一次已完成、没有不一致的核对运行，使原有两部分是 OK，
支付判据的级别因此看得清；期望的原有部分取自同一个库里「还没有支付数据」时的结果。
支付与租户沿用 test_payment_confirm.py 的世界（SQLite 与真 MySQL 各跑一次），
它在每个用例前后删掉支付与事件行，核对运行也在前后清空：MySQL 的库是共用的，
其他告警用例不应看到支付段落。配置判据用 tmp_path 里的假密钥文件（虚构值），不联网。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL`，没设时 skip ——
**不要把 skipped 读成 passed**，CI 里不许 skip。
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from payment_world import PaymentRef, close_by_review
from sqlalchemy import update
from test_payment_confirm import World, _mysql_world, _sqlite_world
from test_wallet_reconciliation_alerts import completed
from test_wallet_reconciliation_start import _wipe

from app.core.config import Settings
from app.core.fx_source import kuala_lumpur_date
from app.models.payments import (
    Payment,
    PaymentEventProcessingStatus,
    PaymentEventVerificationStatus,
    PaymentGateway,
    PaymentGatewayEvent,
    PaymentResolution,
    PaymentStatus,
)
from app.services import alerts
from app.services.alerts import AlertContext, AlertLevel, AlertResult

OK = AlertLevel.OK
P1 = AlertLevel.P1
P2 = AlertLevel.P2

# 马来西亚 2026-11-09 12:00。
NOW = dt.datetime(2026, 11, 9, 4, 0, 0)
SECOND = dt.timedelta(seconds=1)
DAY = dt.timedelta(hours=24)

# 窗口边界：(时刻, 是否计入)。
WINDOW_EDGES = [
    (NOW, True),
    (NOW - DAY + SECOND, True),
    (NOW - DAY, False),
    (NOW + SECOND, False),
]
EDGE_IDS = ["now", "24h-minus-1s", "exactly-24h", "after-now"]

# 虚构的地址与密钥：这些用例不会连到任何主机。
API_BASE_URL = "https://stripe-api.example.test"
FRONTEND_BASE_URL = "https://portal.example.test"


REFUNDED = "charge.refunded"
DISPUTED = "charge.dispute.created"
CHARGE_TYPES = [DISPUTED, REFUNDED]


def segment(
    review: int = 0,
    conflicts: int = 0,
    oldest: int = 0,
    failed: int = 0,
    stale: int = 0,
    disputes: int = 0,
    config: str = "ok",
) -> str:
    """The paragraph design gate #585 v3 §2「告警」gives, verbatim."""
    return (
        f"; payments: review open {review}, event conflicts 24h {conflicts}, "
        f"oldest open event {oldest}s, unmatched or rejected 24h {failed}, "
        f"stale pending {stale}, disputes or refunds 24h {disputes}, config {config}"
    )


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def alert_world(request, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    worlds = _mysql_world(monkeypatch) if request.param == "mysql" else _sqlite_world()
    world = next(worlds)
    _wipe(world.factory)
    completed(world.factory, kuala_lumpur_date(NOW), NOW - dt.timedelta(hours=1))
    # 缺省是支付开关关闭（配置判据不触发），不受运行环境的变量影响。
    use_settings(monkeypatch, Settings(payments_enabled=False))
    try:
        yield world
    finally:
        _wipe(world.factory)
        worlds.close()


def use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(alerts, "get_settings", lambda: settings)


def evaluate(world: World, now: dt.datetime = NOW) -> AlertResult:
    with world.factory() as session:
        return alerts.wallet_negative_balance(session, AlertContext(now=now, currencies=("USD",)))


BASE = "no wallet below zero; reconciliation 2026-11-09 checked 3 inconsistent 0"


def expect(world: World, level: AlertLevel, payments: str = "", now: dt.datetime = NOW) -> None:
    """The whole line: the original two parts unchanged, then the payment paragraph if any."""
    assert evaluate(world, now) == AlertResult(level, BASE + payments)


def review_payment(world: World, review_required_at: dt.datetime) -> PaymentRef:
    payment = world.payment(world.customer(), status=PaymentStatus.REVIEW_REQUIRED)
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(review_required_at=review_required_at)
        )
        session.commit()
    return payment


def credit_by_review(world: World, payment: PaymentRef) -> None:
    """What the manual CREDIT (AIH-TASK-182) leaves on the payment row: `PAID`, resolved."""
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(
                status=PaymentStatus.PAID,
                confirmed_amount=Decimal("100.00"),
                confirmed_currency="MYR",
                paid_at=NOW - DAY,
                resolution=PaymentResolution.CREDITED,
                resolved_at=NOW - DAY,
                resolved_by_user_id=world.admin_id,
                resolution_reason="Fictional review credited",
            )
        )
        session.commit()


def pending_payment(world: World, expires_at: dt.datetime) -> PaymentRef:
    payment = world.payment(world.customer())
    set_expiry(world, payment, expires_at)
    return payment


def set_expiry(world: World, payment: PaymentRef, expires_at: dt.datetime) -> None:
    with world.factory() as session:
        session.execute(
            update(Payment)
            .where(Payment.public_id == payment.public_id)
            .values(expires_at=expires_at)
        )
        session.commit()


def payment_id(world: World, payment: PaymentRef) -> int:
    return int(world.row(payment).id)


def put_event(
    world: World,
    status: PaymentEventProcessingStatus,
    *,
    received_at: dt.datetime = NOW - dt.timedelta(hours=1),
    processed_at: dt.datetime | None = None,
    conflict_at: dt.datetime | None = None,
    event_type: str = "checkout.session.completed",
    linked_to: int | None = None,
    error_code: str | None = None,
) -> None:
    with world.factory() as session:
        session.add(
            PaymentGatewayEvent(
                gateway=PaymentGateway.STRIPE,
                gateway_event_id=f"evt_test_{uuid.uuid4().hex}",
                event_type=event_type,
                payment_id=linked_to,
                payload_fingerprint="0" * 64,
                verification_status=PaymentEventVerificationStatus.VERIFIED,
                processing_status=status,
                attempts=0,
                next_attempt_at=received_at,
                lease_until=None,
                last_error_code=error_code,
                fingerprint_conflict_at=conflict_at,
                received_at=received_at,
                processed_at=processed_at,
                metadata_json={"type": event_type},
            )
        )
        session.commit()


def secret_files(directory: Path, secret_key: str, webhook_secret: str) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "stripe_secret_key"
    hook = directory / "stripe_webhook_secret"
    key.write_text(secret_key, encoding="utf-8")
    hook.write_text(webhook_secret, encoding="utf-8")
    return {"stripe_secret_key_file": str(key), "stripe_webhook_secret_file": str(hook)}


# --- 没有支付数据 ------------------------------------------------------------------------


def test_without_payment_data_the_summary_is_unchanged(alert_world: World) -> None:
    expect(alert_world, OK)


def test_payment_rows_that_meet_no_criterion_change_nothing(alert_world: World) -> None:
    """支付与事件都在，但没有一条落在判据里：摘要仍与原来逐字相同。"""
    world = alert_world
    pending_payment(world, NOW - 3599 * SECOND)
    world.payment(world.customer(), status=PaymentStatus.PAID)
    closed = review_payment(world, NOW - DAY - SECOND)
    close_by_review(world, closed)
    put_event(world, PaymentEventProcessingStatus.PROCESSED, processed_at=NOW - SECOND)
    put_event(world, PaymentEventProcessingStatus.IGNORED, processed_at=NOW - SECOND)
    # 争议与退款：IGNORED 的、窗口外的、不入账结案后的退款，都不计。
    put_event(
        world,
        PaymentEventProcessingStatus.IGNORED,
        processed_at=NOW - SECOND,
        event_type=DISPUTED,
        error_code="LIVEMODE_MISMATCH",
    )
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - DAY,
        processed_at=NOW - DAY,
        event_type=DISPUTED,
    )
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        processed_at=NOW - SECOND,
        event_type=REFUNDED,
        linked_to=payment_id(world, closed),
    )
    put_event(
        world,
        PaymentEventProcessingStatus.REJECTED,
        processed_at=NOW - DAY,
        conflict_at=NOW - DAY,
    )
    put_event(world, PaymentEventProcessingStatus.RECEIVED, received_at=NOW - 900 * SECOND)

    expect(world, OK)


# --- P1：复核、指纹冲突 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "entered",
    [NOW, NOW - DAY + SECOND, NOW - DAY, NOW - dt.timedelta(days=30), NOW + SECOND],
    ids=["now", "24h-minus-1s", "exactly-24h", "30-days", "after-now"],
)
def test_an_open_review_is_p1_no_matter_how_long_ago_it_entered(
    alert_world: World, entered: dt.datetime
) -> None:
    """#585 替换了 A 的 24 小时：未结案就一直报，与 `review_required_at` 无关。"""
    review_payment(alert_world, entered)

    expect(alert_world, P1, segment(review=1))


@pytest.mark.parametrize("resolution", ["CLOSED", "CREDITED"])
def test_a_resolved_review_is_no_longer_reported(alert_world: World, resolution: str) -> None:
    world = alert_world
    resolved = review_payment(world, NOW - SECOND)
    still_open = review_payment(world, NOW - dt.timedelta(days=3))
    if resolution == "CLOSED":
        close_by_review(world, resolved)
    else:
        credit_by_review(world, resolved)

    expect(world, P1, segment(review=1))

    if resolution == "CLOSED":
        close_by_review(world, still_open)
    else:
        credit_by_review(world, still_open)

    expect(world, OK)


@pytest.mark.parametrize(("moment", "counted"), WINDOW_EDGES, ids=EDGE_IDS)
def test_an_event_fingerprint_conflict_in_the_last_24_hours_is_p1(
    alert_world: World, moment: dt.datetime, counted: bool
) -> None:
    put_event(
        alert_world,
        PaymentEventProcessingStatus.PROCESSED,
        processed_at=NOW - DAY - SECOND,
        conflict_at=moment,
    )

    if counted:
        expect(alert_world, P1, segment(conflicts=1))
    else:
        expect(alert_world, OK)


# --- P2：最老未处理事件 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        PaymentEventProcessingStatus.RECEIVED,
        PaymentEventProcessingStatus.RETRY,
        PaymentEventProcessingStatus.PROCESSING,
    ],
    ids=lambda s: s.value,
)
@pytest.mark.parametrize(
    ("waited", "now", "level", "age"),
    [
        (901, NOW, P2, 901),
        (900, NOW, OK, 900),
        # 年龄向下取整：900.5 秒仍是 900，不报。
        (900, NOW + dt.timedelta(milliseconds=500), OK, 900),
        (901, NOW + dt.timedelta(milliseconds=500), P2, 901),
        (3600, NOW, P2, 3600),
    ],
    ids=["901s", "900s", "900.5s", "901.5s", "1h"],
)
def test_an_open_event_waiting_over_900_seconds_is_p2(
    alert_world: World,
    status: PaymentEventProcessingStatus,
    waited: int,
    now: dt.datetime,
    level: AlertLevel,
    age: int,
) -> None:
    put_event(alert_world, status, received_at=NOW - waited * SECOND)
    # 较新的未处理事件不影响「最老」。
    put_event(alert_world, PaymentEventProcessingStatus.RECEIVED, received_at=NOW - SECOND)

    if level is OK:
        expect(alert_world, OK, now=now)
    else:
        expect(alert_world, P2, segment(oldest=age), now=now)


def test_the_oldest_open_event_is_zero_when_it_is_in_the_future_or_absent(
    alert_world: World,
) -> None:
    """`<c>` 没有未处理事件时为 0；`received_at` 晚于 now 时年龄取 0。"""
    world = alert_world
    put_event(world, PaymentEventProcessingStatus.RECEIVED, received_at=NOW + dt.timedelta(hours=1))
    review_payment(world, NOW)

    expect(world, P1, segment(review=1, oldest=0))


def test_settled_events_never_count_as_open(alert_world: World) -> None:
    world = alert_world
    long_ago = NOW - dt.timedelta(days=3)
    for status in (
        PaymentEventProcessingStatus.PROCESSED,
        PaymentEventProcessingStatus.IGNORED,
        PaymentEventProcessingStatus.UNMATCHED,
        PaymentEventProcessingStatus.REJECTED,
    ):
        put_event(world, status, received_at=long_ago, processed_at=long_ago)

    expect(world, OK)


# --- P2：最近 24 小时的 UNMATCHED / REJECTED ------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [PaymentEventProcessingStatus.UNMATCHED, PaymentEventProcessingStatus.REJECTED],
    ids=lambda s: s.value,
)
@pytest.mark.parametrize(("moment", "counted"), WINDOW_EDGES, ids=EDGE_IDS)
def test_unmatched_or_rejected_in_the_last_24_hours_is_p2(
    alert_world: World,
    status: PaymentEventProcessingStatus,
    moment: dt.datetime,
    counted: bool,
) -> None:
    put_event(alert_world, status, received_at=NOW - DAY * 2, processed_at=moment)

    if counted:
        expect(alert_world, P2, segment(failed=1))
    else:
        expect(alert_world, OK)


@pytest.mark.parametrize(
    "status",
    [PaymentEventProcessingStatus.PROCESSED, PaymentEventProcessingStatus.IGNORED],
    ids=lambda s: s.value,
)
def test_processed_and_ignored_events_are_not_failures(
    alert_world: World, status: PaymentEventProcessingStatus
) -> None:
    put_event(alert_world, status, processed_at=NOW)

    expect(alert_world, OK)


# --- P1：最近 24 小时的争议与退款（#585） -----------------------------------------------------


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
@pytest.mark.parametrize(("moment", "counted"), WINDOW_EDGES, ids=EDGE_IDS)
def test_a_dispute_or_refund_received_in_the_last_24_hours_is_p1(
    alert_world: World, event_type: str, moment: dt.datetime, counted: bool
) -> None:
    """窗口按 `received_at`；`processed_at` 留在窗口外，只看这一条判据。"""
    put_event(
        alert_world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=moment,
        processed_at=NOW - DAY * 2,
        event_type=event_type,
    )

    if counted:
        expect(alert_world, P1, segment(disputes=1))
    else:
        expect(alert_world, OK)


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_an_ignored_dispute_or_refund_is_not_counted(alert_world: World, event_type: str) -> None:
    put_event(
        alert_world,
        PaymentEventProcessingStatus.IGNORED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=event_type,
        error_code="LIVEMODE_MISMATCH",
    )

    expect(alert_world, OK)


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_an_unmatched_dispute_or_refund_counts_in_both_criteria(
    alert_world: World, event_type: str
) -> None:
    """关联不上（例如结案时没记下 PI）也照计 P1，同时计入 A 的 UNMATCHED P2。"""
    put_event(
        alert_world,
        PaymentEventProcessingStatus.UNMATCHED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=event_type,
        error_code="PAYMENT_NOT_FOUND",
    )

    expect(alert_world, P1, segment(failed=1, disputes=1))


@pytest.mark.parametrize("event_type", CHARGE_TYPES)
def test_a_dispute_or_refund_not_yet_processed_is_counted(
    alert_world: World, event_type: str
) -> None:
    put_event(
        alert_world, PaymentEventProcessingStatus.RECEIVED, received_at=NOW, event_type=event_type
    )

    expect(alert_world, P1, segment(disputes=1))


def test_a_refund_after_a_close_without_credit_is_not_counted_but_a_dispute_is(
    alert_world: World,
) -> None:
    world = alert_world
    closed = review_payment(world, NOW - DAY * 2)
    close_by_review(world, closed)
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=REFUNDED,
        linked_to=payment_id(world, closed),
    )

    expect(world, OK)

    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=DISPUTED,
        linked_to=payment_id(world, closed),
    )

    expect(world, P1, segment(disputes=1))


@pytest.mark.parametrize(
    "linked", ["paid", "credited", "review", "unlinked"], ids=lambda value: value
)
def test_a_refund_on_any_other_payment_is_counted(alert_world: World, linked: str) -> None:
    """只有「不入账结案」的支付上的退款被排除；PAID（含人工按实付入账）与复核单上的照计。"""
    world = alert_world
    review = 0
    target: int | None = None
    if linked == "paid":
        target = payment_id(world, world.payment(world.customer(), status=PaymentStatus.PAID))
    elif linked == "credited":
        credited = review_payment(world, NOW - DAY * 2)
        credit_by_review(world, credited)
        target = payment_id(world, credited)
    elif linked == "review":
        target = payment_id(world, review_payment(world, NOW - DAY * 2))
        review = 1
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=REFUNDED,
        linked_to=target,
    )

    expect(world, P1, segment(review=review, disputes=1))


def test_other_event_types_are_not_disputes(alert_world: World) -> None:
    for event_type in ("checkout.session.completed", "charge.succeeded"):
        put_event(
            alert_world,
            PaymentEventProcessingStatus.PROCESSED,
            received_at=NOW - SECOND,
            processed_at=NOW - SECOND,
            event_type=event_type,
        )

    expect(alert_world, OK)


# --- P2：过期一小时仍 PENDING（#585） --------------------------------------------------------


@pytest.mark.parametrize(
    ("expires_at", "counted"),
    [
        (NOW - 3601 * SECOND, True),
        (NOW - 3600 * SECOND, True),
        (NOW - 3599 * SECOND, False),
        (NOW, False),
        (NOW + 3600 * SECOND, False),
        (NOW - dt.timedelta(days=6), True),
    ],
    ids=["3601s", "3600s", "3599s", "at-now", "not-expired", "6-days"],
)
def test_a_pending_payment_an_hour_past_its_expiry_is_p2(
    alert_world: World, expires_at: dt.datetime, counted: bool
) -> None:
    pending_payment(alert_world, expires_at)

    if counted:
        expect(alert_world, P2, segment(stale=1))
    else:
        expect(alert_world, OK)


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.EXPIRED,
        PaymentStatus.FAILED,
        PaymentStatus.CANCELLED,
        PaymentStatus.PAID,
    ],
    ids=lambda s: s.value,
)
def test_only_pending_payments_are_stale(alert_world: World, status: PaymentStatus) -> None:
    world = alert_world
    payment = world.payment(world.customer(), status=status)
    set_expiry(world, payment, NOW - dt.timedelta(days=2))

    expect(world, OK)


# --- P1：配置 -----------------------------------------------------------------------------


def _configured(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "payments_enabled": True,
        # 缺省的一对放在子目录里：用例自己写的（坏的）一对放在 tmp_path，不会被它覆盖。
        **secret_files(
            tmp_path / "usable", "sk_test_fictional_alert_key", "whsec_fictional_alert_secret"
        ),
        "stripe_api_base_url": API_BASE_URL,
        "frontend_base_url": FRONTEND_BASE_URL,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_a_complete_configuration_adds_nothing(
    alert_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_settings(monkeypatch, _configured(tmp_path))

    expect(alert_world, OK)


def test_live_keys_are_a_complete_configuration_too(
    alert_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    files = secret_files(tmp_path, "sk_live_fictional_alert_key", "whsec_fictional_alert_secret")
    use_settings(monkeypatch, _configured(tmp_path, **files))

    expect(alert_world, OK)


@pytest.mark.parametrize(
    "broken",
    [
        "secret-key-file-missing",
        "webhook-secret-file-missing",
        "secret-key-file-not-set",
        "secret-key-empty",
        "secret-key-prefix",
        "publishable-key",
        "webhook-secret-prefix",
        "api-base-url-empty",
        "frontend-base-url-empty",
    ],
)
def test_payments_on_with_an_unusable_configuration_is_p1(
    alert_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, broken: str
) -> None:
    overrides: dict[str, object] = {}
    if broken == "secret-key-file-missing":
        overrides["stripe_secret_key_file"] = str(tmp_path / "absent")
    elif broken == "webhook-secret-file-missing":
        overrides["stripe_webhook_secret_file"] = str(tmp_path / "absent")
    elif broken == "secret-key-file-not-set":
        overrides["stripe_secret_key_file"] = ""
    elif broken == "secret-key-empty":
        overrides.update(secret_files(tmp_path, "  \n", "whsec_fictional_alert_secret"))
    elif broken == "secret-key-prefix":
        overrides.update(secret_files(tmp_path, "rk_test_fictional", "whsec_fictional"))
    elif broken == "publishable-key":
        overrides.update(secret_files(tmp_path, "pk_test_fictional", "whsec_fictional"))
    elif broken == "webhook-secret-prefix":
        overrides.update(secret_files(tmp_path, "sk_test_fictional", "wh_fictional"))
    elif broken == "api-base-url-empty":
        overrides["stripe_api_base_url"] = ""
    elif broken == "frontend-base-url-empty":
        overrides["frontend_base_url"] = ""
    use_settings(monkeypatch, _configured(tmp_path, **overrides))

    expect(alert_world, P1, segment(config="missing"))


def test_an_unusable_configuration_is_fine_while_payments_are_off(
    alert_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_settings(
        monkeypatch,
        _configured(
            tmp_path,
            payments_enabled=False,
            stripe_secret_key_file="",
            stripe_api_base_url="",
            frontend_base_url="",
        ),
    )

    expect(alert_world, OK)


# --- 合并 ---------------------------------------------------------------------------------


def test_every_count_lands_in_one_paragraph_at_the_worst_level(
    alert_world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = alert_world
    review_payment(world, NOW - dt.timedelta(hours=2))
    review_payment(world, NOW - dt.timedelta(hours=3))
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        processed_at=NOW - dt.timedelta(hours=5),
        conflict_at=NOW - dt.timedelta(minutes=1),
    )
    put_event(world, PaymentEventProcessingStatus.RETRY, received_at=NOW - 1200 * SECOND)
    put_event(world, PaymentEventProcessingStatus.UNMATCHED, processed_at=NOW - SECOND)
    put_event(world, PaymentEventProcessingStatus.REJECTED, processed_at=NOW - SECOND)
    put_event(world, PaymentEventProcessingStatus.REJECTED, processed_at=NOW - SECOND)
    pending_payment(world, NOW - dt.timedelta(hours=2))
    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - dt.timedelta(hours=4),
        processed_at=NOW - dt.timedelta(hours=4),
        event_type=DISPUTED,
    )
    use_settings(monkeypatch, _configured(tmp_path, frontend_base_url=""))

    result = evaluate(world)

    assert result == AlertResult(
        P1,
        BASE + "; payments: review open 2, event conflicts 24h 1, oldest open event 1200s, "
        "unmatched or rejected 24h 3, stale pending 1, disputes or refunds 24h 1, config missing",
    )


def test_a_stale_pending_alone_is_p2_and_a_dispute_lifts_it_to_p1(alert_world: World) -> None:
    world = alert_world
    pending_payment(world, NOW - dt.timedelta(hours=2))
    put_event(world, PaymentEventProcessingStatus.UNMATCHED, processed_at=NOW - SECOND)

    expect(world, P2, segment(failed=1, stale=1))

    put_event(
        world,
        PaymentEventProcessingStatus.PROCESSED,
        received_at=NOW - SECOND,
        processed_at=NOW - SECOND,
        event_type=REFUNDED,
    )

    expect(world, P1, segment(failed=1, stale=1, disputes=1))


def test_payment_p2_does_not_hide_a_reconciliation_p1(alert_world: World) -> None:
    """级别取各部分最严重的：核对的 P1 不被支付的 P2 盖住，原有两部分文字不变。"""
    world = alert_world
    completed(world.factory, kuala_lumpur_date(NOW), NOW - dt.timedelta(minutes=30), inconsistent=1)
    put_event(world, PaymentEventProcessingStatus.UNMATCHED, processed_at=NOW)

    result = evaluate(world)

    assert result == AlertResult(
        P1,
        "no wallet below zero; reconciliation 2026-11-09 checked 3 inconsistent 1"
        + segment(failed=1),
    )


def test_the_endpoint_line_carries_the_payment_paragraph(alert_world: World) -> None:
    """`GET /internal/alerts` 的那一行：维度名、级别、整段摘要。"""
    world = alert_world
    review_payment(world, NOW)

    body = alerts.alert_lines(world.factory, ("USD",), clock=lambda: NOW)

    assert f"wallet_negative_balance P1 {BASE}{segment(review=1)}\n" in body
