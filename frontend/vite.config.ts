import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],

  server: {
    // `npm run dev` 时把 API 请求转给本机 Compose 栈的 nginx（T0.6 默认发布
    // 8080）。没有这一段的话，浏览器会把 /healthz 发到 Vite 自己的端口上拿到
    // 404，而那个 404 长得**很像**后端挂了。
    proxy: {
      "/healthz": "http://127.0.0.1:8080",
      "/api": "http://127.0.0.1:8080",
    },
  },

  build: {
    // 源码映射会把完整源码摆到公网上。计费平台的前端逻辑里有价格展示、
    // 状态机分支，不需要对外可读。排障靠 request_id 关联服务端日志。
    sourcemap: false,
  },

  test: {
    // 骨架阶段的测试全是纯逻辑（信封解包、i18n key、lint 规则），不碰 DOM。
    // 组件渲染测试要等 T0.8 引入 testing-library，见 TODO 的 T0.7 记录。
    environment: "node",
    include: ["src/**/*.test.ts", "eslint-rules/**/*.test.js"],
  },
});
