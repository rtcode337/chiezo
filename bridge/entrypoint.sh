#!/usr/bin/env bash
# CLI ブリッジの起動。MCP の設定を入れてから HTTP サーバを上げる。
#
# 認証情報はここでは要求しない。 Chiezo の設定 DB(/state/settings.db を読み取り専用で
# マウント)から要求のたびに読むので、鍵が無くても立ち上げてよい ——
# むしろ立っていないと管理画面から鍵を登録できない(到達確認に落ちるため)。
# DB が無い環境向けに、環境変数(CLAUDE_CODE_OAUTH_TOKEN / CODEX_AUTH_JSON)にも落ちる。
set -euo pipefail

CLI="${CHIEZO_BRIDGE_CLI:-claude}"
MCP_URL="${CHIEZO_BRIDGE_MCP_URL:-http://chiezo-app:7010/mcp/knowledge/}"
# 末尾のスラッシュを必ず付ける。無いと本体側が 404 を返し、CLI によっては
# その手前のリダイレクトで落ちる —— 実測: Codex 0.154.0 は
# `MCP HTTP redirects for non-loopback hostnames require HTTPS` で接続を捨て、
# 3 回とも繋がらないまま走った。以前の CLI は黙って辿っていたので、
# 設定を変えていないのに版を上げた日から道具だけが消える。
case "${MCP_URL}" in
    "" | */) ;;
    *) MCP_URL="${MCP_URL}/" ;;
esac

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
        # URL は位置引数(`agy mcp add [flags] <name> <commandOrUrl>`)。`--url` というフラグは
        # 無く、<name> の後ろに置いたフラグは拒否される —— codex と同じ書き方にしていたため
        # 登録は一度も通っておらず、`agy mcp list` は `No MCP servers configured.` のままだった。
        # 出力も戻り値も捨てない: 繋がっていないことに気づけないと、道具を持たない相手として
        # 黙って動き続ける(知識ベースを引けるつもりの頼み方が、引かずに答えられる)。
        if [ -n "${MCP_URL}" ]; then
            agy mcp add chiezo "${MCP_URL}"
        fi
        ;;
    *)
        echo "ERROR: 未対応の CHIEZO_BRIDGE_CLI: ${CLI}（claude / codex / antigravity）" >&2
        exit 2
        ;;
esac

exec uvicorn cli_bridge:app --host 0.0.0.0 --port "${CHIEZO_BRIDGE_PORT:-7013}"
