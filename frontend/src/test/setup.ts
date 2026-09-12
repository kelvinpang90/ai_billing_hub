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

afterEach(() => {
  cleanup();
});
