"""Admin rebill endpoints, end to end (design gate #830 v12 §2「管理端接口」, §6, §7, AIH-TASK-256).

- **预览**：明细的全部字段（规则、价格版本、对账单是 `public_id`，供应商与模型是目录
  `code`）、合计、账本方向与余额预估、不写任何行；没有可入账的差额时 200 且方向与金额为 null；
- **确认**：201 与 Rebill 详情；同键同参重放 200 `replayed: true`；同键异参 409；
- **列表**：游标分页、按 `id` 倒序，翻页期间新增一条 Rebill 时后续页不重复、不漏；按客户筛选，
  不存在的客户是空页；`cursor` 不存在、`limit` 越界是 422；
- **详情**：两个合计、两个更正版本、账本行与全部明细（带所属 Rebill 的 id）；
- **错误码**：设计「管理端接口」错误码汇总里的每一个，HTTP 状态逐个核对；请求体校验；
- **越权**：未登录 401、门户（CUSTOMER）会话 403，且不写库；每个处理函数第一条语句是
  `require_admin(request)`（AST）。

世界见 tests/backend/rebill_world.py（SQLite 与真 MySQL 各一遍；MySQL 那一半在
`BILLING_TEST_DATABASE_URL` 未设置时 skip —— **不要把 skipped 读成 passed**，CI 设了它）。
金额都选二进制精确的值：默认数量按 `V_BASE`、`R_GLOBAL` 计费 4.5、估算成本 2.25，`R_LOW`（×1.5）
是 3.375，`V_HIGH`（单价翻倍）是 9。确认的时钟固定；uuid 是全零占位。
"""

from __future__ import annotations

