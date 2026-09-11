/**
 * 源码里用到的每个 i18n key 都必须在 en.json 里存在，反之也不许有没人用的 key。
 *
 * ⚠️ 缺 key 的失败方式是**安静**的：i18next 默认把 key 原样渲染给用户，看上去
 * 像一句奇怪的英文，不报错也不告警。`src/i18n/index.ts` 已经把它改成醒目标记，
 * 但那只在跑起来之后才看得见；这条用例让它根本进不了 main。
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import en from "./locales/en.json";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

/** 匹配 `t("key")` / `t('key')`。刻意只认字面量。 */
const T_CALL = /\bt\(\s*["']([^"']+)["']/g;

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const full = join(directory, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(full);
    }
    if (/\.tsx?$/.test(entry.name) && !entry.name.endsWith(".test.ts")) {
      return [full];
    }
    return [];
  });
}

function usedKeys(): Map<string, string[]> {
  const used = new Map<string, string[]>();
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, "utf8");
    for (const match of text.matchAll(T_CALL)) {
      const key = match[1];
      if (key === undefined) {
        continue;
      }
      used.set(key, [...(used.get(key) ?? []), file]);
    }
  }
  return used;
}

describe("i18n keys", () => {
  it("finds the t() calls at all", () => {
    // ⚠️ 没有这一条，正则一旦写错（永不匹配），下面两条会在「零个 key」上
    // 空转着通过 —— 测试全绿而校验什么也没做。
    expect(usedKeys().size).toBeGreaterThan(3);
  });

  it("has an English string for every key used in the source", () => {
    const missing = [...usedKeys()].filter(([key]) => !(key in en));
    expect(missing).toEqual([]);
  });

  it("has no unused entries in en.json", () => {
    // 翻译文件里的死 key 会被翻译成别的语言、被人当作现存功能读，是纯负债。
    const used = usedKeys();
    expect(Object.keys(en).filter((key) => !used.has(key))).toEqual([]);
  });

  it("has no empty translations", () => {
    expect(Object.entries(en).filter(([, value]) => value.trim() === "")).toEqual([]);
  });
});
