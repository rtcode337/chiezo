"""「答える」層 — ためた知識で回答まで返す(既定では無効)。

設計の要点:

- **推論は同居させない**。この層がするのは OpenAI 互換の `/chat/completions` を叩くことだけで、
  モデルは別コンテナ(compose の profile `answer` の `chiezo-llm`)か LAN 上の別マシンにいる。
  配信側 chiezo-api が数百 MB で動く前提を壊さないため、ここにモデルを抱えない。
- **`CHIEZO_LLM_URL` が機能フラグを兼ねる**。未設定なら答える層は丸ごと無効
  (`/v1/ask` は 503、管理画面にも無効と出る)。既定では起動しないことをこの 1 変数で守る。
- **検索は `app/main.py` のエンドポイント関数をそのまま呼ぶ**(`app/mcp_server.py` と同じ方針)。
  取り出し方を二重に持つと、片方だけ直されて必ずずれる。
- **2 段の RAG**(クエリ生成 → 取得 → 回答)。質問文をそのまま FTS に入れても当たらないため
  (`app/fts.py` は空白区切りの各語をフレーズにして AND 結合するので、
  「浅草寺はどこにある?」は 1 個の長いフレーズになり何にもマッチしない)、
  検索語は LLM に組み立てさせる。ツール呼び出しループにしないのは、小型のローカルモデルでは
  ツール呼び出しが不安定で、暴走・長時間化しやすいから。何をどう引くかは chiezo 側が決め打つ。
- **クエリ生成が壊れても回答まで到達させる**。小型モデルの JSON 出力は当てにならないので、
  厳密なパース → `"q"` の拾い出し → 質問文そのまま、の順に諦めながら落ちる
  (最後の段は当たりが悪い劣化経路だが、黙って 500 を返すよりはよい)。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

log = logging.getLogger("chiezo.api")

# 検索 1 本あたり見る上位件数(この中から本文を取る文書を選ぶ)
SEARCH_LIMIT = 5
# クエリ生成に許す検索クエリの本数
MAX_QUERIES = 3
# opening がこれより短ければ body も足す(定義文 1 行だけでは答えに足りないため)
MIN_OPENING_CHARS = 200

# ソース種別ごとの 1 行説明(クエリ生成のプロンプトに載せる)
KIND_HINTS = {
    "wikipedia": "一般知識・人物・作品・出来事・用語の解説",
    "osm": "地名・行政区・施設・店舗・駅などの地物と座標",
    "geonames": "全世界の地名(座標・人口・多言語別名)",
}


@dataclass
class Settings:
    url: str
    model: str
    api_key: str | None
    timeout: float
    docs: int
    max_chars: int
    # agent モード(app/agent.py)の上限。rag では使わないが、推論サーバの設定と
    # 一緒に読めたほうが分かりやすいのでここに置く。
    agent_max_steps: int
    agent_tool_chars: int
    agent_timeout: float
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"{self.url}/chat/completions"


def _normalize_base_url(raw: str) -> str:
    """`http://host:8080` でも `http://host:8080/v1` でも受け取れるようにする。"""
    base = raw.strip().rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def _env_num(name: str, default, cast):
    """数値の環境変数を読む。空文字・壊れた値は既定値に倒す。

    compose は未設定の変数を `VAR=` として渡す(空文字が入る)ので、
    素直に float() すると「.env に書いていない」だけで 500 になる。
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        log.warning("ignoring invalid %s=%r; using %r", name, raw, default)
        return default


def load_settings() -> Settings | None:
    """環境変数から設定を読む。`CHIEZO_LLM_URL` が無ければ None(= 答える層は無効)。

    import 時ではなく呼び出しごとに読むのは、環境変数だけで有効・無効を切り替えられる
    ようにするため(テストも monkeypatch で切り替える)。
    """
    raw = os.environ.get("CHIEZO_LLM_URL", "").strip()
    if not raw:
        return None
    api_key = os.environ.get("CHIEZO_LLM_API_KEY", "").strip() or None
    return Settings(
        url=_normalize_base_url(raw),
        # llama-server は 1 プロセス 1 モデルなので名前は何でも通る。
        # Ollama など複数モデルを持つ相手に向けるときだけ実在名が要る。
        model=os.environ.get("CHIEZO_LLM_MODEL", "chiezo").strip() or "chiezo",
        api_key=api_key,
        # DB の 5 秒とは別枠。CPU 推論は数十秒級になる。
        timeout=_env_num("CHIEZO_ANSWER_TIMEOUT", 120.0, float),
        docs=max(1, _env_num("CHIEZO_ANSWER_DOCS", 4, int)),
        max_chars=max(1, _env_num("CHIEZO_ANSWER_MAX_CHARS", 6000, int)),
        # agent モードの 3 つの上限。どれもモデルの窓と待ち時間を有限に保つためのもので、
        # 意味は app/agent.py 冒頭の説明が正。
        agent_max_steps=max(1, _env_num("CHIEZO_AGENT_MAX_STEPS", 6, int)),
        agent_tool_chars=max(200, _env_num("CHIEZO_AGENT_TOOL_CHARS", 3000, int)),
        agent_timeout=_env_num("CHIEZO_AGENT_TIMEOUT", 180.0, float),
    )


