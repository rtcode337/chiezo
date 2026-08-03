#!/bin/sh
# Chiezo 連携用の Claude Code 設定(CLAUDE.md ブロック)を生成する。
#
# 稼働中の Chiezo の設定生成 API(GET /admin/claude-config.txt)に問い合わせて、
# その環境の Claude に「Chiezo に載っている知識が必要なら Chiezo を使う」よう促す
# CLAUDE.md ブロックを取得し、対象ファイルへ書き込む。生成の正は Chiezo 本体
# (api/app/claude_config.py)にあり、このスクリプトは取得と書き込みだけを行う。
# ブロック内の curl 例のベース URL は、Chiezo 側が「このスクリプトがアクセスして
# きた URL のプロトコル・ホスト名・ポート」から導出する。
# POSIX シェル + curl だけで動く。
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
#             --merge {markers,headless}, --print, --no-permissions, --with-hook,
#             --no-mcp, --timeout
#
# 既定で、書き込み先に対応する Claude Code 設定(--user なら ~/.claude/settings.json、
# --project/--target なら <対象ディレクトリ>/.claude/settings.local.json)に
# Chiezo への curl を許可するルール(GET /admin/claude-config.permissions.json の内容)を
# 追記する(permissions.allow への追記のみで、既存の設定は壊さない)。これにより
# Chiezo への curl は毎回の許可プロンプトなしに実行できるようになる。
# この動作が不要なら --no-permissions を付ける。
#
# また既定で、Chiezo の MCP サーバー(<base>/mcp)を Claude Code に登録する:
#   - --user: ユーザースコープ(claude mcp add --scope user)
#   - --project/--target: 対象ディレクトリの .mcp.json(claude mcp add --scope project)
# あわせて CLAUDE.md ブロックに「単発の参照は MCP・大量取得は curl」の使い分けの指示が入る。
# Chiezo は REST と MCP の両方で同じ機能を出しており、単発の参照は引数が構造化された
# MCP のほうが確実(URL エンコードの失敗が無い)なので、curl 用の設定と揃えて既定で入れる。
# 登録が不要なら --no-mcp。
#
# 権限・MCP はどちらも既定で入れる設定なので、**入れられない環境では黙って飛ばさず落とす**。
# 「設定が入ったつもり」で使い始めるほうが困るため。外したいときは明示的に
# --no-permissions / --no-mcp を付ける。
#
# 既存の JSON 設定ファイルへのマージには jq か python3 のどちらかを使う(あるほうを使い、
# jq を優先する)。claude CLI があれば MCP の登録は CLI に任せるのでどちらも要らない。
# 新規ファイルを作るだけで済む場合(既存の settings.json / .mcp.json が無い場合)も要らない。
#
# permissions.allow は**コマンド文字列の前方一致**でしか判定できない。
# 大量取得は必ず `for t in …; do curl …; done` やパイプの形になり、そうなると
# curl が先頭に来ないのでルールが 1 本もマッチせず、いちばん許可したい場面で
# 毎回プロンプトが出る。これを解消したい場合だけ --with-hook を付けると、
# もう一段 PreToolUse フックを設置する(既定では設置しない):
#   - フック本体を <設定ディレクトリ>/hooks/chiezo-autoallow.py に置く
#     (GET /admin/claude-config.hook.py の内容。実行可能にする)
#   - settings の hooks.PreToolUse へ登録する(GET /admin/claude-config.hook.json)
# フックはコマンドを前方一致ではなく構造で見て、「登場する URL が全て Chiezo」かつ
# 「実行されるコマンドが curl/jq/sort 等の読み取り専用」のときだけ自動許可する。
# 条件を外れたら何も出力しないので、その場合は今までどおりプロンプトが出るだけ。
# 設置には python3 が要る(フック本体が Python スクリプトなので実行に必須)。
#
# フックは「Claude が打つ Bash を毎回検査して自動承認しうる」仕掛けで、影響が
# 権限ルールより広い。中身を読んで納得してから入れられるよう、既定では設置せず
# 明示的に --with-hook を指定したときだけ入れる。事前に中身だけ見たいときは
# curl "<base>/admin/claude-config.hook.py" か管理画面 /admin/claude-config を見る。

