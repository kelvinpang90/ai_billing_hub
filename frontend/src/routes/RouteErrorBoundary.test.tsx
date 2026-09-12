/**
 * 分片加载失败时的兜底（T0.10）。
 *
 * ⚠️ 这一条钉的是一个**我自己的改动引入的**失败模式：路由级懒加载之后，切换
 * 路由要现去下载分片，而重新部署会让旧分片从服务器上消失 —— 开着旧标签页的
 * 用户点进另一个路由时请求的是一个已经不存在的文件。没有兜底的话 React 会把
 * 整棵树卸掉，**用户看到一片空白，没有任何提示**。
 *
 * 而「一片空白」正是最难从现象推回原因的那种故障：不报错、不跳转、什么都没有。
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import "../i18n";
import { RouteErrorBoundary } from "./RouteErrorBoundary";

function Exploding(): never {
  // `lazy()` 的分片拉不到时，React 抛出来的就是这一层 —— 对边界来说两者一样。
  throw new Error("Failed to fetch dynamically imported module");
}

describe("RouteErrorBoundary", () => {
  it("renders its children while nothing is wrong", () => {
    // ⚠️ 用 testid 而不是一句字面量文案：`no-hardcoded-jsx-text` 会（正确地）
    // 把后者判成违规，而这里要的本来就只是「子树渲染了没有」。
    render(
      <RouteErrorBoundary>
        <p data-testid="child" />
      </RouteErrorBoundary>,
    );
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("turns a failed chunk into something the user can act on", () => {
    // React 会把边界接住的异常照样打一遍到 console.error，这里静音，
    // 免得一条**预期之内**的报错看起来像用例出了问题。
    const silenced = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      render(
        <RouteErrorBoundary>
          <Exploding />
        </RouteErrorBoundary>,
      );

      expect(screen.getByText("This page could not be loaded")).toBeInTheDocument();
      // ⚠️ 必须给出**可执行的下一步**。只说「出错了」和一片空白的区别不大。
      expect(screen.getByRole("button", { name: "Reload the page" })).toBeInTheDocument();
    } finally {
      silenced.mockRestore();
    }
  });

  it("never puts the error text on screen", () => {
    const silenced = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      render(
        <RouteErrorBoundary>
          <Exploding />
        </RouteErrorBoundary>,
      );
      // 渲染期异常里可能带着组件的 props。§94 的同一条道理：不确定里面有什么，
      // 就不要显示它，也不要记它。
      expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument();
    } finally {
      silenced.mockRestore();
    }
  });
});
