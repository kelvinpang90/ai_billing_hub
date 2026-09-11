/**
 * 应用的供应商装配处。顺序有讲究，见下面各处注释。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import enUS from "antd/locale/en_US";
import { BrowserRouter } from "react-router";

import { AppRoutes } from "./routes";

/**
 * ⚠️ `retry` 显式设成 1，不用默认的 3 次。
 *
 * 计费界面上多数请求要么是读账（重试没坏处但意义不大），要么将来是写操作
 * （重试**有**坏处）。默认 3 次会把一次后端 5xx 变成四次请求、把用户看到错误
 * 的时间推迟好几秒。写操作（mutation）的重试策略要在引入它的那个任务里单独定，
 * **不许靠这里的默认值**。
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
    mutations: { retry: 0 },
  },
});

export function App() {
  return (
    // ConfigProvider 的 locale 管的是 antd 自带文案（日期选择器、分页、空状态），
    // 和我们自己的 i18n 是两套。两套都要显式设成英文，否则组件内文案会跟着
    // 浏览器语言走，出现半中半英的界面。
    <ConfigProvider locale={enUS}>
      {/* antd v5 起静态方法 message.x() / Modal.x() 拿不到 ConfigProvider 的
          上下文（主题、locale 都会丢）。用 <App> 包一层，组件里走
          App.useApp() 的 hook 版本 —— 这样也就不需要 React 19 的兼容补丁包。 */}
      <AntdApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>
  );
}
