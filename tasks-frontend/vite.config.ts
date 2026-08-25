import { execSync } from 'node:child_process'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

/**
 * ビルド番号(JST の日付 + コミット短縮ハッシュ)。フッターに出して、
 * いま動いているのがどのビルドか(SW の旧キャッシュ・未更新のイメージ)を見分けられるようにする。
 * Docker のビルドコンテキストには .git を含めないため、CI からは build-arg の GIT_SHA で受け取り、
 * 手元のビルドでは git から直接引く。どちらも無ければ nogit。
 */
function buildNumber(): string {
  let sha = process.env.GIT_SHA?.trim().slice(0, 7)
  if (!sha) {
    try {
      sha = execSync('git rev-parse --short=7 HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
        .toString()
        .trim()
    } catch {
      sha = 'nogit'
    }
  }
  // タイムゾーンは固定する —— ビルドしたマシンの設定で日付がずれると、
  // 同じコミットから別の番号が出て「どのビルドか」の手掛かりにならないため。
  // sv-SE ロケールは YYYY-MM-DD 固定なので、そのまま日付表記に使える
  const date = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Tokyo' })
    .format(new Date())
    .replaceAll('-', '')
  return `${date}-${sha}`
}

export default defineConfig({
  define: {
    __BUILD_NUMBER__: JSON.stringify(buildNumber()),
  },
  plugins: [
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      // アプリシェルだけを precache する。データは常にオンラインから取る
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,ico,webmanifest}'],
        // /api は network-only (仕様書 §8: データの陳腐化防止)
        navigateFallbackDenylist: [/^\/api\//, /^\/oauth2\//, /^\/login\//],
        runtimeCaching: [
          {
            urlPattern: /^\/api\//,
            handler: 'NetworkOnly',
          },
        ],
      },
      manifest: {
        name: 'やること — Chiezo',
        short_name: 'やること',
        description: 'Claude Code に依頼したいことと、守らせたい共通ルール',
        theme_color: '#5560E0',
        background_color: '#12181f',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        scope: '/',
        lang: 'ja',
        icons: [
          { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: '/icons/icon-512-maskable.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 本体(chiezo-api)が 7010、その周辺が 7011〜7014・7019 を使っているので、
    // やること層は 7015(API)と 7016(開発中のフロント)を取る。
    // 7015 は「コンテナの内も外も、本番も開発も同じ」—— どこを開いても同じ URL になる
    port: 7016,
    // dev server が応答するホスト名。**Vite は localhost と IP 直打ちしか既定で許さない**
    // (DNS リバインディング対策)ので、ローカル DNS で当てたドメインで開くと 403 になる
    //     Blocked request. This host ("…") is not allowed.
    // 通したいホスト名は環境変数で渡す。**開発マシンごとに違う値なのでリポジトリには持たない**:
    //     VITE_ALLOWED_HOSTS=dev.example.lan npm run dev   (カンマ区切りで複数可)
    allowedHosts:
      process.env.VITE_ALLOWED_HOSTS?.split(',')
        .map((host) => host.trim())
        .filter(Boolean) ?? [],
    // 開発時のみ Vite dev server → chiezo-tasks にプロキシする。
    // 本番は同じオリジンから配るのでプロキシも CORS も要らない
    proxy: {
      '/api': { target: 'http://localhost:7015', changeOrigin: false },
      '/oauth2': { target: 'http://localhost:7015', changeOrigin: false },
      '/login': { target: 'http://localhost:7015', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
