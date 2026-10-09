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

/**
 * 记下每个测试排下、还没触发的 `setTimeout`，测试结束时一并取消（见下方 afterEach）。
 * 只包一层登记，参数与返回值原样转给原来的实现。
 */
const pendingTimeouts = new Set<ReturnType<typeof setTimeout>>();
const originalSetTimeout = globalThis.setTimeout;
const originalClearTimeout = globalThis.clearTimeout;
globalThis.setTimeout = ((handler: (...args: unknown[]) => void, ms?: number, ...args: unknown[]) => {
  const handle = originalSetTimeout(
    (...callArgs: unknown[]) => {
      pendingTimeouts.delete(handle);
      handler(...callArgs);
    },
    ms,
    ...args,
  );
  pendingTimeouts.add(handle);
  return handle;
}) as typeof setTimeout;
globalThis.clearTimeout = ((handle?: ReturnType<typeof setTimeout>) => {
  if (handle !== undefined) {
    pendingTimeouts.delete(handle);
  }
  originalClearTimeout(handle);
}) as typeof clearTimeout;

afterEach(() => {
  cleanup();

  // ⚠️ 光 `cleanup()` 不够：`@rc-component/util` 的 `useDelayState` 排下的
  // `setTimeout` **卸载时不会被取消**。最常见的是每个 `Form.Item` 都有的
  // `ErrorList`：antd 的 `useDebounce` 在错误列表为空时排一个 **10ms** 的定时器，
  // 挂载时就排。文件里最后一个测试若在 10ms 内跑完，回调会在 jsdom 已经拆掉之后
  // 才触发，`setState` 进 react-dom 读 `window.event`，抛
  // `ReferenceError: window is not defined`。
  //
  // 症状极具迷惑性：**所有测试都显示通过**，只在末尾多出几个 "Uncaught Exception"。
  // 它**取决于时序** —— 本地连跑几次都不出现，在 CI 上偶发（T0.10 中过一次；
  // 2026-10-09 #237、#238 合并后 main 连中两次，指向 ResetPasswordPage.test.tsx）。
  //
  // 以前这里 await 一个 0ms 定时器，指望先前排下的先烧掉 —— 对 0ms 的有效，对 10ms
  // 的无效。改成等更久只是换一个会被下一个组件打破的数字，所以直接取消：组件都已
  // 卸载，这些回调剩下的只有对已卸载组件的 `setState`，取消不丢任何东西。
  for (const handle of pendingTimeouts) {
    originalClearTimeout(handle);
  }
  pendingTimeouts.clear();
});
