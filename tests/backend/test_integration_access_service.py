"""Integration credential service (design gate #118 v1 §3 INV-13, §4, §5, §6, §7).

三类用例：

- **回滚**（审计写入失败、提交失败 × 建凭据、轮换、吊销）：`factory` 夹具的两个参数，
  在 SQLite 与真 MySQL 上各跑一次；
- **依赖锁与并发的场景**（两个线程同时轮换）：只在真 MySQL 上，SQLite 忽略 `FOR UPDATE`；
- 其余（存储是密文、AAD 绑定、审计与异常里没有 secret、轮换与重叠期、校验可用性、
  唯一约束兜底、没有删除路径）在 SQLite 上。

MySQL 那部分需要 `BILLING_TEST_DATABASE_URL` 指向一个**可以被清空的**库，没设时 skip ——
**不要把 skipped 读成 passed**，CI 设了它并把 skipped 判成失败。

⚠️ secret 在这里都是运行时随机生成的；文件里只出现全零占位值（设计 §2「格式」）。
"""

from __future__ import annotations

import base64
import datetime as dt
import inspect
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.core.config import Settings
from app.core.crypto import DecryptionFailed, decrypt_secret, encrypt_secret, load_keyring
from app.core.database import create_session_factory
from app.models.auth import AuditAction, AuditLog, User, UserRole, UserStatus
from app.models.base import Base
from app.models.integration import CredentialStatus, IntegrationCredential
from app.models.tenancy import Project, Tenant
from app.repositories import integration_access as credential_repository
from app.repositories import tenancy
from app.schemas.integration_access import IssuedCredentialView
from app.services import integration_access
from app.services.auth import RequestContext
from app.services.integration_access import (
    CredentialNotFound,
    CredentialRevoked,
    CredentialVersionConflict,
    ProjectNotFound,
)
from app.services.integration_auth import credential_aad, find_verifiable_credential

TEST_DATABASE_URL = os.environ.get("BILLING_TEST_DATABASE_URL", "")
TEST_EMAIL_DOMAIN = "@integration-access-test.example.com"
COMPANY = "Credential Test Sdn Bhd"

NOW = dt.datetime(2026, 9, 25, 8, 30, 0)
CONTEXT = RequestContext(ip_address="203.0.113.9", user_agent="integration-access-service-test")
REASON = "Rotated after the pilot ended"
WEEK = dt.timedelta(seconds=604_800)

OUR_ACTIONS = [
    AuditAction.API_KEY_CREATE,
    AuditAction.API_KEY_ROTATE,
    AuditAction.API_KEY_REVOKE,
]

API_KEY_PATTERN = re.compile(r"ak_[0-9a-f]{32}")
SECRET_PATTERN = re.compile(r"sk_[0-9a-f]{64}")


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


