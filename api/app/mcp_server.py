"""chiezo を MCP(Streamable HTTP)でも喋らせる薄い層。

設計の要点:

- **ツールの実体は `app/main.py` の REST エンドポイント関数そのもの**。MCP 用に処理を
  書き直すと、必ず片方だけ直されて挙動がずれるので、意図的に同じ関数を呼ぶ。
  ただし FastAPI のエンドポイント関数は既定値が `Query(...)` オブジェクトなので、
  Python から直接呼ぶときは**全パラメータを明示的に渡す**必要がある(省略すると Query
  インスタンスがそのまま値として渡り、`if tag:` が常に真になるような静かな誤動作になる)。
  `tests/test_mcp.py` がシグネチャを突き合わせて渡し漏れを落とす。
- FastMCP は**同期のツール関数をイベントループ上で直接呼ぶ**(非同期関数だけ await する)。
  chiezo のクエリは最大 5 秒ブロックしうるので、必ず `run_in_threadpool` に逃がす。
  そうしないと重いクエリ 1 本で API 全体(管理画面や他のリクエスト)が止まる。
- `stateless_http=True`。読み取り専用・LAN 内・セッションに持つ状態が無いので、
  セッション管理を挟む理由がない(複数ワーカーでも素直に動く)。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Callable

import os

from fastapi import FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool

from app import notes

# doc のレスポンスはそのままモデルのコンテキストに載るので、REST(既定 0 = 無制限)より
# 短く切る。jawiki には 10 万字級の記事があり、既定で全文を返すと 1 回で窓を潰す。
MCP_DOC_MAX_CHARS = 4000

INSTRUCTIONS = """\
LAN 内の読み取り専用ナレッジ API「chiezo」。Wikipedia・OpenStreetMap・GeoNames を
ローカルの SQLite に取り込んであり、オフライン・レート制限なしで引ける。
ここに載っている情報が要るときは、Web 検索や外部 API より先にこちらを使うこと。

使い方の原則:
- まず `search` で当たりを付け、必要な文書だけ `doc` を取る(いきなり全文を取らない)。
- 3 文字未満の語は全文検索できずタイトル前方一致にフォールバックする
  (応答の mode が title_prefix になる)。
- 「カテゴリ○○の記事を全部」のような列挙は `filter`(tag 指定)を使う。
  本文の全文検索で "Category:" 行を探すと、ソートキー付きの記事を静かに取りこぼす。
- どのソースが登録されているかは `sources` で分かる。

`remember` / `recall` が見えている場合、chiezo は短期記憶の置き場も兼ねている:
- ユーザーが「覚えておいて」と言ったこと、後から参照する価値のある調査結果は `remember`。
- 「さっき話したあの件」「先月の件」のように過去のやり取りを指されたら `recall`。
  曖昧な指され方のときは検索語を無理に作らず、`since` だけで新しい順に引くほうが当たる。
