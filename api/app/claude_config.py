"""Claude Code 連携用の CLAUDE.md ブロックを生成する(管理画面のプレビュー用)。

`scripts/gen_claude_config.sh` と同じ内容を、稼働中の chiezo(=この API)から
組み立てる。スクリプトは POSIX sh + curl でソースを HTTP 越しに調べていたが、
こちらは同一プロセスから DB を直接引くので速い(HTTP プローブを打たない)。

管理画面はこの出力を「いま設定を吐き出したら何が出るか」のプレビューとして表示する。
実ファイル(クライアント側の ~/.claude/CLAUDE.md 等)は API からは見えないので触らない。

生成の正はこのモジュール(API 側)に置く方針。gen_claude_config.sh は将来的に
この出力(`GET /admin/claude-config.txt`)を取りに来る形へ寄せられる。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from app import db
from app.registry import FILTER_MIN_SCHEMA_VERSION, Source

BEGIN_MARK = "<!-- BEGIN chiezo (auto-generated) -->"
END_MARK = "<!-- END chiezo -->"


def _sample(src: Source) -> tuple[str, set[str]]:
    """例示に使うサンプルのタイトルと、その doc の extra に入っているキー集合。"""
    try:
        rows = db.query(src.path, "SELECT title, extra FROM docs LIMIT 1")
    except sqlite3.Error:
        return "<タイトル>", set()
    if not rows:
        return "<タイトル>", set()
    title = rows[0]["title"] or "<タイトル>"
    keys: set[str] = set()
    raw = rows[0]["extra"]
    if raw:
        try:
            keys = set(json.loads(raw).keys())
        except (ValueError, TypeError):
            keys = set()
    return title, keys


def _has_value(src: Source, column: str) -> bool:
    """生成列(v2 のみ)に非 NULL の行が 1 件でもあるか。索引があるので速い。

    column は呼び出し側の固定リテラル('lat'/'wikidata')のみ。ユーザー入力は渡さない。
    """
    try:
        rows = db.query(src.path, f"SELECT 1 FROM docs WHERE {column} IS NOT NULL LIMIT 1")
    except sqlite3.Error:
        return False
    return bool(rows)


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
    title, extra_keys = _sample(src)
    query = title if title != "<タイトル>" else "<検索語>"
    has_filter = src.schema_version >= FILTER_MIN_SCHEMA_VERSION

    if src.kind == "wikipedia":
        desc = f"{src.lang} Wikipedia" if src.lang else "Wikipedia"
        paren = f"{desc}, {docs_str}"
        out.append(f"- **{name}**({paren}): 一般知識・人物・作品・地名・用語・出来事など")
        out.append(f'  - 検索:   `curl -s "{base}/v1/{name}/search?q={query}&limit=5"`')
        out.append(f'  - 概要:   `curl -s "{base}/v1/{name}/doc?title={title}&fields=title,opening,tags"`')
        out.append(f'  - 本文:   `curl -s "{base}/v1/{name}/doc?title={title}&max_chars=8000"`')
        out.append(f'  - 候補:   `curl -s "{base}/v1/{name}/titles?prefix={title}"`')
        if "pageviews_month" in extra_keys:
            out.append(
                f'  - 人気度: `curl -s "{base}/v1/{name}/doc?title={title}&fields=title,extra"`'
                " (extra の `pageviews_month`=月次ページビュー(bot 除外)。知名度の客観指標に使える。"
                "**Wikimedia の pageviews API を叩く必要はない**)"
            )
        if has_filter and _has_value(src, "lat"):
            out.append(
                f'  - 座標:   `curl -s "{base}/v1/{name}/doc?title={title}&fields=title,extra"`'
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
        out.append(f'  - 検索:   `curl -s "{base}/v1/{name}/search?q={query}&limit=5"`')
        out.append(
            f'  - 座標等: `curl -s "{base}/v1/{name}/doc?title={title}&fields=title,extra"`'
            " (extra に lat/lon・OSM タグ・住所等)"
        )
        if has_filter:
            area = _sample_area(src)
            out.append(
                "  - 取り違え防止: 同名の別地物がある場合、doc の応答に `alternatives` が付く。"
                f"`&area={area}` や `&feature=railway%3Dstation` で絞り込める(search/doc 共通)"
            )
            out.append(
                f'  - 一括抽出: `curl -s "{base}/v1/{name}/filter?feature=amenity%3Dplace_of_worship'
                f'&area={area}&limit=200"`'
                " (地物種別 × 行政区で全件列挙。応答の `total` で件数が分かり `offset` でページングできる。"
                "**Overpass API を叩く必要はない**)"
            )
            out.append(
                f'  - 範囲抽出: `curl -s "{base}/v1/{name}/filter?feature=tourism%3Dmuseum'
                '&bbox=34.9,135.6,35.1,135.9"`'
                " (bbox は `min_lat,min_lon,max_lat,max_lon`。`feature` はカンマ区切りで複数指定可)"
            )
            if _has_value(src, "wikidata"):
                out.append(
                    f'  - 逆引き: `curl -s "{base}/v1/{name}/filter?wikidata=Q17221&fields=title,extra"`'
                    " (wikidata の Q 番号 → 地物)"
                )
        return

    # その他(geonames 等)
    paren = f"kind={src.kind or '?'}, {docs_str}"
    out.append(f"- **{name}**({paren})")
    out.append(f'  - 検索:   `curl -s "{base}/v1/{name}/search?q={query}&limit=5"`')
    out.append(f'  - 文書:   `curl -s "{base}/v1/{name}/doc?title={title}&fields=title,opening,body"`')


def permission_rules(base_url: str) -> list[str]:
    """settings.json の permissions.allow に追記される chiezo への curl 許可ルール。

    gen_claude_config.sh と同じ 2 本。スクリプトは jq 経路で `unique`(=ソート)して
    既存 allow に足すので、ここでもソート済みで返す。
    """
    base = base_url.rstrip("/")
    return sorted([f"Bash(curl -s {base}/v1/:*)", f"Bash(curl -s {base}/:*)"])


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


def build_block(sources: dict[str, Source], base_url: str, now: datetime | None = None) -> str:
    """CLAUDE.md に貼る chiezo ブロック(マーカー込み)を組み立てて返す。"""
    base = base_url.rstrip("/")
    when = (now or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M %Z").strip()

    out: list[str] = [
        BEGIN_MARK,
        "## chiezo(ローカル知識サーバー)",
        "",
        "LAN 内に読み取り専用の知識検索 API「chiezo」がある。下記ソースに載っている情報が"
        "必要になったら、**Web 検索や外部 API より先に chiezo を使うこと**"
        "(オフライン・レート制限なし・高速)。",
        f"ベース URL: `{base}`",
        "",
        "使い方の要点:",
        "- まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)。"
        "いきなり全文を取らない。",
        "- 3 文字未満の語はタイトル前方一致にフォールバックする"
        "(レスポンスの `mode` が `title_prefix` になる)。",
        '- 応答は JSON。エラーは `{"error": "..."}` 形式。全クエリ 5 秒でタイムアウト(超過は 504)。',
        f'- ソース一覧(最新の登録状況): `curl -s "{base}/v1/sources"`',
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
    out.append(
        f"<sub>この一覧は {when} 時点の chiezo(`{base}`)の登録ソースから自動生成。"
        f"再生成: `scripts/gen_claude_config.sh --base-url {base}`</sub>"
    )
    out.append(END_MARK)
    return "\n".join(out) + "\n"
