"""Envelope encryption (ADR-0004)."""

from __future__ import annotations

import base64
import os

import pytest

from app.core.config import Settings
from app.core.crypto import (
    DecryptionFailed,
    EncryptionNotConfigured,
    decrypt_secret,
    encrypt_secret,
    load_keyring,
)

SECRET = "JBSWY3DPEHPK3PXP"


def write_keyring(tmp_path, *versions: int):
    lines = [f"{version}:{base64.b64encode(os.urandom(32)).decode()}" for version in versions]
    path = tmp_path / "master.key"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_keyring(Settings(master_key_file=str(path)))


def test_round_trip(tmp_path) -> None:
    keyring = write_keyring(tmp_path, 1)
    token, version = encrypt_secret(keyring, SECRET)
    assert version == 1
    assert decrypt_secret(keyring, token) == SECRET


def test_the_plaintext_never_appears_in_the_stored_token(tmp_path) -> None:
    keyring = write_keyring(tmp_path, 1)
    token, _ = encrypt_secret(keyring, SECRET)
    assert SECRET not in token


def test_encrypting_twice_gives_different_ciphertext(tmp_path) -> None:
    """⚠️ 每次现取随机 nonce 与新 DEK。

    相同密文意味着 nonce 被重用 —— AES-GCM 下重用一次 nonce，就能从两段密文
    推出明文异或，认证也一起失效。
    """
    keyring = write_keyring(tmp_path, 1)
    first, _ = encrypt_secret(keyring, SECRET)
    second, _ = encrypt_secret(keyring, SECRET)
    assert first != second


def test_tampering_is_detected(tmp_path) -> None:
    """AES-GCM 带认证标签：改一个字节就解不开，而不是解出垃圾。"""
    keyring = write_keyring(tmp_path, 1)
    token, _ = encrypt_secret(keyring, SECRET)
    marker, version, wrapped, nonce, ciphertext = token.split(".")
    flipped = ciphertext[:-4] + ("A" if ciphertext[-4] != "A" else "B") + ciphertext[-3:]
    with pytest.raises(DecryptionFailed):
        decrypt_secret(keyring, ".".join((marker, version, wrapped, nonce, flipped)))


def test_another_keyring_cannot_open_it(tmp_path) -> None:
    mine = write_keyring(_subdir(tmp_path, "a"), 1)
    other = write_keyring(_subdir(tmp_path, "b"), 1)
    token, _ = encrypt_secret(mine, SECRET)
    with pytest.raises(DecryptionFailed):
        decrypt_secret(other, token)


def test_rotation_keeps_old_rows_readable(tmp_path) -> None:
    """⚠️ 这是信封加密存在的理由。

    换主密钥时只需重新包裹 DEK、不碰密文；而在重包完成之前，**老行必须仍然
    读得出来** —— 否则轮换那一刻所有历史数据立刻不可用。
    """
    old_dir = _subdir(tmp_path, "old")
    old = write_keyring(old_dir, 1)
    token, version = encrypt_secret(old, SECRET)
    assert version == 1

    # 钥匙串里再加一把版本 2：新写入用 2，老行仍按 1 解开。
    both_text = (old_dir / "master.key").read_text(encoding="utf-8").strip()
    both_text += f"\n2:{base64.b64encode(os.urandom(32)).decode()}\n"
    both = load_keyring(Settings(master_key_file=str(_write(tmp_path / "both.key", both_text))))

    assert both.active_version == 2
    assert decrypt_secret(both, token) == SECRET, "轮换后老行必须仍然读得出来"
    _, new_version = encrypt_secret(both, SECRET)
    assert new_version == 2, "新写入要用最新那把"


def test_a_missing_key_version_fails_loudly(tmp_path) -> None:
    """密文里的版本号钥匙串里没有 = 这个部署丢了一把老密钥。必须报错，不能猜。"""
    old = write_keyring(_subdir(tmp_path, "old"), 1)
    token, _ = encrypt_secret(old, SECRET)

    newer = write_keyring(_subdir(tmp_path, "new"), 2)
    with pytest.raises(DecryptionFailed):
        decrypt_secret(newer, token)


def test_without_a_key_file_it_refuses_instead_of_inventing_one() -> None:
    """⚠️ **绝不能**在没配主密钥时临时造一把：那样重启之后所有密文都读不出来。"""
    with pytest.raises(EncryptionNotConfigured):
        load_keyring(Settings(master_key_file=""))


def test_a_missing_file_does_not_leak_its_path(tmp_path) -> None:
    missing = tmp_path / "nope" / "master.key"
    with pytest.raises(EncryptionNotConfigured) as excinfo:
        load_keyring(Settings(master_key_file=str(missing)))
    assert "nope" not in str(excinfo.value)


def test_a_corrupt_entry_fails_loudly_instead_of_being_skipped(tmp_path) -> None:
    """⚠️ 静默跳过坏行的话，钥匙串会少一把，表现是「某些行突然读不出来」——
    而那时候已经没人记得改过这个文件。"""
    path = _write(tmp_path / "master.key", "1:not-base64!!\n")
    with pytest.raises(EncryptionNotConfigured):
        load_keyring(Settings(master_key_file=str(path)))


def test_a_short_key_is_rejected(tmp_path) -> None:
    """AES-256 要 32 字节。短了会被 cryptography 拒绝，但要在加载时就拒绝。"""
    path = _write(tmp_path / "master.key", f"1:{base64.b64encode(b'too-short').decode()}\n")
    with pytest.raises(EncryptionNotConfigured):
        load_keyring(Settings(master_key_file=str(path)))


def test_an_empty_file_is_rejected(tmp_path) -> None:
    path = _write(tmp_path / "master.key", "# only a comment\n\n")
    with pytest.raises(EncryptionNotConfigured):
        load_keyring(Settings(master_key_file=str(path)))


def _subdir(root, name: str):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
