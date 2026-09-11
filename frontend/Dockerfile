# syntax=docker/dockerfile:1
#
# 前端镜像：node 构建静态产物，nginx 发出去。运行期没有 node。

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
FROM node:24-slim AS build

WORKDIR /src

# ⚠️ 先只 COPY 锁文件装依赖，再 COPY 源码。反过来的话，改一行组件就要重装
# 一遍 400 多个包。
COPY package.json package-lock.json ./
# `npm ci` 而不是 `npm install`：它严格照着 package-lock.json 装，锁文件与
# package.json 对不上时**直接失败**。install 会顺手改锁文件，那等于镜像里装的
# 东西和仓库里记录的不是一回事。
RUN npm ci --no-audit --no-fund

COPY tsconfig.json tsconfig.app.json tsconfig.test.json tsconfig.node.json ./
COPY vite.config.ts index.html ./
COPY src ./src

# 构建同时跑类型检查（package.json 的 build 是 `tsc --build && vite build`）——
# 类型错误在这里炸掉，比部署后在浏览器里炸掉便宜得多。
RUN npm run build

# ---------------------------------------------------------------------------
# runtime
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# 默认站点配置换成我们自己的（SPA 兜底 + 缓存策略）。
RUN rm -f /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/conf.d/billing-frontend.conf
COPY --from=build /src/dist /usr/share/nginx/html

EXPOSE 80

# ⚠️ 与边缘 nginx 一样，这里仍是官方镜像的默认跑法（master 进程是 root）。
# 换非 root 镜像属于生产加固，和边缘那层一起归 T0.9 —— 现在只改一层会留下
# 两套不一样的模式，比两层都不改更难维护。
