"""HTTP layer: routers and request/response wiring.

这一层只做协议转换（解析、鉴权、序列化），业务规则一律下沉到 services/。
分组按 spec §101：auth / admin / customer / integration / webhooks，各自在对应
Phase 落地时创建，现在不预建空目录。
"""
