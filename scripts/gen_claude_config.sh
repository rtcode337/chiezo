#!/bin/sh
# chiezo 連携用の Claude Code 設定(CLAUDE.md ブロック)を生成する。
#
# 稼働中の chiezo に問い合わせて登録済みソースを列挙し、その環境の Claude に
# 「chiezo に載っている知識が必要なら chiezo を使う」よう促す CLAUDE.md ブロックを
# 書き込む。POSIX シェル + curl だけで動き、追加インストールは不要(jq も不要。
# JSON は sed で処理)。chiezo の使い方はもともと curl なので前提が増えない。
#
# 使い方:
#   scripts/gen_claude_config.sh                        # 既定: ~/.claude/CLAUDE.md
#   scripts/gen_claude_config.sh -u http://<サーバーIP>:9000
#   scripts/gen_claude_config.sh --project              # ./CLAUDE.md(このプロジェクトだけ)
#   scripts/gen_claude_config.sh --target path/to/CLAUDE.md
#   scripts/gen_claude_config.sh --print                # 書き込まず標準出力へ
#   scripts/gen_claude_config.sh --merge headless       # 既存と賢く統合(claude CLI 必要)
#   scripts/gen_claude_config.sh --offline --sources jawiki,osm_japan
#
# オプション: --base-url/-u, --target/-o, --user(既定), --project,
#             --merge {markers,headless}, --print, --offline, --sources,
#             --no-examples, --no-permissions, --timeout
#
# 既定で、書き込み先に対応する Claude Code 設定(--user なら ~/.claude/settings.json、
# --project/--target なら <対象ディレクトリ>/.claude/settings.local.json)に
# chiezo への curl を許可するルールを追記する(permissions.allow への追記のみで、
# 既存の設定は壊さない)。これにより chiezo への curl は毎回の許可プロンプトなしに
# 実行できるようになる。この動作が不要なら --no-permissions を付ける。

BEGIN_MARK='<!-- BEGIN chiezo (auto-generated) -->'
END_MARK='<!-- END chiezo -->'

BASE="${CHIEZO_URL:-http://localhost:9000}"
TARGET=""
DEST="user"          # user | project | path
MERGE="markers"
PRINT=0
OFFLINE=0
SRCSPEC=""
EXAMPLES=1
WITHPERM=1
TIMEOUT=10

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
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
    --offline)     OFFLINE=1; shift ;;
    --sources)     SRCSPEC="$2"; shift 2 ;;
    --sources=*)   SRCSPEC="${1#*=}"; shift ;;
    --no-examples) EXAMPLES=0; shift ;;
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

