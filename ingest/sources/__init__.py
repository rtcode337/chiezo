"""ソースアダプタのレジストリ。

新ソースの追加手順:
  1. sources/<kind>.py にアダプタモジュールを書く(core.SourceAdapter を満たすクラス)
  2. 下の ADAPTERS に 1 行追加する
それだけで `SOURCE=<name>` で ingest 可能になる。
"""
from __future__ import annotations

from typing import Callable

from core import SourceAdapter
from sources.osm import OsmAdapter
from sources.wikipedia import WikipediaAdapter

# 注意: ソース名の区切りにはアンダースコアを使う(osm_japan)。
# ハイフンは世代ファイル名 <source>-<date>.db の区切りと衝突するため使わない。
ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    "jawiki": lambda: WikipediaAdapter("jawiki", lang="ja"),
    "osm_japan": lambda: OsmAdapter("osm_japan", region="asia/japan", lang="ja"),
    # enwiki を追加する場合は次の 1 行を有効化するだけ:
    # "enwiki": lambda: WikipediaAdapter("enwiki", lang="en"),
    # 他地域の OSM も 1 行で追加できる(検証パラメータは要指定):
    # "osm_france": lambda: OsmAdapter("osm_france", region="europe/france", lang="fr",
    #                                  min_docs=50_000, sample_titles=["Paris", "Lyon"]),
}


def get_adapter(source: str) -> SourceAdapter:
    try:
        return ADAPTERS[source]()
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise SystemExit(f"unknown SOURCE={source!r} (registered: {known})")
