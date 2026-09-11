import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

import noHardcodedJsxText from "./eslint-rules/no-hardcoded-jsx-text.js";

const billing = {
  rules: { "no-hardcoded-jsx-text": noHardcodedJsxText },
};

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**"] },

  js.configs.recommended,
  tseslint.configs.recommendedTypeChecked,
  // ⚠️ 要 `configs.flat` 下的那一份。顶层同名的 `configs["recommended-latest"]`
  // 是旧 eslintrc 形状（plugins 是字符串数组），在 flat config 里会直接报错。
  reactHooks.configs.flat["recommended-latest"],

  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: globals.browser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: { billing },
    rules: {
      // spec §3.2：文案一律走 t()，不许硬编码在组件里。
      "billing/no-hardcoded-jsx-text": "error",
    },
  },

  // 规则自身与构建脚本是 Node 侧的普通 JS，不进 TS 的类型化 lint。
  {
    files: ["eslint.config.js", "eslint-rules/**/*.js"],
    languageOptions: { globals: globals.node },
    ...tseslint.configs.disableTypeChecked,
  },
);
