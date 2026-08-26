"""長期記憶(`memory`)—— 短期記憶を固めたソース。

素材を持っているのは配信側(`app/memory.py`)で、こちらは**取りに行って焼くだけ**。
やり取りは別コンテナのプラグインと同じ契約(`sources/remote.py`)なので、
アダプタの中身はその使い回しで足りる。違うのは 2 点:

- **URL が決まっている**(`CHIEZO_APP_URL` の `/v1/memory`)。プラグインと違って
  相手は必ず chiezo-app なので、設定に URL を書かせる理由が無い
- **`ADAPTERS` に最初から入っている**。だから管理画面の一覧にそのまま出るし、
  `SOURCE=memory` で CLI からも回せる(`CHIEZO_PLUGIN_SOURCES` の設定は要らない)

配信側を import しない点は他のアダプタと同じ。HTTP で話すだけなので、コンテナも
依存も分かれたままでいられる。
"""
from __future__ import annotations

import os

from core import SourceAdapter
from sources.remote import RemotePluginAdapter, RemoteSource

SOURCE_NAME = "memory"

# 素材を配る相手。compose ではサービス名で届くので、通常は書き換えない。
# 配信側を別ホストで動かしている構成でだけ設定する。
APP_URL_ENV = "CHIEZO_APP_URL"
DEFAULT_APP_URL = "http://chiezo-app:7010"


def app_base_url() -> str:
    return (os.environ.get(APP_URL_ENV) or DEFAULT_APP_URL).strip().rstrip("/")


def memory_adapter() -> SourceAdapter:
    return RemotePluginAdapter(
        RemoteSource(
            base_url=f"{app_base_url()}/v1/memory",
            name=SOURCE_NAME,
            kind="memory",
            label="長期記憶(短期記憶を固めたもの)",
            # 数十件から始まるので、ダンプ由来のソースのような下限は課せない。
            # 実際の検証条件は素材の 1 行目(meta)が上書きする。
            min_docs=1,
            # 1 行ずつ流し込むだけ。文書数に関わらずメモリは増えない
            memory_gb=0.5,
        )
    )
