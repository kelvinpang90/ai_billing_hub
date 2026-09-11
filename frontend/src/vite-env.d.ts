/// <reference types="vite/client" />

/**
 * ⚠️ 只有 `VITE_` 前缀的变量会被打进产物，而**打进去的东西是公开的** ——
 * 它们最终出现在浏览器能下载的 JS 里。任何密钥、内部主机名都绝不能走这里
 * （仓库公开，见 ADR-0001；后端的密钥走 Docker secrets，见 ADR-0004）。
 */
interface ImportMetaEnv {
  /** API 根地址。留空时用同源的 `/`，见 src/api/client.ts 的说明。 */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
