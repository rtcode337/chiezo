"""Claude Code 連携用の CLAUDE.md ブロックを生成する(生成の正はここ)。

同一プロセスから DB を直接引いて組み立てる(HTTP プローブを打たないので速く正確)。
`scripts/gen_claude_config.sh` はこの出力(`GET /admin/claude-config.txt`、
`GET /admin/claude-config.permissions.json`、`GET /admin/claude-config.hook.py`、
`GET /admin/claude-config.hook.json`)を取得してクライアント側のファイルへ
書き込むだけの薄いクライアント。管理画面は同じ出力を「いま設定を吐き出したら
何が出るか」のプレビューとして表示する。

curl 例・許可ルールのベース URL は呼び出し側(main.request_origin)が
「アクセス元 URL のプロトコル・ホスト名・ポート」から導出して渡す。
実ファイル(クライアント側の ~/.claude/CLAUDE.md 等)は API からは見えないので触らない。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app import db
from app.registry import (
    FILTER_MIN_SCHEMA_VERSION,
    TAG_COUNTS_MIN_SCHEMA_VERSION,
    TAG_MIN_SCHEMA_VERSION,
    Source,
)

BEGIN_MARK = "<!-- BEGIN chiezo (auto-generated) -->"
END_MARK = "<!-- END chiezo -->"

# クライアント側に置くフック本体のファイル名。settings.json の中身を書き換える
# ときに「以前入れた Chiezo のフック」を見分ける鍵にもなるので、変えると
# 古いエントリが残る(gen_claude_config.sh 側の掃除条件もこの名前を見ている)。
HOOK_FILENAME = "chiezo-autoallow.py"
HOOK_PATH_PLACEHOLDER = "{{HOOK_PATH}}"

# MCP サーバー登録名。Claude 側のツール名の接頭辞(mcp__chiezo__search 等)になるので、
# 変えると既存環境の登録と二重になる。
MCP_SERVER_NAME = "chiezo"

_HOOK_SOURCE = Path(__file__).parent / "hooks" / "chiezo_autoallow.py"
_ORIGIN_LINE_RE = re.compile(r'^CHIEZO_ORIGIN = "[^"]*"$', re.MULTILINE)

# 例示タグを選ぶために読む doc_tags の行数と、その集計に許す時間(_sample_tag 参照)。
_TAG_SAMPLE_ROWS = 200_000
_TAG_SAMPLE_TIMEOUT = 2.0

# links の有無を判定するために読む docs の行数(_has_links 参照)。
_LINKS_SAMPLE_ROWS = 200_000


def _sample(src: Source) -> tuple[str, set[str], list[str]]:
    """例示に使うサンプルのタイトルと、その doc の extra のキー集合・tags。"""
    try:
        rows = db.query(src.path, "SELECT title, extra, tags FROM docs LIMIT 1")
    except sqlite3.Error:
        return "<タイトル>", set(), []
    if not rows:
        return "<タイトル>", set(), []
    title = rows[0]["title"] or "<タイトル>"
    keys: set[str] = set()
    raw = rows[0]["extra"]
    if raw:
        try:
            keys = set(json.loads(raw).keys())
        except (ValueError, TypeError):
            keys = set()
    try:
        tags = json.loads(rows[0]["tags"]) if rows[0]["tags"] else []
    except (ValueError, TypeError):
        tags = []
    return title, keys, tags


def _has_value(src: Source, column: str) -> bool:
    """生成列(v2 のみ)に非 NULL の行が 1 件でもあるか。索引があるので速い。

    column は呼び出し側の固定リテラル('lat'/'wikidata')のみ。ユーザー入力は渡さない。
    """
    try:
        rows = db.query(src.path, f"SELECT 1 FROM docs WHERE {column} IS NOT NULL LIMIT 1")
    except sqlite3.Error:
        return False
    return bool(rows)


def _has_links(src: Source) -> bool:
    """links(出リンク先タイトルの配列)が入っているソースか。

    links は生成列でも索引付きでもないので、`WHERE links IS NOT NULL LIMIT 1` を
    そのまま打つと、**入っていないソースほど遅い**(1 件も無いと全表を舐める。
    実測: geonames 13,391,482 件で 3.2 秒。ここは判定 1 個のために払う額ではない)。
    知りたいのは「このソースは links を持つ設計か」だけなので、先頭の一定件数だけ
    見て決める。links を作るソース(wikipedia)は全記事に入るし、一部にしか入らない
    ソース(osm は wikipedia タグのある地物だけ)でもこの範囲に必ず現れる。
    """
    try:
        rows = db.query(
            src.path,
            "SELECT 1 FROM (SELECT links FROM docs LIMIT ?)"
            " WHERE links IS NOT NULL LIMIT 1",
            (_LINKS_SAMPLE_ROWS,),
        )
    except sqlite3.Error:
        return False
    return bool(rows)


def _sample_tag(src: Source) -> str | None:
    """例示に使う実在のタグ。ソースに何が入っているかは分からないので、
    よく使われているタグを 1 つ採る(jawiki なら「存命人物」等)。

    doc_tags 全体の集計は巨大ソース(jawiki・geonames)では db の 5 秒クエリ
    タイムアウトに達し、結果を捨てたうえでその 5 秒がまるごと
    /admin/claude-config.txt の応答時間になる(実測: 2 ソースで約 11 秒。
    gen_claude_config.sh の既定 --timeout 10 を毎回超えて生成が失敗する)。
    ここが欲しいのは「実在してよく使われているタグ 1 つ」だけで、厳密な最多タグ
    である必要はないので、先頭 _TAG_SAMPLE_ROWS 行だけを数えて近似する。
    それでも駄目なら None(呼び出し側がサンプル文書のタグへフォールバックする)。

    schema_version 4 以降は集計済みの tag_counts があるので、そちらから正確な
    最多タグを一瞬で採れる(近似は 3 のままの DB 向けの経路)。
    """
    if src.schema_version >= TAG_COUNTS_MIN_SCHEMA_VERSION:
        try:
            rows = db.query(
                src.path,
                "SELECT tag FROM tag_counts ORDER BY docs DESC, tag LIMIT 1",
                timeout=_TAG_SAMPLE_TIMEOUT,
            )
        except (sqlite3.Error, db.QueryTimeout):
            return None
        return rows[0]["tag"] if rows else None

    try:
        rows = db.query(
            src.path,
            # NOT INDEXED は必須。idx_doc_tags_tag はこの副問い合わせの被覆索引なので、
            # 放っておくと先頭 N 行が「タグ名の辞書順で先頭」に偏り、標本として使えない
            # (doc_tags は doc_id 順に挿入されるので、rowid 順に読めば実質ランダム標本)。
            "SELECT tag, COUNT(*) AS n FROM (SELECT tag FROM doc_tags NOT INDEXED LIMIT ?)"
            " GROUP BY tag ORDER BY n DESC LIMIT 1",
            (_TAG_SAMPLE_ROWS,),
            timeout=_TAG_SAMPLE_TIMEOUT,
        )
    except (sqlite3.Error, db.QueryTimeout):
        return None
    return rows[0]["tag"] if rows else None


def _sample_area(src: Source) -> str:
    try:
        rows = db.query(
            src.path,
            "SELECT area FROM docs WHERE feature='boundary=administrative'"
            " AND area IS NOT NULL LIMIT 1",
        )
    except sqlite3.Error:
        return "<行政区名>"
    return (rows[0]["area"] if rows else None) or "<行政区名>"


def _emit_source(base: str, src: Source, out: list[str]) -> None:
    name = src.name
    docs_str = f"{src.doc_count:,}件"
    title, extra_keys, sample_tags = _sample(src)
    query = title if title != "<タイトル>" else "<検索語>"
    has_filter = src.schema_version >= FILTER_MIN_SCHEMA_VERSION
    has_tags = src.schema_version >= TAG_MIN_SCHEMA_VERSION

    if src.kind == "wikipedia":
        desc = f"{src.lang} Wikipedia" if src.lang else "Wikipedia"
        paren = f"{desc}, {docs_str}"
        out.append(f"- **{name}**({paren}): 一般知識・人物・作品・地名・用語・出来事など")
        out.append(f'  - 検索:   `curl -sG "{base}/v1/{name}/search?limit=5" --data-urlencode "q={query}"`')
        out.append(
            f'  - 概要:   `curl -sG "{base}/v1/{name}/doc?fields=title,opening,tags"'
            f' --data-urlencode "title={title}"`'
        )
        out.append(f'  - 本文:   `curl -sG "{base}/v1/{name}/doc?max_chars=8000" --data-urlencode "title={title}"`')
        out.append(f'  - 候補:   `curl -sG "{base}/v1/{name}/titles" --data-urlencode "prefix={title}"`')
        if _has_links(src):
            out.append(
                f'  - リンク: `curl -sG "{base}/v1/{name}/links" --data-urlencode "title={title}"`'
                " (その記事から出ている内部リンク先のタイトル一覧。関連記事をたどるのに使う。"
                "**出リンクのみで、被リンク(この記事を指している記事)は取れない**。"
                "本文の出現順そのままなので同じタイトルが何度も入り、`記事名#節名` の形も"
                "混じる。`doc` に渡す前に重複を落として `#` の前で切ること)"
            )
        if has_tags:
            tag = _sample_tag(src) or (sample_tags[0] if sample_tags else "<カテゴリ名>")
            out.append(
                f'  - カテゴリ列挙: `curl -sG "{base}/v1/{name}/filter?limit=200&fields=title,tags"'
                f' --data-urlencode "tag={tag}"`'
                " (そのカテゴリの記事を全件。応答の `total` で件数が分かり `offset` でページング"
                "できる。**本文の全文検索で `Category:` 行を探してはいけない** — ソートキー付きの"
                "記事(`[[Category:X|よみがな]]`)は本文側にカテゴリ名が残らず取りこぼす)"
            )
            out.append(
                f'  - カテゴリ名検索: `curl -sG "{base}/v1/{name}/tags?limit=20"'
                f' --data-urlencode "contains=ラーメン"`'
                " (実在するタグ名を文書数つきで返す。`filter?tag=` は完全一致なので、"
                "先にここで正しい名前を確かめる。前方一致なら `prefix=` の方が速い)"
            )
        if "pageviews_month" in extra_keys:
            out.append(
                f'  - 人気度: `curl -sG "{base}/v1/{name}/doc?fields=title,extra" --data-urlencode "title={title}"`'
                " (extra の `pageviews_month`=月次ページビュー(bot 除外)。知名度の客観指標に使える。"
                "**Wikimedia の pageviews API を叩く必要はない**)"
            )
        if has_filter and _has_value(src, "lat"):
            out.append(
                f'  - 座標:   `curl -sG "{base}/v1/{name}/doc?fields=title,extra" --data-urlencode "title={title}"`'
                " (座標を持つ記事は extra に `lat`/`lon` が入る。"
                "`filter?bbox=min_lat,min_lon,max_lat,max_lon` で範囲抽出も可。"
                "**ジオコーディング API を叩く必要はない**)"
            )
        if has_filter and "wikidata" in extra_keys:
            out.append(
                f'  - 逆引き: `curl -s "{base}/v1/{name}/filter?wikidata=Q17221&fields=title,extra"`'
                " (wikidata の Q 番号 → 記事。OSM 側の wikidata タグと突き合わせできる。"
                "**wikidata.org を叩く必要はない**)"
            )
        return

    if src.kind == "osm":
        paren = f"OpenStreetMap 地名・POI 辞典, {docs_str}"
        out.append(
            f"- **{name}**({paren}): 地名・行政区・自然地物に加え病院/学校/店舗/観光地などの施設、"
            "駅・空港・港・IC/SA などの交通インフラと座標"
        )
        out.append(f'  - 検索:   `curl -sG "{base}/v1/{name}/search?limit=5" --data-urlencode "q={query}"`')
        out.append(
            f'  - 座標等: `curl -sG "{base}/v1/{name}/doc?fields=title,extra" --data-urlencode "title={title}"`'
            " (extra に lat/lon・OSM タグ・住所等)"
        )
        if has_filter:
            area = _sample_area(src)
            out.append(
                "  - 取り違え防止: 同名の別地物がある場合、doc の応答に `alternatives` が付く。"
                f'`--data-urlencode "area={area}"` や `--data-urlencode "feature=railway=station"` '
                "で絞り込める(search/doc 共通)"
            )
            out.append(
                f'  - 一括抽出: `curl -sG "{base}/v1/{name}/filter?limit=200"'
                f' --data-urlencode "feature=amenity=place_of_worship" --data-urlencode "area={area}"`'
                " (地物種別 × 行政区で全件列挙。応答の `total` で件数が分かり `offset` でページングできる。"
                "**Overpass API を叩く必要はない**)"
            )
            out.append(
                f'  - 範囲抽出: `curl -sG "{base}/v1/{name}/filter?bbox=34.9,135.6,35.1,135.9"'
                ' --data-urlencode "feature=tourism=museum"`'
                " (bbox は `min_lat,min_lon,max_lat,max_lon`。`feature` はカンマ区切りで複数指定可)"
            )
            if _has_value(src, "wikidata"):
                out.append(
                    f'  - 逆引き: `curl -s "{base}/v1/{name}/filter?wikidata=Q17221&fields=title,extra"`'
                    " (wikidata の Q 番号 → 地物)"
                )
        if _has_links(src):
            out.append(
                f'  - 対応記事: `curl -sG "{base}/v1/{name}/links" --data-urlencode "title={title}"`'
                " (OSM の `wikipedia` タグから作った対応記事のタイトル。言語プレフィックスは"
                "外してあるので Wikipedia ソースの `doc?title=` にそのまま渡せる。"
                "タグの無い地物は空配列)"
            )
        return

    # その他(geonames 等)
    paren = f"kind={src.kind or '?'}, {docs_str}"
    out.append(f"- **{name}**({paren})")
    out.append(f'  - 検索:   `curl -sG "{base}/v1/{name}/search?limit=5" --data-urlencode "q={query}"`')
    out.append(
        f'  - 文書:   `curl -sG "{base}/v1/{name}/doc?fields=title,opening,body"'
        f' --data-urlencode "title={title}"`'
    )
    if _has_links(src):
        out.append(
            f'  - リンク: `curl -sG "{base}/v1/{name}/links" --data-urlencode "title={title}"`'
            " (その文書から出ているリンク先タイトルの一覧。**出リンクのみで、被リンクは取れない**。"
            "重複を落とすのは呼び出し側の仕事)"
        )
    if has_tags and (tag := _sample_tag(src) or (sample_tags[0] if sample_tags else None)):
        out.append(
            f'  - タグ列挙: `curl -sG "{base}/v1/{name}/filter?limit=200"'
            f' --data-urlencode "tag={tag}"`'
            f' (同じタグを持つ文書を全件。タグ名は `curl -sG "{base}/v1/{name}/tags"'
            ' --data-urlencode "contains=<語>"` で探せる)'
        )


def permission_rules(base_url: str) -> list[str]:
    """settings.json の permissions.allow に追記される Chiezo への curl 許可ルール。

    Bash ルールはコマンド文字列の前方一致なので、実際に打たれる形のぶんだけ変種が要る:
    `-s`(単純 GET)/`-sG`(--data-urlencode 併用)× URL のクォート有無 の 4 本。
    `/v1/` 個別ルールは `/` 前方一致に包含されるので出さない。
    スクリプトは jq 経路で `unique`(=ソート)して既存 allow に足すので、
    ここでもソート済みで返す。
    """
    base = base_url.rstrip("/")
    return sorted(
        f"Bash(curl {flags} {quote}{base}/:*)"
        for flags in ("-s", "-sG")
        for quote in ("", '"')
    )


def permission_json(base_url: str) -> str:
    """権限ファイル(新規作成時)の中身。既存ファイルには allow へ追記マージされる。"""
    return (
        json.dumps(
            {"permissions": {"allow": permission_rules(base_url)}},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def mcp_servers_json(base_url: str) -> str:
    """MCP サーバー登録の断片(`.mcp.json` の中身そのもの)。

    プロジェクト用 `.mcp.json` には新規作成ならこのまま、既存があれば
    `mcpServers.chiezo` としてマージされる。ユーザースコープは Claude Code CLI
    (`claude mcp add --scope user`)経由で登録するので、この JSON は使わず
    URL(`<base>/mcp`)だけを使う(いずれも gen_claude_config.sh 側の仕事)。
    """
    base = base_url.rstrip("/")
    return (
        json.dumps(
            {
                "mcpServers": {
                    MCP_SERVER_NAME: {"type": "http", "url": f"{base}/mcp"},
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def hook_script(base_url: str) -> str:
    """クライアントへ配る PreToolUse フック本体(`app/hooks/chiezo_autoallow.py`)。

    前方一致の `permissions.allow` は curl が先頭に来る形にしか効かないので、
    ループやパイプで大量に引くとき(= いちばん許可したい場面)には 1 本もマッチ
    しない。フックはコマンドを構造で見て「Chiezo だけを読む」ものを自動許可する。
    ここでは配信時にベース URL を埋め込むだけで、判定ロジックはフック側にある。
    """
    base = base_url.rstrip("/")
    src = _HOOK_SOURCE.read_text(encoding="utf-8")
    replaced, n = _ORIGIN_LINE_RE.subn(f"CHIEZO_ORIGIN = {json.dumps(base)}", src)
    if n != 1:
        # 差し替え対象を見失ったまま配ると localhost 宛てのフックを配ることになる。
        raise RuntimeError(f"CHIEZO_ORIGIN を {_HOOK_SOURCE} に見つけられない(置換 {n} 件)")
    return replaced


def hook_settings_json() -> str:
    """settings.json の `hooks` へマージされる断片。

    フック本体の設置先はクライアント側(`--user` か `--project` か)で変わり
    api からは見えないので、コマンドは `{{HOOK_PATH}}` のままにしておき、
    絶対パスへの差し替えは gen_claude_config.sh が行う。
    """
    return (
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": HOOK_PATH_PLACEHOLDER,
                                    "timeout": 10,
                                }
                            ],
                        }
                    ]
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def build_block(
    sources: dict[str, Source],
    base_url: str,
    now: datetime | None = None,
    hook: bool = False,
    mcp: bool = False,
) -> str:
    """CLAUDE.md に貼る Chiezo ブロック(マーカー込み)を組み立てて返す。

    `hook=True` のときだけ、自動許可フックに載る書き方の指示を足す。フックは
    クライアント側で `--with-hook` を指定したときにしか入らないので、既定で
    書いてしまうと入れていない環境には嘘になる(gen_claude_config.sh が
    フックを設置するときだけ `?hook=1` で取りに来る)。
    `mcp=True` も同じ理屈で、MCP サーバーを登録した環境にだけ使い分けの指示を足す
    (`--with-mcp` のときだけ `?mcp=1` で取りに来る)。
    """
    base = base_url.rstrip("/")
    when = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M %Z").strip()

    out: list[str] = [
        BEGIN_MARK,
        "## Chiezo(ローカル知識サーバー)",
        "",
        "LAN 内に読み取り専用の知識検索 API「Chiezo」がある。下記ソースに載っている情報が"
        "必要になったら、**Web 検索や外部 API より先に Chiezo を使うこと**"
        "(オフライン・レート制限なし・高速)。",
        f"ベース URL: `{base}`",
        "",
        "使い方の要点:",
        "- まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)。"
        "いきなり全文を取らない。",
        "- 3 文字未満の語はタイトル前方一致にフォールバックする"
        "(レスポンスの `mode` が `title_prefix` になる)。",
        "- 日本語・スペース等を含むパラメータ(`q`/`title`/`prefix`/`area`/`feature`/`tag`)は"
        "下記例のとおり `curl -sG --data-urlencode` で渡す"
        "(URL に直接埋め込むとサーバーに Invalid HTTP request で弾かれる)。",
        '- 応答は JSON。エラーは `{"error": "..."}` 形式。全クエリ 5 秒でタイムアウト(超過は 504)。'
        "**504 は「0 件」ではなく「取れなかった」を意味する**ので、空の結果として先へ進まず、"
        "`limit` を小さくして取り直す(そのまま続けると静かに取りこぼす)。",
        f'- ソース一覧(最新の登録状況): `curl -s "{base}/v1/sources"`',
    ]

    if mcp:
        out.append(
            f"- MCP ツール(`mcp__{MCP_SERVER_NAME}__search` / `doc` / `filter` 等)も"
            "同じ機能を提供している。**単発の参照は MCP ツールを優先**してよい"
            "(引数が構造化されていて URL エンコードの失敗が無い)。"
            "**大量取得・後処理があるときは curl** — MCP の応答は必ずコンテキストを"
            "通るので、ページングや突合はファイルに落とせる curl のほうが向く。"
        )

    if hook:
        out.append(
            "- 大量に引くときは `for` ループやパイプにまとめてよい(許可プロンプトは出ない)。"
            "ただし Chiezo 以外のホストを混ぜる・`$(...)`・`eval`・`sed`/`awk`・"
            "ファイルへの書き出し(`curl -o`、`> file`)を使うと自動許可から外れて"
            "毎回プロンプトになるので、取得は `curl`→`jq` の読み取りだけで完結させる。"
        )

    out += [
        "",
        "### 収録ソース",
    ]

    ordered = sorted(sources.values(), key=lambda s: s.name)
    if ordered:
        for src in ordered:
            _emit_source(base, src, out)
    else:
        out.append("- (生成時点で登録済みソースは 0 件だった。取り込み後に本ブロックを再生成すること)")

    out.append("")
    # 再生成の案内には今回のフラグを引き継がせる(--with-mcp で生成したブロックを
    # フラグ無しで再生成すると、登録は残っているのに使い分けの指示だけ消えるため)
    regen = f"scripts/gen_claude_config.sh --base-url {base}"
    if mcp:
        regen += " --with-mcp"
    if hook:
        regen += " --with-hook"
    out.append(
        f"<sub>この一覧は {when} 時点の Chiezo(`{base}`)の登録ソースから自動生成。"
        f"再生成: `{regen}`</sub>"
    )
    out.append(END_MARK)
    return "\n".join(out) + "\n"
