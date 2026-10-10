import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// OpenClaw Worker 的沙箱（AppContainer）里 lstat 不了 worktree 的上级目录，vitest 检查
// jsdom 是否安装时用的 realpath 因此失败，误报 MISSING DEPENDENCY 并以 1 退出。依赖由
// package-lock.json 固定、`npm ci` 装好，这道检查对本项目没有信息量，所以跳过。`??=`
// 保留外部显式给的值。见 .platform/commands.yaml 的 frontend.test。
process.env.VITEST_SKIP_INSTALL_CHECKS ??= "1";

export default defineConfig({
  plugins: [react()],

  resolve: {
    // Worker 里 node_modules 以只读 junction 提供。按真实路径解析的话，同一个包会经
    // junction 与真实路径各加载一份：vitest 被加载两次，jest-dom 的 matcher 注册到另一份
    // 上，toBeInTheDocument 报 Invalid Chai property。普通 `npm ci` 的 node_modules 没有
    // 链接，这条不改变任何解析结果。
    preserveSymlinks: true,
  },

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
    // 默认 5 秒在 OpenClaw Worker 的沙箱里不够：antd 表单类用例本机最慢不到 2 秒，沙箱里多个
    // 文件并行抢 CPU，会被拖过 5 秒（AIH-TASK-035 那次 run：本机 357 个全过，沙箱里 6 个旧
    // 用例连续几轮超时，每轮还不一样）。AIH-TASK-037 那次 run 又有一个单独给了 15 秒的旧用例
    // 在沙箱里超时，于是全局与各文件单独给的值统一提到 20 秒；真卡死的用例照样会超时。
    testTimeout: 20_000,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx", "eslint-rules/**/*.test.js"],
  },
});
