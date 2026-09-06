"""集めたソース —— AI が集めたものを長期記憶へ焼く。

素材を持っているのは配信側(`app/collect.py`)で、こちらは**取りに行って焼くだけ**。
長期記憶(`corpus/`)へ書けるのは ingest だけ、という線をこの層でも崩さないための形で、
やり取りは固化(`sources/memory.py`)とまったく同じ契約(`sources/remote.py`)を使う。

固化と違うのは **1 つではなく可変**なところ。収集は画面や REST から実行時に増えるので、
アダプタを固定で並べられない。`ADAPTERS` に書く代わりに、**カタログを配信側に聞いて
その場で組み立てる**(`sources/__init__.py` の `collect_adapters`)。

**毎回焼き直すが、中身は積み上がる。** 素材が「前世代 + 新しく集めたぶん」なので、
ブルーグリーン(全件の作り直し)に乗せたまま追記として振る舞う —— 世代は今と 1 つ前
だけが残る(`main.switch_db`)。
"""
from __future__ import annotations

import logging
import os

from core import SourceAdapter
from sources.remote import PluginError, RemotePluginAdapter, RemoteSource, _get_json

log = logging.getLogger("chiezo.ingest")

# 素材を配る相手。compose ではサービス名で届くので、通常は書き換えない
APP_URL_ENV = "CHIEZO_APP_URL"
DEFAULT_APP_URL = "http://chiezo-app:7010"

# カタログを引くときの上限(秒)。配信側はローカルの SQLite を読むだけなので短くてよい
CATALOG_TIMEOUT = 10.0


def app_base_url() -> str:
    return (os.environ.get(APP_URL_ENV) or DEFAULT_APP_URL).strip().rstrip("/")


def base_url() -> str:
    return f"{app_base_url()}/v1/collect"


def _source(entry: dict) -> RemoteSource:
    return RemoteSource(
        base_url=base_url(),
        name=str(entry["name"]),
        kind=str(entry.get("kind") or "collect"),
        lang=entry.get("lang"),
        label=str(entry.get("label") or entry["name"]),
        min_docs=int(entry.get("min_docs") or 1),
        memory_gb=float(entry.get("memory_gb") or 0.5),
    )


def catalog() -> list[RemoteSource]:
    """いま焼ける収集の一覧を配信側に聞く。

    **繋がらなくても落とさない**(空で返す)。収集は任意の層で、配信側が
    立っていない・無効にしている構成が普通にある —— そこで ingest 全体を
    止めると、他のソースの取り込みまで巻き添えになる。
    """
    url = f"{base_url()}/sources"
    try:
        payload = _get_json(url, CATALOG_TIMEOUT)
    except Exception as e:
        log.debug("collect catalog unavailable (%s): %s", url, e)
        return []
    entries = payload.get("sources")
    if not isinstance(entries, list):
        raise PluginError(f"{url} must return a 'sources' list")
    return [_source(e) for e in entries if isinstance(e, dict) and e.get("name")]


def adapters() -> dict[str, callable]:
    """`ADAPTERS` に混ぜる形(名前 → 生成関数)。"""
    return {src.name: (lambda s=src: RemotePluginAdapter(s)) for src in catalog()}


def adapter_for(name: str) -> SourceAdapter | None:
    """名前で 1 つだけ引く(`get_adapter` から呼ぶ)。"""
    for src in catalog():
        if src.name == name:
            return RemotePluginAdapter(src)
    return None
