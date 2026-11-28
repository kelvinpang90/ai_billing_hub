"""Checks on a database restored from backup (spec §98.1; runbook「整台 VPS 没了」→「恢复后对账」).

`python -m app.cli restore-verify --database <恢复库>` 调用这里；在恢复主机上经
`deploy/restore_verify.sh` 运行。

⚠️ **只读**：每一项都在自己的会话里读完就回滚，不写任何表、不写审计。对着恢复库跑，不和生产比：
恢复主机上没有生产库可比，而生产在恢复期间照常在写，比出来的差异说明不了什么（每周自动演练
`deploy/restore_drill.sh` 的说明里有同一条）。这里只回答「恢复出来的库自己是否完整、能不能用」。

查什么（spec §98.1 点名的钱包、账本、用量事件、支付、对账单、文档，加上主密钥）：

1. 迁移版本等于镜像里代码的 head：恢复库能直接交给这个版本的代码
2. 每个钱包与它的账本一致：用每日核对同一个判定 `check_wallet_snapshot`，问题码见那里
3. 用量事件、支付、对账单、待投递事件按状态的行数，收据、账本、审计、账号的行数：留作对账记录，
   不判对错
4. 每一份收据、每一份已定稿对账单都能从快照渲染出 PDF：文档就是快照，渲染不出等于文档丢了
5. 每一条密文都能用这把主密钥解开：TOTP、集成凭据、状态 webhook 签名密钥
6. 几张主要表最晚一行的时刻：最晚的那个减去故障时刻，就是这次实测的 RPO

⚠️ **输出不含任何密钥、明文、金额、公司名与邮箱**：失败只带行的 `public_id`（没有的用 id）与问题码或
异常类名。这份输出要贴进演练记录与 PR 描述，而仓库是公开的。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.crypto import MasterKeyring, decrypt_secret
from app.models.auth import AuditLog, DomainOutbox, TwoFactorSetting, User
from app.models.integration import IntegrationCredential, ProjectWebhookSecret
from app.models.payments import Payment
from app.models.receipts import PaymentReceipt
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tenancy import Project, Tenant
from app.models.usage import UsageEvent
from app.models.wallet import WalletTransaction
from app.repositories.wallet import check_wallet_snapshot
from app.services.integration_auth import credential_aad
from app.services.receipt_pdf import render_receipt_pdf
from app.services.statement_pdf import render_statement_pdf
from app.services.webhook_signing import webhook_secret_aad

_PDF_MAGIC: Final = b"%PDF-"


@dataclass
class Report:
    """What the checks saw. `failures` 为空才算通过；`facts` 是要留进记录的数字。"""

    facts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def verify(factory: sessionmaker[Session], *, keyring: MasterKeyring, expected_head: str) -> Report:
    """Run every check against the database behind `factory`; never raises for a failed check."""
    report = Report()
    _check_schema(factory, expected_head, report)
    _check_wallets(factory, report)
    _count_rows(factory, report)
    _render_documents(factory, report)
    _decrypt_secrets(factory, keyring, report)
    _latest_writes(factory, report)
    return report


def _read(factory: sessionmaker[Session], query: Callable[[Session], Any]) -> Any:
    with factory() as session:
        try:
            return query(session)
        finally:
            session.rollback()


def _check_schema(factory: sessionmaker[Session], expected_head: str, report: Report) -> None:
    versions = _read(
        factory,
        lambda s: s.execute(text("SELECT version_num FROM alembic_version")).scalars().all(),
    )
    if list(versions) != [expected_head]:
        report.failures.append(
            f"schema: alembic_version is {sorted(versions)}, the code expects [{expected_head!r}]"
        )
        return
    report.facts.append(f"schema: alembic_version {expected_head} matches the code")


def _check_wallets(factory: sessionmaker[Session], report: Report) -> None:
    tenants = _read(
        factory, lambda s: s.execute(select(Tenant.id, Tenant.public_id).order_by(Tenant.id)).all()
    )
    ledger_rows = 0
    bad = 0
    for tenant_id, public_id in tenants:
        # 每个钱包一个会话：与每日核对相同，一个一致性快照里读完钱包与它的全部账本。
        result = _read(factory, lambda s, tid=tenant_id: check_wallet_snapshot(s, tid))
        ledger_rows += result.ledger_rows
        if result.problem_codes:
            bad += 1
            codes = ",".join(result.problem_codes)
            report.failures.append(f"wallet: tenant {public_id}: {codes}")
    report.facts.append(
        f"wallets: {len(tenants)} tenants checked, {ledger_rows} ledger rows read,"
        f" {bad} inconsistent"
    )


def _by_status(factory: sessionmaker[Session], column: Any) -> str:
    rows = _read(factory, lambda s: s.execute(select(column, func.count()).group_by(column)).all())
    counted = sorted((str(getattr(status, "value", status)), int(n)) for status, n in rows)
    return " ".join(f"{status}={n}" for status, n in counted) or "none"


def _count(factory: sessionmaker[Session], model: Any) -> int:
    return int(_read(factory, lambda s: s.scalar(select(func.count()).select_from(model))))


def _count_rows(factory: sessionmaker[Session], report: Report) -> None:
    report.facts.append(f"ledger rows: {_count(factory, WalletTransaction)}")
    report.facts.append(f"usage_events: {_by_status(factory, UsageEvent.status)}")
    report.facts.append(f"payments: {_by_status(factory, Payment.status)}")
    report.facts.append(f"payment_receipts: {_count(factory, PaymentReceipt)}")
    report.facts.append(f"monthly_statements: {_by_status(factory, MonthlyStatement.status)}")
    report.facts.append(f"audit_logs: {_count(factory, AuditLog)}")
    report.facts.append(f"users: {_count(factory, User)}")
    # 待投递的事件：起栈之后 beat 会自动补投，这里只看量级。
    report.facts.append(f"domain_outbox: {_by_status(factory, DomainOutbox.status)}")


def _render_documents(factory: sessionmaker[Session], report: Report) -> None:
    receipts = _read(
        factory,
        lambda s: s.execute(
            select(PaymentReceipt.public_id, PaymentReceipt.snapshot_json).order_by(
                PaymentReceipt.id
            )
        ).all(),
    )
    rendered = _render_each(
        "receipt",
        ((public_id, lambda snap=snap: render_receipt_pdf(snap)) for public_id, snap in receipts),
        report,
    )
    report.facts.append(f"receipts: {rendered}/{len(receipts)} rendered")

    statements = _read(
        factory,
        lambda s: s.execute(
            select(
                MonthlyStatement.public_id,
                MonthlyStatement.snapshot_json,
                MonthlyStatement.statement_number,
                MonthlyStatement.finalized_at,
            )
            .where(MonthlyStatement.status == StatementStatus.FINALIZED)
            .order_by(MonthlyStatement.id)
        ).all(),
    )
    rendered = _render_each(
        "statement",
        (
            (
                public_id,
                lambda snap=snap, number=number, at=at: render_statement_pdf(
                    snap, statement_number=number, finalized_at=at
                ),
            )
            for public_id, snap, number, at in statements
        ),
        report,
    )
    report.facts.append(f"finalized statements: {rendered}/{len(statements)} rendered")


def _render_each(
    kind: str, items: Iterable[tuple[str, Callable[[], bytes]]], report: Report
) -> int:
    # 渲染在会话关闭之后做，与两个下载接口相同；异常只记类名，不记原文（原文可能带快照内容）。
    rendered = 0
    for public_id, render in items:
        try:
            body = render()
        except Exception as exc:  # noqa: BLE001 — 任何渲染失败都要报出来，不让一份坏文档挡住其余的
            report.failures.append(f"{kind}: {public_id}: {type(exc).__name__}")
            continue
        if not body.startswith(_PDF_MAGIC):
            report.failures.append(f"{kind}: {public_id}: output is not a PDF")
            continue
        rendered += 1
    return rendered


def _decrypt_secrets(
    factory: sessionmaker[Session], keyring: MasterKeyring, report: Report
) -> None:
    totp = _read(
        factory,
        lambda s: s.execute(
            select(TwoFactorSetting.user_id, TwoFactorSetting.encrypted_totp_secret).order_by(
                TwoFactorSetting.user_id
            )
        ).all(),
    )
    opened = _decrypt_each(
        "totp", ((f"user {user_id}", token, None) for user_id, token in totp), keyring, report
    )
    report.facts.append(f"totp secrets: {opened}/{len(totp)} decrypted")

    credentials = _read(
        factory,
        lambda s: s.execute(
            select(
                IntegrationCredential.id,
                IntegrationCredential.public_api_key,
                IntegrationCredential.key_version,
                IntegrationCredential.encrypted_secret,
            ).order_by(IntegrationCredential.id)
        ).all(),
    )
    opened = _decrypt_each(
        "integration credential",
        (
            (f"id {row_id}", token, credential_aad(api_key, version))
            for row_id, api_key, version, token in credentials
        ),
        keyring,
        report,
    )
    report.facts.append(f"integration credentials: {opened}/{len(credentials)} decrypted")

    webhook = _read(
        factory,
        lambda s: s.execute(
            select(
                ProjectWebhookSecret.id,
                Project.public_id,
                ProjectWebhookSecret.key_version,
                ProjectWebhookSecret.encrypted_secret,
            )
            .join(Project, Project.id == ProjectWebhookSecret.project_id)
            .order_by(ProjectWebhookSecret.id)
        ).all(),
    )
    opened = _decrypt_each(
        "webhook secret",
        (
            (f"id {row_id}", token, webhook_secret_aad(project_public_id, version))
            for row_id, project_public_id, version, token in webhook
        ),
        keyring,
        report,
    )
    report.facts.append(f"webhook secrets: {opened}/{len(webhook)} decrypted")


def _decrypt_each(
    kind: str,
    items: Iterable[tuple[str, str, bytes | None]],
    keyring: MasterKeyring,
    report: Report,
) -> int:
    opened = 0
    for label, token, aad in items:
        try:
            decrypt_secret(keyring, token, associated_data=aad)
        except Exception as exc:  # noqa: BLE001 — 解不开的每一条都要报，明文与原文一概不留
            report.failures.append(f"{kind}: {label}: {type(exc).__name__}")
            continue
        opened += 1
    return opened


def _latest_writes(factory: sessionmaker[Session], report: Report) -> None:
    columns = (
        ("audit_logs.created_at", AuditLog.created_at),
        ("wallet_transactions.created_at", WalletTransaction.created_at),
        ("usage_events.received_at", UsageEvent.received_at),
        ("payments.created_at", Payment.created_at),
    )
    latest: list[dt.datetime] = []
    for name, column in columns:
        moment = _read(factory, lambda s, c=column: s.scalar(select(func.max(c))))
        report.facts.append(f"latest {name}: {_utc(moment)}")
        if moment is not None:
            latest.append(moment)
    newest = max(latest) if latest else None
    report.facts.append(f"latest write overall: {_utc(newest)} (RPO = failure time minus this)")


def _utc(moment: dt.datetime | None) -> str:
    # 本仓库的时刻是不带时区的 UTC（spec §109）。
    return "none" if moment is None else f"{moment.isoformat(sep=' ', timespec='seconds')} UTC"
