/**
 * i18n 配置。spec §3.2：**V1 只出英文，但结构必须是 i18n-ready 的** ——
 * 以后要加中文与马来文，那时候再回头把散在组件里的文案抠出来，代价高得多。
 *
 * 「文案不许硬编码在组件里」这条由 `eslint-rules/no-hardcoded-jsx-text.js`
 * 机械保证；「用到的 key 必须真的存在」由 `keys.test.ts` 机械保证。
 * 两条都不是靠自觉。
 */

import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import en from "./locales/en.json";

export const DEFAULT_LANGUAGE = "en";
export const TRANSLATION_NAMESPACE = "translation";

/** 缺 key 时渲染成这个样子 —— 见下面对 `parseMissingKeyHandler` 的说明。 */
export function missingKeyMarker(key: string): string {
  return `⟪ missing:${key} ⟫`;
}

void i18n.use(initReactI18next).init({
  resources: { [DEFAULT_LANGUAGE]: { [TRANSLATION_NAMESPACE]: en } },
  lng: DEFAULT_LANGUAGE,
  fallbackLng: DEFAULT_LANGUAGE,

  // ⚠️ 两个分隔符都关掉，key 一律当成**扁平字符串**。
  //
  // 默认开着时 `"wallet.balance"` 会被拆成 `resources.wallet.balance` 去查。
  // 于是「key 不存在」和「key 的父节点是个字符串」两种情况**失败方式一模一样**，
  // 都是安静地把 key 本身渲染出去。关掉之后 key 就是一次普通的对象属性查找，
  // keys.test.ts 也才能做到逐字精确校验。
  keySeparator: false,
  nsSeparator: false,

  interpolation: {
    // React 自己就会转义，i18next 再转一次会把 & 变成 &amp;。
    escapeValue: false,
  },

  // ⚠️ 缺 key 时 i18next 默认**把 key 原样渲染给用户**，看上去像一句奇怪的英文，
  // 不报错、不告警。这里改成一眼能看出不对的形状；CI 里由 keys.test.ts 兜底，
  // 保证它根本到不了用户面前。
  parseMissingKeyHandler: missingKeyMarker,
});

export default i18n;