# ---- JSON 取り出し(sed のみ) ---------------------------------------------
# 単一オブジェクトの文字列/数値フィールドを取り出す。
jstr() { printf '%s' "$1" | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'; }
jnum() { printf '%s' "$1" | sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p'; }

# 数値に3桁区切りを付ける(POSIX パラメータ展開のみ)。
commafy() {
  n="$1"; out=""
  [ -z "$n" ] && return 0
  while [ ${#n} -gt 3 ]; do
    last3=${n#"${n%???}"}; n=${n%???}; out=",${last3}${out}"
  done
  printf '%s%s' "$n" "$out"
}

sample_title() {
  [ "$EXAMPLES" -eq 1 ] && [ "$OFFLINE" -eq 0 ] || return 0
  curl -fsS --max-time "$TIMEOUT" "$BASE/v1/$1/random?limit=1" 2>/dev/null \
    | sed -n 's/.*"title"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
}

# そのソースの doc.extra に指定キーが入っているか(ダンプ世代によっては未突合)。
# 「載っていない情報を案内して Claude を無駄足させる」のを防ぐため、案内文は
# 実際にデータがあることを確認できたものだけ出す。
has_extra_key() {  # source title key
  [ "$EXAMPLES" -eq 1 ] && [ "$OFFLINE" -eq 0 ] || return 1
  [ -n "$2" ] && [ "$2" != "<タイトル>" ] || return 1
  curl -fsS --max-time "$TIMEOUT" --get --data-urlencode "title=$2" \
    --data-urlencode 'fields=title,extra' "$BASE/v1/$1/doc" 2>/dev/null \
    | grep -q "\"$3\""
}

# /filter(属性での一括抽出)が使えるか。条件なしで叩くと、対応していれば 400 で
# 「条件を 1 つ以上指定せよ」が返り、schema_version 1 の古い DB では 409 が返る。
# (400 応答の本文を読みたいので -f は付けない)
has_filter() {  # source
  [ "$OFFLINE" -eq 0 ] || return 1
  curl -s --max-time "$TIMEOUT" "$BASE/v1/$1/filter" 2>/dev/null | grep -q 'at least one'
}

# そのソースに実在する area(行政区名)を 1 つ拾う。無ければ空。
sample_area() {  # source
  [ "$EXAMPLES" -eq 1 ] && [ "$OFFLINE" -eq 0 ] || return 0
  curl -fsS --max-time "$TIMEOUT" \
    "$BASE/v1/$1/filter?feature=boundary%3Dadministrative&fields=title,area&limit=50" 2>/dev/null \
    | tr ',' '\n' | sed -n 's/.*"area"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1
}

# そのソースの文書に座標が入っているか。ランダムに拾ったサンプル記事は座標を持たない
# ことが多いため、doc ではなく全球 bbox の filter に 1 件でも当たるかで判定する。
has_coordinates() {  # source
  [ "$OFFLINE" -eq 0 ] || return 1
  curl -fsS --max-time "$TIMEOUT" \
    "$BASE/v1/$1/filter?bbox=-90,-180,90,180&limit=1" 2>/dev/null \
    | grep -q '"lat"'
}

# そのソースの文書に wikidata の Q 番号が入っているか(逆引きを案内してよいか)。
has_wikidata_column() {  # source
  [ "$OFFLINE" -eq 0 ] || return 1
  curl -fsS --max-time "$TIMEOUT" \
    "$BASE/v1/$1/filter?feature=boundary%3Dadministrative&fields=title,wikidata&limit=50" 2>/dev/null \
    | grep -q '"wikidata"[[:space:]]*:[[:space:]]*"Q'
}

# ---- ブロック生成 ----------------------------------------------------------
BLOCK="$(mktemp)"
trap 'rm -f "$BLOCK"' EXIT

emit_source() {  # name kind lang docs
  _name="$1"; _kind="$2"; _lang="$3"; _docs="$4"
  _docs_str=""; [ -n "$_docs" ] && _docs_str="$(commafy "$_docs")件"
  _title="$(sample_title "$_name")"; [ -z "$_title" ] && _title="<タイトル>"
  _query="$_title"; [ "$_title" = "<タイトル>" ] && _query="<検索語>"
  case "$_kind" in
    wikipedia)
      _desc="Wikipedia"; [ -n "$_lang" ] && _desc="$_lang Wikipedia"
      _paren="$_desc"; [ -n "$_docs_str" ] && _paren="$_desc, $_docs_str"
      printf -- '- **%s**(%s): 一般知識・人物・作品・地名・用語・出来事など\n' "$_name" "$_paren" >>"$BLOCK"
      printf -- '  - 検索:   `curl -s "%s/v1/%s/search?q=%s&limit=5"`\n' "$BASE" "$_name" "$_query" >>"$BLOCK"
      printf -- '  - 概要:   `curl -s "%s/v1/%s/doc?title=%s&fields=title,opening,tags"`\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      printf -- '  - 本文:   `curl -s "%s/v1/%s/doc?title=%s&max_chars=8000"`\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      printf -- '  - 候補:   `curl -s "%s/v1/%s/titles?prefix=%s"`\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      if has_extra_key "$_name" "$_title" pageviews_month; then
        printf -- '  - 人気度: `curl -s "%s/v1/%s/doc?title=%s&fields=title,extra"` (extra の `pageviews_month`=月次ページビュー(bot 除外)。知名度の客観指標に使える。**Wikimedia の pageviews API を叩く必要はない**)\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      fi
      if has_filter "$_name" && has_coordinates "$_name"; then
        printf -- '  - 座標:   `curl -s "%s/v1/%s/doc?title=%s&fields=title,extra"` (座標を持つ記事は extra に `lat`/`lon` が入る。`filter?bbox=min_lat,min_lon,max_lat,max_lon` で範囲抽出も可。**ジオコーディング API を叩く必要はない**)\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      fi
      if has_filter "$_name" && has_extra_key "$_name" "$_title" wikidata; then
        printf -- '  - 逆引き: `curl -s "%s/v1/%s/filter?wikidata=Q17221&fields=title,extra"` (wikidata の Q 番号 → 記事。OSM 側の wikidata タグと突き合わせできる。**wikidata.org を叩く必要はない**)\n' "$BASE" "$_name" >>"$BLOCK"
      fi
      ;;
    osm)
      _paren="OpenStreetMap 地名・POI 辞典"; [ -n "$_docs_str" ] && _paren="$_paren, $_docs_str"
      printf -- '- **%s**(%s): 地名・行政区・自然地物に加え病院/学校/店舗/観光地などの施設、駅・空港・港・IC/SA などの交通インフラと座標\n' "$_name" "$_paren" >>"$BLOCK"
      printf -- '  - 検索:   `curl -s "%s/v1/%s/search?q=%s&limit=5"`\n' "$BASE" "$_name" "$_query" >>"$BLOCK"
      printf -- '  - 座標等: `curl -s "%s/v1/%s/doc?title=%s&fields=title,extra"` (extra に lat/lon・OSM タグ・住所等)\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      if has_filter "$_name"; then
        _area="$(sample_area "$_name")"; [ -n "$_area" ] || _area="<行政区名>"
        printf -- '  - 取り違え防止: 同名の別地物がある場合、doc の応答に `alternatives` が付く。`&area=%s` や `&feature=railway%%3Dstation` で絞り込める(search/doc 共通)\n' "$_area" >>"$BLOCK"
        printf -- '  - 一括抽出: `curl -s "%s/v1/%s/filter?feature=amenity%%3Dplace_of_worship&area=%s&limit=200"` (地物種別 × 行政区で全件列挙。応答の `total` で件数が分かり `offset` でページングできる。**Overpass API を叩く必要はない**)\n' "$BASE" "$_name" "$_area" >>"$BLOCK"
        printf -- '  - 範囲抽出: `curl -s "%s/v1/%s/filter?feature=tourism%%3Dmuseum&bbox=34.9,135.6,35.1,135.9"` (bbox は `min_lat,min_lon,max_lat,max_lon`。`feature` はカンマ区切りで複数指定可)\n' "$BASE" "$_name" >>"$BLOCK"
        if has_wikidata_column "$_name"; then
          printf -- '  - 逆引き: `curl -s "%s/v1/%s/filter?wikidata=Q17221&fields=title,extra"` (wikidata の Q 番号 → 地物)\n' "$BASE" "$_name" >>"$BLOCK"
        fi
      fi
      ;;
    *)
      _paren="kind=${_kind:-?}"; [ -n "$_docs_str" ] && _paren="$_paren, $_docs_str"
      printf -- '- **%s**(%s)\n' "$_name" "$_paren" >>"$BLOCK"
      printf -- '  - 検索:   `curl -s "%s/v1/%s/search?q=%s&limit=5"`\n' "$BASE" "$_name" "$_query" >>"$BLOCK"
      printf -- '  - 文書:   `curl -s "%s/v1/%s/doc?title=%s&fields=title,opening,body"`\n' "$BASE" "$_name" "$_title" >>"$BLOCK"
      ;;
  esac
}

# ソース一覧を用意
NSRC=0
if [ "$OFFLINE" -eq 1 ]; then
  [ -n "$SRCSPEC" ] || die "--offline では --sources が必要(例: --sources jawiki,osm_japan)"
else
  SOURCES_JSON="$(curl -fsS --max-time "$TIMEOUT" "$BASE/v1/sources" 2>/dev/null)" \
    || die "chiezo に接続できません($BASE/v1/sources)。--base-url を確認するか、--offline --sources を使用。"
fi

when="$(date '+%Y-%m-%d %H:%M %Z')"

# ヘッダ
{
  printf '%s\n' "$BEGIN_MARK"
  printf '%s\n' '## chiezo(ローカル知識サーバー)'
  printf '\n'
  printf '%s\n' 'LAN 内に読み取り専用の知識検索 API「chiezo」がある。下記ソースに載っている情報が必要になったら、**Web 検索や外部 API より先に chiezo を使うこと**(オフライン・レート制限なし・高速)。'
  printf -- 'ベース URL: `%s`\n' "$BASE"
  printf '\n'
  printf '%s\n' '使い方の要点:'
  printf '%s\n' '- まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)。いきなり全文を取らない。'
  printf '%s\n' '- 3 文字未満の語はタイトル前方一致にフォールバックする(レスポンスの `mode` が `title_prefix` になる)。'
  printf '%s\n' '- 応答は JSON。エラーは `{"error": "..."}` 形式。全クエリ 5 秒でタイムアウト(超過は 504)。'
  printf -- '- ソース一覧(最新の登録状況): `curl -s "%s/v1/sources"`\n' "$BASE"
  printf '\n'
  printf '%s\n' '### 収録ソース'
} >"$BLOCK"

# ソース行
if [ "$OFFLINE" -eq 1 ]; then
  OLDIFS="$IFS"; IFS=,
  for item in $SRCSPEC; do
    IFS="$OLDIFS"
    [ -n "$item" ] || continue
    name="${item%%:*}"
    if [ "$name" = "$item" ]; then kind=""; else kind="${item#*:}"; fi
    if [ -z "$kind" ]; then
      case "$name" in *osm*) kind="osm" ;; *) kind="wikipedia" ;; esac
    fi
    emit_source "$name" "$kind" "" ""
    NSRC=$((NSRC + 1))
    IFS=,
  done
  IFS="$OLDIFS"