def is_enabled() -> bool:
    return bool(os.environ.get("CHIEZO_LLM_URL", "").strip())


def require_settings() -> Settings:
    cfg = load_settings()
    if cfg is None:
        raise HTTPException(
            503,
            {
                "error": "answering is disabled",
                "hint": "推論サーバの OpenAI 互換 URL を CHIEZO_LLM_URL に設定すると有効になる"
                        "(compose なら `docker compose --profile answer up -d`)",
            },
        )
    return cfg


def _llm_client(cfg: Settings) -> httpx.AsyncClient:
    """推論サーバ向けの HTTP クライアント。

    テストはここを差し替えて `httpx.MockTransport` を挿す(偽の OpenAI 互換サーバを
    立てずに、クエリ生成 → 取得 → 回答の全経路を通せるようにするため)。
    """
    headers = {"Content-Type": "application/json", **cfg.extra_headers}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    return httpx.AsyncClient(timeout=cfg.timeout, headers=headers)


# ---- 推論サーバとのやり取り -------------------------------------------------


def _payload(cfg: Settings, messages: list[dict], *, stream: bool, **extra) -> dict:
    return {
        "model": cfg.model,
        "messages": messages,
        "stream": stream,
        **extra,
    }


def _upstream_error(exc: Exception) -> HTTPException:
    """推論サーバ側の失敗を、chiezo のエラー形式に翻訳する。"""
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, {"error": f"llm timeout: {exc}"})
    return HTTPException(502, {"error": f"llm unreachable: {exc}"})


async def complete_message(cfg: Settings, messages: list[dict], **extra) -> dict:
    """1 回の応答を**メッセージまるごと**取る。

    `_complete` が本文だけを返すのに対し、こちらは `tool_calls` を含む assistant
    メッセージをそのまま返す(agent モードは次のターンにこれを丸ごと積み直す必要がある)。
    """
    try:
        async with _llm_client(cfg) as client:
            res = await client.post(
                cfg.endpoint, json=_payload(cfg, messages, stream=False, **extra)
            )
    except httpx.HTTPError as e:
        raise _upstream_error(e) from None
    if res.status_code >= 400:
        raise HTTPException(
            502, {"error": f"llm error {res.status_code}", "detail": res.text[:500]}
        )
    try:
        message = res.json()["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise HTTPException(502, {"error": f"unexpected llm response: {e}"}) from None
    if not isinstance(message, dict):
        raise HTTPException(502, {"error": "unexpected llm response: message is not an object"})
    return message


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_ORPHAN_THINK_END = re.compile(r"^\s*</think>")


def content_of(message: dict) -> str:
    """assistant メッセージの本文を取り出し、思考タグの残骸を落とす。

    思考(reasoning)を出すモデルでは、推論サーバの設定次第で思考の中身や閉じタグだけが
    `content` に残る。実測: Qwen3 に `--reasoning-budget 0`(思考させない)を掛けると、
    本文の先頭に `</think>` だけが付いてきた。設定は chiezo が握っていない
    (LAN 上の別サーバかもしれない)ので、受け側で落とす。
    """
    text = _THINK_BLOCK.sub("", message.get("content") or "")
    return _ORPHAN_THINK_END.sub("", text).strip()


async def _complete(cfg: Settings, messages: list[dict], **extra) -> str:
    """1 回の応答の本文だけを取る(クエリ生成・回答用)。"""
    return content_of(await complete_message(cfg, messages, **extra))


async def _stream(cfg: Settings, messages: list[dict], **extra) -> AsyncIterator[str]:
    """OpenAI 互換の SSE を読んで、本文の差分だけを順に返す。"""
    try:
        async with _llm_client(cfg) as client:
            async with client.stream(
                "POST", cfg.endpoint, json=_payload(cfg, messages, stream=True, **extra)
            ) as res:
                if res.status_code >= 400:
                    body = (await res.aread()).decode("utf-8", "replace")
                    raise HTTPException(
                        502, {"error": f"llm error {res.status_code}", "detail": body[:500]}
                    )
                async for line in res.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"].get("content") or ""
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue  # 使い物にならないフレームは黙って捨てる
                    if delta:
                        yield delta
    except httpx.HTTPError as e:
        raise _upstream_error(e) from None


# ---- 段 1: クエリ生成 -------------------------------------------------------


PLAN_SYSTEM = """\
あなたは全文検索のクエリを組み立てる補助システムです。ユーザーの質問に答えるために、
ローカル知識ベース「chiezo」を検索するクエリを組み立ててください。

規則:
- 出力は次の形の JSON だけ。説明文やコードフェンスは書かない。
  {"queries": [{"source": "<ソース名>", "q": "<検索語>"}]}
- 検索語は質問文をそのまま入れない。名詞・固有名詞を 1〜3 語、空白区切りで書く
  (全文検索は空白区切りの各語の AND なので、語を増やすほど当たらなくなる)
- 3 文字以上の語を使う(2 文字以下は索引で引けない)
- クエリは最大 %d 件。関係のないソースは含めない
""" % MAX_QUERIES


def source_catalog(request: Request) -> list[dict]:
    sources = request.app.state.sources
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "lang": s.lang,
            "docs": s.doc_count,
            "hint": KIND_HINTS.get(s.kind, ""),
        }
        for s in sorted(sources.values(), key=lambda s: s.name)
    ]


