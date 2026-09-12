"""Password hashing and strength rules (spec §53)."""

from __future__ import annotations

import pytest

from app.core.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    WeakPassword,
    common_passwords,
    hash_password,
    validate_password_strength,
    verify_password,
)

GOOD = "a-perfectly-fine-passphrase"


def test_hash_is_argon2id_and_verifies() -> None:
    encoded = hash_password(GOOD)
    # 前缀里带着算法与参数 —— 这正是以后能逐个升级开销参数的原因。
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, GOOD)


def test_wrong_password_returns_false_instead_of_raising() -> None:
    """调用方要的是一个布尔值。让它抛异常，早晚有人用 try/except 当条件分支。"""
    assert verify_password(hash_password(GOOD), "not-the-password") is False


def test_a_corrupt_hash_does_not_blow_up() -> None:
    """库里的哈希被改坏时必须是「验不过」，不能是 500。"""
    assert verify_password("not-a-real-hash", GOOD) is False


def test_two_hashes_of_the_same_password_differ() -> None:
    """带盐。两次相同说明盐没生效，那样同密码的用户在库里可见地相同。"""
    assert hash_password(GOOD) != hash_password(GOOD)


@pytest.mark.parametrize("length", [MIN_PASSWORD_LENGTH - 1, MAX_PASSWORD_LENGTH + 1])
def test_length_bounds_are_enforced(length: int) -> None:
    with pytest.raises(WeakPassword):
        validate_password_strength("a" * length)


def test_the_lower_bound_itself_is_accepted() -> None:
    """边界值要两侧都测：只测「太短被拒」的话，把下限设成 100 也能通过。"""
    validate_password_strength("a" * MIN_PASSWORD_LENGTH)


def test_the_bounds_themselves_are_sane() -> None:
    """⚠️ 绝对断言，不跟着常量走。

    上面那些用例写的是 `MIN_PASSWORD_LENGTH - 1`，**把下限改成 1 它们照样绿** ——
    变异测试抓到过这一点。相对断言只能证明「边界两侧行为不同」，证明不了
    边界本身在合理的位置。
    """
    assert MIN_PASSWORD_LENGTH >= 12, "NIST SP 800-63B 的长度下限"
    # 上限不是洁癖：Argon2 的开销随输入长度走，不设上限就是一条 DoS 通道。
    assert MAX_PASSWORD_LENGTH <= 1024


def test_the_bundled_wordlist_is_the_real_thing() -> None:
    """⚠️ 绝对断言。

    第一版只放了 13 条占位并把真表推给 T0.9 —— 实现闸门判为阻断项，判得对：
    设计 v5 写的是内置约一万条，把它推给下一个任务和「写进不做」没有区别。
    """
    words = common_passwords()
    assert len(words) > 9_000
    # 抽查几条公认的弱口令确实在表里。
    assert {"password", "123456", "qwerty", "letmein"} <= words


def test_common_passwords_are_rejected() -> None:
    """够长、但在表里 —— 这正是长度下限挡不住、需要这份表的那一类。"""
    long_but_common = [word for word in common_passwords() if len(word) >= MIN_PASSWORD_LENGTH]
    assert long_but_common, "otherwise this check can never fire"
    for word in long_but_common[:20]:
        with pytest.raises(WeakPassword):
            validate_password_strength(word)


def test_matching_is_case_insensitive() -> None:
    """表里存的是小写。大小写变体是**最廉价的**绕过，必须一起挡。"""
    sample = next(word for word in common_passwords() if len(word) >= MIN_PASSWORD_LENGTH)
    with pytest.raises(WeakPassword):
        validate_password_strength(sample.upper())


def test_the_length_floor_does_most_of_the_work() -> None:
    """⚠️ 这条记录的是一个**实测事实**，不是断言表的大小。

    一万条常见口令里只有二十几条长度 ≥ 12 —— 12 位下限已经挡掉了 99.8%。
    所以真正在挡弱口令的是长度下限，这份表补的是那少数「够长但仍然烂」的。
    把这个数字钉下来，是为了让以后有人想放宽长度下限时，能看到代价有多大。
    """
    long_ones = [word for word in common_passwords() if len(word) >= MIN_PASSWORD_LENGTH]
    assert len(long_ones) < len(common_passwords()) // 100


def test_password_matching_the_account_name_is_rejected() -> None:
    """弱口令表里查不到，但被猜中的代价一样。"""
    with pytest.raises(WeakPassword):
        validate_password_strength("kelvin.admin", email="Kelvin.Admin@example.com")


def test_no_composition_rules() -> None:
    """⚠️ 刻意不强制大小写 / 数字 / 符号（NIST SP 800-63B）。

    组成规则把人推向 `Password1!` 这类可预测模式。这条用例是**反向**的：
    它保证以后没人「顺手加强一下」把组成规则加回来。
    """
    validate_password_strength("aaaaaaaaaaaaaaaa")
