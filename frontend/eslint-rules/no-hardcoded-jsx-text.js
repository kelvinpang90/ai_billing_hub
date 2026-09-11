/**
 * 禁止把面向用户的文案硬编码在组件里（spec §3.2：frontend must be i18n-ready）。
 *
 * 为什么要自己写而不是用 eslint-plugin-react 的 jsx-no-literals：那个插件的 peer
 * 只到 eslint 9.7，而 eslint 9 已经停止支持。与其为一条规则把整个 lint 链钉死在
 * 一个不再收安全修复的大版本上，不如自己维护这 60 行 —— 何况这条规则是本项目的
 * 硬要求，本来就该由本项目负责。
 *
 * 它管三种写法：
 *   <p>Save</p>                 JSX 文本
 *   <p>{"Save"}</p>             表达式容器里的字符串
 *   <Input placeholder="Save">  面向用户的属性
 *
 * 不管的：纯空白、纯标点与数字（`·` `—` `(` `1` 之类排版字符不值得进翻译文件）。
 * 确实需要例外时用标准的 eslint-disable-next-line，**要写明理由**。
 */

/** 至少含一个字母才算「文案」。数字与标点不算。 */
const HAS_LETTER = /\p{L}/u;

/** 默认盯住这些属性：它们的值会直接显示给用户或被读屏软件念出来。 */
const DEFAULT_TEXT_PROPS = [
  "alt",
  "aria-label",
  "aria-placeholder",
  "description",
  "label",
  "placeholder",
  "title",
  "tooltip",
];

/** @type {import("eslint").Rule.RuleModule} */
const rule = {
  meta: {
    type: "problem",
    docs: {
      description: "Disallow user-facing text literals in JSX; use the t() function instead.",
    },
    schema: [
      {
        type: "object",
        properties: {
          textProps: { type: "array", items: { type: "string" }, uniqueItems: true },
        },
        additionalProperties: false,
      },
    ],
    messages: {
      hardcoded:
        'Hardcoded user-facing text "{{text}}". Move it into src/i18n/locales/en.json and render it with t().',
    },
  },

  create(context) {
    const textProps = new Set(context.options[0]?.textProps ?? DEFAULT_TEXT_PROPS);

    function report(node, raw) {
      const text = raw.trim();
      if (text === "" || !HAS_LETTER.test(text)) {
        return;
      }
      context.report({ node, messageId: "hardcoded", data: { text } });
    }

    function isStringLiteral(node) {
      return node?.type === "Literal" && typeof node.value === "string";
    }

    return {
      JSXText(node) {
        report(node, node.value);
      },

      JSXExpressionContainer(node) {
        // 只看直接渲染出去的那些：<p>{"Save"}</p>。属性里的由 JSXAttribute 管。
        const parentType = node.parent?.type;
        if (parentType !== "JSXElement" && parentType !== "JSXFragment") {
          return;
        }
        if (isStringLiteral(node.expression)) {
          report(node.expression, node.expression.value);
        }
      },

      JSXAttribute(node) {
        const name = node.name?.type === "JSXIdentifier" ? node.name.name : null;
        if (name === null || !textProps.has(name)) {
          return;
        }
        if (isStringLiteral(node.value)) {
          report(node.value, node.value.value);
          return;
        }
        if (
          node.value?.type === "JSXExpressionContainer" &&
          isStringLiteral(node.value.expression)
        ) {
          report(node.value.expression, node.value.expression.value);
        }
      },
    };
  },
};

export default rule;
