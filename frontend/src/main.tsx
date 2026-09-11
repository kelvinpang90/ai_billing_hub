import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// ⚠️ i18n 必须在任何组件渲染之前完成初始化，否则首帧会渲染出缺 key 的标记。
// 这个 import 有副作用（它 init 了全局实例），所以位置不能动。
import "./i18n";

import { App } from "./App";

const container = document.getElementById("root");
if (container === null) {
  // 挂载点不在 = index.html 被改坏了。安静失败会得到一个空白页面，
  // 而空白页面是最难排查的一种故障。
  throw new Error("Missing #root element; index.html is not the one this build expects.");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
