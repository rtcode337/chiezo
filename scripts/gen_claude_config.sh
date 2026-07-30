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
#             --merge {markers,headless}, --print, --no-permissions, --with-hook,
#             --with-mcp, --timeout
#
# 既定で、書き込み先に対応する Claude Code 設定(--user なら ~/.claude/settings.json、
# --project/--target なら <対象ディレクトリ>/.claude/settings.local.json)に
# chiezo への curl を許可するルール(GET /admin/claude-config.permissions.json の内容)を
# 追記する(permissions.allow への追記のみで、既存の設定は壊さない)。これにより
# chiezo への curl は毎回の許可プロンプトなしに実行できるようになる。
# この動作が不要なら --no-permissions を付ける。
#
# ただし permissions.allow は**コマンド文字列の前方一致**でしか判定できない。
# 大量取得は必ず `for t in …; do curl …; done` やパイプの形になり、そうなると
# curl が先頭に来ないのでルールが 1 本もマッチせず、いちばん許可したい場面で
# 毎回プロンプトが出る。これを解消したい場合だけ --with-hook を付けると、
# もう一段 PreToolUse フックを設置する(既定では設置しない):
#   - フック本体を <設定ディレクトリ>/hooks/chiezo-autoallow.py に置く
#     (GET /admin/claude-config.hook.py の内容。実行可能にする)
#   - settings の hooks.PreToolUse へ登録する(GET /admin/claude-config.hook.json)
# フックはコマンドを前方一致ではなく構造で見て、「登場する URL が全て chiezo」かつ
# 「実行されるコマンドが curl/jq/sort 等の読み取り専用」のときだけ自動許可する。
# 条件を外れたら何も出力しないので、その場合は今までどおりプロンプトが出るだけ。
# 設置には python3(フックの実行)と jq(settings のマージ)が要る。
#
# フックは「Claude が打つ Bash を毎回検査して自動承認しうる」仕掛けで、影響が
# 権限ルールより広い。中身を読んで納得してから入れられるよう、既定では設置せず
# 明示的に --with-hook を指定したときだけ入れる。事前に中身だけ見たいときは
# curl "<base>/admin/claude-config.hook.py" か管理画面 /admin/claude-config を見る。
#
# また既定で、chiezo の MCP サーバー(<base>/mcp)を Claude Code に登録する:
#   - --user: ユーザースコープに登録(claude mcp add --scope user。claude CLI が無い環境では
#     jq で ~/.claude.json の mcpServers へ直接マージする)
#   - --project/--target: 対象ディレクトリの .mcp.json へマージ(GET /admin/claude-config.mcp.json)
# あわせて CLAUDE.md ブロックに「単発の参照は MCP・大量取得は curl」の使い分けの指示が入る。
# chiezo は REST と MCP の両方で同じ機能を出しており、単発の参照は引数が構造化された
# MCP のほうが確実(URL エンコードの失敗が無い)なので、curl 用の設定と揃えて既定で入れる。
# 登録が不要なら --no-mcp。前提(claude CLI か jq)が無い環境では警告して登録だけ飛ばす。

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
MCPEXPLICIT=0       # --with-mcp を明示されたか(前提が欠けたとき落とすか黙って飛ばすかの分岐)
TIMEOUT=10
MCP_NAME='chiezo'   # api 側 claude_config.MCP_SERVER_NAME と一致させる

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'
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
    --with-hook)   WITHHOOK=1; shift ;;
    --no-hook)     WITHHOOK=0; shift ;;        # 既定なので実質 no-op(明示用に残す)
    --with-mcp)    WITHMCP=1; MCPEXPLICIT=1; shift ;;  # 既定なので実質 no-op(明示用に残す)
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
HAS_JQ=0
command -v jq >/dev/null 2>&1 && HAS_JQ=1

# フックを実際に設置するかは、CLAUDE.md ブロックの内容(自動許可を前提にした
# 書き方の指示を入れるか)にも影響するので、取得より先に確定させる。
# 明示的に頼まれた設置なので、前提が欠けていれば黙って諦めず落とす。
if [ "$WITHHOOK" -eq 1 ]; then
  command -v python3 >/dev/null 2>&1 || die "--with-hook には python3 が必要です(フックの実行に使う)"
  [ "$HAS_JQ" -eq 1 ] || die "--with-hook には jq が必要です(settings のマージに使う)"
fi

