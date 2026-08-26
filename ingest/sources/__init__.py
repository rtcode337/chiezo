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

このリポジトリに入れられないソース(公開できないプライベートな情報など)は、
このリポジトリに入れられないソースは、別コンテナのプラグインから借りる
(`sources/remote.py`、`CHIEZO_PLUGIN_SOURCES`)。プラグインは Chiezo のコードを含まず、
文書を配るだけでよい。手順は `docs/adding-a-source.md` のケース 3 が正。
"""
from __future__ import annotations

from collections.abc import Callable

from core import SourceAdapter
from sources.geonames import GeonamesAdapter
from sources.memory import memory_adapter
from sources.osm import DEFAULT_VALIDATION, OsmAdapter
from sources.osm_regions import OSM_REGIONS, OsmRegion
from sources.remote import load_remote_adapters
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
    # 長期記憶(短期記憶を固めたもの)。素材は配信側(app/memory.py)が配り、こちらは
    # 取りに行って焼くだけ。**組み込みにしてある**ので、設定を足さなくても管理画面の
    # 一覧に出るし、SOURCE=memory で CLI からも回せる(sources/memory.py)。
    "memory": memory_adapter,
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




def get_adapter(source: str) -> SourceAdapter:
    if source in ADAPTERS:
        return ADAPTERS[source]()
    # 別コンテナのプラグイン(CHIEZO_PLUGIN_SOURCES)。問い合わせるのはここで初めて ——
    # import 時に聞きに行くと、プラグインが起動していない間は本体を立てられなくなる。
    remote = load_remote_adapters()
    if source in remote:
        return remote[source]()
    # osm_<国> だけで 195 件・<lang>wiki は 348 件あるため、全部並べず件数で示す
    others = sorted(
        n for n in ADAPTERS if not n.startswith("osm_") and n not in WIKIPEDIA_EDITIONS
    )
    known = ", ".join([
        *others,
        *sorted(f"{n} (plugin)" for n in remote),
        f"<lang>wiki {len(WIKIPEDIA_EDITIONS)} 件",
        f"osm_<国> {len(OSM_REGIONS)} 件",
    ])
    # 使い方の間違いなので、例外の連鎖を見せても助けにならない
    raise SystemExit(f"unknown SOURCE={source!r} (registered: {known})")
