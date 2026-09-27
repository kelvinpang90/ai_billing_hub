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
