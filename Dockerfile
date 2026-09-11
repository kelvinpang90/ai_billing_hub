# syntax=docker/dockerfile:1
#
# 后端镜像：api / celery-worker / celery-beat 三个服务共用同一个镜像，只换 command。
# 三者跑的是同一份代码，分成三个镜像只会让「worker 和 api 版本不一致」变成可能。
#
# 两阶段：build 造 wheel，runtime 只装 wheel。这样 setuptools / build 这些
# 构建期工具不会留在运行镜像里。

# ---------------------------------------------------------------------------
# build —— 造 wheel
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS build

WORKDIR /src
# 只 COPY 打包真正需要的东西。COPY . 会把 .git、测试、文档一起带进构建缓存，
# 改一个 Markdown 就让 wheel 重建。
COPY pyproject.toml ./
COPY app ./app

RUN python -m pip install --no-cache-dir build==1.2.2.post1 \
    && python -m build --wheel --outdir /dist

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# ⚠️ 这个 UID 是 ADR-0004 要求 Phase 0 定下来的那一个（docs/adr/ADR-0004-credential-encryption.md）。
# 宿主机的主密钥文件必须 `chown 10001:10001` + `chmod 0400`，因为 Compose 的
# `file:` 型 secret 走 bind mount，`uid` / `gid` / `mode` 三个选项**只在 swarm
# 模式生效、普通 compose 下被忽略**——宿主机的属主与权限原样带进容器。
# **绝不允许为了读密钥把容器改回 root。**改这个数字就要同步改宿主机文件属主，
# tests/backend/test_compose.py 会钉住它与文档一致。
ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# ⚠️ WORKDIR **不能**叫 /app：cwd 会进 sys.path，一个叫 app 的目录在那里会让
# `import app` 的解析变得要靠运气。已装进 site-packages 的那一份才是唯一的一份。
WORKDIR /srv/billing

# alembic/ 与 alembic.ini **不在 wheel 里**（wheel 只打 `app*` 包），必须单独
# COPY。漏了的话，镜像一切正常，直到部署时 `alembic upgrade head` 报 no config。
COPY alembic.ini ./
COPY alembic ./alembic

# beat 要往磁盘写调度状态文件。非 root 进程写不进 /srv/billing（root 属主），
# 所以给它一个属于 APP_UID 的目录；compose 把命名卷挂在这里，卷会继承这个属主。
RUN mkdir -p /var/lib/celery && chown "${APP_UID}:${APP_GID}" /var/lib/celery

# 装完就地验一次「装进去的那一份」能用。PR #22 上真发生过 wheel 少打子包、
# 测试全绿而容器起不来——那种缺陷只有在安装后的环境里才看得见。
COPY scripts/packaging_smoke.py /tmp/packaging_smoke.py
RUN python /tmp/packaging_smoke.py && rm -f /tmp/packaging_smoke.py

USER ${APP_UID}:${APP_GID}

EXPOSE 8000

# 默认跑 API；worker / beat 在 compose 里覆盖 command。
# ⚠️ 这里**不做** `alembic upgrade head`。多个 API 实例同时启动会同时迁移
# （spec §100 要求 API 可水平扩展），而 §98 要求迁移有明确顺序、可回滚、
# 并发部署串行化。迁移是一条显式命令，不是启动副作用。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
