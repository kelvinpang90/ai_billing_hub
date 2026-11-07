# acuven-billing-client

`acuven_billing_client` 是给外部接入方（不经 AI 模块调模型的应用）用的 Python 包：把用量事件可靠地上报给
Acuven 计费平台，并按平台的有效状态决定是否拦截新的模型调用。它负责建用量事件、本地 outbox、后台投递与重试、
请求签名、有效状态对账、状态 webhook 验签与健康指标，接入方不必了解计费 API 的细节。

设计依据：设计闸门 #571（`docs/design/AIH-TASK-164-billing-client.md`）。

- 只用标准库，没有第三方运行时依赖；需要 Python 3.12 或更高版本。
- 不发布到 PyPI。

## 安装

用 pip 从本仓库的 git 地址安装，**钉到一个提交**，并指定 `subdirectory=integration-client`：

```text
acuven-billing-client @ git+https://<仓库地址>.git@<提交 SHA>#subdirectory=integration-client
```

把上面这一行写进接入方自己的依赖文件（例如 `requirements.txt`）；`<仓库地址>` 与 `<提交 SHA>` 换成实际值。
升级时改钉的提交；包的版本号写在 `acuven_billing_client.__version__` 与 `pyproject.toml`，两处一致。

## 接入指南

接入指南在 AIH-TASK-171 补全。
