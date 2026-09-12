"""Operational commands: `python -m app.cli ...`

**管理员账号只能在这里创建，没有自助注册端点。**一个能自己注册管理员的 HTTP
端点就是整个计费平台的后门 —— 它守着钱包调整、定价发布和退款。

T0.8d（忘记密码 / 重置密码）落地之前，**管理员密码重置也走这里**。那是一条带
期限的临时路径，写在 TODO 的 T0.9 上线前置里。
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.core.config import get_settings
from app.core.database import create_database_engine, create_session_factory
from app.core.emails import InvalidEmail, normalise_email
from app.core.passwords import WeakPassword, hash_password, validate_password_strength
from app.models.auth import AuditAction, User, UserRole, UserStatus
from app.services.auth import RequestContext, record_audit, utc_now


def _read_password(supplied: str | None) -> str:
    """Prompt twice unless the caller passed one explicitly.

    ⚠️ 默认走 `getpass`，**不从命令行参数读**：命令行参数会进 shell 历史、
    进 `ps` 的输出、进容器的进程列表。`--password` 只留给非交互场景，
    并且帮助文本里写明了它的代价。
    """
    if supplied is not None:
        return supplied
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        print("Passwords do not match.", file=sys.stderr)
        raise SystemExit(2)
    return first


def create_admin(email: str, password: str | None) -> int:
    settings = get_settings()
    try:
        # ⚠️ 和登录端点走同一个函数。CLI 不校验的话，能建出一个登录端点拒绝的
        # 账号 —— 账号存在、密码正确、却永远登不进去。
        normalised = normalise_email(email)
    except InvalidEmail as exc:
        print(exc.message, file=sys.stderr)
        return 2
    secret = _read_password(password)

    try:
        validate_password_strength(secret, email=normalised)
    except WeakPassword as exc:
        print(exc.message, file=sys.stderr)
        return 2

    engine = create_database_engine(settings)
    session_factory = create_session_factory(engine)
    now = utc_now()

    with session_factory() as session:
        # 唯一约束是真正的保证；这次查询只是为了给出一句像样的错误。
        from sqlalchemy import select

        if session.execute(select(User).where(User.email == normalised)).scalar_one_or_none():
            print(f"An account already exists for {normalised}.", file=sys.stderr)
            return 1

        user = User(
            email=normalised,
            password_hash=hash_password(secret),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        session.flush()
        record_audit(
            session,
            action=AuditAction.USER_CREATED,
            # 这条不是 HTTP 请求，没有来源地址；写死 "cli" 比留空更好读。
            context=RequestContext(ip_address=None, user_agent="cli"),
            now=now,
            actor=user,
            entity_type="users",
            entity_id=str(user.id),
            # ⚠️ after_state 只放显式挑好的字段，绝不 dump 整个对象。
            after_state={"email": normalised, "role": UserRole.ADMIN.value},
            reason="BOOTSTRAP",
        )
        session.commit()

    print(f"Created admin {normalised}.")
    # ADMIN 的 2FA 是强制的（spec §54），所以新账号第一次登录会停在注册那一步。
    # 说清楚比让人对着 `stage=ENROL_2FA` 猜要好。
    print(
        "Next: sign in once to set up two-factor authentication "
        "(the first login returns stage=ENROL_2FA).",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    admin = sub.add_parser("create-admin", help="Create an ADMIN account.")
    admin.add_argument("--email", required=True)
    admin.add_argument(
        "--password",
        default=None,
        help="Avoid this outside automation: it lands in shell history and `ps` output.",
    )

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        return create_admin(args.email, args.password)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
