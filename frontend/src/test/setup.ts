/**
 * 每个测试文件跑之前先执行这里。
 *
 * ⚠️ `cleanup()` 不是可选的：testing-library 把组件挂到同一个 document 上，
 * 不卸载的话下一个测试里 `getByRole` 会同时看见上一个测试留下的那棵树，
 * 报「found multiple elements」——而那条报错**指向的是无辜的那个测试**。
 */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

/**
 * jsdom 没实现 `matchMedia`，而 antd 的响应式栅格在挂载时就会调它。
 * 补一个永远「不匹配」的实现 —— 等于所有断点都按最小屏算，够用了：
 * 这个仓库不测响应式布局，真要测也得用真浏览器。
 */
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }) as MediaQueryList,
});

/**
 * jsdom 也没有 `ResizeObserver`，而 antd 的 `Input.TextArea` 不管开没开 `autoSize`
 * 挂载时都要它，缺了直接 `ReferenceError: ResizeObserver is not defined`。
 * AIH-TASK-016 的 Worker run 就栽在这里：测试文件只能各自补，公共的放这里一处。
 * 空实现即可 —— 这个仓库不测尺寸变化。只在没有时才装，不覆盖环境自带的实现。
 */
class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = NoopResizeObserver;
}

afterEach(async () => {
  cleanup();

  // ⚠️ 光 `cleanup()` 不够：antd 的按钮 loading 走 `@rc-component/util` 的
  // `useDelayState`，它排了一个 `setTimeout`，而**卸载并不会把它取消**。测试结束
  // 得够快的话，那个回调会在 jsdom 已经拆掉之后才触发，抛
  // `ReferenceError: window is not defined`。
  //
  // 症状极具迷惑性：**所有测试都显示通过**，只在末尾多出几个 "Uncaught Exception"，
  // 而且它指的文件常常不是真正排下那个定时器的地方。更糟的是它**取决于时序** ——
  // 本地连跑几次都不出现，在 CI 上偶发（T0.10 的 CI 上真的中了一次）。
  //
  // 这里 await 一个 0ms 定时器：先前排下的那些会在它之前烧掉，而此刻 jsdom 还活着。
  await new Promise((resolve) => setTimeout(resolve, 0));
});
