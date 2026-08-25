/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

/** ビルド番号(JST 日付 + コミット短縮ハッシュ)。vite.config.ts の define で注入される */
declare const __BUILD_NUMBER__: string

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
