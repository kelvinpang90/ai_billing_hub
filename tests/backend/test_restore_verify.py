"""Checks on a restored database (app/services/restore_verify.py, `app.cli restore-verify`).

在 SQLite 上造一个「恢复出来的库」：一切完好时全部通过；钱包、文档、密文、迁移版本各坏一处时
逐条报出，且输出里没有明文、公司名与金额（输出要贴进公开仓库的 PR）。
"""

from __future__ import annotations

import base64
import datetime as dt
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker
from test_receipt_pdf import _snapshot as receipt_snapshot
from test_statement_pdf import _FINALIZED, _NUMBER
from test_statement_pdf import _snapshot as statement_snapshot
from test_usage_billing import T0, _sqlite_factory
from test_wallet_reconciliation import fund, make_tenant

from app import cli
from app.core.config import Settings
from app.core.crypto import MasterKeyring, encrypt_secret, load_keyring
from app.models.auth import TwoFactorSetting, User, UserRole, UserStatus
from app.models.integration import (
    CredentialStatus,
    IntegrationCredential,
    ProjectWebhookSecret,
    WebhookSecretStatus,
)
from app.models.receipts import PaymentReceipt
from app.models.statements import MonthlyStatement, StatementStatus
from app.models.tenancy import Project
from app.models.wallet import Wallet
from app.services.integration_auth import credential_aad
from app.services.restore_verify import verify
from app.services.webhook_signing import webhook_secret_aad

_REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = _REPO_ROOT / "deploy" / "restore_verify.sh"
HEAD = "0099_restore_verify_head"
TOTP_PLAINTEXT = "JBSWY3DPEHPK3PXP"
CREDENTIAL_PLAINTEXT = "credential-secret-never-printed"
WEBHOOK_PLAINTEXT = "webhook-secret-never-printed"
API_KEY = "ak_restore_verify_0001"


def _keyring(tmp_path: Path, name: str) -> MasterKeyring:
    path = tmp_path / name
    path.write_text(f"1:{base64.b64encode(os.urandom(32)).decode()}\n", encoding="utf-8")
    return load_keyring(Settings(master_key_file=str(path)))


@pytest.fixture
def keyring(tmp_path: Path) -> MasterKeyring:
    return _keyring(tmp_path, "master.key")


@pytest.fixture
def factory(keyring: MasterKeyring) -> Iterator[sessionmaker[Session]]:
    """A restored-looking database: one funded wallet, one of each document and secret."""
    for made in _sqlite_factory():
        with made() as session:
            session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            session.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": HEAD})
            session.commit()
        tenant_id = make_tenant(made)
        fund(made, tenant_id, 100, "25.50")
        _seed(made, tenant_id, keyring)
        yield made


def _seed(factory: sessionmaker[Session], tenant_id: int, keyring: MasterKeyring) -> None:
    project_public_id = str(uuid.uuid4())
    with factory() as session:
        user = User(
            email="restore-verify@example.invalid",
            password_hash="x",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=T0,
            updated_at=T0,
        )
        project = Project(
            public_id=project_public_id,
            tenant_id=tenant_id,
            name="restore",
            created_at=T0,
            updated_at=T0,
        )
        session.add_all([user, project])
        session.flush()
        totp, totp_version = encrypt_secret(keyring, TOTP_PLAINTEXT)
        session.add(
            TwoFactorSetting(
                user_id=user.id,
                encrypted_totp_secret=totp,
                key_version=totp_version,
                confirmed_at=T0,
                created_at=T0,
                updated_at=T0,
            )
        )
        credential, credential_version = encrypt_secret(
            keyring, CREDENTIAL_PLAINTEXT, associated_data=credential_aad(API_KEY, 1)
        )
        session.add(
            IntegrationCredential(
                tenant_id=tenant_id,
                project_id=project.id,
                public_api_key=API_KEY,
                key_version=1,
                encrypted_secret=credential,
                encryption_key_version=credential_version,
                status=CredentialStatus.ACTIVE,
                valid_from=T0,
                created_at=T0,
            )
        )
        webhook, webhook_version = encrypt_secret(
            keyring, WEBHOOK_PLAINTEXT, associated_data=webhook_secret_aad(project_public_id, 1)
        )
        session.add(
            ProjectWebhookSecret(
                project_id=project.id,
                tenant_id=tenant_id,
                key_version=1,
                status=WebhookSecretStatus.PENDING,
                encrypted_secret=webhook,
                encryption_key_version=webhook_version,
                created_at=T0,
            )
        )
        session.add(
            PaymentReceipt(
                public_id=str(uuid.uuid4()),
                payment_id=1,
                tenant_id=tenant_id,
                receipt_number="ACV-AIR-2026-000001",
                series_year=2026,
                sequence=1,
                amount="1000.00",
                currency="MYR",
                tax_policy_version_id=1,
                snapshot_json=receipt_snapshot(),
                issued_at=T0,
            )
        )
        for status, number, period in (
            (StatementStatus.FINALIZED, _NUMBER, dt.datetime(2026, 11, 30, 16)),
            # 非定稿的对账单不渲染（下载接口只给 FINALIZED）。
            (StatementStatus.DRAFT, "ST-202701-0123ABCE", dt.datetime(2026, 12, 31, 16)),
        ):
            session.add(
                MonthlyStatement(
                    public_id=str(uuid.uuid4()),
                    tenant_id=tenant_id,
                    statement_number=number,
                    period_start=period,
                    period_end=period + dt.timedelta(days=31),
                    cutoff_at=period + dt.timedelta(days=32),
                    status=status,
                    ledger_high_sequence=2,
                    snapshot_json=statement_snapshot(),
                    tax_policy_version_id=1,
                    finalized_at=_FINALIZED if status is StatementStatus.FINALIZED else None,
                    created_at=T0,
                )
            )
        session.commit()


