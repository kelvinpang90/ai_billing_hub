"""Envelope encryption for secrets we must be able to read back (ADR-0004).

**为什么不能像密码那样单向哈希**：TOTP 校验、HMAC 验签都需要**密钥原文**。
spec 第 1542 行写死了这一点 —— 「Password-style one-way hashing is not valid
for these secrets」。

**信封**（ADR-0004 第 1 节）：

```text
明文  --AES-256-GCM(DEK)-->  密文
DEK   --AES-256-GCM(主密钥)-->  包裹后的 DEK，与密文同存
主密钥 --> 宿主机文件，经 Docker secret 注入
```

一条 secret 一把 DEK。**这正是「换主密钥时只需重新包裹 DEK、不碰密文」的原因** ——
否则轮换主密钥要把整张表解密再加密一遍。

存储格式是一个自描述的字符串，一列装得下：

```text
v1.<key_version>.<wrapped_dek>.<nonce>.<ciphertext>
```

`key_version` 另外单独存一列，是为了轮换时能**查出还有哪些行用着老版本** ——
只存在字符串里就得全表扫。
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings
from app.core.errors import AppError

logger = logging.getLogger(__name__)

FORMAT_VERSION = "v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class EncryptionNotConfigured(AppError):
    """No master key is available, so this deployment cannot handle secrets."""

    def __init__(self) -> None:
        super().__init__(
            "Secret storage is not configured.",
            code="ENCRYPTION_NOT_CONFIGURED",
            http_status=503,
        )


class DecryptionFailed(AppError):
    """The ciphertext could not be opened — wrong key, or it was tampered with.

    ⚠️ **不要对调用方区分「密钥不对」与「数据被改过」。**AES-GCM 的认证标签
    验不过这两种情况长得一样，而区分它们只对攻击者有价值。
    """

    def __init__(self) -> None:
        super().__init__(
            "A stored secret could not be read.",
            code="DECRYPTION_FAILED",
            http_status=500,
        )


@dataclass(frozen=True)
class MasterKeyring:
    """Every master key this deployment can decrypt with, newest first.

    ⚠️ **必须能同时持有多把。**轮换时新写入用新版本，而库里还有一堆用老版本
    包裹的行；只留一把的话，换密钥的那一刻所有历史数据立刻读不出来。
    重新包裹那些老行是一个单独的后台任务（尚未实现，见 TODO）。
    """

    keys: dict[int, bytes]
    active_version: int

    def key_for(self, version: int) -> bytes:
        key = self.keys.get(version)
        if key is None:
            # 版本号在密文里，钥匙串里却没有 —— 说明这个部署丢了一把老密钥。
            logger.error("No master key for the requested version", extra={"key_version": version})
            raise DecryptionFailed
        return key


def _decode_key(raw: str) -> bytes:
    key = base64.b64decode(raw, validate=True)
    if len(key) != _KEY_BYTES:
        raise ValueError(f"a master key must be {_KEY_BYTES} bytes, got {len(key)}")
    return key


def load_keyring(settings: Settings) -> MasterKeyring:
    """Read the master keyring from the file named by settings.

    文件格式，一行一把，**版本号最大的那把用于加密**：

    ```text
    1:<base64 的 32 字节>
    2:<base64 的 32 字节>
    ```

    ⚠️ 只写一把裸 base64（没有 `版本:` 前缀）也接受，当作版本 1 —— 但那样
    将来轮换要先改文件格式。生成方式写在 `.env.example` 里。
    """
    path = settings.master_key_file.strip()
    if not path:
        raise EncryptionNotConfigured
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        # ⚠️ 不把路径写进异常消息：它会进 §107 的错误响应。
        logger.error("Could not read the master key file", exc_info=True)
        raise EncryptionNotConfigured from None

    keys: dict[int, bytes] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        version_text, _, key_text = entry.partition(":")
        try:
            version = int(version_text) if key_text else 1
            keys[version] = _decode_key(key_text or version_text)
        except ValueError:
            # ⚠️ 坏行**不静默跳过**：一个笔误让钥匙串少一把，表现是「某些行突然
            # 读不出来」，而那时候已经没人记得改过这个文件。
            logger.error("The master key file has an unusable entry")
            raise EncryptionNotConfigured from None

    if not keys:
        logger.error("The master key file is empty")
        raise EncryptionNotConfigured
    return MasterKeyring(keys=keys, active_version=max(keys))


def encrypt_secret(keyring: MasterKeyring, plaintext: str) -> tuple[str, int]:
    """Encrypt with a fresh DEK. Returns (stored string, key version)."""
    dek = AESGCM.generate_key(bit_length=256)
    data_nonce = _random_nonce()
    ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext.encode("utf-8"), None)

    wrap_nonce = _random_nonce()
    master = keyring.key_for(keyring.active_version)
    wrapped = AESGCM(master).encrypt(wrap_nonce, dek, None)

    token = ".".join(
        (
            FORMAT_VERSION,
            str(keyring.active_version),
            _b64(wrap_nonce + wrapped),
            _b64(data_nonce),
            _b64(ciphertext),
        )
    )
    return token, keyring.active_version


def decrypt_secret(keyring: MasterKeyring, token: str) -> str:
    """Open a stored secret, or raise :class:`DecryptionFailed`."""
    try:
        marker, version_text, wrapped_text, nonce_text, ciphertext_text = token.split(".")
        if marker != FORMAT_VERSION:
            raise ValueError(f"unknown format marker: {marker}")
        version = int(version_text)
        wrapped_blob = _unb64(wrapped_text)
        data_nonce = _unb64(nonce_text)
        ciphertext = _unb64(ciphertext_text)
    except (ValueError, TypeError):
        logger.error("A stored secret is not in the expected format")
        raise DecryptionFailed from None

    master = keyring.key_for(version)
    try:
        dek = AESGCM(master).decrypt(wrapped_blob[:_NONCE_BYTES], wrapped_blob[_NONCE_BYTES:], None)
        return AESGCM(dek).decrypt(data_nonce, ciphertext, None).decode("utf-8")
    except (InvalidTag, ValueError):
        # 密钥不对与数据被篡改在这里长得一样 —— 对外也保持一样。
        logger.error("A stored secret failed authentication", extra={"key_version": version})
        raise DecryptionFailed from None


def _random_nonce() -> bytes:
    # ⚠️ GCM 的 nonce **绝不能重复**：同一把密钥下重用一次 nonce，就能从两段
    # 密文推出明文异或，认证也一起失效。每次现取随机数，不用计数器。
    return os.urandom(_NONCE_BYTES)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)
