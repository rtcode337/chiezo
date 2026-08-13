#!/usr/bin/env bash
# CLI ブリッジの起動。認証情報を置いてから HTTP サーバを上げる。
#
# **認証情報はイメージに焼かない**。環境変数で受け取り、ファイルが要る CLI(Codex)だけ
# ここで書き出す。コンテナは使い捨てなので、止めれば認証ファイルごと消える。
set -euo pipefail

CLI="${CHIEZO_BRIDGE_CLI:-claude}"
MCP_URL="${CHIEZO_BRIDGE_MCP_URL:-http://chiezo-api:7010/mcp}"

case "${CLI}" in
    claude)
        # Claude Code は OAuth トークンを環境変数で受け取る(ホストの ~/.claude は持ち込まない)。
        # 発行は手元の端末で `claude setup-token`。
        if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
            echo "ERROR: CLAUDE_CODE_OAUTH_TOKEN が未設定です。" >&2
            echo "  手元の端末で 'claude setup-token' を実行し、.env に書いてください。" >&2
            exit 1
        fi
        # MCP の設定は server.py が起動時に書く(--mcp-config に渡すファイル)。
        ;;
    codex)
        # Codex はサブスクで使う場合 API キーではなく OAuth で、認証結果はファイルに載る。
        # 手元で `codex login --device-auth` を済ませ、~/.codex/auth.json の中身を
        # CODEX_AUTH_JSON に入れて渡す(API キー経路は従量課金になるので使わない)。
        if [ -z "${CODEX_AUTH_JSON:-}" ]; then
            echo "ERROR: CODEX_AUTH_JSON が未設定です。" >&2
            echo "  手元の端末で 'codex login --device-auth' を実行し、" >&2
            echo "  ~/.codex/auth.json の中身を .env に書いてください。" >&2
            exit 1
        fi
        mkdir -p "${CODEX_HOME}"
        chmod 700 "${CODEX_HOME}"
        printf '%s' "${CODEX_AUTH_JSON}" > "${CODEX_HOME}/auth.json"
        chmod 600 "${CODEX_HOME}/auth.json"
        # Chiezo の MCP を設定に入れる。`codex mcp add` は同名があると失敗するので、
        # 先に消してから足す(コンテナは毎回作り直されるが、再起動でも同じ結果になるように)。
        codex mcp remove chiezo >/dev/null 2>&1 || true
        codex mcp add chiezo --url "${MCP_URL}"
        ;;
    *)
        echo "ERROR: 未対応の CHIEZO_BRIDGE_CLI: ${CLI}（claude / codex）" >&2
        exit 2
        ;;
esac

exec uvicorn cli_bridge:app --host 0.0.0.0 --port "${CHIEZO_BRIDGE_PORT:-7013}"
