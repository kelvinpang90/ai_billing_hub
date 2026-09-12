/**
 * 页面分片加载失败时的兜底（T0.10 的路由级懒加载派生）。
 *
 * ⚠️ **懒加载引入了一个原本不存在的失败模式。**改之前整个应用是一个分片，
 * 页面一打开就全在手上了；改之后切换路由要**现去下载**那一片。重新部署会让旧
 * 分片的文件名从服务器上消失 —— 于是一个开着旧标签页的用户点进另一个路由时，
 * 请求的是一个已经不存在的文件。
 *
 * 没有这层兜底的话，`lazy()` 的 Promise 被拒，React 把整棵树卸掉：**用户看到
 * 一片空白，控制台之外没有任何提示**。而这不是罕见情况，每次发版都会制造一批。
 *
 * 它刻意接住**所有**渲染期异常，不只是分片加载失败：分不清的时候，「告诉用户
 * 刷新一下」也比一片空白强。
 *
 * ⚠️ 不打印错误对象。渲染期异常里可能带着组件的 props（§94 的同一条道理：
 * 不确定内容里有什么，就不要把它写进任何日志）。排障靠 request_id 关联服务端。
 */

import { Component, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

interface State {
  failed: boolean;
}

export class RouteErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  // ⚠️ **刻意没有 `componentDidCatch`。**边界靠 `getDerivedStateFromError` 就能
  // 工作；`componentDidCatch` 存在的意义是「把错误报到某处」，而这里刻意不报
  // （见文件头）。留一个空实现只会让人以为那里将来要填点什么。

  render(): ReactNode {
    return this.state.failed ? <ReloadPrompt /> : this.props.children;
  }
}

/**
 * 文案要走 `t()`，而 `useTranslation` 是 hook —— 类组件里用不了，所以拆成一个
 * 函数组件。（错误边界目前只能是类组件，React 没给 hook 版本。）
 *
 * ⚠️ **刻意只用原生元素，不碰 antd。**两个理由，都很实在：
 *
 * 1. 这段 UI 存在的前提是「别的东西已经坏了」。它依赖的东西越少，能在越多种
 *    坏法下还画得出来 —— 一个自己也要先加载一堆组件的错误页，正好会在最需要
 *    它的时候一起挂掉
 * 2. 错误边界在**首屏**的依赖图里（它必须是）。第一版用了 antd 的 `Result`，
 *    于是那 35 kB 从按需分片挪进了入口分片，**把路由懒加载省下的 40 kB 吃掉了
 *    大半**（实测 `/login` 首屏从 933.91 反弹到 968.79 kB）。见 perf-baseline.md 第 7 节
 */
function ReloadPrompt() {
  const { t } = useTranslation();
  return (
    <div style={{ maxWidth: 420, margin: "96px auto", padding: "0 24px", textAlign: "center" }}>
      <h2 style={{ marginBottom: 8, fontSize: 20 }}>{t("appError.title")}</h2>
      <p style={{ marginBottom: 24, color: "#595959" }}>{t("appError.description")}</p>
      <button type="button" onClick={() => window.location.reload()}>
        {t("appError.reload")}
      </button>
    </div>
  );
}
