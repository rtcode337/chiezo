"""ソースアダプタのレジストリ。

新ソースの追加手順:
  1. sources/<kind>.py にアダプタモジュールを書く(core.SourceAdapter を満たすクラス)
  2. 下の ADAPTERS に 1 行追加する
それだけで `SOURCE=<name>` で ingest 可能になる。

OSM の国別ソース(`osm_<国>`)と Wikipedia の言語版(`<lang>wiki`)は例外で、
自動生成カタログ(`osm_regions.OSM_REGIONS` 195 件 /
`wikipedia_editions.WIKIPEDIA_EDITIONS` 348 件)から機械的に登録している。
どの国・言語も「使いたくなったときに初期化できる」状態にしておきたいが、数百行を
手で書き足して綴りを保守するのは現実的でないため。

このリポジトリに入れられないソース(社内 wiki、社内サーバーから集めた構成情報など)は、
別リポジトリのモジュールを `CHIEZO_SOURCE_PLUGINS` で差し込む(下の
`load_plugin_adapters()`)。手順は `docs/adding-a-source.md` のケース 3 が正。
"""
from __future__ import annotations

import importlib
import os
import re
from typing import Callable

from core import SourceAdapter
from sources.geonames import GeonamesAdapter
from sources.osm import DEFAULT_VALIDATION, OsmAdapter
from sources.osm_regions import OSM_REGIONS, OsmRegion
from sources.wikipedia import DEFAULT_VALIDATION as WIKIPEDIA_DEFAULT_VALIDATION
from sources.wikipedia import WikipediaAdapter
from sources.wikipedia_editions import WIKIPEDIA_EDITIONS, WikipediaEdition

# 注意: ソース名の区切りにはアンダースコアを使う(osm_japan, zh_yuewiki)。
# ハイフンは世代ファイル名 <source>-<date>.db の区切りと衝突するため使わない
# (OSM_REGIONS.source は Geofabrik の slug のハイフンを変換済み。Wikipedia の
# dbname はもともとアンダースコア区切り)。
ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    "jawiki": lambda: WikipediaAdapter("jawiki", lang="ja"),
    # 全世界の地名は OSM ではなく GeoNames で賄う。大陸単位の OSM 抽出(europe だけで pbf
    # 32GB、ノード索引 100GB 超、構築 1 日以上)は現実的でないうえ、実測で osm_japan の 73% は
    # 店舗・施設の裾であり「全世界の地名」には過剰だった。GeoNames は地名辞典に振り切っている
    # ぶん約 400MB・1 ソースで全世界を賄える(その代わり店舗・営業時間は持たない)。
    # 店舗レベルの詳細が要る国だけ、下の osm_<国> を個別に取り込む。
    "geonames": lambda: GeonamesAdapter(),
}


def _osm_factory(region: OsmRegion) -> Callable[[], SourceAdapter]:
    """カタログ 1 行から OSM アダプタの生成関数を作る。

    min_docs / sample_titles は osm.py の DEFAULT_VALIDATION に明示があればそちらを優先する
    (osm_japan は「東京駅」「富士山」まで確認する手厚い検証を持っているため、カタログの
    機械的な下限で上書きしてはいけない)。
    """
    explicit = region.source in DEFAULT_VALIDATION
    return lambda: OsmAdapter(
        region.source,
        region=region.region,
        lang=region.lang,
        min_docs=None if explicit else region.min_docs,
        # RAM 索引で要るメモリの目安。取り込み前のメモリ検査に使われる
        ram_index_memory_gb=region.memory_gb,
        # 12GiB に収まらない国はディスク索引を既定にする(遅い代わりに 2GiB で焼ける)
        default_node_index=region.node_index,
    )


ADAPTERS.update({r.source: _osm_factory(r) for r in OSM_REGIONS.values()})


def _wikipedia_factory(edition: WikipediaEdition) -> Callable[[], SourceAdapter]:
    """カタログ 1 行から Wikipedia アダプタの生成関数を作る。

    min_docs は wikipedia.py の DEFAULT_VALIDATION に明示があればそちらを優先する
    (jawiki / enwiki は実在タイトルの検証まで持つ手厚い設定があるため、カタログの
    機械的な下限で上書きしてはいけない)。
    """
    explicit = edition.wiki_id in WIKIPEDIA_DEFAULT_VALIDATION
    return lambda: WikipediaAdapter(
        edition.wiki_id,
        lang=edition.lang,
        min_docs=None if explicit else edition.min_docs,
    )