import ast
import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from provider_cost_world import zero_uuid
from rebill_world import (
    A1,
    A2,
    B1,
    INTERNAL,
    R_GLOBAL,
    R_HUGE,
    R_LOW,
    R_OTHER_CUSTOMER,
    V_BASE,
    V_HIGH,
    V_OTHER_MODEL,
    ZERO_TOKENS,
    A,
    B,
    EventRef,
    LineSpec,
    RebillApi,
    RebillRef,
    RebillWorld,
    api_factory,
    build_world,
    rebill_api,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.api import admin_rebills
from app.models.auth import AuditAction, AuditLog, DomainOutbox
from app.models.statements import MonthlyStatement
from app.models.tenancy import AccountStatus, Tenant
from app.models.usage import UsageEventStatus
from app.models.usage_rebills import UsageRebill, UsageRebillLine
from app.models.wallet import ReferenceType, TransactionType, WalletTransaction
from app.repositories.wallet import post_transaction

REBILLS: Final = "/api/v1/admin/rebills"
PREVIEW: Final = REBILLS + "/preview"
START: Final = dt.datetime(2026, 11, 10, 0, 0, 0)
END: Final = dt.datetime(2026, 11, 20, 0, 0, 0)
HOUR: Final = dt.timedelta(hours=1)
FUNDED_AT: Final = dt.datetime(2026, 10, 2, 0, 0, 0)
# 确认的时钟：带小数秒，验证截到整秒。
NOW: Final = dt.datetime(2026, 11, 25, 3, 0, 0, 500000)
REASON: Final = "  Fictional price correction  "
UNKNOWN: Final = zero_uuid(999_999)
# 只有输出 token：0.25 USD → 1.125 MYR（估算成本）→ ×2 = 2.25；R_LOW 下 1.6875。
OUTPUT_ONLY: Final = (0, 1000, 0, 0)

LINE_FIELDS: Final = {
    "usage_event_id",
    "occurred_at",
    "project",
    "provider",
    "model",
    "previous_charge",
    "corrected_charge",
    "difference",
    "previous_pricing_rule",
    "corrected_pricing_rule",
    "previous_provider_price_version",
    "corrected_provider_price_version",
    "corrected_estimated_provider_cost_myr",
    "original_period",
    "original_statement_id",
}
PREVIEW_FIELDS: Final = {
    "lines",
    "unchanged_count",
    "event_count",
    "previous_total",
    "corrected_total",
    "difference",
    "ledger_type",
    "ledger_amount",
    "balance",
    "balance_after",
    "preview_token",
}
LIST_FIELDS: Final = {
    "id",
    "customer_id",
    "event_count",
    "difference",
    "ledger_type",
    "ledger_amount",
    "reason",
    "created_by_email",
    "created_at",
}
DETAIL_FIELDS: Final = LIST_FIELDS | {
    "previous_total",
    "corrected_total",
    "corrective_pricing_rule_id",
    "corrective_provider_price_version_id",
    "wallet_transaction_id",
    "lines",
}
CONFIRM_FIELDS: Final = DETAIL_FIELDS | {"replayed"}


# --- 夹具 -----------------------------------------------------------------------------


@pytest.fixture(params=["sqlite", "mysql"])
def api_db(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from api_factory(request.param, monkeypatch)


@dataclass
class Scene:
    world: RebillWorld
    api: RebillApi

    def post(self, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
        return self.api.client.post(
            path, json=body, headers=self.api.admin if headers is None else headers
        )

    def get(self, path: str, headers: dict[str, str] | None = None, **params: Any) -> Any:
        return self.api.client.get(
            path, params=params, headers=self.api.admin if headers is None else headers
        )


@pytest.fixture
def scene(tmp_path, api_db, monkeypatch) -> Scene:
    world = build_world(api_db)
    monkeypatch.setattr(admin_rebills, "utc_now", lambda: NOW)
    return Scene(world, rebill_api(tmp_path, world))


# --- 辅助 -----------------------------------------------------------------------------


def _fund(w: RebillWorld, customer: str, amount: str) -> None:
    with w.factory() as session:
        post_transaction(
            session,
            tenant_id=w.customers[customer].tenant_id,
            transaction_type=TransactionType.ADJUSTMENT_CREDIT,
            amount=Decimal(amount),
            reference_type=ReferenceType.ADMIN_ADJUSTMENT,
            reference_id=zero_uuid(w.number()),
            now=FUNDED_AT,
            created_by=w.admin_id,
            description="Fictional funding",
        )
        session.commit()


def _key(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _criteria(**extra: Any) -> dict[str, Any]:
    return {"occurred_from": "2026-11-10T00:00:00Z", "occurred_to": "2026-11-20T00:00:00Z", **extra}


def preview_body(
    w: RebillWorld,
    customer: str = A,
    *,
    selection: dict[str, Any] | None = None,
    events: list[EventRef] | None = None,
    rule: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    if selection is None:
        selection = (
            {"usage_event_ids": [item.public_id for item in events]}
            if events is not None
            else _criteria()
        )
    body: dict[str, Any] = {"customer_id": w.customers[customer].public_id, "selection": selection}
    if rule is not None:
        body["corrective_pricing_rule_id"] = w.public_rule(rule)
    if version is not None:
        body["corrective_provider_price_version_id"] = w.public_version(version)
    return body


def confirm_body(
    preview: dict[str, Any], token: str, *, key: int = 1, reason: str = REASON
) -> dict[str, Any]:
    return {**preview, "preview_token": token, "reason": reason, "idempotency_key": _key(key)}


def error(response: Any) -> tuple[int, str | None]:
    return response.status_code, (response.json().get("error") or {}).get("code")


def message(response: Any) -> str:
    return response.json()["error"]["message"]


def _count(w: RebillWorld, model: Any, *conditions: Any) -> int:
    with w.factory() as session:
        return int(
            session.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()
        )


def footprint(w: RebillWorld) -> tuple[int, ...]:
    """Every table a confirm writes: nothing may change when a request fails."""
    return (
        _count(w, WalletTransaction),
        _count(w, UsageRebill),
        _count(w, UsageRebillLine),
        _count(w, AuditLog),
        _count(w, DomainOutbox),
    )


def rebill_ledger_rows(w: RebillWorld) -> list[WalletTransaction]:
    with w.factory() as session:
        return list(
            session.execute(
                select(WalletTransaction)
                .where(WalletTransaction.reference_type == ReferenceType.REBILL)
                .order_by(WalletTransaction.id)
            ).scalars()
        )


def previewed(scene: Scene, body: dict[str, Any]) -> dict[str, Any]:
    response = scene.post(PREVIEW, body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def confirmed(scene: Scene, body: dict[str, Any]) -> dict[str, Any]:
    response = scene.post(REBILLS, body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def preview_and_confirm(scene: Scene, body: dict[str, Any], *, key: int = 1) -> dict[str, Any]:
    token = previewed(scene, body)["preview_token"]
    return confirmed(scene, confirm_body(body, token, key=key))


def statement_public_id(w: RebillWorld, statement_id: int) -> str:
    with w.factory() as session:
        row = session.get(MonthlyStatement, statement_id)
        assert row is not None
        return row.public_id


# --- 预览 -----------------------------------------------------------------------------


def test_preview_shows_every_changed_event_and_writes_nothing(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    first = w.add_event(project=A1, occurred_at=START)
    second = w.add_event(project=A2, occurred_at=START + HOUR, tokens=OUTPUT_ONLY)
    w.add_event(project=A1, occurred_at=START + 2 * HOUR, tokens=ZERO_TOKENS)
    before = footprint(w)

    data = previewed(scene, preview_body(w, rule=R_LOW))

    assert set(data) == PREVIEW_FIELDS
    for line in data["lines"]:
        assert set(line) == LINE_FIELDS
    assert data["lines"][0] == {
        "usage_event_id": first.public_id,
        "occurred_at": "2026-11-10T00:00:00",
        "project": w.projects[A1].public_id,
        "provider": "anthropic",
        "model": "claude-test",
        "previous_charge": "4.50000000",
        "corrected_charge": "3.37500000",
        "difference": "-1.12500000",
        "previous_pricing_rule": w.public_rule(R_GLOBAL),
        "corrected_pricing_rule": w.public_rule(R_LOW),
        "previous_provider_price_version": w.public_version(V_BASE),
        "corrected_provider_price_version": w.public_version(V_BASE),
        "corrected_estimated_provider_cost_myr": "2.25000000",
        "original_period": "2026-11",
        "original_statement_id": None,
    }
    second_line = data["lines"][1]
    assert second_line["usage_event_id"] == second.public_id
    assert second_line["project"] == w.projects[A2].public_id
    assert (second_line["previous_charge"], second_line["corrected_charge"]) == (
        "2.25000000",
        "1.68750000",
    )
    assert second_line["corrected_estimated_provider_cost_myr"] == "1.12500000"
    # 计费额为 0 的事件按 R_LOW 仍是 0：不进明细，也不进合计（设计 v9）。
    assert (data["unchanged_count"], data["event_count"]) == (1, 2)
    assert (data["previous_total"], data["corrected_total"], data["difference"]) == (
        "6.75000000",
        "5.06250000",
        "-1.68750000",
    )
    assert (data["ledger_type"], data["ledger_amount"]) == ("REBILL_CREDIT", "1.68750000")
    # 100 − 4.5 − 2.25；确认后多 1.6875。
    assert (data["balance"], data["balance_after"]) == ("93.25000000", "94.93750000")
    token = data["preview_token"]
    assert len(token) == 64 and token == token.lower()
    int(token, 16)
    assert footprint(w) == before


def test_a_preview_with_nothing_to_post_is_200_with_null_ledger_fields(scene: Scene) -> None:
    """设计 v7：没有可入账的差额时预览 200，方向与金额为 null；确认才 409。"""
    w = scene.world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, events=[event])

    data = previewed(scene, body)

    assert data["lines"] == []
    assert (data["unchanged_count"], data["event_count"]) == (1, 0)
    assert (data["previous_total"], data["corrected_total"], data["difference"]) == (
        "0.00000000",
        "0.00000000",
        "0.00000000",
    )
    assert (data["ledger_type"], data["ledger_amount"]) == (None, None)
    assert data["balance_after"] == data["balance"] == "95.50000000"
    before = footprint(w)
    response = scene.post(REBILLS, confirm_body(body, data["preview_token"]))
    assert error(response) == (409, "NOTHING_TO_REBILL")
    assert footprint(w) == before


def test_corrections_that_cancel_out_preview_fine_and_refuse_to_confirm(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    up = w.add_event(project=A1, occurred_at=START)
    down = w.add_event(project=A1, occurred_at=START + HOUR)
    # 当前有效计费分别是 4.75 与 4.25（版本不变），按当前有效版本重算回 4.5。
    w.add_rebill(A, [LineSpec(up, "4.75")], created_at=END)
    w.add_rebill(A, [LineSpec(down, "4.25")], created_at=END)
    body = preview_body(w, events=[up, down])

    data = previewed(scene, body)

    assert [line["difference"] for line in data["lines"]] == ["-0.25000000", "0.25000000"]
    assert (data["event_count"], data["difference"]) == (2, "0.00000000")
    assert (data["ledger_type"], data["ledger_amount"]) == (None, None)
    assert data["balance_after"] == data["balance"]
    before = footprint(w)
    response = scene.post(REBILLS, confirm_body(body, data["preview_token"]))
    assert error(response) == (409, "REBILL_NET_ZERO")
    assert footprint(w) == before


def test_occurred_times_accept_microseconds_and_any_zone(scene: Scene) -> None:
    """RFC 3339 带时区，小数秒到微秒，规范化为 6 位微秒的 UTC（设计 v7）。"""
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    zoned = preview_body(
        w,
        rule=R_LOW,
        selection={
            "occurred_from": "2026-11-10T08:00:00+08:00",
            "occurred_to": "2026-11-20T00:00:00.5Z",
        },
    )
    canonical = preview_body(
        w,
        rule=R_LOW,
        selection={
            "occurred_from": "2026-11-10T00:00:00.000000Z",
            "occurred_to": "2026-11-20T00:00:00.500000Z",
        },
    )

    token = previewed(scene, zoned)["preview_token"]

    # 指纹的输入是规范化的 selection：两种写法得到同一个指纹，可以交叉确认。
    assert previewed(scene, canonical)["preview_token"] == token
    result = confirmed(scene, confirm_body(canonical, token))
    with w.factory() as session:
        audit = session.execute(
            select(AuditLog).where(AuditLog.action == AuditAction.REBILL)
        ).scalar_one()
    assert json.loads(audit.after_state or "")["selection"] == {
        "occurred_from": "2026-11-10T00:00:00.000000Z",
        "occurred_to": "2026-11-20T00:00:00.500000Z",
        "project_id": None,
        "provider": None,
        "model": None,
    }
    assert audit.entity_id == result["id"]


def test_criteria_filter_on_project_provider_and_model(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    in_a1 = w.add_event(project=A1, occurred_at=START)
    w.add_event(project=A2, occurred_at=START + HOUR)
    selection = _criteria(
        project_id=w.projects[A1].public_id, provider="anthropic", model="claude-test"
    )

    data = previewed(scene, preview_body(w, rule=R_LOW, selection=selection))

    assert [line["usage_event_id"] for line in data["lines"]] == [in_a1.public_id]
    other_model = _criteria(model="claude-other")
    response = scene.post(PREVIEW, preview_body(w, rule=R_LOW, selection=other_model))
    assert error(response) == (409, "NOTHING_TO_REBILL")


# --- 确认与重放 ---------------------------------------------------------------------------


def test_confirm_posts_once_and_the_same_key_replays(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    w.add_event(project=A2, occurred_at=START + HOUR, tokens=OUTPUT_ONLY)
    body = preview_body(w, rule=R_LOW)
    preview = previewed(scene, body)
    request = confirm_body(body, preview["preview_token"])

    response = scene.post(REBILLS, request)

    assert response.status_code == 201, response.text
    first = response.json()["data"]
    assert set(first) == CONFIRM_FIELDS
    [ledger] = rebill_ledger_rows(w)
    assert first["replayed"] is False
    assert first["customer_id"] == w.customers[A].public_id
    assert (first["event_count"], first["difference"]) == (2, "-1.68750000")
    assert (first["previous_total"], first["corrected_total"]) == ("6.75000000", "5.06250000")
    assert (first["ledger_type"], first["ledger_amount"]) == ("REBILL_CREDIT", "1.68750000")
    assert first["reason"] == REASON.strip()
    assert first["created_by_email"] == w.admin.email
    assert first["created_at"] == "2026-11-25T03:00:00"
    assert first["corrective_pricing_rule_id"] == w.public_rule(R_LOW)
    assert first["corrective_provider_price_version_id"] is None
    assert first["wallet_transaction_id"] == ledger.public_id
    # 明细的字段与预览明细相同，另加所属 Rebill 的 id。
    assert first["lines"] == [{**line, "rebill_id": first["id"]} for line in preview["lines"]]
    assert ledger.reference_id == first["id"]
    assert ledger.amount == Decimal("1.6875")
    # INV-7：账本 metadata 只有 Rebill id 与事件数。
    assert ledger.metadata_json == {"rebill": first["id"], "events": 2}

    again = scene.post(REBILLS, request)

    assert again.status_code == 200, again.text
    assert again.json()["data"] == {**first, "replayed": True}
    assert len(rebill_ledger_rows(w)) == 1
    before = footprint(w)
    different = scene.post(REBILLS, {**request, "reason": "A different reason"})
    assert error(different) == (409, "REBILL_CONFLICT")
    assert footprint(w) == before


def test_another_rebill_after_the_preview_makes_it_stale(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    event = w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, rule=R_LOW)
    token = previewed(scene, body)["preview_token"]
    preview_and_confirm(scene, preview_body(w, events=[event], version=V_HIGH), key=2)
    before = footprint(w)

    response = scene.post(REBILLS, confirm_body(body, token))

    assert error(response) == (409, "REBILL_PREVIEW_STALE")
    assert footprint(w) == before
    # 重新预览之后照常确认：以第一次的更正计费 9 为基准。
    fresh = previewed(scene, body)
    assert fresh["lines"][0]["previous_charge"] == "9.00000000"
    confirmed(scene, confirm_body(body, fresh["preview_token"], key=3))


def test_a_new_event_in_the_range_makes_the_preview_stale(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, rule=R_LOW)
    token = previewed(scene, body)["preview_token"]
    w.add_event(project=A1, occurred_at=START + HOUR)
    before = footprint(w)

    response = scene.post(REBILLS, confirm_body(body, token))

    assert error(response) == (409, "REBILL_PREVIEW_STALE")
    assert footprint(w) == before


def test_a_closed_account_is_409_and_nothing_is_written(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, rule=R_LOW)
    token = previewed(scene, body)["preview_token"]
    with w.factory() as session:
        session.execute(
            update(Tenant)
            .where(Tenant.id == w.customers[A].tenant_id)
            .values(account_status=AccountStatus.CLOSED)
        )
        session.commit()
    before = footprint(w)

    response = scene.post(REBILLS, confirm_body(body, token))

    assert error(response) == (409, "ACCOUNT_CLOSED")
    assert footprint(w) == before


# --- 详情 -----------------------------------------------------------------------------


def test_the_detail_has_the_totals_the_references_and_every_line(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    october = w.add_event(project=A1, occurred_at=dt.datetime(2026, 10, 15, 0, 0, 0))
    statement = statement_public_id(w, w.add_statement(A, "2026-10"))
    november = w.add_event(project=A2, occurred_at=START)
    body = preview_body(w, events=[november, october], rule=R_LOW, version=V_HIGH)
    result = preview_and_confirm(scene, body)

    response = scene.get(f"{REBILLS}/{result['id']}")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == DETAIL_FIELDS
    assert data == {key: value for key, value in result.items() if key != "replayed"}
    assert (data["previous_total"], data["corrected_total"], data["difference"]) == (
        "9.00000000",
        "13.50000000",
        "4.50000000",
    )
    assert (data["ledger_type"], data["ledger_amount"]) == ("REBILL_DEBIT", "-4.50000000")
    assert data["corrective_pricing_rule_id"] == w.public_rule(R_LOW)
    assert data["corrective_provider_price_version_id"] == w.public_version(V_HIGH)
    [ledger] = rebill_ledger_rows(w)
    assert data["wallet_transaction_id"] == ledger.public_id
    # 明细按明细 id（= 事件 id）升序：十月的事件先建。
    assert [line["usage_event_id"] for line in data["lines"]] == [
        october.public_id,
        november.public_id,
    ]
    for line in data["lines"]:
        assert set(line) == LINE_FIELDS | {"rebill_id"}
        assert line["rebill_id"] == data["id"]
        assert (line["previous_charge"], line["corrected_charge"]) == ("4.50000000", "6.75000000")
        assert line["corrected_pricing_rule"] == w.public_rule(R_LOW)
        assert line["corrected_provider_price_version"] == w.public_version(V_HIGH)
        assert line["previous_provider_price_version"] == w.public_version(V_BASE)
        # V_HIGH 单价翻倍：估算成本 4.5。
        assert line["corrected_estimated_provider_cost_myr"] == "4.50000000"
    assert (data["lines"][0]["original_period"], data["lines"][0]["original_statement_id"]) == (
        "2026-10",
        statement,
    )
    assert (data["lines"][1]["original_period"], data["lines"][1]["original_statement_id"]) == (
        "2026-11",
        None,
    )


def test_an_unknown_rebill_is_404(scene: Scene) -> None:
    response = scene.get(f"{REBILLS}/{UNKNOWN}")

    assert error(response) == (404, "REBILL_NOT_FOUND")


# --- 列表 -----------------------------------------------------------------------------


def _direct_rebill(w: RebillWorld, customer: str, project: str, hour: int) -> RebillRef:
    event = w.add_event(project=project, occurred_at=START + hour * HOUR)
    return w.add_rebill(customer, [LineSpec(event, "3.5")])


def _page(scene: Scene, **params: Any) -> tuple[list[str], str | None]:
    response = scene.get(REBILLS, **params)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert set(data) == {"items", "next_cursor"}
    return [item["id"] for item in data["items"]], data["next_cursor"]


def test_the_list_pages_by_cursor_newest_first_without_repeats_or_gaps(scene: Scene) -> None:
    w = scene.world
    made = [
        _direct_rebill(w, customer, project, hour)
        for hour, (customer, project) in enumerate([(A, A1), (B, B1), (A, A2), (B, B1), (A, A1)])
    ]
    newest_first = [item.public_id for item in reversed(made)]

    first, cursor = _page(scene, limit=2)
    assert first == newest_first[:2]
    assert cursor == first[-1]
    # 翻页期间新增一条：只会出现在第一页之前，不让后续页重复或漏看。
    added = _direct_rebill(w, A, A2, 9)
    second, cursor = _page(scene, limit=2, cursor=cursor)
    assert second == newest_first[2:4]
    assert cursor == second[-1]
    third, cursor = _page(scene, limit=2, cursor=cursor)
    assert third == newest_first[4:]
    assert cursor is None
    assert first + second + third == newest_first

    fresh, _ = _page(scene, limit=2)
    assert fresh == [added.public_id, newest_first[0]]
    everything, cursor = _page(scene)
    assert (everything, cursor) == ([added.public_id, *newest_first], None)


def test_the_list_items_carry_the_documented_fields(scene: Scene) -> None:
    w = scene.world
    made = _direct_rebill(w, A, A1, 0)

    response = scene.get(REBILLS)

    [item] = response.json()["data"]["items"]
    assert set(item) == LIST_FIELDS
    assert item == {
        "id": made.public_id,
        "customer_id": w.customers[A].public_id,
        "event_count": 1,
        "difference": "-1.00000000",
        "ledger_type": "REBILL_CREDIT",
        "ledger_amount": "1.00000000",
        "reason": "Fictional rebill for a test",
        "created_by_email": w.admin.email,
        "created_at": "2026-12-20T00:00:00",
    }


def test_the_list_filters_on_a_customer(scene: Scene) -> None:
    w = scene.world
    of_a = [_direct_rebill(w, A, A1, 0), _direct_rebill(w, A, A2, 2)]
    of_b = _direct_rebill(w, B, B1, 1)

    assert _page(scene, customer_id=w.customers[A].public_id) == (
        [of_a[1].public_id, of_a[0].public_id],
        None,
    )
    assert _page(scene, customer_id=w.customers[B].public_id) == ([of_b.public_id], None)
    # 不存在的客户：空页（照 GET /api/v1/admin/usage-events 的同名筛选），不报 404。
    assert _page(scene, customer_id=UNKNOWN) == ([], None)
    assert _page(scene, customer_id=w.customers[INTERNAL].public_id) == ([], None)
    # 客户筛选与游标一起用。
    assert _page(scene, customer_id=w.customers[A].public_id, limit=1) == (
        [of_a[1].public_id],
        of_a[1].public_id,
    )
    assert _page(
        scene, customer_id=w.customers[A].public_id, limit=1, cursor=of_a[1].public_id
    ) == ([of_a[0].public_id], None)


@pytest.mark.parametrize(
    "params",
    [{"cursor": UNKNOWN}, {"limit": 0}, {"limit": 101}, {"limit": "many"}],
    ids=["unknown-cursor", "limit-0", "limit-101", "limit-text"],
)
def test_list_query_validation(scene: Scene, params: dict[str, Any]) -> None:
    _direct_rebill(scene.world, A, A1, 0)

    response = scene.get(REBILLS, **params)

    assert error(response) == (422, "VALIDATION_ERROR")


def test_the_cursor_is_checked_after_authentication(scene: Scene) -> None:
    response = scene.get(REBILLS, headers={}, cursor=UNKNOWN)

    assert error(response) == (401, "TOKEN_INVALID")


# --- 错误码 ---------------------------------------------------------------------------


def _both(scene: Scene, body: dict[str, Any]) -> list[tuple[int, str | None]]:
    """The error of a preview and of a confirm of the same body (nothing written)."""
    w = scene.world
    before = footprint(w)
    found = [
        error(scene.post(PREVIEW, body)),
        error(scene.post(REBILLS, confirm_body(body, "0" * 64))),
    ]
    assert footprint(w) == before
    return found


def test_lookups_that_find_nothing_are_404(scene: Scene) -> None:
    w = scene.world
    w.add_event(project=A1, occurred_at=START)
    cases = [
        ({"customer_id": UNKNOWN, "selection": _criteria()}, "CUSTOMER_NOT_FOUND"),
        (preview_body(w, selection={"usage_event_ids": [UNKNOWN]}), "USAGE_EVENT_NOT_FOUND"),
        (
            preview_body(w, selection=_criteria(project_id=w.projects[B1].public_id)),
            "PROJECT_NOT_FOUND",
        ),
        ({**preview_body(w), "corrective_pricing_rule_id": UNKNOWN}, "PRICING_RULE_NOT_FOUND"),
        (
            {**preview_body(w), "corrective_provider_price_version_id": UNKNOWN},
            "PRICE_VERSION_NOT_FOUND",
        ),
    ]
    for body, code in cases:
        assert _both(scene, body) == [(404, code)] * 2, code


def test_conflicts_are_409(scene: Scene) -> None:
    w = scene.world
    foreign = w.add_event(project=B1, occurred_at=START)
    pending = w.add_event(project=A1, occurred_at=START, status=UsageEventStatus.RECEIVED)

    assert _both(scene, preview_body(w, INTERNAL, rule=R_LOW)) == [(409, "REBILL_NOT_PREPAID")] * 2
    # INV-8：别的客户的事件 409，不是 404；消息只带事件的 public_id。
    for event in (foreign, pending):
        body = preview_body(w, events=[event])
        assert _both(scene, body) == [(409, "USAGE_EVENT_NOT_REBILLABLE")] * 2
        assert event.public_id in message(scene.post(PREVIEW, body))
    # 条件范围里没有可选事件：预览与确认都是 409。
    empty = preview_body(w, rule=R_LOW, selection=_criteria(occurred_to="2026-11-10T00:00:01Z"))
    assert _both(scene, empty) == [(409, "NOTHING_TO_REBILL")] * 2


def test_too_large_selections_and_inapplicable_versions_are_422(scene: Scene) -> None:
    w = scene.world
    event = w.add_event(project=A1, occurred_at=START)

    for body, code in [
        (preview_body(w, events=[event], version=V_OTHER_MODEL), "REBILL_PRICE_NOT_APPLICABLE"),
        # INV-8：别的客户的规则 422。
        (preview_body(w, events=[event], rule=R_OTHER_CUSTOMER), "REBILL_RULE_NOT_APPLICABLE"),
        (preview_body(w, events=[event], rule=R_HUGE), "AMOUNT_OUT_OF_RANGE"),
    ]:
        assert _both(scene, body) == [(422, code)] * 2, code
        if code != "AMOUNT_OUT_OF_RANGE":
            # 第一个不适用事件的 public_id 只在消息里，不加响应字段。
            assert event.public_id in message(scene.post(PREVIEW, body))

    w.add_zero_events(
        project=A1, moments=[START + dt.timedelta(seconds=n + 1) for n in range(1000)]
    )
    response = scene.post(PREVIEW, preview_body(w))
    assert error(response) == (422, "REBILL_SELECTION_TOO_LARGE")
    assert message(response) == "Selection matches 1001 usage events; at most 1000."
    assert response.json()["data"] is None


# --- 请求体校验 ------------------------------------------------------------------------

_FROM: Final = "2026-11-10T00:00:00Z"
_TO: Final = "2026-11-20T00:00:00Z"
_BAD_SELECTIONS: Final = {
    "both-shapes": {"usage_event_ids": [UNKNOWN], "occurred_from": _FROM, "occurred_to": _TO},
    "ids-with-a-criterion": {"usage_event_ids": [UNKNOWN], "provider": "anthropic"},
    "neither": {},
    "only-from": {"occurred_from": _FROM},
    "only-to": {"occurred_to": _TO},
    "empty-ids": {"usage_event_ids": []},
    "too-many-ids": {"usage_event_ids": [zero_uuid(n) for n in range(1, 1002)]},
    "repeated-ids": {"usage_event_ids": [UNKNOWN, UNKNOWN]},
    "ids-not-strings": {"usage_event_ids": [1]},
    "no-zone": {"occurred_from": "2026-11-10T00:00:00", "occurred_to": _TO},
    "seven-fraction-digits": {"occurred_from": "2026-11-10T00:00:00.0000001Z", "occurred_to": _TO},
    "not-a-time": {"occurred_from": "2026-13-40T00:00:00Z", "occurred_to": _TO},
    "date-only": {"occurred_from": "2026-11-10", "occurred_to": _TO},
    "a-number": {"occurred_from": 1, "occurred_to": _TO},
    "from-equals-to": {"occurred_from": _FROM, "occurred_to": "2026-11-10T08:00:00+08:00"},
    "from-after-to": {"occurred_from": _TO, "occurred_to": _FROM},
    "empty-project": {"occurred_from": _FROM, "occurred_to": _TO, "project_id": ""},
    "unknown-key": {"occurred_from": _FROM, "occurred_to": _TO, "customer": "x"},
}


@pytest.mark.parametrize("path", [PREVIEW, REBILLS])
@pytest.mark.parametrize("name", sorted(_BAD_SELECTIONS))
def test_a_bad_selection_is_422(scene: Scene, path: str, name: str) -> None:
    w = scene.world
    w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, rule=R_LOW, selection=_BAD_SELECTIONS[name])
    if path == REBILLS:
        body = confirm_body(body, "0" * 64)
    before = footprint(w)

    response = scene.post(path, body)

    assert error(response) == (422, "VALIDATION_ERROR")
    assert footprint(w) == before


_BAD_FIELDS: Final = {
    "no-customer": {"customer_id": None},
    "empty-customer": {"customer_id": ""},
    "a-manual-amount": {"amount": "1.00"},
    "a-tenant-id": {"tenant_id": 1},
    "empty-rule": {"corrective_pricing_rule_id": ""},
    "token-uppercase": {"preview_token": "A" * 64},
    "token-short": {"preview_token": "0" * 63},
    "token-not-hex": {"preview_token": "g" * 64},
    "token-missing": {"preview_token": None},
    "reason-empty": {"reason": ""},
    "reason-spaces": {"reason": "   "},
    "reason-control-whitespace": {"reason": "\t\n\r"},
    # 数据库兜不住的非 ASCII 空白由请求校验拒绝（设计 v12）。
    "reason-no-break-space": {"reason": "  "},
    "reason-too-long": {"reason": "x" * 256},
    "key-uppercase": {"idempotency_key": "00000000-0000-4000-8000-00000000000A"},
    "key-not-uuid": {"idempotency_key": "not-a-uuid"},
    "key-missing": {"idempotency_key": None},
}


@pytest.mark.parametrize("name", sorted(_BAD_FIELDS))
def test_a_bad_confirm_field_is_422(scene: Scene, name: str) -> None:
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    preview = preview_body(w, rule=R_LOW)
    body = confirm_body(preview, previewed(scene, preview)["preview_token"])
    for field, value in _BAD_FIELDS[name].items():
        if value is None:
            body.pop(field, None)
        else:
            body[field] = value
    before = footprint(w)

    response = scene.post(REBILLS, body)

    assert error(response) == (422, "VALIDATION_ERROR")
    assert footprint(w) == before


def test_a_reason_of_exactly_255_characters_after_trimming_is_accepted(scene: Scene) -> None:
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    body = preview_body(w, rule=R_LOW)
    token = previewed(scene, body)["preview_token"]

    result = confirmed(scene, confirm_body(body, token, reason=f"  {'x' * 255}  "))

    assert result["reason"] == "x" * 255


# --- 越权 -----------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["anonymous", "customer"])
@pytest.mark.parametrize("route", ["preview", "confirm", "list", "detail"])
def test_only_an_admin_may_call(scene: Scene, caller: str, route: str) -> None:
    """INV-8：匿名 401、门户会话 403，且不写库；请求本身合法，漏了鉴权就会真的成功。"""
    w = scene.world
    _fund(w, A, "100")
    w.add_event(project=A1, occurred_at=START)
    made = _direct_rebill(w, A, A2, 1)
    body = preview_body(w, rule=R_LOW)
    token = previewed(scene, body)["preview_token"]
    headers = {} if caller == "anonymous" else scene.api.customer
    before = footprint(w)

    if route == "preview":
        response = scene.post(PREVIEW, body, headers=headers)
    elif route == "confirm":
        response = scene.post(REBILLS, confirm_body(body, token), headers=headers)
    elif route == "list":
        response = scene.get(REBILLS, headers=headers)
    else:
        response = scene.get(f"{REBILLS}/{made.public_id}", headers=headers)

    expected = (401, "TOKEN_INVALID") if caller == "anonymous" else (403, "ADMIN_REQUIRED")
    assert error(response) == expected
    assert response.json()["data"] is None
    assert footprint(w) == before


_MODULE: Final = Path(__file__).resolve().parents[2] / "app" / "api" / "admin_rebills.py"


def _handlers() -> list[ast.FunctionDef]:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            for decorator in node.decorator_list
        )
    ]


def test_every_handler_starts_with_require_admin() -> None:
    handlers = _handlers()
    # 预览、确认、列表、详情；0 个说明 AST 扫描什么都没找到。
    assert len(handlers) == 4

    for handler in handlers:
        body = handler.body
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        statement = body[0]
        value = statement.value if isinstance(statement, ast.Assign | ast.Expr) else None
        assert isinstance(value, ast.Call), handler.name
        assert isinstance(value.func, ast.Name) and value.func.id == "require_admin"
        assert [ast.unparse(arg) for arg in value.args] == ["request"]


def test_the_paths_say_rebills_not_reprocess(scene: Scene) -> None:
    paths = {
        path
        for path in scene.api.client.app.openapi()["paths"]
        if path.startswith("/api/v1/admin/rebills")
    }

    assert paths == {REBILLS, PREVIEW, REBILLS + "/{rebill_id}"}
    assert not any("reprocess" in path for path in paths)