BEGIN_MARK='<!-- BEGIN chiezo (auto-generated) -->'
END_MARK='<!-- END chiezo -->'
HOOK_FILENAME='chiezo-autoallow.py'   # api 側 claude_config.HOOK_FILENAME と一致させる

BASE="${CHIEZO_URL:-http://localhost:9000}"
TARGET=""
DEST="user"          # user | project | path
MERGE="markers"
PRINT=0
WITHPERM=1
WITHHOOK=0          # フックは明示的な --with-hook のときだけ設置する
WITHMCP=1           # MCP 登録は既定で行う(--no-mcp で無効化)
TIMEOUT=10
MCP_NAME='chiezo'   # api 側 claude_config.MCP_SERVER_NAME と一致させる

die() { echo "error: $*" >&2; exit 1; }

usage() {
  # 冒頭のコメントブロック(2 行目〜最初の空行)をそのままヘルプにする
  sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
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
      die "$1 は廃止されました。生成は Chiezo 本体(/admin/claude-config.txt)が行うため、稼働中の Chiezo が必要です" ;;
    --with-permissions) WITHPERM=1; shift ;;   # 既定で有効(後方互換のため残す)
    --no-permissions) WITHPERM=0; shift ;;
    --with-hook)   WITHHOOK=1; shift ;;
    --no-hook)     WITHHOOK=0; shift ;;        # 既定なので実質 no-op(明示用に残す)
    --with-mcp)    WITHMCP=1; shift ;;         # 既定なので実質 no-op(明示用に残す)
    --no-mcp)      WITHMCP=0; shift ;;
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

# 権限とフックの書き込み先。--user は TARGET_FILE 自体が ~/.claude/CLAUDE.md なので、
# そのディレクトリがそのまま Claude Code のユーザー設定ディレクトリ(二重に .claude を
# 付けない)。--project/--target は TARGET_FILE の隣にプロジェクト用 .claude/ を置く。
if [ "$DEST" = "user" ]; then
  sdir="$(dirname "$TARGET_FILE")"
  sfile="$sdir/settings.json"
else
  sdir="$(dirname "$TARGET_FILE")/.claude"
  sfile="$sdir/settings.local.json"
fi

# 既存 JSON へのマージに使う道具を1つ選ぶ。jq を優先し、無ければ python3。
# どちらも無い環境でも「新規ファイルを作るだけ」で済む場面はあるので、
# ここでは選ぶだけにして、実際にマージが要る場面ごとに前提を検査する。
JSONTOOL=""
if command -v jq >/dev/null 2>&1; then
  JSONTOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSONTOOL=python3
fi

HAS_CLAUDE=0
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1

# MCP の登録先。claude CLI があれば CLI に任せるので直接は触らないが、
# 前提の検査とメッセージの表示にファイル名が要る。
if [ "$DEST" = "user" ]; then
  mcpfile="$HOME/.claude.json"
else
  mcpfile="$(dirname "$TARGET_FILE")/.mcp.json"
fi

# ---- 前提の検査(取得より先に済ませる) ------------------------------------
# フックと MCP を実際に入れるかは、CLAUDE.md ブロックの内容(それを前提にした
# 書き方の指示を入れるか)にも影響するので、取得より先に確定させる。
# 権限・MCP は既定で入れる設定なので、入れられないなら黙って飛ばさず落とす
# (--print は何も書き込まないので検査しない)。
if [ "$PRINT" -eq 0 ]; then
  if [ "$WITHHOOK" -eq 1 ]; then
    command -v python3 >/dev/null 2>&1 \
      || die "--with-hook には python3 が必要です(フック本体が Python スクリプトなので実行に必須)"
    JSONTOOL="${JSONTOOL:-python3}"
  fi
  if [ "$WITHPERM" -eq 1 ] && [ -f "$sfile" ] && [ -z "$JSONTOOL" ]; then
    die "権限の追記には jq か python3 が必要です(既存の $sfile へマージするため)。--no-permissions で外せます"
  fi
  if [ "$WITHMCP" -eq 1 ] && [ "$HAS_CLAUDE" -eq 0 ] && [ -f "$mcpfile" ] && [ -z "$JSONTOOL" ]; then
    die "MCP の登録には claude CLI か jq か python3 が必要です(既存の $mcpfile へマージするため)。--no-mcp で外せます"
  fi