def settings_with(master_key_file: str, overlap_seconds: int) -> Settings:
    return Settings(
        master_key_file=master_key_file, credential_rotation_overlap_seconds=overlap_seconds
    )


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
    # 建表走 alembic：复合外键、排序规则与 CHECK 以迁移建出来的为准。
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

    凭据行没有删除接口（设计 §6）；这里是测试清场，直接删表行。
    """
    ours = select(Tenant.id).where(Tenant.company_name == COMPANY)
    with engine.begin() as connection:
        connection.execute(
            delete(IntegrationCredential).where(IntegrationCredential.tenant_id.in_(ours))
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

    def create(self, *, now: dt.datetime = NOW) -> IssuedCredentialView:
        return integration_access.create_credential(
            self.factory,
            self.settings,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            context=CONTEXT,
            now=now,
        )

    def rotate(
        self,
        api_key: str,
        current: int,
        *,
        now: dt.datetime = NOW,
        settings: Settings | None = None,
    ) -> IssuedCredentialView:
        return integration_access.rotate_credential(
            self.factory,
            settings or self.settings,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            api_key=api_key,
            current_key_version=current,
            context=CONTEXT,
            now=now,
        )

    def revoke_version(self, api_key: str, version: int, *, now: dt.datetime = NOW):
        return integration_access.revoke_version(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            api_key=api_key,
            key_version=version,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )

    def revoke_key(self, api_key: str, *, now: dt.datetime = NOW):
        return integration_access.revoke_key(
            self.factory,
            actor=self.admin,
            customer_id=self.customer_id,
            project_id=self.project_id,
            api_key=api_key,
            reason=REASON,
            context=CONTEXT,
            now=now,
        )


def rows(factory, api_key: str | None = None) -> list[IntegrationCredential]:
    statement = select(IntegrationCredential).order_by(IntegrationCredential.id)
    if api_key is not None:
        statement = statement.where(IntegrationCredential.public_api_key == api_key)
    with factory() as session:
        return list(session.execute(statement).scalars())


def audits(factory, action: AuditAction) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    with factory() as session:
        return list(session.execute(statement).scalars())


def snapshot(factory) -> dict[str, object]:
    """Everything the three actions may write, as committed."""
    columns = (
        IntegrationCredential.public_api_key,
        IntegrationCredential.key_version,
        IntegrationCredential.status,
        IntegrationCredential.valid_until,
        IntegrationCredential.revoked_at,
        IntegrationCredential.encrypted_secret,
    )
    ours = AuditLog.action.in_(OUR_ACTIONS)
    with factory() as session:
        stored = session.execute(select(*columns).order_by(IntegrationCredential.id)).all()
        count = session.execute(select(func.count()).select_from(AuditLog).where(ours))
        return {"credentials": [tuple(row) for row in stored], "audits": count.scalar_one()}


def recorded_secrets(monkeypatch) -> list[str]:
    """Every secret the service generates, so a test can look for it where it must not be."""
    real = integration_access._new_secret
    seen: list[str] = []

    def recording() -> str:
        value = real()
        seen.append(value)
        return value

    monkeypatch.setattr(integration_access, "_new_secret", recording)
    return seen


def break_the_audit(monkeypatch) -> None:
    """The audit row fails at flush: `created_at` is NULL (NOT NULL → IntegrityError)."""
    real = integration_access.record_audit

    def broken(session: Session, **options: object) -> None:
        real(session, **{**options, "now": None})  # type: ignore[arg-type]

    monkeypatch.setattr(integration_access, "record_audit", broken)


def break_the_commit(monkeypatch) -> list[Session]:
    calls: list[Session] = []

    def failing_commit(self: Session) -> None:
        calls.append(self)
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(Session, "commit", failing_commit)
    return calls


def opened(settings: Settings, row: IntegrationCredential) -> str:
    """Decrypt a stored row with its own AAD, as the verification path will."""
    aad = credential_aad(row.public_api_key, row.key_version)
    return decrypt_secret(load_keyring(settings), row.encrypted_secret, associated_data=aad)


def exception_chain(error: BaseException | None) -> Iterator[BaseException]:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        yield error
        error = error.__cause__ or error.__context__


# --- 回滚：SQLite 与 MySQL 各一次（INV-13） ---------------------------------------


@pytest.mark.parametrize("failure", ["audit", "commit"])
@pytest.mark.parametrize("action", ["create", "rotate", "revoke_version", "revoke_key"])
def test_a_failed_action_leaves_nothing(factory, settings, monkeypatch, action, failure) -> None:
    """设计 §3 INV-13：凭据行与审计都与调用前相同；已生成的 secret 从未返回。"""
    scene = Scene(factory, settings)
    existing = None if action == "create" else scene.create()
    before = snapshot(factory)
    generated = recorded_secrets(monkeypatch)
    if failure == "audit":
        break_the_audit(monkeypatch)
        expected: type[Exception] = IntegrityError
    else:
        break_the_commit(monkeypatch)
        expected = RuntimeError

    with pytest.raises(expected) as raised:
        if action == "create":
            scene.create()
        elif action == "rotate":
            assert existing is not None
            scene.rotate(existing.api_key, 1)
        elif action == "revoke_version":
            assert existing is not None
            scene.revoke_version(existing.api_key, 1)
        else:
            assert existing is not None
            scene.revoke_key(existing.api_key)

    # 下面的核对只读、不提交，所以不必先撤掉提交的替身。
    assert snapshot(factory) == before
    # 异常及其整条因果链里都没有这次生成的 secret（设计 §6、§7「泄露：异常信息」）。
    assert len(generated) == (0 if action.startswith("revoke") else 1)
    for error in exception_chain(raised.value):
        for text in (str(error), repr(error)):
            for secret in generated:
                assert secret not in text


# --- 建凭据：存储是密文 ---------------------------------------------------------


def test_the_secret_is_stored_only_as_ciphertext(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)

    issued = scene.create()

    secret = issued.secret.get_secret_value()
    assert API_KEY_PATTERN.fullmatch(issued.api_key)
    assert SECRET_PATTERN.fullmatch(secret)
    assert (issued.key_version, issued.status, issued.valid_until) == (1, "ACTIVE", None)
    assert issued.verifiable is True
    [row] = rows(sqlite_factory)
    assert secret not in row.encrypted_secret
    assert secret[3:] not in row.encrypted_secret
    assert opened(settings, row) == secret
    # 主密钥版本与签名版本是两列、两个值（ADR-0004 §4）。
    assert row.encryption_key_version == load_keyring(settings).active_version == 3
    assert row.key_version == 1
    assert (row.valid_from, row.created_at, row.revoked_at, row.last_used_at) == (
        NOW,
        NOW,
        None,
        None,
    )


def test_the_secret_does_not_show_in_the_views_repr(sqlite_factory, settings) -> None:
    """`SecretStr`：repr / str 是掩码，只有序列化响应时才取出原值（设计 §6）。"""
    issued = Scene(sqlite_factory, settings).create()
    secret = issued.secret.get_secret_value()

    assert secret not in repr(issued)
    assert secret not in str(issued)
    assert json.loads(issued.model_dump_json())["secret"] == secret


def test_every_create_is_a_new_key(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)

    first, second = scene.create(), scene.create()

    assert first.api_key != second.api_key
    assert first.secret.get_secret_value() != second.secret.get_secret_value()
    assert (first.key_version, second.key_version) == (1, 1)


def test_an_unknown_customer_or_project_writes_nothing(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    _, other_project = make_project(sqlite_factory)
    before = snapshot(sqlite_factory)

    scene.customer_id = str(uuid.uuid4())
    with pytest.raises(integration_access.CustomerNotFound):
        scene.create()
    # 别的客户的项目与不存在的项目是同一个 404。
    for project_id in (other_project, str(uuid.uuid4())):
        scene.customer_id = make_project(sqlite_factory)[0]
        scene.project_id = project_id
        with pytest.raises(ProjectNotFound):
            scene.create()

    assert snapshot(sqlite_factory)["credentials"] == before["credentials"]
    assert snapshot(sqlite_factory)["audits"] == before["audits"]


# --- AAD 绑定 ------------------------------------------------------------------


def test_a_ciphertext_copied_to_another_row_cannot_be_opened(sqlite_factory, settings) -> None:
    """把一行的密文拷到另一行（换 key、换版本）：`DecryptionFailed`（设计 §2、§7）。"""
    scene = Scene(sqlite_factory, settings)
    first = scene.create()
    second = scene.create()
    scene.rotate(first.api_key, 1)
    keyring = load_keyring(settings)
    by_row = {(row.public_api_key, row.key_version): row for row in rows(sqlite_factory)}
    first_v1 = by_row[(first.api_key, 1)].encrypted_secret

    # 原位置照常解开。
    opened = decrypt_secret(keyring, first_v1, associated_data=credential_aad(first.api_key, 1))
    assert opened == first.secret.get_secret_value()
    # 换 key、换版本都解不开。
    for target in ((second.api_key, 1), (first.api_key, 2)):
        with pytest.raises(DecryptionFailed):
            decrypt_secret(keyring, first_v1, associated_data=credential_aad(*target))
    # 真把密文写进另一行再按那一行解密，也一样。
    with sqlite_factory() as session:
        session.execute(
            update(IntegrationCredential)
            .where(IntegrationCredential.public_api_key == second.api_key)
            .values(encrypted_secret=first_v1)
        )
        session.commit()
    [moved] = rows(sqlite_factory, second.api_key)
    with pytest.raises(DecryptionFailed):
        decrypt_secret(
            keyring, moved.encrypted_secret, associated_data=credential_aad(second.api_key, 1)
        )


def test_ciphertext_without_associated_data_still_opens(settings) -> None:
    """2FA 的既有密文不带 AAD，照常解开（`crypto.py` 只加了默认 None 的参数）。"""
    keyring = load_keyring(settings)
    stored, _ = encrypt_secret(keyring, "JBSWY3DPEHPK3PXP")

    assert decrypt_secret(keyring, stored) == "JBSWY3DPEHPK3PXP"


# --- 审计里没有 secret ----------------------------------------------------------


def test_the_audit_trail_has_the_listed_fields_and_no_secret(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    rotated = scene.rotate(created.api_key, 1)
    scene.revoke_version(created.api_key, 1)
    scene.revoke_key(created.api_key)
    secrets = [view.secret.get_secret_value() for view in (created, rotated)]
    ciphertexts = [row.encrypted_secret for row in rows(sqlite_factory)]

    [create] = audits(sqlite_factory, AuditAction.API_KEY_CREATE)
    [rotate] = audits(sqlite_factory, AuditAction.API_KEY_ROTATE)
    revoke_one, revoke_all = audits(sqlite_factory, AuditAction.API_KEY_REVOKE)

    for audit in (create, rotate, revoke_one, revoke_all):
        assert (audit.actor_user_id, audit.actor_role) == (scene.admin.id, "ADMIN")
        assert (audit.entity_type, audit.entity_id) == ("integration_credential", created.api_key)
        assert (audit.ip_address, audit.user_agent) == (CONTEXT.ip_address, CONTEXT.user_agent)
        assert audit.created_at == NOW
        for text in (audit.before_state or "", audit.after_state or "", audit.reason or ""):
            for leaked in secrets + ciphertexts:
                assert leaked not in text
            assert "encryption_key_version" not in text
            assert "encrypted_secret" not in text

    assert create.before_state is None
    assert json.loads(create.after_state or "{}") == {
        "api_key": created.api_key,
        "key_version": 1,
        "project_public_id": scene.project_id,
        "tenant_public_id": scene.customer_id,
        "valid_from": NOW.isoformat(),
    }
    until = (NOW + WEEK).isoformat()
    assert json.loads(rotate.before_state or "{}") == {
        "versions": [{"key_version": 1, "valid_until": None}]
    }
    assert json.loads(rotate.after_state or "{}") == {
        "key_version": 2,
        "versions": [{"key_version": 1, "valid_until": until}],
    }
    assert rotate.reason is None
    assert json.loads(revoke_one.before_state or "{}") == {
        "versions": [{"key_version": 1, "status": "ACTIVE"}]
    }
    assert json.loads(revoke_one.after_state or "{}") == {
        "versions": [{"key_version": 1, "status": "REVOKED"}]
    }
    # 吊销整个 key 时版本 1 早已吊销：只列受影响的版本 2。
    assert json.loads(revoke_all.before_state or "{}") == {
        "versions": [{"key_version": 2, "status": "ACTIVE"}]
    }
    assert json.loads(revoke_all.after_state or "{}") == {
        "versions": [{"key_version": 2, "status": "REVOKED"}]
    }
    assert revoke_one.reason == revoke_all.reason == REASON


# --- 轮换 -----------------------------------------------------------------------


def test_rotation_adds_the_next_version_of_the_same_key(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()

    rotated = scene.rotate(created.api_key, 1)

    assert rotated.api_key == created.api_key
    assert rotated.key_version == 2
    assert rotated.secret.get_secret_value() != created.secret.get_secret_value()
    assert (rotated.valid_until, rotated.verifiable) == (None, True)
    old, new = rows(sqlite_factory, created.api_key)
    assert (old.key_version, old.valid_until) == (1, NOW + WEEK)
    assert (new.key_version, new.valid_until) == (2, None)
    assert old.status is new.status is CredentialStatus.ACTIVE
    # 同一个 api_key 的所有版本属于同一个项目（设计 §2）。
    assert (new.tenant_id, new.project_id) == (old.tenant_id, old.project_id)
    # 旧行的密文不动；两行各自按自己的 AAD 解开。
    assert opened(settings, old) == created.secret.get_secret_value()
    assert opened(settings, new) == rotated.secret.get_secret_value()
    assert len(audits(sqlite_factory, AuditAction.API_KEY_ROTATE)) == 1


@pytest.mark.parametrize("overlap", [0, 3600])
def test_the_overlap_is_configurable(sqlite_factory, master_key_file, overlap: int) -> None:
    settings = settings_with(master_key_file, overlap)
    scene = Scene(sqlite_factory, settings)
    created = scene.create()

    scene.rotate(created.api_key, 1)

    old, _new = rows(sqlite_factory, created.api_key)
    assert old.valid_until == NOW + dt.timedelta(seconds=overlap)
    with sqlite_factory() as session:
        still_usable = find_verifiable_credential(session, created.api_key, 1, NOW)
    # 0 表示轮换即让旧版本立刻失效。
    assert (still_usable is not None) is (overlap > 0)


def test_the_default_overlap_is_seven_days() -> None:
    field = Settings.model_fields["credential_rotation_overlap_seconds"]
    assert field.default == 604_800


def test_a_shorter_existing_end_is_kept(sqlite_factory, master_key_file) -> None:
    """只改 `valid_until` 为 NULL 或晚于 `now + 重叠期` 的旧版本（设计 §4）。"""
    scene = Scene(sqlite_factory, settings_with(master_key_file, 3600))
    created = scene.create()
    scene.rotate(created.api_key, 1)
    later = NOW + dt.timedelta(minutes=10)

    scene.rotate(created.api_key, 2, now=later, settings=settings_with(master_key_file, 604_800))

    first, second, third = rows(sqlite_factory, created.api_key)
    assert first.valid_until == NOW + dt.timedelta(seconds=3600)
    assert second.valid_until == later + WEEK
    assert third.valid_until is None


def test_a_stale_current_version_is_a_conflict(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    scene.rotate(created.api_key, 1)
    before = snapshot(sqlite_factory)

    for stale in (1, 3):
        with pytest.raises(CredentialVersionConflict):
            scene.rotate(created.api_key, stale)

    assert snapshot(sqlite_factory) == before


def test_rotating_a_fully_revoked_key_is_refused(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    scene.revoke_key(created.api_key)
    before = snapshot(sqlite_factory)

    with pytest.raises(CredentialRevoked):
        scene.rotate(created.api_key, 1)

    assert snapshot(sqlite_factory) == before


def test_rotating_after_revoking_the_newest_version_is_allowed(sqlite_factory, settings) -> None:
    """吊销最新版本、旧版本还在重叠期内：这个 key 仍有 ACTIVE 版本，可以再轮换。"""
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    scene.rotate(created.api_key, 1)
    scene.revoke_version(created.api_key, 2)

    third = scene.rotate(created.api_key, 2)

    assert third.key_version == 3


def test_the_unique_constraint_is_the_last_guard(sqlite_factory, settings, monkeypatch) -> None:
    """锁之外撞上 `(public_api_key, key_version)`：映射为同一个 409，什么都不留下。"""
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    before = snapshot(sqlite_factory)

    def duplicate(session: Session, **_options: object) -> IntegrationCredential:
        raise IntegrityError("INSERT INTO integration_credentials", {}, Exception("duplicate"))

    monkeypatch.setattr(credential_repository, "insert_credential", duplicate)

    with pytest.raises(CredentialVersionConflict):
        scene.rotate(created.api_key, 1)

    assert snapshot(sqlite_factory) == before


def test_another_projects_key_is_not_found(sqlite_factory, settings) -> None:
    mine = Scene(sqlite_factory, settings)
    theirs = Scene(sqlite_factory, settings)
    their_key = theirs.create().api_key
    before = snapshot(sqlite_factory)

    with pytest.raises(CredentialNotFound):
        mine.rotate(their_key, 1)
    with pytest.raises(CredentialNotFound):
        mine.revoke_version(their_key, 1)
    with pytest.raises(CredentialNotFound):
        mine.revoke_key(their_key)

    assert snapshot(sqlite_factory) == before


# --- 吊销 -----------------------------------------------------------------------


def test_revoking_a_version_is_idempotent(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    later = NOW + dt.timedelta(minutes=5)

    first = scene.revoke_version(created.api_key, 1, now=later)
    after_first = snapshot(sqlite_factory)
    second = scene.revoke_version(created.api_key, 1, now=later + dt.timedelta(minutes=5))

    assert (first.status, first.revoked_at, first.verifiable) == ("REVOKED", later, False)
    # 第二次：同一个状态、同一个吊销时刻，不写任何东西。
    assert second == first
    assert snapshot(sqlite_factory) == after_first
    assert len(audits(sqlite_factory, AuditAction.API_KEY_REVOKE)) == 1
    with pytest.raises(CredentialNotFound):
        scene.revoke_version(created.api_key, 2)


def test_revoking_the_key_revokes_every_version(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    scene.rotate(created.api_key, 1)

    views = scene.revoke_key(created.api_key)

    assert [(view.key_version, view.status) for view in views] == [(1, "REVOKED"), (2, "REVOKED")]
    assert {row.status for row in rows(sqlite_factory)} == {CredentialStatus.REVOKED}
    [audit] = audits(sqlite_factory, AuditAction.API_KEY_REVOKE)
    versions = json.loads(audit.after_state or "{}")["versions"]
    assert [entry["key_version"] for entry in versions] == [1, 2]

    again = scene.revoke_key(created.api_key)
    assert again == views
    assert len(audits(sqlite_factory, AuditAction.API_KEY_REVOKE)) == 1


def test_revocation_keeps_the_row_and_its_ciphertext(sqlite_factory, settings) -> None:
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    [before] = rows(sqlite_factory)

    scene.revoke_key(created.api_key)

    [after] = rows(sqlite_factory)
    assert after.encrypted_secret == before.encrypted_secret
    assert (after.status, after.revoked_at) == (CredentialStatus.REVOKED, NOW)


# --- 校验可用性 -----------------------------------------------------------------


def test_find_verifiable_credential(sqlite_factory, master_key_file) -> None:
    """设计 §7：生效中的版本与重叠期内的旧版本返回行，其余全部 `None`。"""
    scene = Scene(sqlite_factory, settings_with(master_key_file, 3600))
    created = scene.create()
    scene.rotate(created.api_key, 1)
    revoked = scene.create()
    scene.revoke_key(revoked.api_key)
    key = created.api_key
    inside = NOW + dt.timedelta(minutes=30)
    after = NOW + dt.timedelta(hours=2)

    def found(api_key: object, version: object, moment: dt.datetime) -> int | None:
        with sqlite_factory() as session:
            row = find_verifiable_credential(session, api_key, version, moment)
            return None if row is None else row.key_version

    assert found(key, 2, inside) == 2  # 生效中
    assert found(key, 1, inside) == 1  # 重叠期内的旧版本
    assert found(key, 1, after) is None  # valid_until 已过
    assert found(key, 1, NOW + dt.timedelta(seconds=3600)) is None  # 恰好到期
    assert found(key, 2, after) == 2
    assert found(key, 2, NOW - dt.timedelta(seconds=1)) is None  # 还没生效
    assert found(revoked.api_key, 1, inside) is None  # 已吊销
    assert found("ak_" + "0" * 32, 1, inside) is None  # 不存在
    for version in (0, -1, 3, True, "1", 1.0, 2**40):
        assert found(key, version, inside) is None, version
    assert found(key.upper(), 2, inside) is None


def test_find_verifiable_credential_after_revoking_the_newest(sqlite_factory, settings) -> None:
    """吊销最新版本、旧版本还在重叠期内：只剩旧版本可用，直到它过期（设计 §4）。"""
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    scene.rotate(created.api_key, 1)
    scene.revoke_version(created.api_key, 2)

    with sqlite_factory() as session:
        assert find_verifiable_credential(session, created.api_key, 2, NOW) is None
        old = find_verifiable_credential(session, created.api_key, 1, NOW)
        assert old is not None and old.key_version == 1
        assert find_verifiable_credential(session, created.api_key, 1, NOW + WEEK) is None


# --- 加锁 -----------------------------------------------------------------------


def test_rotation_and_revocation_lock_the_keys_versions(
    sqlite_factory, settings, monkeypatch
) -> None:
    """设计 §2：轮换与吊销先按 api_key 锁住全部版本行；客户与项目不加锁。"""
    scene = Scene(sqlite_factory, settings)
    created = scene.create()
    real_lock = credential_repository.lock_key_versions
    real_tenant = tenancy.get_tenant_by_public_id
    locked: list[str] = []
    tenant_locks: list[bool] = []

    def lock_spy(session: Session, *, project_id: int, api_key: str):
        locked.append(api_key)
        return real_lock(session, project_id=project_id, api_key=api_key)

    def tenant_spy(session: Session, customer_id: str, *, for_update: bool = False):
        tenant_locks.append(for_update)
        return real_tenant(session, customer_id, for_update=for_update)

    monkeypatch.setattr(credential_repository, "lock_key_versions", lock_spy)
    monkeypatch.setattr(tenancy, "get_tenant_by_public_id", tenant_spy)

    scene.rotate(created.api_key, 1)
    scene.revoke_version(created.api_key, 1)
    scene.revoke_key(created.api_key)

    assert locked == [created.api_key] * 3
    assert tenant_locks == [False] * 3


def test_the_lock_query_is_for_update_and_ordered_by_version() -> None:
    source = inspect.getsource(credential_repository.lock_key_versions)

    assert ".with_for_update()" in source
    assert ".order_by(IntegrationCredential.key_version)" in source


def test_two_concurrent_rotations_make_exactly_one_new_version(mysql_factory, settings) -> None:
    """设计 §7「并发轮换」：恰好一个成功、一个 409；只有版本 2，没有版本 3；没有 500。"""
    scene = Scene(mysql_factory, settings)
    created = scene.create()
    start = threading.Barrier(2)

    def attempt(_index: int) -> object:
        start.wait(timeout=30)
        try:
            return scene.rotate(created.api_key, 1)
        except CredentialVersionConflict as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))

    issued = [result for result in results if isinstance(result, IssuedCredentialView)]
    conflicts = [result for result in results if isinstance(result, CredentialVersionConflict)]
    assert (len(issued), len(conflicts)) == (1, 1)
    assert [row.key_version for row in rows(mysql_factory, created.api_key)] == [1, 2]
    assert len(audits(mysql_factory, AuditAction.API_KEY_ROTATE)) == 1
    [winner] = issued
    [_old, new] = rows(mysql_factory, created.api_key)
    assert opened(settings, new) == winner.secret.get_secret_value()


# --- 模块边界 -------------------------------------------------------------------


def test_there_is_no_delete_path() -> None:
    """INV-6：凭据行永久保留。repository 与服务里都没有删除。"""
    for module in (credential_repository, integration_access):
        source = inspect.getsource(module).lower()
        assert "delete" not in source, module.__name__


def test_the_service_and_repository_do_not_log() -> None:
    for module in (credential_repository, integration_access):
        source = inspect.getsource(module)
        assert "logging" not in source, module.__name__
        assert "logger" not in source, module.__name__


def test_the_status_column_fits_every_status() -> None:
    column = IntegrationCredential.__table__.c.status
    assert column.type.length == 16
    assert max(len(status.value) for status in CredentialStatus) <= column.type.length
