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
    // T0.8c 起有了组件测试（登录页、路由守卫），所以默认环境从 node 换成 jsdom。
    // 纯逻辑的测试在 jsdom 里照跑不误；反过来「默认 node + 需要 DOM 的文件各自
    // 加 docblock」是个只在忘记时才发作的陷阱，而且报错是 `document is not
    // defined` 这种指不回原因的话。
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.ts", "src/**/*.test.tsx", "eslint-rules/**/*.test.js"],
  },
});