def _plan_user_prompt(question: str, catalog: list[dict]) -> str:
    lines = []
    for c in catalog:
        lang = f" / {c['lang']}" if c["lang"] else ""
        hint = f" — {c['hint']}" if c["hint"] else ""
        lines.append(f"- {c['name']}({c['kind']}{lang} / {c['docs']:,} 件){hint}")
    return "利用できるソース:\n" + "\n".join(lines) + f"\n\n質問: {question}"


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_Q_FIELD = re.compile(r'"q"\s*:\s*"([^"]+)"')
_SOURCE_FIELD = re.compile(r'"source"\s*:\s*"([^"]+)"')


def parse_plan(raw: str, known: list[str]) -> list[dict]:
    """クエリ生成の応答から検索クエリを取り出す。取れなければ空リスト。

    小型モデルはコードフェンスで包んだり途中で切れたりするので、
    ①素直な JSON ②`{...}` の抜き出し ③`"q"`/`"source"` の拾い出し、と諦めながら落ちる。
    """
    text = raw.strip()
    for candidate in (text, (m.group(0) if (m := _JSON_BLOCK.search(text)) else "")):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        queries = data.get("queries") if isinstance(data, dict) else None
        if isinstance(queries, list):
            return _validate_queries(
                [
                    {"source": str(q.get("source", "")), "q": str(q.get("q", ""))}
                    for q in queries
                    if isinstance(q, dict)
                ],
                known,
            )
    # JSON として壊れていても、欲しい 2 つのフィールドだけなら拾えることが多い
    qs = _Q_FIELD.findall(text)
    srcs = _SOURCE_FIELD.findall(text)
    if qs:
        return _validate_queries(
            [
                {"source": srcs[i] if i < len(srcs) else (known[0] if known else ""), "q": q}
                for i, q in enumerate(qs)
            ],
            known,
        )
    return []


def _validate_queries(queries: list[dict], known: list[str]) -> list[dict]:
    """存在しないソース・空クエリを落とし、重複を潰して MAX_QUERIES 件までに切る。"""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for spec in queries:
        source, q = spec["source"].strip(), spec["q"].strip()
        if source not in known or not q:
            continue
        if (source, q) in seen:
            continue
        seen.add((source, q))
        out.append({"source": source, "q": q})
        if len(out) >= MAX_QUERIES:
            break
    return out


def fallback_queries(question: str, known: list[str]) -> list[dict]:
    """クエリ生成が失敗したときの劣化経路: 質問文をそのまま各ソースへ投げる。

    日本語は空白で切れないので当たりは悪い(「浅草寺はどこにある?」は 1 フレーズになる)。
    それでも黙って 0 件を返すよりは、当たれば答えられるぶんましという判断。
    句読点と記号だけは落として、いちばん長い断片を検索語にする。
    """
    parts = [p for p in re.split(r"[\s、。,.!?！?・「」『』()()]+", question) if p]
    term = max(parts, key=len) if parts else question.strip()
    return [{"source": s, "q": term} for s in known[:MAX_QUERIES]]


