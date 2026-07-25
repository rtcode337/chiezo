#!/bin/sh
# chiezo 連携用の Claude Code 設定(CLAUDE.md ブロック)を生成する。
#
# 稼働中の chiezo の設定生成 API(GET /admin/claude-config.txt)に問い合わせて、
# その環境の Claude に「chiezo に載っている知識が必要なら chiezo を使う」よう促す
# CLAUDE.md ブロックを取得し、対象ファイルへ書き込む。生成の正は chiezo 本体
# (api/app/claude_config.py)にあり、このスクリプトは取得と書き込みだけを行う。
# ブロック内の curl 例のベース URL は、chiezo 側が「このスクリプトがアクセスして
# きた URL のプロトコル・ホスト名・ポート」から導出する。
# POSIX シェル + curl だけで動き、追加インストールは不要。
#
# 使い方:
#   scripts/gen_claude_config.sh                        # 既定: ~/.claude/CLAUDE.md
#   scripts/gen_claude_config.sh -u http://<サーバーIP>:9000
#   scripts/gen_claude_config.sh --project              # ./CLAUDE.md(このプロジェクトだけ)
#   scripts/gen_claude_config.sh --target path/to/CLAUDE.md
#   scripts/gen_claude_config.sh --print                # 書き込まず標準出力へ
#   scripts/gen_claude_config.sh --merge headless       # 既存と賢く統合(claude CLI 必要)
#
# オプション: --base-url/-u, --target/-o, --user(既定), --project,
#             --merge {markers,headless}, --print, --no-permissions, --timeout
#
# 既定で、書き込み先に対応する Claude Code 設定(--user なら ~/.claude/settings.json、
# --project/--target なら <対象ディレクトリ>/.claude/settings.local.json)に
# chiezo への curl を許可するルール(GET /admin/claude-config.permissions.json の内容)を
# 追記する(permissions.allow への追記のみで、既存の設定は壊さない)。これにより
# chiezo への curl は毎回の許可プロンプトなしに実行できるようになる。
# この動作が不要なら --no-permissions を付ける。

BEGIN_MARK='<!-- BEGIN chiezo (auto-generated) -->'
END_MARK='<!-- END chiezo -->'

BASE="${CHIEZO_URL:-http://localhost:9000}"
TARGET=""
DEST="user"          # user | project | path
MERGE="markers"
PRINT=0
WITHPERM=1
TIMEOUT=10

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

# ---- 引数解析 --------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    -u|--base-url) BASE="$2"; shift 2 ;;
    --base-url=*)  BASE="${1#*=}"; shift ;;
    -o|--target)   TARGET="$2"; DEST="path"; shift 2 ;;
    --target=*)    TARGET="${1#*=}"; DEST="path"; shift ;;
    --user)        DEST="user"; shift ;;
    --project)     DEST="project"; shift ;;
    --merge)       MERGE="$2"; shift 2 ;;
    --merge=*)     MERGE="${1#*=}"; shift ;;
    --print)       PRINT=1; shift ;;
    --offline|--sources|--sources=*|--no-examples)
      die "$1 は廃止されました。生成は chiezo 本体(/admin/claude-config.txt)が行うため、稼働中の chiezo が必要です" ;;
    --with-permissions) WITHPERM=1; shift ;;   # 既定で有効(後方互換のため残す)
    --no-permissions) WITHPERM=0; shift ;;
    --timeout)     TIMEOUT="$2"; shift 2 ;;
    --timeout=*)   TIMEOUT="${1#*=}"; shift ;;
    -h|--help)     usage ;;
    *) die "unknown option: $1(--help 参照)" ;;
  esac
done

BASE="${BASE%/}"
case "$MERGE" in markers|headless) ;; *) die "--merge は markers か headless" ;; esac
command -v curl >/dev/null 2>&1 || die "curl が見つかりません(このスクリプトは curl が必須)"

# ---- 書き込み先の決定 ------------------------------------------------------
case "$DEST" in
  path)    TARGET_FILE="$TARGET" ;;
  project) TARGET_FILE="CLAUDE.md" ;;
  user)    TARGET_FILE="${HOME}/.claude/CLAUDE.md" ;;
esac

# ---- API から取得 ----------------------------------------------------------
# ベース URL(curl 例・許可ルール)はサーバー側がアクセス元 URL から導出するので、
# ここで接続に使った $BASE と生成物の中の URL は一致する。
BLOCK="$(mktemp)"
PERMS="$(mktemp)"
trap 'rm -f "$BLOCK" "$PERMS"' EXIT

curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.txt" -o "$BLOCK" \
  || die "chiezo に接続できません($BASE/admin/claude-config.txt)。--base-url を確認(稼働中の chiezo が必要)。"
grep -qF "$BEGIN_MARK" "$BLOCK" \
  || die "応答が CLAUDE.md ブロックではありません($BASE は chiezo の URL か確認)"