def _all_output(report) -> str:
    return "\n".join(report.facts + report.failures)


def test_an_intact_restore_passes_and_records_the_numbers(factory, keyring) -> None:
    report = verify(factory, keyring=keyring, expected_head=HEAD)

    assert report.failures == []
    assert report.passed
    assert f"schema: alembic_version {HEAD} matches the code" in report.facts
    assert "wallets: 1 tenants checked, 2 ledger rows read, 0 inconsistent" in report.facts
    assert "ledger rows: 2" in report.facts
    assert "monthly_statements: DRAFT=1 FINALIZED=1" in report.facts
    assert "receipts: 1/1 rendered" in report.facts
    assert "finalized statements: 1/1 rendered" in report.facts
    assert "totp secrets: 1/1 decrypted" in report.facts
    assert "integration credentials: 1/1 decrypted" in report.facts
    assert "webhook secrets: 1/1 decrypted" in report.facts
    assert "usage_events: none" in report.facts
    assert "users: 1" in report.facts
    assert any(line.startswith("domain_outbox: ") for line in report.facts)
    assert "latest payments.created_at: none" in report.facts
    assert any(line.startswith("latest write overall: ") and "UTC" in line for line in report.facts)


def test_every_broken_piece_is_reported_without_leaking_it(factory, keyring, tmp_path) -> None:
    other = _keyring(tmp_path, "other.key")
    with factory() as session:
        session.execute(update(Wallet).values(balance="999.00"))
        broken = receipt_snapshot()
        del broken["issuer"]
        session.execute(update(PaymentReceipt).values(snapshot_json=broken))
        # 别的主密钥加密的 TOTP：库导得进来，但这把钥匙打不开。
        token, _ = encrypt_secret(other, TOTP_PLAINTEXT)
        session.execute(update(TwoFactorSetting).values(encrypted_totp_secret=token))
        # 密文对、AAD 不对（行被挪到了别的 api key 上）。
        session.execute(update(IntegrationCredential).values(public_api_key="ak_moved_elsewhere"))
        session.commit()

    report = verify(factory, keyring=keyring, expected_head="0100_newer_code")

    assert not report.passed
    failures = report.failures
    assert failures[0] == (
        f"schema: alembic_version is ['{HEAD}'], the code expects ['0100_newer_code']"
    )
    assert any(f.startswith("wallet: tenant ") and "BALANCE_NOT_LEDGER_SUM" in f for f in failures)
    assert any(
        f.startswith("receipt: ") and f.endswith(": ReceiptSnapshotInvalid") for f in failures
    )
    assert any(f.startswith("totp: user ") and f.endswith(": DecryptionFailed") for f in failures)
    assert any(
        f.startswith("integration credential: id ") and f.endswith(": DecryptionFailed")
        for f in failures
    )
    assert len(failures) == 5
    assert "wallets: 1 tenants checked, 2 ledger rows read, 1 inconsistent" in report.facts
    assert "receipts: 0/1 rendered" in report.facts
    assert "webhook secrets: 1/1 decrypted" in report.facts
    output = _all_output(report)
    for leaked in (
        TOTP_PLAINTEXT,
        CREDENTIAL_PLAINTEXT,
        WEBHOOK_PLAINTEXT,
        "Fictional Reconciliation",
        "Example Sdn Bhd",
        # 带小数点：输出里的 uuid 是十六进制，不会含「.」，只断言 "999" 会偶尔撞上随机 uuid。
        "999.00",
        "125.50",
    ):
        assert leaked not in output


def test_a_receipt_the_download_would_refuse_is_reported(factory, keyring) -> None:
    """快照完好、列里的收据号格式不对：下载接口拒绝下载，核对也要报出来。"""
    with factory() as session:
        session.execute(update(PaymentReceipt).values(receipt_number="ACV-AIR-26-1"))
        session.commit()

    report = verify(factory, keyring=keyring, expected_head=HEAD)

    assert len(report.failures) == 1
    assert report.failures[0].startswith("receipt: ")
    assert report.failures[0].endswith(": ReceiptSnapshotInvalid")
    assert "receipts: 0/1 rendered" in report.facts


@pytest.mark.parametrize("database", ["billing;DROP", "", "billing-restore"])
def test_the_cli_refuses_a_database_name_it_would_not_restore_into(database, capsys) -> None:
    assert cli.main(["restore-verify", "--database", database]) == 2
    assert "database name must be [A-Za-z0-9_]" in capsys.readouterr().err


def test_the_cli_takes_the_root_password_only_from_the_environment(monkeypatch, capsys) -> None:
    monkeypatch.delenv("BILLING_MYSQL_ROOT_PASSWORD", raising=False)
    assert cli.main(["restore-verify", "--database", "billing_restore"]) == 2
    assert "BILLING_MYSQL_ROOT_PASSWORD is not set" in capsys.readouterr().err


def test_the_wrapper_runs_one_isolated_container_and_keeps_the_password_off_the_command_line() -> (
    None
):
    """⚠️ `--no-deps`：不顺带起 worker / beat（恢复库会开始发信、推 webhook）。口令只经环境变量。"""
    script = WRAPPER.read_text(encoding="utf-8")
    command = (
        "$COMPOSE run --rm --no-deps -T -e BILLING_MYSQL_ROOT_PASSWORD api \\\n"
        '    python -m app.cli restore-verify --database "$DATABASE"'
    )
    assert command in script
    assert 'export BILLING_MYSQL_ROOT_PASSWORD="$ROOT_PW"' in script
    assert "source " not in script and "\n. " not in script
    assert "*[!A-Za-z0-9_]*) die" in script
