/**
 * 这条规则是「文案不许硬编码在组件里」的**唯一**机械保证，所以它自己也得有
 * 测试 —— 一条永不触发的 lint 规则和没有规则完全一样，而且看起来更让人放心。
 */

import { RuleTester } from "eslint";
import { describe, it } from "vitest";

import rule from "./no-hardcoded-jsx-text.js";

const ruleTester = new RuleTester({
  languageOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

describe("no-hardcoded-jsx-text", () => {
  it("flags user-facing literals and leaves everything else alone", () => {
    ruleTester.run("no-hardcoded-jsx-text", rule, {
      valid: [
        // 走 t() 的正常写法
        'const A = () => <p>{t("dashboard.title")}</p>;',
        'const A = () => <Input placeholder={t("search")} />;',
        // 变量与数字不是文案
        "const A = () => <p>{name}</p>;",
        "const A = () => <p>{42}</p>;",
        // 纯排版字符不值得进翻译文件
        'const A = () => <p>{" · "}</p>;',
        "const A = () => <p>{\"(\"}{value}{\")\"}</p>;",
        // 非文案属性照常写字面量
        'const A = () => <Button type="primary" />;',
        'const A = () => <div className="wrapper" />;',
      ],
      invalid: [
        {
          code: "const A = () => <p>Save</p>;",
          errors: [{ messageId: "hardcoded" }],
        },
        {
          code: 'const A = () => <p>{"Save"}</p>;',
          errors: [{ messageId: "hardcoded" }],
        },
        {
          code: 'const A = () => <Input placeholder="Search customers" />;',
          errors: [{ messageId: "hardcoded" }],
        },
        {
          code: 'const A = () => <img alt="Company logo" />;',
          errors: [{ messageId: "hardcoded" }],
        },
        {
          code: 'const A = () => <Card title={"Wallet"} />;',
          errors: [{ messageId: "hardcoded" }],
        },
        {
          // 夹在表达式之间的裸文本最容易漏看
          code: 'const A = () => <p>Balance: {amount}</p>;',
          errors: [{ messageId: "hardcoded" }],
        },
      ],
    });
  });
});