NSRC="$(grep -c '^- \*\*' "$BLOCK")"

# ---- 出力 ------------------------------------------------------------------
if [ "$PRINT" -eq 1 ]; then
  cat "$BLOCK"
  exit 0
fi

upsert() {  # target blockfile
  _t="$1"; _bf="$2"
  mkdir -p "$(dirname "$_t")"
  if [ -f "$_t" ] && grep -qF "$BEGIN_MARK" "$_t" && grep -qF "$END_MARK" "$_t"; then
    awk -v b="$BEGIN_MARK" -v e="$END_MARK" -v bf="$_bf" '
      index($0, b) { inb=1; while ((getline line < bf) > 0) print line; next }
      inb && index($0, e) { inb=0; next }
      inb { next }
      { print }
    ' "$_t" >"$_t.tmp" && mv "$_t.tmp" "$_t"
  else
    { [ -s "$_t" ] && cat "$_t" && printf '\n'; cat "$_bf"; } >"$_t.tmp" && mv "$_t.tmp" "$_t"
  fi
}

if [ "$MERGE" = "headless" ]; then
  command -v claude >/dev/null 2>&1 || die "--merge headless には Claude Code CLI(claude)が必要"
  mkdir -p "$(dirname "$TARGET_FILE")"; [ -f "$TARGET_FILE" ] || : >"$TARGET_FILE"
  KEEP="$(mktemp)"; cp "$BLOCK" "$KEEP"   # claude 実行中に消えないよう退避
  prompt="\`$TARGET_FILE\` に chiezo 連携の案内を統合してください。統合内容は \`$KEEP\`(chiezo が自動生成したブロック)にあります。要件: (1) \`$KEEP\` の内容をマーカー \`$BEGIN_MARK\` から \`$END_MARK\` ごと取り込む。(2) 既に chiezo の記述やマーカーブロックがあれば重複させず今回の内容で置き換える。(3) それ以外の既存記述・体裁は壊さない。\`$TARGET_FILE\` を直接編集してください。"
  echo "→ claude -p でヘッドレス統合を実行中(target=$TARGET_FILE)…" >&2
  ( cd "$(dirname "$TARGET_FILE")" && claude -p "$prompt" ); rc=$?
  rm -f "$KEEP"
  [ "$rc" -eq 0 ] || die "claude -p が失敗(rc=$rc)"
  echo "ヘッドレス統合が完了しました: $TARGET_FILE" >&2
else
  [ -f "$TARGET_FILE" ] && act="更新" || act="作成"
  upsert "$TARGET_FILE" "$BLOCK"
  echo "${act}しました: $TARGET_FILE($NSRC ソース, base=$BASE)" >&2
fi

# ---- 権限(既定で有効) -----------------------------------------------------
if [ "$WITHPERM" -eq 1 ]; then
  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.permissions.json" -o "$PERMS" \
    || die "権限ルールを取得できません($BASE/admin/claude-config.permissions.json)"
  # 応答 JSON の permissions.allow から "Bash(...)" ルールを 1 行 1 本で取り出す
  RULES="$(sed -n 's/^[[:space:]]*"\(Bash([^"]*)\)".*/\1/p' "$PERMS")"
  [ -n "$RULES" ] || die "権限ルールが応答に含まれていません($BASE/admin/claude-config.permissions.json)"

  # --user は TARGET_FILE 自体が ~/.claude/CLAUDE.md なので、そのディレクトリが
  # そのまま Claude Code のユーザー設定ディレクトリ(二重に .claude を付けない)。
  # --project/--target は TARGET_FILE の隣にプロジェクト用 .claude/ を置く。
  if [ "$DEST" = "user" ]; then
    sdir="$(dirname "$TARGET_FILE")"
    sfile="$sdir/settings.json"
  else
    sdir="$(dirname "$TARGET_FILE")/.claude"
    sfile="$sdir/settings.local.json"
  fi
  mkdir -p "$sdir"
  if [ ! -f "$sfile" ]; then
    # 新規作成: API の応答がそのまま新規ファイルの中身
    cp "$PERMS" "$sfile"
    echo "権限を追加しました: $sfile" >&2
  elif command -v jq >/dev/null 2>&1; then
    printf '%s\n' "$RULES" | while IFS= read -r rule; do
      [ -n "$rule" ] || continue
      tmp="$(mktemp)"
      jq --arg r "$rule" \
        '.permissions.allow = ((.permissions.allow // []) + [$r] | unique)' \
        "$sfile" >"$tmp" && mv "$tmp" "$sfile" || rm -f "$tmp"
    done
    echo "権限を追加しました: $sfile" >&2
  else
    echo "注意: jq が無いため $sfile を自動編集できません。permissions.allow に手動追加してください:" >&2
    printf '%s\n' "$RULES" | sed 's/^/  /' >&2
  fi
fi
