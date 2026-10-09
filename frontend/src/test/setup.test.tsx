/**
 * 公共测试配置（setup.ts）补的那些 jsdom 缺口：不带局部替身也能渲染用到它们的 antd 组件。
 *
 * ⚠️ 本文件**不许**自己补替身 —— 它测的正是「setup.ts 已经补好了」。
 */

import { render, screen } from "@testing-library/react";
import { Input } from "antd";
import { describe, expect, it } from "vitest";

describe("test setup", () => {
  it("renders antd TextArea without a local ResizeObserver stub", () => {
    render(<Input.TextArea />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("renders antd TextArea with autoSize without a local ResizeObserver stub", () => {
    render(<Input.TextArea autoSize />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });
});

describe("test setup timers", () => {
  // ⚠️ 这两条**按顺序**依赖：第一条排下定时器就结束，第二条确认它没有在第一条的
  // afterEach 之后触发。真实场景是 antd `Form.Item` 的 10ms 定时器在 jsdom 拆掉后
  // 才触发（见 setup.ts）；这里用 20ms，旧的「等一个 0ms 定时器」挡不住它。
  let leftoverFired = false;

  it("may leave a timer behind when it finishes", () => {
    setTimeout(() => {
      leftoverFired = true;
    }, 20);
  });

  it("never sees a timer the previous test left behind fire", async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));

    expect(leftoverFired).toBe(false);
  });
});