# MCP 登録の前提も取得より先に確定させる(ブロックに使い分けの指示を入れるかに影響する)。
# ユーザースコープの MCP 設定は claude CLI の管理ファイル(~/.claude.json)にあるので、
# CLI があればそれ経由で登録する。CLI の無い環境(VS Code 拡張のみ等)では
# jq で ~/.claude.json の mcpServers キーへ直接マージする(他のキーは触らない)。
HAS_CLAUDE=0
command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1
if [ "$WITHMCP" -eq 1 ] && [ "$DEST" = "user" ] && [ "$HAS_CLAUDE" -eq 0 ] && [ "$HAS_JQ" -eq 0 ]; then
  # 既定の動作なので、前提が無い環境では登録だけ諦めて CLAUDE.md の生成は続ける
  # (明示的に頼まれたときだけ落とす)。
  if [ "$MCPEXPLICIT" -eq 1 ]; then
    die "--user での --with-mcp には claude CLI か jq のどちらかが必要です(~/.claude.json への登録に使う)"
  fi
  echo "注意: claude CLI も jq も無いため MCP サーバーの登録を飛ばします(--no-mcp で警告を消せます)" >&2
  WITHMCP=0
fi

# ---- API から取得 ----------------------------------------------------------
# ベース URL(curl 例・許可ルール)はサーバー側がアクセス元 URL から導出するので、
# ここで接続に使った $BASE と生成物の中の URL は一致する。
BLOCK="$(mktemp)"
PERMS="$(mktemp)"
HOOKJ="$(mktemp)"
MCPJ=""             # --with-mcp のときだけ後段で mktemp する
trap 'rm -f "$BLOCK" "$PERMS" "$HOOKJ" "$MCPJ"' EXIT

# フックを入れるときだけ ?hook=1、MCP を登録するときだけ mcp=1。それぞれの前提に
# 立った書き方の指示が本文に足される(入れていない環境にその指示を書いても嘘になる
# ので、既定では足さない)。
BLOCK_URL="$BASE/admin/claude-config.txt?hook=$WITHHOOK&mcp=$WITHMCP"

curl -fsS --max-time "$TIMEOUT" "$BLOCK_URL" -o "$BLOCK" \
  || die "chiezo に接続できません($BLOCK_URL)。--base-url を確認(稼働中の chiezo が必要)。"
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

# ---- 権限・フックの書き込み ------------------------------------------------
# jq でのマージ対象にするため、無ければ空 JSON で作る。
ensure_settings() { [ -f "$sfile" ] || printf '{}\n' >"$sfile"; }

if [ "$WITHPERM" -eq 1 ] || [ "$WITHHOOK" -eq 1 ]; then
  mkdir -p "$sdir"
fi

# ---- 権限(既定で有効) -----------------------------------------------------
if [ "$WITHPERM" -eq 1 ]; then
  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.permissions.json" -o "$PERMS" \
    || die "権限ルールを取得できません($BASE/admin/claude-config.permissions.json)"
  # 応答 JSON の permissions.allow から "Bash(...)" ルールを 1 行 1 本で取り出す。
  # ルールには `"` を含むもの(クォート付き curl 用の変種)があるため jq を優先し、
  # 無い場合は sed で JSON エスケープ(\")を復元しながら近似抽出する。
  if [ "$HAS_JQ" -eq 1 ]; then
    RULES="$(jq -r '.permissions.allow[]' "$PERMS")"
  else
    RULES="$(sed -n 's/^[[:space:]]*"\(Bash(.*)\)",\{0,1\}$/\1/p' "$PERMS" | sed 's/\\"/"/g')"
  fi
  [ -n "$RULES" ] || die "権限ルールが応答に含まれていません($BASE/admin/claude-config.permissions.json)"

  if [ "$HAS_JQ" -eq 1 ]; then
    ensure_settings
    printf '%s\n' "$RULES" | while IFS= read -r rule; do
      [ -n "$rule" ] || continue
      tmp="$(mktemp)"
      jq --arg r "$rule" \
        '.permissions.allow = ((.permissions.allow // []) + [$r] | unique)' \
        "$sfile" >"$tmp" && mv "$tmp" "$sfile" || rm -f "$tmp"
    done
    echo "権限を追加しました: $sfile" >&2
  elif [ ! -f "$sfile" ]; then
    # 新規作成: API の応答がそのまま新規ファイルの中身
    cp "$PERMS" "$sfile"
    echo "権限を追加しました: $sfile" >&2
  else
    echo "注意: jq が無いため $sfile を自動編集できません。permissions.allow に手動追加してください:" >&2
    printf '%s\n' "$RULES" | sed 's/^/  /' >&2
  fi
fi

# ---- 自動許可フック(--with-hook のときだけ) -------------------------------
# permissions.allow は前方一致なので、ループやパイプに包まれた curl には効かない。
# コマンドを構造で見て「chiezo だけを読む読み取り専用コマンド」を自動許可する
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
    die "応答がフック本体ではありません($BASE は chiezo の URL か、版が古くないか確認)"
  fi
  mv "$hfile.tmp" "$hfile"
  chmod 755 "$hfile"

  curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.hook.json" -o "$HOOKJ" \
    || die "フック設定を取得できません($BASE/admin/claude-config.hook.json)"

  ensure_settings
  # {{HOOK_PATH}} を実際の絶対パスへ差し替えたうえで hooks.PreToolUse へマージする。
  # 先に「コマンドが $HOOK_FILENAME を指す既存エントリ」を全部落としてから足すので、
  # 何度実行しても増えず、設置先を変えた場合も古いパスのエントリが残らない。
  tmp="$(mktemp)"
  if jq --slurpfile new "$HOOKJ" --arg path "$hfile" --arg fname "$HOOK_FILENAME" '
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
    ' "$sfile" >"$tmp"; then
    mv "$tmp" "$sfile"
    echo "フックを設置しました: $hfile" >&2
    echo "  設定に登録しました: $sfile(hooks.PreToolUse)" >&2
    echo "  反映には Claude Code の再起動か /hooks を一度開くことが必要な場合があります" >&2
  else
    rm -f "$tmp"
    die "$sfile への hooks マージに失敗しました(JSON が壊れていないか確認)"
  fi
