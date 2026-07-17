"""ソースアダプタのレジストリ。

新ソースの追加手順:
  1. sources/<kind>.py にアダプタモジュールを書く(core.SourceAdapter を満たすクラス)
  2. 下の ADAPTERS に 1 行追加する
それだけで `SOURCE=<name>` で ingest 可能になる。
"""
from __future__ import annotations

from typing import Callable

from core import SourceAdapter
from sources.wikipedia import WikipediaAdapter

ADAPTERS: dict[str, Callable[[], SourceAdapter]] = {
    "jawiki": lambda: WikipediaAdapter("jawiki", lang="ja"),
    # enwiki を追加する場合は次の 1 行を有効化するだけ:
    # "enwiki": lambda: WikipediaAdapter("enwiki", lang="en"),
}


def get_adapter(source: str) -> SourceAdapter:
    try:
        return ADAPTERS[source]()
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise SystemExit(f"unknown SOURCE={source!r} (registered: {known})")