fi

# ---- JSON マージ(jq が無ければ python3 で同じことをする) ------------------
# どれも一時ファイルへ書いて、成功したときだけ差し替える(途中で落ちても元が残る)。

merge_permissions() {  # settings_file perms_response_file
  _f="$1"; _p="$2"; _tmp="$(mktemp)"
  if [ "$JSONTOOL" = jq ]; then
    jq --slurpfile new "$_p" \
      '.permissions.allow = ((.permissions.allow // []) + $new[0].permissions.allow | unique)' \
      "$_f" >"$_tmp"
  else
    CHIEZO_SETTINGS="$_f" CHIEZO_PERMS="$_p" python3 -c '
import json, os, sys
d = json.load(open(os.environ["CHIEZO_SETTINGS"], encoding="utf-8"))
new = json.load(open(os.environ["CHIEZO_PERMS"], encoding="utf-8"))["permissions"]["allow"]
p = d.setdefault("permissions", {})
p["allow"] = sorted(set((p.get("allow") or []) + new))
sys.stdout.buffer.write(json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
' >"$_tmp"
  fi || { rm -f "$_tmp"; return 1; }
  mv "$_tmp" "$_f"
}

merge_mcp() {  # target_file mcp_response_file
  _f="$1"; _m="$2"; _tmp="$(mktemp)"
  if [ "$JSONTOOL" = jq ]; then
    jq --slurpfile new "$_m" --arg name "$MCP_NAME" \
      '.mcpServers = ((.mcpServers // {}) + {($name): $new[0].mcpServers[$name]})' \
      "$_f" >"$_tmp"
  else
    CHIEZO_FILE="$_f" CHIEZO_MCP="$_m" CHIEZO_NAME="$MCP_NAME" python3 -c '
import json, os, sys
name = os.environ["CHIEZO_NAME"]
d = json.load(open(os.environ["CHIEZO_FILE"], encoding="utf-8"))
new = json.load(open(os.environ["CHIEZO_MCP"], encoding="utf-8"))["mcpServers"][name]
d.setdefault("mcpServers", {})[name] = new
sys.stdout.buffer.write(json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
' >"$_tmp"
  fi || { rm -f "$_tmp"; return 1; }
  mv "$_tmp" "$_f"
}

# {{HOOK_PATH}} を実際の絶対パスへ差し替えたうえで hooks.PreToolUse へマージする。
# 先に「コマンドが $HOOK_FILENAME を指す既存エントリ」を全部落としてから足すので、
# 何度実行しても増えず、設置先を変えた場合も古いパスのエントリが残らない。
merge_hook() {  # settings_file hook_json_file hook_path
  _f="$1"; _h="$2"; _path="$3"; _tmp="$(mktemp)"
  if [ "$JSONTOOL" = jq ]; then
    jq --slurpfile new "$_h" --arg path "$_path" --arg fname "$HOOK_FILENAME" '
      ($new[0].hooks.PreToolUse
        | map(.hooks |= map(if .command == "{{HOOK_PATH}}" then .command = $path else . end))
      ) as $entries
      | .hooks = (.hooks // {})
      | .hooks.PreToolUse = (
          [ (.hooks.PreToolUse // [])[]
            | .hooks = [ (.hooks // [])[]
                | select(((.command // "") | contains($fname)) | not) ]
            | select((.hooks | length) > 0) ]
          + $entries
        )
    ' "$_f" >"$_tmp"
  else
    CHIEZO_SETTINGS="$_f" CHIEZO_HOOKJ="$_h" CHIEZO_HOOKPATH="$_path" \
    CHIEZO_HOOKNAME="$HOOK_FILENAME" python3 -c '
import json, os, sys
path, fname = os.environ["CHIEZO_HOOKPATH"], os.environ["CHIEZO_HOOKNAME"]
d = json.load(open(os.environ["CHIEZO_SETTINGS"], encoding="utf-8"))
entries = json.load(open(os.environ["CHIEZO_HOOKJ"], encoding="utf-8"))["hooks"]["PreToolUse"]
for e in entries:
    for h in e.get("hooks") or []:
        if h.get("command") == "{{HOOK_PATH}}":
            h["command"] = path
kept = []
for e in d.get("hooks", {}).get("PreToolUse") or []:
    hooks = [h for h in (e.get("hooks") or []) if fname not in (h.get("command") or "")]
    if hooks:
        e["hooks"] = hooks
        kept.append(e)
d.setdefault("hooks", {})["PreToolUse"] = kept + entries
sys.stdout.buffer.write(json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
' >"$_tmp"
  fi || { rm -f "$_tmp"; return 1; }
  mv "$_tmp" "$_f"
}

# ---- API から取得 ----------------------------------------------------------
# ベース URL(curl 例・許可ルール)はサーバー側がアクセス元 URL から導出するので、
# ここで接続に使った $BASE と生成物の中の URL は一致する。
BLOCK="$(mktemp)"
PERMS="$(mktemp)"
HOOKJ="$(mktemp)"
MCPJ=""             # MCP を自前でマージするときだけ後段で mktemp する
trap 'rm -f "$BLOCK" "$PERMS" "$HOOKJ" "$MCPJ"' EXIT

# フックを入れるときだけ ?hook=1、MCP を登録するときだけ mcp=1。それぞれの前提に
# 立った書き方の指示が本文に足される(入れていない環境にその指示を書いても嘘になる
# ので、既定では足さない)。
BLOCK_URL="$BASE/admin/claude-config.txt?hook=$WITHHOOK&mcp=$WITHMCP"

curl -fsS --max-time "$TIMEOUT" "$BLOCK_URL" -o "$BLOCK" \
  || die "Chiezo に接続できません($BLOCK_URL)。--base-url を確認(稼働中の Chiezo が必要)。"
grep -qF "$BEGIN_MARK" "$BLOCK" \
  || die "応答が CLAUDE.md ブロックではありません($BASE は Chiezo の URL か確認)"
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
  prompt="\`$TARGET_FILE\` に Chiezo 連携の案内を統合してください。統合内容は \`$KEEP\`(Chiezo が自動生成したブロック)にあります。要件: (1) \`$KEEP\` の内容をマーカー \`$BEGIN_MARK\` から \`$END_MARK\` ごと取り込む。(2) 既に Chiezo の記述やマーカーブロックがあれば重複させず今回の内容で置き換える。(3) それ以外の既存記述・体裁は壊さない。\`$TARGET_FILE\` を直接編集してください。"
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

# ---- 権限・フックの書き込み ------------------------------------------------
if [ "$WITHPERM" -eq 1 ] || [ "$WITHHOOK" -eq 1 ]; then
  mkdir -p "$sdir"
fi

# ---- 権限(既定で有効) -----------------------------------------------------
if [ "$WITHPERM" -eq 1 ]; then
  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.permissions.json" -o "$PERMS" \
    || die "権限ルールを取得できません($BASE/admin/claude-config.permissions.json)"
  grep -q '"Bash(' "$PERMS" \
    || die "権限ルールが応答に含まれていません($BASE/admin/claude-config.permissions.json)"
  if [ ! -f "$sfile" ]; then
    cp "$PERMS" "$sfile"   # 新規作成: API の応答がそのまま新規ファイルの中身
  else
    merge_permissions "$sfile" "$PERMS" \
      || die "$sfile への permissions マージに失敗しました(JSON が壊れていないか確認)"
  fi
  echo "権限を追加しました: $sfile" >&2
fi

# ---- 自動許可フック(--with-hook のときだけ) -------------------------------
# permissions.allow は前方一致なので、ループやパイプに包まれた curl には効かない。
# コマンドを構造で見て「Chiezo だけを読む読み取り専用コマンド」を自動許可する
# PreToolUse フックを設置する。Claude が打つ Bash を毎回検査して自動承認しうる
# 仕掛けなので、既定では入れず明示的に頼まれたときだけ入れる(前提の検査は冒頭で済み)。
if [ "$WITHHOOK" -eq 1 ]; then
  hdir="$sdir/hooks"
  hfile="$hdir/$HOOK_FILENAME"
  mkdir -p "$hdir"
  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.hook.py" -o "$hfile.tmp" \
    || die "フック本体を取得できません($BASE/admin/claude-config.hook.py)"
  # 取得物が本当にフックか確かめてから既存を置き換える(エラーページを置かない)
  if ! grep -q 'permissionDecision' "$hfile.tmp"; then
    rm -f "$hfile.tmp"
    die "応答がフック本体ではありません($BASE は Chiezo の URL か、版が古くないか確認)"
  fi
  mv "$hfile.tmp" "$hfile"
  chmod 755 "$hfile"

  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.hook.json" -o "$HOOKJ" \
    || die "フック設定を取得できません($BASE/admin/claude-config.hook.json)"

  [ -f "$sfile" ] || printf '{}\n' >"$sfile"   # マージ対象にするため無ければ空 JSON で作る
  merge_hook "$sfile" "$HOOKJ" "$hfile" \
    || die "$sfile への hooks マージに失敗しました(JSON が壊れていないか確認)"
  echo "フックを設置しました: $hfile" >&2
  echo "  設定に登録しました: $sfile(hooks.PreToolUse)" >&2
  echo "  反映には Claude Code の再起動か /hooks を一度開くことが必要な場合があります" >&2
fi

# ---- MCP サーバー登録(既定で行う。--no-mcp で無効) -------------------------
# Chiezo は MCP サーバーでもある($BASE/mcp、Streamable HTTP)。単発の参照は
# MCP ツールのほうが確実(引数が構造化されていて URL エンコードの失敗が無い)なので、
# curl 用の CLAUDE.md ブロック・権限と揃えて既定で登録する。ツール定義はコンテキストに
# 常駐するが(7 ツールで約 4.4k 字)、既定で入れている CLAUDE.md ブロックと同程度で、
# Chiezo を設定する時点で使う前提の環境なのだから、片方だけ渋る理由が無い。
# フックを既定で入れないのは security 上の理由(Bash を自動承認しうる)で、こことは別。
if [ "$WITHMCP" -eq 1 ]; then
  if [ "$HAS_CLAUDE" -eq 1 ]; then
    # claude CLI があれば user / project どちらのスコープも CLI に任せる(設定ファイルの
    # 構造を自前で知らずに済む)。project スコープの書き込み先はカレントディレクトリの
    # .mcp.json なので、対象ディレクトリへ移ってから実行する。
    # add は既存名と衝突すると失敗するので、先に同名を消してから足す(= 何度実行しても同じ)。
    if [ "$DEST" = "user" ]; then mscope=user; mcwd="." ; else mscope=project; mcwd="$(dirname "$TARGET_FILE")"; fi
    (
      cd "$mcwd" || exit 1
      claude mcp remove --scope "$mscope" "$MCP_NAME" >/dev/null 2>&1
      claude mcp add --scope "$mscope" --transport http "$MCP_NAME" "$BASE/mcp" >/dev/null
    ) || die "claude mcp add に失敗しました(claude mcp list で状態を確認)"
    echo "MCP サーバーを登録しました: $mcpfile($MCP_NAME → $BASE/mcp、$mscope スコープ)" >&2
  else
    # CLI の無い環境(VS Code 拡張のみ等)。設定ファイルへ直接マージする。
    # ユーザースコープの登録先は claude CLI の管理ファイル ~/.claude.json の
    # mcpServers キー(他のキーは触らない)、プロジェクトスコープは .mcp.json。
    MCPJ="$(mktemp)"
    curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.mcp.json" -o "$MCPJ" \
      || die "MCP 設定を取得できません($BASE/admin/claude-config.mcp.json)"
    if [ ! -f "$mcpfile" ]; then
      cp "$MCPJ" "$mcpfile"   # 新規なら API の応答がそのまま中身
    else
      merge_mcp "$mcpfile" "$MCPJ" \
        || die "$mcpfile への mcpServers マージに失敗しました(JSON が壊れていないか確認)"
    fi
    echo "MCP サーバーを登録しました: $mcpfile($MCP_NAME → $BASE/mcp)" >&2
  fi
  echo "  反映には Claude Code の再起動(新しいセッションの開始)が必要です" >&2
fi