else
  # "sources":[ {..},{..} ] を1オブジェクト1行へ分割
  OBJS="$(printf '%s' "$SOURCES_JSON" \
    | sed -e 's/.*"sources"[[:space:]]*:[[:space:]]*\[//' -e 's/\][[:space:]]*}[[:space:]]*$//' \
    | sed 's/},[[:space:]]*{/}\
{/g')"
  if [ -n "$(printf '%s' "$OBJS" | tr -d '[:space:]')" ]; then
    printf '%s\n' "$OBJS" | while IFS= read -r obj; do
      [ -n "$(printf '%s' "$obj" | tr -d '[:space:]')" ] || continue
      emit_source "$(jstr "$obj" name)" "$(jstr "$obj" kind)" "$(jstr "$obj" lang)" "$(jnum "$obj" docs)"
    done
    NSRC="$(printf '%s\n' "$OBJS" | grep -c '"name"')"
  fi
fi

if [ "$NSRC" -eq 0 ]; then
  printf '%s\n' '- (生成時点で登録済みソースは 0 件だった。取り込み後に本ブロックを再生成すること)' >>"$BLOCK"
fi

# フッタ
{
  printf '\n'
  printf -- '<sub>この一覧は %s 時点の chiezo(`%s`)の登録ソースから自動生成。再生成: `scripts/gen_claude_config.sh --base-url %s`</sub>\n' "$when" "$BASE" "$BASE"
  printf '%s\n' "$END_MARK"
} >>"$BLOCK"

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
  rule1="Bash(curl -s $BASE/v1/:*)"
  rule2="Bash(curl -s $BASE/:*)"
  if command -v jq >/dev/null 2>&1; then
    tmp="$(mktemp)"
    if [ -f "$sfile" ]; then
      jq --arg r1 "$rule1" --arg r2 "$rule2" \
        '.permissions.allow = ((.permissions.allow // []) + [$r1, $r2] | unique)' \
        "$sfile" >"$tmp" && mv "$tmp" "$sfile"
    else
      jq -n --arg r1 "$rule1" --arg r2 "$rule2" \
        '{permissions: {allow: [$r1, $r2]}}' >"$tmp" && mv "$tmp" "$sfile"
    fi
    echo "権限を追加しました: $sfile" >&2
  elif [ ! -f "$sfile" ]; then
    printf '{\n  "permissions": {\n    "allow": [\n      "%s",\n      "%s"\n    ]\n  }\n}\n' "$rule1" "$rule2" >"$sfile"
    echo "権限を追加しました: $sfile" >&2
  else
    echo "注意: jq が無いため $sfile を自動編集できません。allow に手動追加してください: $rule1 / $rule2" >&2
  fi
fi
