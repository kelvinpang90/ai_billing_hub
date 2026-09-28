"""Outbound webhook signing service and library (design gate #135 v1 §3 INV-8/13, §4–§7).

四类用例：

- **回滚**（审计写入失败、提交失败 × 签发、启用、退役）与**数据库兜底**（绕过服务层插第二个
  ACTIVE / PENDING、重复版本号、违反 CHECK）：`factory` 夹具的两个参数，在 SQLite 与真 MySQL
  上各跑一次。SQLite 那一半同时验证设计 §10 假设 1（生成列上的唯一索引、允许多个 NULL）；
- **依赖锁与并发的场景**（两个线程同时签发、同时启用）：只在真 MySQL 上，SQLite 忽略
  `FOR UPDATE`；
- 其余（存储是密文、AAD 绑定、审计 / 日志 / 异常 / repr 里没有 secret、唯一约束映射、
  加锁、没有删除路径）在 SQLite 上；
- **签名库**：`sign_status_webhook` 与 `signing_material`，纯函数部分不连库。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。

⚠️ secret 在这里都是运行时随机生成的；文件里只出现全零占位值（设计 §2「secret 格式」）。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import inspect
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.crypto import DecryptionFailed, decrypt_secret, load_keyring
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import ProjectWebhookSecret, WebhookSecretStatus
from app.models.tenancy import Project, Tenant
from app.repositories import tenancy
from app.repositories import webhook_signing as webhook_repository
from app.schemas.webhook_signing import IssuedWebhookSecretView
from app.services import webhook_signing
from app.services.auth import RequestContext
from app.services.integration_access import ProjectNotFound
from app.services.integration_auth import canonical_request, sign
from app.services.webhook_signing import (
    SigningMaterial,
    WebhookSecretConflict,
    WebhookSecretNotFound,
    WebhookSecretNotPending,
    WebhookSecretPendingExists,
    sign_status_webhook,
    signing_material,
    webhook_secret_aad,
)

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@webhook-signing-test.example.com"
COMPANY = "Webhook Signing Test Sdn Bhd"

NOW = dt.datetime(2026, 9, 28, 8, 30, 0)
# 2026-09-28T08:30:00Z 的 Unix 纪元秒，手算写死，不用被测代码再算一遍。
NOW_EPOCH = "1790584200"
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="webhook-signing-service-test")
REASON = "Integration backend rotated"

OUR_ACTIONS = [
    AuditAction.WEBHOOK_SECRET_ISSUE,
    AuditAction.WEBHOOK_SECRET_ACTIVATE,
    AuditAction.WEBHOOK_SECRET_RETIRE,
]

SECRET_PATTERN = re.compile(r"whs_[0-9a-f]{64}")

# 全零占位值（secret-scan）。
ZERO_SECRET = "whs_" + "0" * 64
ZERO_EVENT_ID = "00000000-0000-4000-8000-000000000000"
ZERO_PROJECT_ID = "00000000-0000-4000-8000-000000000000"


# --- 夹具 -----------------------------------------------------------------------


@pytest.fixture
def master_key_file(tmp_path) -> str:
    """版本 1 与 3 两把：活动版本是 3，与签名版本 1 不同，两列混用就看得出来。"""
    lines = [f"{version}:{base64.b64encode(os.urandom(32)).decode()}" for version in (1, 3)]
    path = tmp_path / "master.key"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def settings(master_key_file) -> Settings:
    return Settings(master_key_file=master_key_file)


@pytest.fixture(params=["sqlite", "mysql"])
def factory(request, monkeypatch) -> Iterator[sessionmaker[Session]]:
    if request.param == "mysql":
        yield from _mysql_factory(monkeypatch)
        return
    yield from _sqlite_factory()


@pytest.fixture
def sqlite_factory() -> Iterator[sessionmaker[Session]]:
    yield from _sqlite_factory()


@pytest.fixture
def mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    yield from _mysql_factory(monkeypatch)


def _sqlite_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def _mysql_factory(monkeypatch) -> Iterator[sessionmaker[Session]]:
    if not TEST_DATABASE_URL:
        pytest.skip("BILLING_TEST_DATABASE_URL is not set; the MySQL half needs a real MySQL")
    # 建表走 alembic：生成列、唯一索引、复合外键与 CHECK 以迁移 0008 建出来的为准。
    from app.core.config import get_settings

    monkeypatch.setenv("BILLING_DATABASE_URL", TEST_DATABASE_URL)
    get_settings.cache_clear()
    command.upgrade(Config("alembic.ini"), "head")
    get_settings.cache_clear()

    engine = create_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=2)
    _clean(engine)
    try:
        yield create_session_factory(engine)
    finally:
        _clean(engine)
        engine.dispose()


def _clean(engine: Engine) -> None:
    """⚠️ 库是共享的，每个用例前后都清场，只删本文件建的行。

    密钥行没有删除接口（设计 §6）；这里是测试清场，直接删表行。
    """
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
    with engine.begin() as connection:
        connection.execute(
            delete(ProjectWebhookSecret).where(ProjectWebhookSecret.tenant_id.in_(ours))
        )
        connection.execute(delete(Project).where(Project.tenant_id.in_(ours)))
        connection.execute(delete(Tenant).where(Tenant.company_name == COMPANY))
        connection.execute(delete(AuditLog).where(AuditLog.action.in_(OUR_ACTIONS)))
        connection.execute(delete(User).where(User.email.like(f"%{TEST_EMAIL_DOMAIN}")))


# --- 帮手 -----------------------------------------------------------------------


def make_admin(factory) -> User:
    """A committed ADMIN, detached with its columns loaded — what `require_admin` returns."""
    with factory() as session:
        user = User(
            email=f"{uuid.uuid4().hex}{TEST_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(user)
        session.commit()
        return user


def make_project(factory) -> tuple[str, str]:
    """(customer public id, project public id)."""
    with factory() as session:
        tenant = tenancy.create_tenant(
            session, company_name=COMPANY, email="ops@example.com", now=NOW
        )
        project = tenancy.create_project(session, tenant_id=tenant.id, name="Chatbot", now=NOW)
        session.commit()
        return tenant.public_id, project.public_id


class Scene:
    """One admin, one customer with one project, and the settings to act with."""

    def __init__(self, factory, settings: Settings) -> None:
        self.factory = factory
        self.settings = settings
        self.admin = make_admin(factory)
        self.customer_id, self.project_id = make_project(factory)

    @property
    def internal_project_id(self) -> int:
        with self.factory() as session:
            statement = select(Project.id).where(Project.public_id == self.project_id)
            return int(session.execute(statement).scalar_one())

    def issue(self, *, now: dt.datetime = NOW) -> IssuedWebhookSecretView:
        return webhook_signing.issue_secret(
            self.factory,
            self.settings,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            context=CONTEXT,
            now=now,
        )

    def activate(self, version: int, *, now: dt.datetime = NOW):
        return webhook_signing.activate_secret(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            key_version=version,
            context=CONTEXT,
            now=now,
        )

    def retire(self, version: int, *, now: dt.datetime = NOW):
        return webhook_signing.retire_secret(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            key_version=version,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )

    def material(self) -> SigningMaterial | None:
        with self.factory() as session:
            return signing_material(session, load_keyring(self.settings), self.internal_project_id)


def rows(factory, project_id: str | None = None) -> list[ProjectWebhookSecret]:
    statement = select(ProjectWebhookSecret).order_by(ProjectWebhookSecret.id)
    if project_id is not None:
        owned = select(Project.id).where(Project.public_id == project_id)
        statement = statement.where(ProjectWebhookSecret.project_id.in_(owned))
    with factory() as session:
        return list(session.execute(statement).scalars())


def statuses(factory, project_id: str) -> list[tuple[int, str]]:
    return [(row.key_version, row.status.value) for row in rows(factory, project_id)]


def audits(factory, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(factory) -> dict[str, object]:
    """Everything the three actions may write, as committed."""
    columns = (
        ProjectWebhookSecret.project_id,
        ProjectWebhookSecret.key_version,
        ProjectWebhookSecret.status,
        ProjectWebhookSecret.activated_at,
        ProjectWebhookSecret.retired_at,
        ProjectWebhookSecret.encrypted_secret,
    )
    ours = AuditLog.action.in_(OUR_ACTIONS)
    with factory() as session:
        stored = session.execute(select(*columns).order_by(ProjectWebhookSecret.id)).all()
        count = session.execute(select(func.count()).select_from(AuditLog).where(ours))
        return {"secrets": [tuple(row) for row in stored], "audits": count.scalar_one()}


def recorded_secrets(monkeypatch) -> list[str]:
    """Every secret the service generates, so a test can look for it where it must not be."""
    real = webhook_signing._new_secret
    seen: list[str] = []

    def recording() -> str:
        value = real()
        seen.append(value)
        return value

    monkeypatch.setattr(webhook_signing, "_new_secret", recording)
    return seen


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = webhook_signing.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(webhook_signing, "record_audit", broken)


def break_the_commit(monkeypatch) -> None:
    def failing_commit(self: Session) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)


def opened(settings: Settings, project_id: str, row: ProjectWebhookSecret) -> str:
    """Decrypt a stored row with its own AAD, as `signing_material` does."""
    aad = webhook_secret_aad(project_id, row.key_version)
    return decrypt_secret(load_keyring(settings), row.encrypted_secret, associated_data=aad)


def exception_chain(error: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


# --- 回滚：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("action", ["issue", "activate", "retire"])
def test_a_failed_action_leaves_nothing(factory, settings, monkeypatch, action, failure) -> None:
    """设计 §7「事务中途失败」：没有新行、没有审计；启用失败后仍是原来的 ACTIVE。"""
    scene = Scene(factory, settings)
    # 版本 1 是 ACTIVE、版本 2 是 PENDING：启用 2 要在一个事务里改两行。
    scene.issue()
    scene.activate(1)
    if action != "issue":
        scene.issue()
    before = snapshot(factory)
    generated = recorded_secrets(monkeypatch)
    if failure == "audit":
        break_the_audit(monkeypatch)
        expected: type[Exception] = IntegrityError
    else:
        break_the_commit(monkeypatch)
        expected = RuntimeError

    with pytest.raises(expected) as raised:
        if action == "issue":
            scene.issue()
        elif action == "activate":
            scene.activate(2)
        else:
            scene.retire(2)

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory) == before
    if action == "issue":
        assert statuses(factory, scene.project_id) == [(1, "ACTIVE")]
    else:
        assert statuses(factory, scene.project_id) == [(1, "ACTIVE"), (2, "PENDING")]
    # 异常及其整条因果链里都没有这次生成的 secret（设计 §6）。
    assert len(generated) == (1 if action == "issue" else 0)
    for error in exception_chain(raised.value):
        for text in (str(error), repr(error)):
            for secret in generated:
                assert secret not in text


# --- 数据库兜底：SQLite 与 MySQL 各一次 -------------------------------------------


def _insert_raw(factory, scene: Scene, **values: object) -> None:
    with factory() as session:
        project = session.execute(
            select(Project).where(Project.public_id == scene.project_id)
        ).scalar_one()
        row = {
            "tenant_id": project.tenant_id,
            "project_id": project.id,
            "key_version": 9,
            "status": "PENDING",
            "encrypted_secret": "not-a-real-ciphertext",
            "encryption_key_version": 1,
            "created_at": NOW,
            **values,
        }
        session.execute(insert(ProjectWebhookSecret).values(**row))
        session.commit()


@pytest.mark.parametrize(
    "values",
    [
        {"status": "PENDING"},
        {"status": "ACTIVE", "activated_at": NOW},
        {"key_version": 1, "status": "RETIRED", "retired_at": NOW},
    ],
    ids=["second-pending", "second-active", "duplicate-version"],
)
def test_the_database_refuses_a_second_active_pending_or_version(
    factory, settings, values: dict
) -> None:
    """设计 §7「数据库兜底」：绕过服务层直接插第二个 PENDING / ACTIVE、重复版本号，被拒绝。

    SQLite 这一半就是设计 §10 假设 1 在 SQLite 上的验证：生成列上的唯一索引成立。
    """
    scene = Scene(factory, settings)
    scene.issue()
    scene.activate(1)
    scene.issue()
    before = snapshot(factory)

    # 唯一冲突两个驱动都报 IntegrityError。
    with pytest.raises(IntegrityError):
        _insert_raw(factory, scene, **values)

    assert snapshot(factory) == before


@pytest.mark.parametrize(
    "values",
    [
        {"key_version": 0},
        {"key_version": -1},
        {"status": "REVOKED"},
        {"status": "PENDING", "activated_at": NOW},
        {"status": "PENDING", "retired_at": NOW},
        {"status": "ACTIVE"},
        {"status": "ACTIVE", "activated_at": NOW, "retired_at": NOW},
        {"status": "RETIRED"},
    ],
    ids=[
        "version-0",
        "version-negative",
        "status-unknown",
        "pending-with-activated",
        "pending-with-retired",
        "active-without-activated",
        "active-with-retired",
        "retired-without-time",
    ],
)
def test_the_database_refuses_rows_that_break_a_check(factory, settings, values: dict) -> None:
    """版本下限、状态取值、状态与时间一致（设计 §2）。项目里没有别的行，只会撞 CHECK。"""
    scene = Scene(factory, settings)
    before = snapshot(factory)

    # ⚠️ MySQL 的 CHECK 违例（3819）不是每个驱动都映射成 IntegrityError，所以接 DBAPIError。
    with pytest.raises(DBAPIError):
        _insert_raw(factory, scene, **values)

    assert snapshot(factory) == before


def test_many_retired_versions_coexist(factory, settings) -> None:
    """唯一索引允许多个 NULL：RETIRED 行在两个生成列上都是 NULL（设计 §10 假设 1）。"""
    scene = Scene(factory, settings)
    for _round in range(3):
        scene.issue()
        scene.retire(rows(factory, scene.project_id)[-1].key_version)
    scene.issue()
    scene.activate(4)
    scene.issue()
    # 别的项目有自己的一个 ACTIVE 与一个 PENDING，互不影响。
    other = Scene(factory, settings)
    other.issue()
    other.activate(1)
    other.issue()

    assert statuses(factory, scene.project_id) == [
        (1, "RETIRED"),
        (2, "RETIRED"),
        (3, "RETIRED"),
        (4, "ACTIVE"),
        (5, "PENDING"),
    ]
    project = scene.internal_project_id
    slots = [(row.active_slot, row.pending_slot) for row in rows(factory, scene.project_id)]
    assert slots == [(None, None)] * 3 + [(project, None), (None, project)]


# --- 签发：存储是密文 -------------------------------------------------------------


def test_the_secret_is_stored_only_as_ciphertext(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)

    issued = scene.issue()

    secret = issued.secret.get_secret_value()
    assert SECRET_PATTERN.fullmatch(secret)
    assert (issued.key_version, issued.status) == (1, "PENDING")
    assert (issued.created_at, issued.activated_at, issued.retired_at) == (NOW, None, None)
    [row] = rows(sqlite_factory)
    assert secret not in row.encrypted_secret
    assert secret[4:] not in row.encrypted_secret
    assert opened(settings, scene.project_id, row) == secret
    # 主密钥版本与签名版本是两列、两个值（ADR-0004 §4）。
    assert row.encryption_key_version == load_keyring(settings).active_version == 3
    assert row.key_version == 1


def test_the_secret_does_not_show_in_reprs(sqlite_factory, settings) -> None:
    """`SecretStr` 与 `SigningMaterial`：repr / str 里没有明文（设计 §6）。"""
    scene = Scene(sqlite_factory, settings)
    issued = scene.issue()
    secret = issued.secret.get_secret_value()
    scene.activate(1)
    material = scene.material()

    assert material is not None
    for text in (repr(issued), str(issued), repr(material), str(material)):
        assert secret not in text
    assert json.loads(issued.model_dump_json())["secret"] == secret
    assert material.secret == secret


def test_versions_count_up_and_are_never_reused(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)

    first = scene.issue()
    scene.retire(1)
    second = scene.issue()

    assert (first.key_version, second.key_version) == (1, 2)
    assert first.secret.get_secret_value() != second.secret.get_secret_value()


def test_an_unknown_customer_or_project_writes_nothing(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    _, other_project = make_project(sqlite_factory)
    before = snapshot(sqlite_factory)

    scene.customer_id = str(uuid.uuid4())
    with pytest.raises(webhook_signing.CustomerNotFound):
        scene.issue()
    # 别的客户的项目与不存在的项目是同一个 404。
    for project_id in (other_project, str(uuid.uuid4())):
        scene.customer_id = make_project(sqlite_factory)[0]
        scene.project_id = project_id
        for attempt in (scene.issue, lambda: scene.activate(1), lambda: scene.retire(1)):
            with pytest.raises(ProjectNotFound):
                attempt()

    assert snapshot(sqlite_factory) == before


def test_another_projects_version_is_not_found(sqlite_factory, settings) -> None:
    mine = Scene(sqlite_factory, settings)
    theirs = Scene(sqlite_factory, settings)
    theirs.issue()
    before = snapshot(sqlite_factory)

    # 版本 1 只属于别人的项目：在我的项目下查不到。
    with pytest.raises(WebhookSecretNotFound):
        mine.activate(1)
    with pytest.raises(WebhookSecretNotFound):
        mine.retire(1)

    assert snapshot(sqlite_factory) == before


# --- AAD 绑定 ------------------------------------------------------------------


def test_a_ciphertext_copied_elsewhere_cannot_be_opened(sqlite_factory, settings) -> None:
    """设计 §7「AAD 绑定」：A 项目版本 1 的密文拷到 B 项目或版本 2，解密失败。"""
    a = Scene(sqlite_factory, settings)
    b = Scene(sqlite_factory, settings)
    a_secret = a.issue().secret.get_secret_value()
    b.issue()
    keyring = load_keyring(settings)
    [a_row] = rows(sqlite_factory, a.project_id)

    assert opened(settings, a.project_id, a_row) == a_secret
    for project_id, version in ((b.project_id, 1), (a.project_id, 2)):
        with pytest.raises(DecryptionFailed):
            decrypt_secret(
                keyring,
                a_row.encrypted_secret,
                associated_data=webhook_secret_aad(project_id, version),
            )

    # 真把密文写进 B 的行、启用 B：签名库解不开，也不返回任何明文。
    with sqlite_factory() as session:
        session.execute(
            update(ProjectWebhookSecret)
            .where(ProjectWebhookSecret.project_id == b.internal_project_id)
            .values(encrypted_secret=a_row.encrypted_secret)
        )
        session.commit()
    b.activate(1)
    with pytest.raises(DecryptionFailed):
        b.material()


def test_the_aad_is_the_documented_ascii_string() -> None:
    assert webhook_secret_aad(ZERO_PROJECT_ID, 7) == (
        b"project_webhook_secrets|00000000-0000-4000-8000-000000000000|7"
    )


# --- 审计与日志里没有 secret -----------------------------------------------------


def test_the_audit_trail_has_the_listed_fields_and_no_secret(
    sqlite_factory, settings, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    scene = Scene(sqlite_factory, settings)
    first = scene.issue()
    scene.activate(1)
    second = scene.issue()
    scene.activate(2)
    scene.retire(2)
    secrets = [view.secret.get_secret_value() for view in (first, second)]
    ciphertexts = [row.encrypted_secret for row in rows(sqlite_factory)]

    issue_one, issue_two = audits(sqlite_factory, AuditAction.WEBHOOK_SECRET_ISSUE)
    activate_one, activate_two = audits(sqlite_factory, AuditAction.WEBHOOK_SECRET_ACTIVATE)
    [retire] = audits(sqlite_factory, AuditAction.WEBHOOK_SECRET_RETIRE)

    for audit in (issue_one, issue_two, activate_one, activate_two, retire):
        assert (audit.actor_user_id, audit.actor_role) == (scene.admin.id, "ADMIN")
        assert (audit.entity_type, audit.entity_id) == ("project_webhook_secret", scene.project_id)
        assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
        assert audit.created_at == NOW
        for text in (audit.before_state or "", audit.after_state or "", audit.reason or ""):
            for leaked in secrets + ciphertexts:
                assert leaked not in text
            assert "encryption_key_version" not in text
            assert "encrypted_secret" not in text

    assert issue_one.before_state is None
    assert json.loads(issue_one.after_state or "{}") == {"key_version": 1, "status": "PENDING"}
    assert json.loads(issue_two.after_state or "{}") == {"key_version": 2, "status": "PENDING"}
    assert json.loads(activate_one.before_state or "{}") == {
        "versions": [{"key_version": 1, "status": "PENDING"}]
    }
    assert json.loads(activate_one.after_state or "{}") == {
        "versions": [{"key_version": 1, "status": "ACTIVE"}]
    }
    # 新旧两个版本都在（设计 §2「审计」）。
    assert json.loads(activate_two.before_state or "{}") == {
        "versions": [
            {"key_version": 1, "status": "ACTIVE"},
            {"key_version": 2, "status": "PENDING"},
        ]
    }
    assert json.loads(activate_two.after_state or "{}") == {
        "versions": [
            {"key_version": 1, "status": "RETIRED"},
            {"key_version": 2, "status": "ACTIVE"},
        ]
    }
    assert json.loads(retire.before_state or "{}") == {"key_version": 2, "status": "ACTIVE"}
    assert json.loads(retire.after_state or "{}") == {"key_version": 2, "status": "RETIRED"}
    assert retire.reason == REASON
    assert issue_one.reason is None and activate_two.reason is None

    # 签名库也走一遍，再看日志：什么 secret、密文都没有（这一层根本不写日志）。
    assert scene.material() is None
    for leaked in secrets + ciphertexts:
        assert leaked not in caplog.text


# --- 状态机 ---------------------------------------------------------------------


def test_activation_retires_the_previous_active(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    scene.issue()
    scene.activate(1)
    scene.issue()
    later = NOW + dt.timedelta(minutes=5)

    versions = scene.activate(2, now=later)

    assert [(view.key_version, view.status) for view in versions] == [
        (1, "RETIRED"),
        (2, "ACTIVE"),
    ]
    old, new = rows(sqlite_factory, scene.project_id)
    assert (old.activated_at, old.retired_at) == (NOW, later)
    assert (new.activated_at, new.retired_at) == (later, None)
    # 退役只改状态、不清密文（设计 §6）。
    assert opened(settings, scene.project_id, old)


def test_the_state_machine_refusals_write_nothing(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    scene.issue()

    before = snapshot(sqlite_factory)
    with pytest.raises(WebhookSecretPendingExists):
        scene.issue()
    assert snapshot(sqlite_factory) == before

    scene.retire(1)
    before = snapshot(sqlite_factory)
    with pytest.raises(WebhookSecretNotPending):
        scene.activate(1)
    with pytest.raises(WebhookSecretNotFound):
        scene.activate(2)
    assert snapshot(sqlite_factory) == before


def test_repeating_activate_or_retire_writes_nothing(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    scene.issue()
    first = scene.activate(1)
    after_first = snapshot(sqlite_factory)

    again = scene.activate(1, now=NOW + dt.timedelta(minutes=5))

    assert again == first
    assert snapshot(sqlite_factory) == after_first

    retired = scene.retire(1)
    after_retire = snapshot(sqlite_factory)
    retired_again = scene.retire(1, now=NOW + dt.timedelta(minutes=5))

    assert retired_again == retired
    assert snapshot(sqlite_factory) == after_retire
    assert len(audits(sqlite_factory, AuditAction.WEBHOOK_SECRET_RETIRE)) == 1


def test_the_unique_indexes_are_the_last_guard(sqlite_factory, settings, monkeypatch) -> None:
    """锁之外撞上唯一索引：签发与启用都映射为 409 `WEBHOOK_SECRET_CONFLICT`，不留痕迹。"""
    scene = Scene(sqlite_factory, settings)
    scene.issue()
    scene.activate(1)
    before = snapshot(sqlite_factory)

    def duplicate(*_args: object, **_options: object) -> None:
        raise IntegrityError("INSERT INTO project_webhook_secrets", {}, Exception("duplicate"))

    monkeypatch.setattr(webhook_repository, "insert_pending", duplicate)
    with pytest.raises(WebhookSecretConflict):
        scene.issue()
    assert snapshot(sqlite_factory) == before

    monkeypatch.undo()
    scene.issue()
    before = snapshot(sqlite_factory)
    monkeypatch.setattr(webhook_repository, "activate", duplicate)
    with pytest.raises(WebhookSecretConflict):
        scene.activate(2)
    assert snapshot(sqlite_factory) == before


# --- 加锁 -----------------------------------------------------------------------


def test_every_write_locks_the_project_first(sqlite_factory, settings, monkeypatch) -> None:
    """设计 §4「串行化」：签发、启用、退役都先锁项目行，再读版本。"""
    scene = Scene(sqlite_factory, settings)
    real_lock = webhook_repository.lock_project
    real_versions = webhook_repository.versions_for_project
    calls: list[str] = []

    def lock_spy(session: Session, project_id: int) -> None:
        calls.append("lock")
        real_lock(session, project_id)

    def versions_spy(session: Session, project_id: int):
        calls.append("versions")
        return real_versions(session, project_id)

    monkeypatch.setattr(webhook_repository, "lock_project", lock_spy)
    monkeypatch.setattr(webhook_repository, "versions_for_project", versions_spy)

    scene.issue()
    scene.activate(1)
    scene.retire(1)

    assert calls == ["lock", "versions"] * 3


def test_the_lock_queries_are_for_update() -> None:
    """项目行锁住；版本也用加锁读，读到的是最新提交而不是事务快照。"""
    for function in (webhook_repository.lock_project, webhook_repository.versions_for_project):
        assert ".with_for_update()" in inspect.getsource(function), function.__name__


def test_two_concurrent_issues_make_exactly_one_pending(mysql_factory, settings) -> None:
    """设计 §7「并发」：恰好一个成功、一个 409；只有一个 PENDING；没有 500。"""
    scene = Scene(mysql_factory, settings)
    start = threading.Barrier(2)

    def attempt(_index: int) -> object:
        start.wait(timeout=30)
        try:
            return scene.issue()
        except WebhookSecretPendingExists as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    issued = [result for result in results if isinstance(result, IssuedWebhookSecretView)]
    conflicts = [result for result in results if isinstance(result, WebhookSecretPendingExists)]
    assert (len(issued), len(conflicts)) == (1, 1)
    assert statuses(mysql_factory, scene.project_id) == [(1, "PENDING")]
    assert len(audits(mysql_factory, AuditAction.WEBHOOK_SECRET_ISSUE)) == 1
    [row] = rows(mysql_factory, scene.project_id)
    assert opened(settings, scene.project_id, row) == issued[0].secret.get_secret_value()


def test_two_concurrent_activations_switch_exactly_once(mysql_factory, settings) -> None:
    """设计 §7「并发」的启用一半。

    ⚠️ 设计 §7 写的是「两个线程同时启用不同的 PENDING」，但 `pending_slot` 唯一索引让一个
    项目至多一个 PENDING，造不出那份数据；这里改测唯一可造的情形：两个线程同时启用同一个
    PENDING。按设计 §4，后到的读到「已是 ACTIVE」，200 且不写 —— 所以是两个成功、只有一次
    切换，而不是「一个 409」。偏离记在 docs/TODO.md 的 AIH-TASK-019 记录段。
    """
    scene = Scene(mysql_factory, settings)
    scene.issue()
    scene.activate(1)
    scene.issue()
    start = threading.Barrier(2)

    def attempt(_index: int) -> object:
        start.wait(timeout=30)
        return scene.activate(2, now=NOW + dt.timedelta(minutes=5))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    expected = [(1, "RETIRED"), (2, "ACTIVE")]
    for result in results:
        assert [(view.key_version, view.status) for view in result] == expected
    assert statuses(mysql_factory, scene.project_id) == expected
    # 版本 1 的那一次，加上这一次；没有第三条。
    assert len(audits(mysql_factory, AuditAction.WEBHOOK_SECRET_ACTIVATE)) == 2


# --- 签名库 ---------------------------------------------------------------------


def test_sign_status_webhook_headers() -> None:
    """设计 §7「签名库」：四个头；签名等于手算；第四行是 event id；时间戳是整数秒。"""
    body = b'{"event":"probe"}'
    path = "/webhooks/acuven?b=2&a=1"

    headers = sign_status_webhook(
        ZERO_SECRET,
        3,
        url_path_and_query=path,
        event_id=ZERO_EVENT_ID,
        body=body,
        now=NOW,
    )

    assert set(headers) == {
        "X-Acuven-Timestamp",
        "X-Acuven-Signature",
        "X-Acuven-Event-ID",
        "X-Acuven-Key-Version",
    }
    assert headers["X-Acuven-Timestamp"] == NOW_EPOCH
    assert headers["X-Acuven-Event-ID"] == ZERO_EVENT_ID
    assert headers["X-Acuven-Key-Version"] == "3"
    canonical = canonical_request("POST", path, NOW_EPOCH, ZERO_EVENT_ID, body)
    assert canonical.split("\n")[3] == ZERO_EVENT_ID
    assert headers["X-Acuven-Signature"] == sign(ZERO_SECRET, canonical)
    # 不经 canonical_request，逐行手写一遍：集成方照 docs/api.md 实现的就是这个。
    by_hand = "\n".join(
        (
            "POST",
            "/webhooks/acuven?a=1&b=2",
            NOW_EPOCH,
            ZERO_EVENT_ID,
            hashlib.sha256(body).hexdigest(),
        )
    )
    expected = hmac.new(ZERO_SECRET.encode(), by_hand.encode(), hashlib.sha256).hexdigest()
    assert headers["X-Acuven-Signature"] == expected


def test_the_timestamp_is_whole_seconds_and_time_zone_safe() -> None:
    def stamp(moment: dt.datetime) -> str:
        return sign_status_webhook(
            ZERO_SECRET, 1, url_path_and_query="/", event_id=ZERO_EVENT_ID, body=b"", now=moment
        )["X-Acuven-Timestamp"]

    assert stamp(NOW.replace(microsecond=999_999)) == NOW_EPOCH
    assert stamp(NOW.replace(tzinfo=dt.UTC)) == NOW_EPOCH
    plus_eight = dt.timezone(dt.timedelta(hours=8))
    assert stamp(dt.datetime(2026, 9, 28, 16, 30, 0, tzinfo=plus_eight)) == NOW_EPOCH
    assert re.fullmatch(r"[1-9][0-9]*", stamp(NOW))


def test_a_different_event_id_is_a_different_signature() -> None:
    def signature(event_id: str) -> str:
        return sign_status_webhook(
            ZERO_SECRET, 1, url_path_and_query="/", event_id=event_id, body=b"", now=NOW
        )["X-Acuven-Signature"]

    assert signature(ZERO_EVENT_ID) != signature("00000000-0000-4000-8000-000000000001")


def test_signing_material_follows_the_active_version(sqlite_factory, settings) -> None:
    """没有 ACTIVE → None；启用后是它；退役 ACTIVE 后又是 None（设计 §7「退役 ACTIVE」）。"""
    scene = Scene(sqlite_factory, settings)
    assert scene.material() is None

    first = scene.issue()
    assert scene.material() is None  # PENDING 不能用来签名

    scene.activate(1)
    material = scene.material()
    assert material == SigningMaterial(secret=first.secret.get_secret_value(), key_version=1)

    second = scene.issue()
    assert scene.material() == material  # 新 PENDING 不影响此刻的签名
    scene.activate(2)
    assert scene.material() == SigningMaterial(
        secret=second.secret.get_secret_value(), key_version=2
    )

    scene.retire(2)
    assert scene.material() is None


def test_signing_material_is_per_project(sqlite_factory, settings) -> None:
    mine = Scene(sqlite_factory, settings)
    theirs = Scene(sqlite_factory, settings)
    theirs.issue()
    theirs.activate(1)

    assert mine.material() is None
    assert theirs.material() is not None


# --- 模块边界 -------------------------------------------------------------------


def test_there_is_no_delete_path() -> None:
    """退役只改状态；repository 与服务里都没有删除（设计 §6）。"""
    for module in (webhook_repository, webhook_signing):
        source = inspect.getsource(module).lower()
        assert "delete" not in source, module.__name__


def test_the_service_and_repository_do_not_log() -> None:
    for module in (webhook_repository, webhook_signing):
        source = inspect.getsource(module)
        assert "logging" not in source, module.__name__
        assert "logger" not in source, module.__name__


def test_the_status_column_fits_every_status() -> None:
    column = ProjectWebhookSecret.__table__.c.status
    assert column.type.length == 16
    assert max(len(status.value) for status in WebhookSecretStatus) <= column.type.length


def test_the_signing_library_reuses_the_inbound_canonicalisation() -> None:
    """设计 §1：复用 `canonical_request` 与 `sign`，不另写一套。"""
    source = inspect.getsource(webhook_signing.sign_status_webhook)

    assert "canonical_request(" in source
    assert "sign(secret, canonical)" in source
    assert "hashlib" not in inspect.getsource(webhook_signing)
