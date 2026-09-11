# archive — 历史评审记录

这里的文件是对**某个历史版本** spec 的评审与核对记录，内容冻结，只作追溯用。

| 文件 | 针对的版本 | 是什么 |
| --- | --- | --- |
| [SPEC_REVIEW_v1.0.md](SPEC_REVIEW_v1.0.md) | v1.0 | 评审意见（P0×7 / P1×10 / P2×8） |
| [REVIEW_FOLLOWUP_v1.1.md](REVIEW_FOLLOWUP_v1.1.md) | v1.1 | 25 条意见在 v1.1 的落实核对（20 解决 / 5 残留） |

⚠️ **这里的 `§N` 指的是当时那个版本的章节**。现行 spec 已退役若干章节、也改过若干条文，所以 `scripts/check_docs.py` 对本目录**不校验章节引用**（链接与约定串照查）。要查某条意见现在落在哪，以 [docs/REQUIREMENTS.md](../REQUIREMENTS.md) 的追溯表与 [docs/REVIEW-LOG.md](../REVIEW-LOG.md) 为准。

放进这里的条件：文件评审 / 核对的对象是一个**已被后续版本取代**的 spec，且后续版本已把结论吸收或在 TODO / ADR 里登记。放进来之后不再修改正文。