# 明示登録済み(jawiki)はそのまま残す(カタログ側と内容は等価だが、明示が正)
ADAPTERS.update({
    e.wiki_id: _wikipedia_factory(e)
    for e in WIKIPEDIA_EDITIONS.values()
    if e.wiki_id not in ADAPTERS
})


# ---- 外部プラグイン ---------------------------------------------------------

PLUGIN_ENV = "CHIEZO_SOURCE_PLUGINS"

# ソース名に許す文字。ハイフンを弾くのは世代ファイル名 `<source>-<date>.db` の区切りと
# 衝突するため(取り込み自体は通り、ブルーグリーン切り替えの段で壊れる)。`/` や `.` も
# ファイル名になる以上ここで止める。
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def load_plugin_adapters(spec: str | None = None) -> dict[str, Callable[[], SourceAdapter]]:
    """外部モジュールのアダプタを取り込む。

    `CHIEZO_SOURCE_PLUGINS` にモジュール名をカンマ区切りで並べると、それぞれの
    `ADAPTERS`(このモジュールと同じ `{ソース名: 生成関数}` の dict)を取り込む。
    社外に出せないソースを別リポジトリに置いたまま、chiezo のイメージを `FROM` で
    継承して足せるようにするための唯一の口。

    **失敗は握り潰さず落とす**(`SystemExit`)。指定したのに読み込まれていない状態を
    許すと、後から「unknown SOURCE」や「管理画面に出ない」として現れて原因が分からない。
    プラグインは opt-in(設定した人は入れるつもりでいる)なので、壊れているなら
    起動時に気づけるほうがよい。chiezo-trigger が起動しなくなるのも、黙って
    カタログが欠けているより分かりやすい。

    既存ソースと同名は**受け付けない**。上書きを許すと `jawiki` を影で差し替えるような
    取り違えに気づけないため(名前を変えれば済む話なので、安全側に倒す)。
    """
    raw = os.environ.get(PLUGIN_ENV, "") if spec is None else spec
    loaded: dict[str, Callable[[], SourceAdapter]] = {}
    for module_name in (n.strip() for n in raw.split(",")):
        if not module_name:
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception as e:  # noqa: BLE001 - import 中の任意の失敗をそのまま見せる
            raise SystemExit(f"{PLUGIN_ENV}: cannot import {module_name!r}: {e}") from None
        adapters = getattr(module, "ADAPTERS", None)
        if not isinstance(adapters, dict) or not adapters:
            raise SystemExit(
                f"{PLUGIN_ENV}: {module_name!r} must define a non-empty ADAPTERS dict"
                " ({source_name: factory})"
            )
        for source, factory in adapters.items():
            if not isinstance(source, str) or not _SOURCE_NAME.match(source):
                raise SystemExit(
                    f"{PLUGIN_ENV}: {module_name!r} has an invalid source name {source!r}"
                    " (use [A-Za-z0-9_] only; '-' collides with the <source>-<date>.db separator)"
                )
            if source in ADAPTERS or source in loaded:
                raise SystemExit(
                    f"{PLUGIN_ENV}: {module_name!r} redefines an existing source {source!r};"
                    " rename it instead of shadowing"
                )
            if not callable(factory):
                raise SystemExit(
                    f"{PLUGIN_ENV}: {module_name!r} ADAPTERS[{source!r}] must be callable"
                    " (a zero-argument factory returning a SourceAdapter)"
                )
            loaded[source] = factory
    return loaded


PLUGIN_ADAPTERS = load_plugin_adapters()
ADAPTERS.update(PLUGIN_ADAPTERS)


def get_adapter(source: str) -> SourceAdapter:
    try:
        return ADAPTERS[source]()
    except KeyError:
        # osm_<国> だけで 195 件・<lang>wiki は 348 件あるため、全部並べず件数で示す
        others = sorted(
            n for n in ADAPTERS if not n.startswith("osm_") and n not in WIKIPEDIA_EDITIONS
        )
        known = ", ".join([
            *others,
            f"<lang>wiki {len(WIKIPEDIA_EDITIONS)} 件",
            f"osm_<国> {len(OSM_REGIONS)} 件",
        ])
        raise SystemExit(f"unknown SOURCE={source!r} (registered: {known})")
