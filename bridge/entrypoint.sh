#!/usr/bin/env bash
# CLI ブリッジの起動。MCP の設定を入れてから HTTP サーバを上げる。
#
# 認証情報はここでは要求しない。 Chiezo の設定 DB(/state/settings.db を読み取り専用で
# マウント)から要求のたびに読むので、鍵が無くても立ち上げてよい ——
# むしろ立っていないと管理画面から鍵を登録できない(到達確認に落ちるため)。
# DB が無い環境向けに、環境変数(CLAUDE_CODE_OAUTH_TOKEN / CODEX_AUTH_JSON)にも落ちる。
set -euo pipefail

CLI="${CHIEZO_BRIDGE_CLI:-claude}"
MCP_URL="${CHIEZO_BRIDGE_MCP_URL:-http://chiezo-app:7010/mcp}"

case "${CLI}" in
    claude)
        # MCP の設定は cli_bridge.py が起動時に書く(--mcp-config に渡すファイル)。
        ;;
    codex)
        # Codex は設定ファイルに MCP を持つ。`codex mcp add` は同名があると失敗するので、
        # 先に消してから足す(コンテナは毎回作り直されるが、再起動でも同じ結果になるように)。
        mkdir -p "${CODEX_HOME}"
        chmod 700 "${CODEX_HOME}"
        codex mcp remove chiezo >/dev/null 2>&1 || true
        [ -n "${MCP_URL}" ] && codex mcp add chiezo --url "${MCP_URL}"
        ;;
    antigravity)
        # 認証はコンテナ内で 1 回サインインした結果を HOME 配下のキャッシュから読む
        # (API キー方式が無い)。HOME を書き込み可能なボリュームにバインドしてあれば、
        # コンテナを作り直しても消えない。サインインは
        #   docker compose exec chiezo-bridge-antigravity agy
        # を対話で 1 回実行して、表示される URL で済ませる。
        mkdir -p "${HOME}"
        agy mcp remove chiezo >/dev/null 2>&1 || true
        [ -n "${MCP_URL}" ] && agy mcp add chiezo --url "${MCP_URL}" >/dev/null 2>&1 || true
        ;;
    *)
        echo "ERROR: 未対応の CHIEZO_BRIDGE_CLI: ${CLI}（claude / codex / antigravity）" >&2
        exit 2
        ;;
esac

exec uvicorn cli_bridge:app --host 0.0.0.0 --port "${CHIEZO_BRIDGE_PORT:-7013}"