fi

# ---- MCP サーバー登録(既定で行う。--no-mcp で無効) -------------------------
# chiezo は MCP サーバーでもある($BASE/mcp、Streamable HTTP)。単発の参照は
# MCP ツールのほうが確実(引数が構造化されていて URL エンコードの失敗が無い)なので、
# curl 用の CLAUDE.md ブロック・権限と揃えて既定で登録する。ツール定義はコンテキストに
# 常駐するが(7 ツールで約 4.4k 字)、既定で入れている CLAUDE.md ブロックと同程度で、
# chiezo を設定する時点で使う前提の環境なのだから、片方だけ渋る理由が無い。
# フックを既定で入れないのは security 上の理由(Bash を自動承認しうる)で、こことは別。
if [ "$WITHMCP" -eq 1 ]; then
  if [ "$DEST" = "user" ] && [ "$HAS_CLAUDE" -eq 1 ]; then
    # ユーザースコープは claude CLI 経由が第一候補。add は既存名と衝突すると
    # 失敗するので、先に同名を消してから足す(= 何度実行しても同じ結果)。
    claude mcp remove --scope user "$MCP_NAME" >/dev/null 2>&1 || true
    claude mcp add --scope user --transport http "$MCP_NAME" "$BASE/mcp" >/dev/null \
      || die "claude mcp add に失敗しました(claude mcp list で状態を確認)"
    echo "MCP サーバーを登録しました: $MCP_NAME → $BASE/mcp(ユーザースコープ)" >&2
  elif [ "$DEST" = "user" ]; then
    # CLI の無い環境(前提検査済みなので jq はある)。~/.claude.json の mcpServers へ
    # 直接マージする。無ければ mcpServers だけの新規ファイルとして作る。
    ufile="$HOME/.claude.json"
    MCPJ="$(mktemp)"
    curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.mcp.json" -o "$MCPJ" \
      || die "MCP 設定を取得できません($BASE/admin/claude-config.mcp.json)"
    [ -f "$ufile" ] || printf '{}\n' >"$ufile"
    tmp="$(mktemp)"
    if jq --slurpfile new "$MCPJ" --arg name "$MCP_NAME" \
        '.mcpServers = ((.mcpServers // {}) + {($name): $new[0].mcpServers[$name]})' \
        "$ufile" >"$tmp"; then
      mv "$tmp" "$ufile"
      echo "MCP サーバーを登録しました: $ufile($MCP_NAME → $BASE/mcp)" >&2
    else
      rm -f "$tmp"
      die "$ufile への mcpServers マージに失敗しました(JSON が壊れていないか確認)"
    fi
  else
    # プロジェクトスコープは対象ディレクトリの .mcp.json(VCS で共有される想定の場所)。
    mfile="$(dirname "$TARGET_FILE")/.mcp.json"
    MCPJ="$(mktemp)"
    curl -fsS --max-time "$TIMEOUT" "$BASE/admin/claude-config.mcp.json" -o "$MCPJ" \
      || die "MCP 設定を取得できません($BASE/admin/claude-config.mcp.json)"
    if [ ! -f "$mfile" ]; then
      cp "$MCPJ" "$mfile"
      echo "MCP サーバーを登録しました: $mfile($MCP_NAME → $BASE/mcp)" >&2
    elif [ "$HAS_JQ" -eq 1 ]; then
      tmp="$(mktemp)"
      if jq --slurpfile new "$MCPJ" --arg name "$MCP_NAME" \
          '.mcpServers = ((.mcpServers // {}) + {($name): $new[0].mcpServers[$name]})' \
          "$mfile" >"$tmp"; then
        mv "$tmp" "$mfile"
        echo "MCP サーバーを登録しました: $mfile($MCP_NAME → $BASE/mcp)" >&2
      else
        rm -f "$tmp"
        die "$mfile への mcpServers マージに失敗しました(JSON が壊れていないか確認)"
      fi
    else
      echo "注意: jq が無いため既存の $mfile を自動編集できません。mcpServers に手動追加してください:" >&2
      sed 's/^/  /' "$MCPJ" >&2
    fi
  fi
  echo "  反映には Claude Code の再起動(新しいセッションの開始)が必要です" >&2
fi