"""


def _transport_security() -> TransportSecuritySettings:
    """Host ヘッダ検証の設定。

    FastMCP の既定は「localhost 系の Host しか受け付けない」(DNS リバインディング対策)。
    そのままだと LAN の別マシンから `http://192.168.0.3:9000/mcp` を叩いた時点で
    421 になり、この API の使い方(LAN 内から引く)がまるごと成立しない。
    chiezo は REST 側も認証なし・LAN 内前提なので、既定では検証を外して足並みを揃える。
    絞りたい場合は `CHIEZO_MCP_ALLOWED_HOSTS` に許可する Host をカンマ区切りで書く
    (`192.168.0.3:9000,localhost:*` のように、末尾 `:*` でポート任意を表せる)。
    """
    allowed = [h.strip() for h in os.environ.get("CHIEZO_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed,
        allowed_origins=[f"http://{h}" for h in allowed] + [f"https://{h}" for h in allowed],
    )


def _request(app: FastAPI) -> Any:
    """エンドポイント関数に渡す最小の器。

    どの関数も `request.app.state.sources` しか触らないので、これで足りる
    (MCP 経由には本物の HTTP リクエストが存在しない)。
    """
    return SimpleNamespace(app=app)


def _call(fn: Callable[..., Any], **kwargs: Any) -> Any:
    """エンドポイント関数を呼び、HTTPException を MCP のツールエラーに翻訳する。

    HTTP のステータスコードは MCP には無いので、本文の JSON をそのまま文字列で返す
    (404 の candidates や 409 の移行案内など、モデルが次の手を決めるのに要る情報が
    detail に入っているため、握り潰さない)。
    """
    try:
        return fn(**kwargs)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
        raise ToolError(json.dumps(detail, ensure_ascii=False)) from None


def build_mcp(app: FastAPI) -> FastMCP:
    """FastAPI アプリに紐づく MCP サーバーを組み立てて返す。

    `app/main.py` の末尾から呼ばれる。main を遅延 import しているのは循環参照を
    避けるため(呼ばれる時点で main の関数定義は済んでいる)。
    """
    from app import main as api

    mcp = FastMCP(
        "chiezo",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        # マウント側で /mcp を担当するので、この app 自身はルート直下で待ち受ける
        streamable_http_path="/",
        transport_security=_transport_security(),
    )

    @mcp.tool(description="登録済みソースの一覧(名前・種類・言語・文書数・スキーマ版)を返す。")
    async def sources() -> dict:
        return await run_in_threadpool(_call, api.list_sources, request=_request(app))

    @mcp.tool(
        description=(
            "全文検索。まずこれで当たりを付け、必要な文書だけ doc で取る。"
            "area / feature / bbox / tag で絞り込める(同名の別地物の取り違え防止)。"
            "3 文字未満の語はタイトル前方一致にフォールバックする(mode=title_prefix)。"
        )
    )
    async def search(
        source: str,
        q: str,
        limit: int = 10,
        offset: int = 0,
        area: str | None = None,
        feature: str | None = None,
        bbox: str | None = None,
        tag: str | None = None,
    ) -> dict:
        return await run_in_threadpool(
            _call, api.search,
            request=_request(app), source=source, q=q, limit=limit, offset=offset,
            area=area, feature=feature, bbox=bbox, tag=tag,
        )

    @mcp.tool(
        description=(
            "タイトル完全一致(別名も解決)で 1 文書を取る。"
            f"body は既定で {MCP_DOC_MAX_CHARS} 字に切る(全文が要るなら max_chars を上げる)。"
            "fields は title,opening,body,tags,links,updated_at,extra から選ぶ。"
            "同名の別地物があるときは alternatives が付くので、area / feature / tag で絞る。"
        )
    )
    async def doc(
        source: str,
        title: str,
        fields: str | None = None,
        max_chars: int = MCP_DOC_MAX_CHARS,
        area: str | None = None,
        feature: str | None = None,
        bbox: str | None = None,
        tag: str | None = None,
    ) -> dict:
        return await run_in_threadpool(
            _call, api.get_doc_by_title,
            request=_request(app), source=source, title=title, fields=fields,
            max_chars=max_chars, area=area, feature=feature, bbox=bbox, tag=tag,
        )

    @mcp.tool(
        description=(
            "属性での一括抽出(全文検索ではなく等価・範囲条件の AND)。"
            "「カテゴリ○○の記事を全件」は tag、「京都府の寺社を全件」は feature+area、"
            "wikidata の Q 番号からの逆引きは wikidata を使う。"
            "feature と tag はカンマ区切りで複数指定できる(その中は OR)。"
            "総件数 total が返るので offset でページングする。"
        )
    )
    async def filter(
        source: str,
        feature: str | None = None,
        area: str | None = None,
        bbox: str | None = None,
        wikidata: str | None = None,
        tag: str | None = None,
        fields: str | None = None,
        limit: int = 50,
        offset: int = 0,
        max_chars: int = MCP_DOC_MAX_CHARS,
    ) -> dict:
        return await run_in_threadpool(
            _call, api.filter_docs,
            request=_request(app), source=source, feature=feature, area=area, bbox=bbox,
            wikidata=wikidata, tag=tag, fields=fields, limit=limit, offset=offset,
            max_chars=max_chars,
        )

    @mcp.tool(
        description=(
            "タグ名(Wikipedia のカテゴリ等)を文書数つきで列挙する。"
            "filter の tag は完全一致なので、名前が不確かなときは先にこれで実在する名前を確かめる。"
            "prefix は前方一致(速い)、contains は部分一致(索引が効かず遅い)。"
        )
    )
    async def tags(
        source: str,
        prefix: str | None = None,
        contains: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        return await run_in_threadpool(
            _call, api.list_tags,
            request=_request(app), source=source, prefix=prefix, contains=contains,
            limit=limit, offset=offset,
        )

    @mcp.tool(description="タイトルの前方一致候補を返す。表記揺れの確認や短い語の引き当てに使う。")
    async def titles(source: str, prefix: str, limit: int = 20) -> dict:
        return await run_in_threadpool(
            _call, api.titles,
            request=_request(app), source=source, prefix=prefix, limit=limit,
        )

    @mcp.tool(description=(
        "その文書から出ているリンク(関連文書のタイトル)の一覧を返す。"
        "出リンクのみで、被リンク(この文書を指している文書)は取れない。"
        "本文の出現順そのままなので重複があり、`記事名#節名` の形も混じる。"
        "doc に渡す前に重複を落として `#` の前で切ること。"
    ))
    async def links(source: str, title: str) -> dict:
        return await run_in_threadpool(
            _call, api.links,
            request=_request(app), source=source, title=title, direction="out",
        )

    # notes(短期記憶)は無効なこともあるので、有効なときだけ道具を出す。
    # ツール定義は常時コンテキストに載るため、使えないものを並べない。
    if notes.is_enabled():
        _register_memory_tools(mcp, app)

    return mcp


def _register_memory_tools(mcp: FastMCP, app: FastAPI) -> None:
    """覚える・思い出すの 2 つ。

    この 2 つがあることの意味は「常駐するのはこの定義(数百字)だけで、覚えた中身は
    引いたときにしかコンテキストに載らない」こと。CLAUDE.md や記憶ファイルは毎回
    全部載るので、件数が増えるほど関係ない話にもトークンを払うことになる。
    """
    from app import main as api

    @mcp.tool(description=(
        "ユーザーが「覚えておいて」と言ったこと、後から参照する価値のある調査結果や"
        "決定事項を chiezo に保存する。保存先はローカルで、外部には出ない。"
        "tags はカンマ区切りで、後から絞り込むのに使える。"
    ))
    async def remember(text: str, title: str | None = None, tags: str | None = None) -> dict:
        return await run_in_threadpool(
            _call, api.remember,
            request=_request(app), text=text, title=title, tags=tags,
        )

    @mcp.tool(description=(
        "以前 remember で保存したことを思い出す。**新しい順**に返る。"
        "「さっき話したあの件」「先月お願いしたあれ」のように過去のやり取りを"
        "指されたら、まずこれを引くこと。"
        "q は全文検索(trigram なので 3 文字以上の語が要る)。"
        "**曖昧な指され方をしたときは q を省いて since だけで引くほうが当たる**"
        "(「あの件」は語が一致しないため)。since/until は 2026-07-31 の形でよい。"
    ))
    async def recall(
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        tag: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        return await run_in_threadpool(
            _call, api.recall_notes,
            request=_request(app), q=q, since=since, until=until, tag=tag,
            limit=limit, offset=offset,
        )
