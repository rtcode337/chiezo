"""ingest/sources/__init__.py の ADAPTERS と対応する、既知ソースの静的な一覧。

chiezo-api は ingest 側のアダプタ実装を import しない(コンテナが別・依存関係も別のため)。
管理画面の「初期化」ボタンにどのソース名を出すかだけを知ればよいので、名前と表示用の
kind/lang だけをここに複製する。

新ソースを ingest/sources/__init__.py の ADAPTERS に追加したら、あわせてここにも
1 行追加すること(CLAUDE.md の「新ソースの追加手順」参照)。
"""
from __future__ import annotations

KNOWN_SOURCES: dict[str, dict[str, str]] = {
    "jawiki": {"kind": "wikipedia", "lang": "ja"},
    "osm_japan": {"kind": "osm", "lang": "ja"},
    "osm_europe": {"kind": "osm", "lang": ""},
}