async def plan_queries(
    cfg: Settings, request: Request, question: str, source: str | None
) -> list[dict]:
    """質問から検索クエリを組み立てる。

    `source` を指定されたときも**この段は省かない**。ソースを固定してもクエリ生成の
    仕事(質問文 → 検索語)は残るからで、ここを飛ばすと「浅草寺はどこにある?」が
    そのまま FTS に入って 0 件になる。指定は選べるソースを 1 つに絞るだけに使う。
    """
    known = [c["name"] for c in source_catalog(request)]
    if not known:
        raise HTTPException(503, {"error": "no sources registered"})
    if source is not None:
        if source not in known:
            raise HTTPException(
                404, {"error": f"unknown source: {source}", "sources": known}
            )
        known = [source]
    catalog = [c for c in source_catalog(request) if c["name"] in known]
    raw = await _complete(
        cfg,
        [
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": _plan_user_prompt(question, catalog)},
        ],
        temperature=0.0,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    queries = parse_plan(raw, known)
    if not queries:
        log.info("query planning produced nothing usable; falling back to the raw question")
        return fallback_queries(question, known)
    return queries


# ---- 段 2: 取得(LLM を使わない) -------------------------------------------


def gather_context(
    request: Request, queries: list[dict], cfg: Settings
) -> tuple[list[dict], list[dict]]:
    """検索クエリを実行し、(抜粋, 出典) を返す。同期(呼び出し側がスレッドへ逃がす)。

    検索・文書取得は `app/main.py` のエンドポイント関数をそのまま呼ぶ。FastAPI の
    エンドポイントは既定値が `Query(...)` オブジェクトなので、**全パラメータを明示的に渡す**
    (省略すると Query インスタンスが値として入り、例外にならず静かに壊れる)。
    """
    from app import main as api

    # 1 本のクエリが枠を独占しないよう、ヒットは検索ごとに束ねてから順繰りに取る
    per_query: list[list] = []
    for spec in queries:
        try:
            res = api.search(
                request=request, source=spec["source"], q=spec["q"],
                area=None, feature=None, bbox=None, tag=None,
                limit=SEARCH_LIMIT, offset=0,
            )
        except HTTPException as e:
            log.info("search failed during ask (%s): %s", spec, e.detail)
            continue
        per_query.append([(spec["source"], hit) for hit in res["results"]])

    picked: list[tuple[str, dict]] = []
    seen: set[tuple[str, int]] = set()
    for rank in range(SEARCH_LIMIT):
        for hits in per_query:
            if rank >= len(hits):
                continue
            source, hit = hits[rank]
            key = (source, hit["doc_id"])
            if key in seen:
                continue
            seen.add(key)
            picked.append((source, hit))
            if len(picked) >= cfg.docs:
                break
        if len(picked) >= cfg.docs:
            break

    budget = cfg.max_chars
    per_doc = max(1, cfg.max_chars // max(1, cfg.docs))
    snippets: list[dict] = []
    references: list[dict] = []
    for source, hit in picked:
        if budget <= 0:
            break
        try:
            doc = api.get_doc_by_id(
                request=request, source=source, doc_id=hit["doc_id"],
                fields="title,opening,body", max_chars=0,
            )
        except HTTPException:
            continue
        opening = (doc.get("opening") or "").strip()
        body = (doc.get("body") or "").strip()
        text = opening
        if len(text) < MIN_OPENING_CHARS and body:
            text = f"{opening}\n{body}".strip()
        text = text[: min(per_doc, budget)].strip()
        if not text:
            continue
        budget -= len(text)
        n = len(references) + 1
        snippets.append({"n": n, "source": source, "title": doc["title"], "text": text})
        references.append({
            "n": n,
            "source": source,
            "title": doc["title"],
            "doc_id": hit["doc_id"],
            "url": f"/{source}/doc/{hit['doc_id']}",
        })
    return snippets, references


# ---- 段 3: 回答 -------------------------------------------------------------


# 回答方針は 2 つあり、`grounded` で切り替える。これは chiezo の設計思想ではなく
# **モデルの幻覚への対処**なので、固定の制約にはしない(chiezo は AI 用の知識ベースで、
# ローカル LLM はそれを使う側。持っている知識を封じるのが目的ではない)。
ANSWER_SYSTEM_GROUNDED = """\
あなたはローカル知識ベース「chiezo」の回答係です。渡された抜粋を根拠に、日本語で簡潔に答えてください。

規則:
- 抜粋に書かれていないことは答えない。根拠が無ければ「抜粋からは分かりません」と言う
- 事実を述べた文には、根拠にした抜粋の番号を [1] の形で付ける
- 抜粋の丸写しではなく、質問に答える形にまとめる
"""

ANSWER_SYSTEM_OPEN = """\
あなたはローカル知識ベース「chiezo」を引ける回答係です。chiezo から取ってきた抜粋を踏まえ、
日本語で簡潔に答えてください。

規則:
- 抜粋に書かれていることは、根拠にした番号を [1] の形で付ける
- 抜粋で足りない部分は自分の知識で補ってよい。ただしその部分には番号を付けない
- 抜粋と自分の知識が食い違うときは抜粋を優先し、食い違い自体も述べる
"""

# grounded=1 なのに抜粋が 1 件も取れなかったときの答え。ここで LLM を呼ばないのは、
# 実測で小型モデル(gemma-3-1b)が「抜粋が空でも自分の知識で答えてしまう」ことを
# 確かめたため。守れない約束をプロンプトだけに委ねず、経路として断つ。
NO_CONTEXT_ANSWER = (
    "抜粋からは分かりません(chiezo で該当する文書が見つかりませんでした)。"
    "検索語を変えるか、source を指定するか、grounded=0 で聞き直してください。"
)


def build_answer_messages(
    question: str, snippets: list[dict], grounded: bool = True
) -> list[dict]:
    if snippets:
        blocks = "\n\n".join(
            f"[{s['n']}] {s['source']} / {s['title']}\n{s['text']}" for s in snippets
        )
    else:
        # grounded=0 でここに来る(grounded=1 は has_no_basis で手前で止まる)。
        # 念を押さないと小型モデルは根拠が無くても [1] を書く(実測: gemma-3-1b)。
        # 応答の references が空なら本文中の番号は無意味、というのが呼び出し側との契約。
        blocks = (
            "(該当する文書が見つかりませんでした。"
            "根拠が 1 件も無いので、[1] のような出典番号は絶対に付けないこと)"
        )
    return [
        {"role": "system", "content": ANSWER_SYSTEM_GROUNDED if grounded else ANSWER_SYSTEM_OPEN},
        {"role": "user", "content": f"# 抜粋\n{blocks}\n\n# 質問\n{question}"},
    ]


def has_no_basis(snippets: list[dict], grounded: bool) -> bool:
    """grounded なのに根拠が 1 件も無い状態(= LLM を呼ぶ意味がない)。"""
    return grounded and not snippets


# ---- 呼び出し口 -------------------------------------------------------------


async def prepare(
    cfg: Settings, request: Request, question: str, source: str | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """クエリ生成 → 取得 まで進め、(クエリ, 抜粋, 出典) を返す。"""
    queries = await plan_queries(cfg, request, question, source)
    snippets, references = await run_in_threadpool(gather_context, request, queries, cfg)
    return queries, snippets, references


async def answer(
    cfg: Settings,
    request: Request,
    question: str,
    source: str | None,
    grounded: bool = True,
) -> dict:
    """まとめて 1 つの JSON を返す(非ストリーミング)。"""
    queries, snippets, references = await prepare(cfg, request, question, source)
    if has_no_basis(snippets, grounded):
        text = NO_CONTEXT_ANSWER
    else:
        text = await _complete(
            cfg, build_answer_messages(question, snippets, grounded), temperature=0.2
        )
    return {
        "question": question,
        "answer": text.strip(),
        "references": references,
        "queries": queries,
        "grounded": grounded,
        "model": cfg.model,
    }


async def stream_answer(
    cfg: Settings, question: str, snippets: list[dict], grounded: bool = True
) -> AsyncIterator[str]:
    """回答本文を差分で返す(`/v1/ask?stream=1` の本体)。

    取得(`prepare`)は呼び出し側が**先に**済ませること。ストリーミング応答は
    ヘッダを送った後で失敗してもステータスコードを変えられないので、
    クエリ生成・検索の失敗は流し始める前に HTTP のエラーとして返す必要がある。
    """
    if has_no_basis(snippets, grounded):
        yield NO_CONTEXT_ANSWER
        return
    async for delta in _stream(
        cfg, build_answer_messages(question, snippets, grounded), temperature=0.2
    ):
        yield delta
