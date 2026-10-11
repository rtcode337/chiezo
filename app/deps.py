"""REST とブラウズ画面が共有する下ごしらえ。

`app/main.py`(REST)と `app/views/`(人間向け HTML)の両方から使うものだけを置く。
main に置いたままだと views が main を import することになり、main → views(router の
登録)との間で循環参照になる。ここは app の他モジュールを import しない。

- ソースの取り出し(`get_source`)
- 並び順の SQL 断片(`exact_title_first` / `relevance_order`)
- 持っていないものを明示的に断る検査(`require_*`)
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from app import db
from app.registry import Source

# 関連度(bm25)に人気度(rank_score)を混ぜる重み。0 にすると従来どおり bm25 のみ。
# 実測(scripts/fts_lab.py で本番 jawiki 3 万件・重みを 0〜2 で振った)から 0.4 を採った。
# 0.3〜0.5 で「ラーメン」に対する有名店、「浅草寺」に対する浅草のような順当な記事が
# 上がり、2.0 まで上げると語の関連が薄い人気記事(織田信長など)を拾い始める。
POPULARITY_WEIGHT = 0.4


def get_source(request: Request, source: str) -> Source:
    sources: dict[str, Source] = request.app.state.sources
    src = sources.get(source)
    if src is None:
        raise HTTPException(
            404,
            {"error": f"unknown source: {source}", "sources": sorted(sources)},
        )
    return src


def exact_title_first(prefix: str = "") -> str:
    """タイトルが検索語と完全一致する文書を最上位へ寄せる、ORDER BY の第 1 キー。

    bm25 は「その語をよく含む文書」を上に置くが、「その語そのものを説明している文書」を
    特別扱いはしない。実測でも `京都` の検索で京都市・近鉄京都線が上に来て、記事「京都」は
    5 位以内に入らなかった(本文が長いぶん bm25 の長さ正規化で不利になるため)。
    百科事典的な引き方では同名の記事があればそれが答えなので、人気度や関連度と混ぜず、
    独立した段として先に置く(完全一致が無いクエリでは何も起きない)。

    `lower()` は英語版 wiki 向け(SQLite の lower は ASCII のみなので日本語では実質無効)。
    呼び出し側は WHERE 句のパラメータの後・LIMIT の前に検索語を渡すこと。
    """
    return f"CASE WHEN lower({prefix}title) = lower(?) THEN 0 ELSE 1 END"


def relevance_order(prefix: str = "") -> str:
    """関連度に人気度を混ぜた ORDER BY 句を返す(FTS 検索用)。

    bm25() は「良い一致ほど小さい負値」なので、人気度で係数を大きくするほど上位へ動く。
    rank_score を 0〜1 に丸めてから使うのは保険で、範囲を外れた値(人口の生値のような
    桁の大きい数)が入っていても、その 1 列だけで並びが決まらないようにするため。
    """
    score = f"MIN(1.0, MAX(0.0, COALESCE({prefix}rank_score, 0.0)))"
    return (
        f"{exact_title_first(prefix)},"
        f" bm25(docs_fts, 5.0, 1.0) * (1.0 + {POPULARITY_WEIGHT} * {score}) ASC"
    )


def has_recency_index(src: Source) -> bool:
    """新しい順に引く索引(`idx_docs_updated`)を持っているか。

    **コアスキーマには入っていない**(溜まっていくソースだけが持つ)。
    `sqlite_master` を 1 行引くだけなので、毎回確かめてよい。
    """
    try:
        rows = db.query(
            src.path,
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_docs_updated'",
        )
    except Exception:
        # 読めないソースは「持っていない」でよい(断る側へ倒す)
        return False
    return bool(rows)


def require_recency_index(src: Source) -> None:
    """新しい順に引けないソースを明示的に断る。

    **黙って全走査させない。** `updated_at` の索引はコアスキーマに無いので、
    持たないソースで並べ替えると `docs` を全部読むことになる(jawiki なら 150 万行)。
    しかも**ダンプ由来のソースでは並べても意味を成さない** —— `updated_at` は
    記事の版や取り込み時刻で、新しさの順にはならない。
    """
    if not has_recency_index(src):
        raise HTTPException(
            409,
            {
                "error": f"source {src.name} cannot be listed by recency",
                "reason": "新しい順に引く索引を持っていません",
                "hint": "溜まっていくソース(集めたもの・覚えたこと)だけが対応しています。"
                        "ダンプ由来のソースは updated_at が新しさを表さないので、"
                        "search / filter で引いてください",
            },
        )


def has_feature_area(src: Source) -> bool:
    """このソースが `feature` / `area` を持っているか(索引だけで分かる)。

    持っているのは地物のソース(osm・geonames)だけで、wikipedia 系の文書はどれも
    NULL。1 件だけ探せばよいので `idx_docs_feature_area` の先頭を覗いて判定する
    (`app/claude_config.py` が索引付きの列を同じ形で探っているのと同じやり方)。
    結果は Source に覚える — 走査のたびに作り直されるので、DB を差し替えれば消える。
    """
    cached = getattr(src, "_has_feature_area", None)
    if cached is None:
        rows = db.query(
            src.path,
            "SELECT 1 FROM docs INDEXED BY idx_docs_feature_area"
            " WHERE feature IS NOT NULL LIMIT 1",
            (),
        )
        cached = bool(rows)
        object.__setattr__(src, "_has_feature_area", cached)
    return cached


def require_attributes(src: Source, *, feature: str | None, area: str | None) -> None:
    """持っていない属性で絞ろうとしたら、0 件ではなく理由を返す。

    wikipedia 系のソースに `area=東京都` を付けると、条件としては正しいのに必ず 0 件になる。
    人にとっても分かりにくいが、agent モードでは致命的だった: モデルは 0 件を見ても
    理由が分からず、絞り込みを付けたまま検索語だけ変えて何度も空振りする(実測)。
    「そのソースにその属性は無い」と言えば、次の手に移れる。
    """
    if not (feature or area) or has_feature_area(src):
        return
    raise HTTPException(
        400,
        {
            "error": f"source {src.name} has no feature/area attributes",
            "hint": "地物の属性(feature / area)を持つのは osm・geonames などの地物ソースだけ。"
                    "wikipedia 系のソースは tag(カテゴリ名)で絞るか、絞り込み無しで引くこと",
        },
    )
