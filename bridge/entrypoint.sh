#!/usr/bin/env bash
# CLI ブリッジの起動。MCP の設定を入れてから HTTP サーバを上げる。
#
# **認証情報はここでは要求しない。** Chiezo の設定 DB(/state/settings.db を読み取り専用で
# マウント)から要求のたびに読むので、鍵が無くても立ち上げてよい ——
# むしろ立っていないと管理画面から鍵を登録できない(到達確認に落ちるため)。
# DB が無い環境向けに、環境変数(CLAUDE_CODE_OAUTH_TOKEN / CODEX_AUTH_JSON)にも落ちる。
set -euo pipefail

CLI="${CHIEZO_BRIDGE_CLI:-claude}"
MCP_URL="${CHIEZO_BRIDGE_MCP_URL:-http://chiezo-api:7010/mcp}"

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
        codex mcp add chiezo --url "${MCP_URL}"
        ;;
    *)
        echo "ERROR: 未対応の CHIEZO_BRIDGE_CLI: ${CLI}（claude / codex）" >&2
        exit 2
        ;;
esac

exec uvicorn cli_bridge:app --host 0.0.0.0 --port "${CHIEZO_BRIDGE_PORT:-7013}"
