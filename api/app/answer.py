"""「使う」層 — ためた知識で回答まで返す(既定では無効)。

設計の要点:

- 推論は同居させない。この層がするのは OpenAI 互換の `/chat/completions` を叩くことだけで、
  モデルは別コンテナ(compose の profile `answer` の `chiezo-llm`)か LAN 上の別マシンにいる。
  配信側 chiezo-api が数百 MB で動く前提を壊さないため、ここにモデルを抱えない。
- `CHIEZO_LLM_URL` が機能フラグを兼ねる。未設定なら使う層は丸ごと無効
  (`/v1/ask` は 503、管理画面にも無効と出る)。既定では起動しないことをこの 1 変数で守る。
- 話す相手は複数持てる(バックエンド)。`CHIEZO_LLM_URL` が名前なしの既定で、
  `CHIEZO_LLM_<名前>_URL` を足すと選べる相手が増える。要求するのは OpenAI 互換の
  `/chat/completions` だけなので、ローカルの推論サーバでも Gemini・OpenRouter でも、
  CLI を OpenAI 互換に見せるブリッジでも、同じ 1 本の口で扱える。
- 検索は `app/main.py` のエンドポイント関数をそのまま呼ぶ(`app/mcp_server.py` と同じ方針)。
  取り出し方を二重に持つと、片方だけ直されて必ずずれる。
- 2 段の RAG(クエリ生成 → 取得 → 回答)。質問文をそのまま FTS に入れても当たらないため
  (`app/fts.py` は空白区切りの各語をフレーズにして AND 結合するので、
  「浅草寺はどこにある?」は 1 個の長いフレーズになり何にもマッチしない)、
  検索語は LLM に組み立てさせる。ツール呼び出しループにしないのは、小型のローカルモデルでは
  ツール呼び出しが不安定で、暴走・長時間化しやすいから。何をどう引くかは Chiezo 側が決め打つ。
- クエリ生成が壊れても回答まで到達させる。小型モデルの JSON 出力は当てにならないので、
  厳密なパース → `"q"` の拾い出し → 質問文そのまま、の順に諦めながら落ちる
  (最後の段は当たりが悪い劣化経路だが、黙って 500 を返すよりはよい)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app import ai_log, providers, settings_store, usage_store
from app.pages import doc_url

log = logging.getLogger("chiezo.api")

# 検索 1 本あたり見る上位件数(この中から本文を取る文書を選ぶ)
SEARCH_LIMIT = 5
# クエリ生成に許す検索クエリの本数
MAX_QUERIES = 3
# opening がこれより短ければ body も足す(定義文 1 行だけでは答えに足りないため)
MIN_OPENING_CHARS = 200
# 会話(/v1/chat)でモデルに見せる直前のやり取りの数。長くするほど「さっきの話」に
# 追従できるが、毎回のプロンプトが伸びる(rag はこれをクエリ生成にも使う)。
HISTORY_TURNS = 6

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
    # どの相手の設定か（`app/providers.py` の ID）。画面の表示に使う。
    name: str = ""
    # 考える量（`reasoning_effort`）。空なら送らない = 相手の既定に任せる。
    effort: str = ""
    # モデルを控え（`app/providers.py` の決め打ち）から当てたか。 控えは相手の都合で
    # 古くなる（実測: 保存も選択もしていない Gemini が 404 になった＝その名前のモデルが
    # 消えていた）ので、当てた場合は後から相手に聞いて選び直す（`ensure_model`）。
    model_is_fallback: bool = False

    @property
    def endpoint(self) -> str:
        return f"{self.url}/chat/completions"


def _normalize_base_url(raw: str) -> str:
    """`http://host:8080` でも `http://host:8080/v1` でも受け取れるようにする。

    補うのはパスを持たない相手にだけ。llama-server や Ollama は `http://host:11434`
    のようにホストだけを書きがちなので `/v1` を足すが、既にパスがある相手に足すと壊れる ——
    Gemini の OpenAI 互換の口は `https://generativelanguage.googleapis.com/v1beta/openai`
    で、その下が直接 `chat/completions` である(`/v1` を挟む場所は無い)。
    末尾が `/v1` かどうかだけを見ていると、この形に `/v1` を足して 404 にしてしまう。
    """
    base = raw.strip().rstrip("/")
    parsed = urlsplit(base)
    # パス(ホスト以降)を持っていれば、書かれたとおりに使う。
    if parsed.path.strip("/"):
        return base
    return f"{base}/v1"


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


def normalize_model_id(raw: str) -> str:
    """`models/gemini-3.7-flash` → `gemini-3.7-flash`。

    Gemini の `/models` だけが `models/` を付けて返すが、同じ相手の
    `chat/completions` はその形を受け付けず 404 になる(実測)。画面の選択肢は
    一覧から作るので、剥がさないと選んだ瞬間に必ず失敗する。

    剥がすのは先頭の `models/` だけ。OpenRouter の `qwen/qwen3-coder:free` の
    ようなスラッシュを含む ID は触らない。
    """
    return (raw or "").strip().removeprefix("models/")


def normalize_backend(name: str | None) -> str:
    """クエリ等で受け取った相手の名前を内部表記に寄せる。空なら「先頭の相手」。"""
    token = (name or "").strip().lower()
    if token:
        return token
    names = backend_names()
    return names[0] if names else ""


def backend_names() -> list[str]:
    """いま話せる相手（管理画面で有効にしてあるもの）を、画面の並び順で返す。

    同居の推論サーバも外部のサービスも CLI ブリッジも同じ扱い。 特別扱いする相手は無い。
    コンテナが立っているかどうかまではここでは見ない（HTTP を叩くので遅い）。
    そちらは `app/views/ai_settings.py` が管理画面を描くときに確かめる。
    """
    # 元栓が閉じていれば、相手が何台有効でも話せない。
    if not settings_store.answer_enabled():
        return []
    stored = settings_store.load_all()
    return [
        spec.id
        for spec in providers.all_providers()
        if stored.get(spec.id, settings_store.ProviderSetting(spec.id)).enabled
    ]


def backend_label(backend: str) -> str:
    """画面に出す相手の名前。見出しのモデル名とは別で、どの設定かを指す。"""
    return providers.label_of(backend)


def normalize_effort(backend: str, effort: str | None) -> str:
    """選ばれたエフォートを検証する。知らない値は空（＝相手の既定）にする。

    相手が検証してくれない。 claude は `--effort bogus` を黙って受け取り、
    エラーも警告も出さずに既定で動く（実測）—— 打ち間違いに気づけないので、
    ここで一覧に無いものを落とす。
    """
    name = (effort or "").strip().lower()
    return name if name in providers.efforts_of(backend) else ""


def load_settings(
    backend: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    *,
    require_enabled: bool = True,
) -> Settings | None:
    """相手の設定を組み立てる。使えない相手なら None。

    URL と表示名は `app/providers.py` の決め打ち、on/off と API キーとモデルは
    管理画面の設定（`app/settings_store.py`）から。

    `model` を渡すと、保存してある既定より優先する（会話のたびに選び直せるようにするため）。

    タイムアウトや抜粋の量（`CHIEZO_ANSWER_*`）は相手で分けない —— 相手が変わっても
    「どれだけ根拠を積むか」は Chiezo 側の都合だから。
    """
    if not settings_store.answer_enabled():
        return None
    name = normalize_backend(backend)
    spec = providers.get(name)
    if spec is None:
        return None
    stored = settings_store.load(name)
    # 「接続を試す」だけは無効の相手にも組み立てる。 試さないと on にできない仕様なので、
    # ここで無効を弾くと「試せないから on にできない」の堂々巡りになる（実際に踏んだ）。
    if require_enabled and not stored.enabled:
        return None
    # 認証情報の要る相手で未登録なら使えない（管理画面が on にさせないが、設定を直に
    # 書き換えられた場合の保険でもある）。
    if spec.credential == providers.CRED_REQUIRED and not stored.has_credential:
        return None
    chosen = normalize_model_id(model or stored.model or "")
    # モデルを選ばなかったとき。 指定が要る相手には控えの先頭を当てる（Gemini に
    # モデル無しで投げても通らない）が、自分で決められる相手（CLI ブリッジ・1 プロセス
    # 1 モデルの推論サーバ）には何も渡さない —— 画面の「既定」がそれを選べる。
    from_fallback = False
    if not chosen and spec.model_required and spec.models:
        chosen = spec.models[0]
        from_fallback = True
    return Settings(
        name=name,
        model_is_fallback=from_fallback,
        effort=normalize_effort(name, effort),
        url=_normalize_base_url(providers.url_of(spec)),
        # 空でも通る相手（1 プロセス 1 モデルの推論サーバ・CLI ブリッジ）がいるので、
        # 決まらないときは無難な既定を置く。
        model=chosen or "chiezo",
        api_key=stored.credential or None,
        # DB の 5 秒とは別枠。CPU 推論は数十秒級になる。
        #
        # CLI ブリッジの相手だけ桁を変える。 あちらは道具を何度も引くので分単位に
        # なりうるうえ、ブリッジ自身が上限(`CHIEZO_BRIDGE_TIMEOUT`。既定 300 秒、
        # compose では 600 秒)を持っている。待つ側が先に切れてはいけない ——
        # 切れると画面には ReadTimeout しか出ず、「向こうが何秒で諦めたか」も
        # 「そもそも何が起きたか」も分からなくなる(実測: claude を effort=high で
        # 呼んだら 120 秒で切れ、504 llm timeout しか残らなかった)。
        # ブリッジ側の上限を 900 秒より伸ばすときは、こちらも一緒に伸ばすこと。
        timeout=_env_num("CHIEZO_ANSWER_TIMEOUT", _default_timeout(spec), float),
        docs=max(1, _env_num("CHIEZO_ANSWER_DOCS", 4, int)),
        max_chars=max(1, _env_num("CHIEZO_ANSWER_MAX_CHARS", 6000, int)),
        # agent モードの 3 つの上限。意味は app/agent.py 冒頭の説明が正。
        agent_max_steps=max(1, _env_num("CHIEZO_AGENT_MAX_STEPS", 6, int)),
        agent_tool_chars=max(200, _env_num("CHIEZO_AGENT_TOOL_CHARS", 3000, int)),
        agent_timeout=_env_num("CHIEZO_AGENT_TIMEOUT", 180.0, float),
    )


# CLI ブリッジ経由の相手を待つ秒数の既定。ブリッジ自身の上限(既定 300 / compose 600)より
# 長く取る —— 待つ側が先に切れると、向こうの判断が一切見えなくなるため。
BRIDGE_TIMEOUT_SECONDS = 900.0
# API で直に叩く相手・推論サーバの既定。1 往復なのでこの桁で足りる。
DIRECT_TIMEOUT_SECONDS = 120.0


def _default_timeout(spec) -> float:
    """その相手を待つ既定の秒数。CLI ブリッジだけ桁が違う。"""
    if spec is not None and getattr(spec, "bridge", False):
        return BRIDGE_TIMEOUT_SECONDS
    return DIRECT_TIMEOUT_SECONDS


def is_enabled() -> bool:
    """使う層が有効か（話せる相手が 1 つでもあれば有効）。"""
    return bool(backend_names())


# 画面に出すモデル名の見当。推論サーバに毎回聞かずに済むよう覚えておく
# (相手が落ちているときにページの表示まで待たされないようにするため)。
_MODEL_LABEL_CACHE: dict[str, tuple[float, str | None]] = {}
MODEL_LABEL_TTL = 300.0


def short_model_name(model_id: str) -> str:
    """`Qwen/Qwen3-8B-GGUF:Q4_K_M` → `Qwen3-8B`(見出しに出す用)。

    配布元・GGUF・量子化の別は、話している相手を名乗るのには要らない。
    """
    name = model_id.split("/")[-1].split(":")[0].strip()
    for suffix in ("-GGUF", "-gguf"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name or model_id


async def model_label(cfg: Settings) -> str | None:
    """いま話している相手(モデル)の名前。分からなければ None。

    Chiezo は知識ベースで、話す相手は AI という関係を画面に出すために要る。
    設定でモデルが決まっていればそれを、決まっていなければ相手の `/models` に聞く
    (llama-server は 1 プロセス 1 モデルなので、選ばずに使う運用のほうが普通)。
    相手が落ちていても画面は出したいので、失敗は None として覚えて先へ進む。

    一覧が 2 つ以上あるときは名乗らない。 それは「選べるもの」の並びであって、
    いま使われているものではない —— CLI ブリッジは受け付けるエイリアスを全部返すので、
    先頭を採ると `sonnet` のように選んでもいないモデル名が画面に出る。
    呼び出し側が相手の名前（`Claude Code`）に落とせるよう、ここは None を返す。
    """
    # 設定で決まっているモデルがあればそれを名乗る（相手に聞くのは決まっていないときだけ）。
    explicit = cfg.model if cfg.model and cfg.model != "chiezo" else ""
    if explicit:
        return short_model_name(explicit)
    now = time.monotonic()
    cached = _MODEL_LABEL_CACHE.get(cfg.url)
    if cached and now - cached[0] < MODEL_LABEL_TTL:
        return cached[1]
    label = None
    try:
        async with _llm_client(cfg) as client:
            res = await client.get(f"{cfg.url}/models", timeout=3.0)
        entries = res.json().get("data") or []
        # 1 つだけなら「それしか無い」ので名乗れる（llama-server がこれ）。
        if len(entries) == 1:
            label = short_model_name(str(entries[0].get("id") or "")) or None
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        label = None
    _MODEL_LABEL_CACHE[cfg.url] = (now, label)
    return label


async def reachable(url: str, api_key: str | None = None, timeout: float = 3.0) -> bool:
    """その URL に OpenAI 互換の相手がいるか。

    CLI ブリッジを on にしてよいかの判定に使う。ブリッジは別コンテナで、
    compose のコメントを外していなければ立っていない。立っていない相手を有効にしても
    会話のたびに失敗するだけなので、管理画面はここが真のときだけ on を押させる。

    待ち時間を短くしてあるのは、管理画面を開くたびに走るため（相手が落ちていても
    ページは出したい）。
    """
    base = _normalize_base_url(url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(f"{base}/models", headers=headers)
        return res.status_code < 500
    except httpx.HTTPError:
        return False


async def check_credential(cfg: Settings) -> tuple[bool, str]:
    """その相手といま実際に話せるか。(判定, 理由) を返す。

    `/models` を引くだけで、会話は 1 往復もしない。 打ち間違えたキーや期限切れは
    「登録されているか」では分からず、会話して初めて失敗する —— それを登録の直後に
    確かめられるようにするためのもので、確かめるたびにサブスクの枠を食っては本末転倒。

    CLI ブリッジは `/health?check=1` を持っていて、そちらは CLI に直接聞く
    (`claude auth status` 等)。ブリッジかどうかは呼び出し側が判断する。
    """
    try:
        async with _llm_client(cfg) as client:
            res = await client.get(f"{cfg.url}/models", timeout=15.0)
    except httpx.HTTPError as e:
        return False, f"つながりません: {e}"
    if res.status_code == 200:
        return True, ""
    if res.status_code in (401, 403):
        return False, f"認証情報が受け付けられませんでした（HTTP {res.status_code}）"
    return False, f"HTTP {res.status_code}: {res.text[:200]}"


# 相手が名乗るモデルの控え。管理画面と会話画面が開くたびに聞かずに済むよう覚えておく。
_MODELS_CACHE: dict[str, tuple[float, list[str]]] = {}
MODELS_TTL = 300.0


async def available_models(backend: str) -> list[str]:
    """会話で選べるモデルの一覧。

    相手に聞くのを優先し、聞けなければコードの候補に落ちる。 OpenRouter のように
    提供モデルが頻繁に入れ替わる相手ではコードの控えがすぐ古くなるし、CLI ブリッジのように
    一覧を持たない相手もいる。両方あるときは相手の答えが正。
    """
    name = normalize_backend(backend)
    now = time.monotonic()
    cached = _MODELS_CACHE.get(name)
    if cached and now - cached[0] < MODELS_TTL:
        return cached[1]

    fallback: list[str] = []
    spec = providers.get(name)
    if spec is not None:
        fallback = list(spec.models)

    models: list[str] = []
    cfg = load_settings(name)
    if cfg is not None:
        try:
            async with _llm_client(cfg) as client:
                res = await client.get(f"{cfg.url}/models", timeout=5.0)
            entries = res.json().get("data") or []
            models = [normalize_model_id(str(e.get("id"))) for e in entries if e.get("id")]
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            models = []

    out = models or fallback
    _MODELS_CACHE[name] = (now, out)
    return out


def default_mode() -> str:
    """`mode` を省いたときの既定(`CHIEZO_ASK_DEFAULT_MODE`)。

    素の既定を `rag` にしてあるのは、agent がツール呼び出しの安定するモデル(8B 級)と
    GPU を前提にするため。環境ごとに違う判断なので、潤沢な環境では .env で
    `agent` に倒せるようにしてある(小さな機械に設定が持ち込まれない側に倒す)。
    """
    value = os.environ.get("CHIEZO_ASK_DEFAULT_MODE", "").strip().lower()
    return value if value in ("rag", "agent") else "rag"


def resolve_mode(backend: str | None, mode: str | None) -> str:
    """その相手で実際に使える引き方。道具を引けない相手では agent を選ばせない。

    agent は「モデル自身に道具を引かせる」やり方なので、MCP を引けない相手
    (Codex。上流の不具合)では道具の無いまま 1 往復するだけになり、Chiezo の知識が
    まったく効かない答えが返る。rag なら Chiezo 側が抜粋を集めてプロンプトに載せるので、
    同じ相手でも根拠つきで答えられる —— 黙って質を落とすより、引き方を倒すほうがよい。
    """
    chosen = (mode or default_mode()).strip().lower()
    if chosen not in ("rag", "agent"):
        chosen = default_mode()
    spec = providers.get(normalize_backend(backend))
    if chosen == "agent" and spec is not None and not spec.can_use_mcp:
        return "rag"
    return chosen


def default_grounded() -> bool:
    """`grounded` を省いたときの既定(`CHIEZO_ASK_DEFAULT_GROUNDED`)。

    素の既定は 1(Chiezo で取れたことだけを根拠にする)。0 にすると足りない分を
    モデルの知識で補うので、会話として自然になる代わりに幻覚のリスクを引き受ける。
    """
    value = os.environ.get("CHIEZO_ASK_DEFAULT_GROUNDED", "").strip().lower()
    return value not in ("0", "false", "no", "off")


def require_settings(
    backend: str | None = None, model: str | None = None, effort: str | None = None
) -> Settings:
    cfg = load_settings(backend, model, effort)
    if cfg is not None:
        return cfg
    name = normalize_backend(backend)
    # 「使う層ごと無効」と「その相手だけ知らない」を区別する。前者は設定の入口を、
    # 後者は選べる相手の一覧を出したほうが、次にすることが分かる。
    known = backend_names()
    if not known:
        raise HTTPException(
            503,
            {
                "error": "answering is disabled",
                "hint": "管理画面（/admin）で「答える」層を有効にし、話す相手を on にすると使えるようになる",
            },
        )
    hint = "管理画面（/admin）で有効にすると選べるようになる"
    if providers.get(name) is None:
        hint = f"知らない相手です。選べるのは: {', '.join(known)}"
    raise HTTPException(
        404,
        {"error": f"unknown backend: {name}", "backends": known, "hint": hint},
    )


async def ensure_model(cfg: Settings) -> Settings:
    """モデルを控えから当てたときだけ、相手に聞いて先頭へ差し替える。

    控え(`app/providers.py`)は相手の都合で古くなる —— 消えたモデル名を送ると 404 に
    なり、画面には「llm error 404」としか出ない。相手が一覧を返せないときは控えのまま
    (それ以上できることが無い)。選んだ・保存したモデルには触らない。
    """
    if not cfg.model_is_fallback:
        return cfg
    models = await available_models(cfg.name)
    if not models or cfg.model in models:
        return cfg

    # 控えの中で、まだ相手にあるものを優先する。 相手の一覧は「新しい順」でも
    # 「会話用だけ」でもない(Gemini は引退したモデル・読み上げ・埋め込みまで並べ、
    # 先頭は古い 2.5 系)。控えの並びはこちらが選んだ順なので、そこから生き残りを拾う
    spec = providers.get(cfg.name)
    for candidate in (spec.models if spec else ()):  # 控えの順
        if candidate in models:
            cfg.model = candidate
            return cfg

    # 控えが全部消えていたら、相手の先頭に賭ける(何も送らないよりは通る見込みがある)
    cfg.model = models[0]
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
    payload = {
        "model": cfg.model,
        "messages": messages,
        "stream": stream,
        **extra,
    }
    # 選ばれたときだけ送る。 既定では触らない —— 知らない項目を無視せず
    # 400 で弾く相手がいるので、使わない機能を毎回載せない。
    if cfg.effort:
        payload["reasoning_effort"] = cfg.effort
    return payload


# 相手のエラー本文から画面に出す理由を取るとき、切り詰める長さ。
REASON_MAX = 300


def _upstream_reason(body: str) -> str:
    """相手が返したエラー本文から、構造化された一言だけを取り出す。

    本文をそのまま返さないのは `_upstream_error` と同じ理由(内部構成が漏れる)。
    かといって握り潰すと、画面には「llm error 502」しか出ず何が起きたか追えない
    —— CLI ブリッジが「root では権限確認を飛ばせない」と言っていたのに、
    それが一切画面へ出ずに詰まったことがある。

    そこで決まった場所に入っている文言だけを通す。読めない形なら空を返す
    (status code だけが出る)。
    """
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    # Gemini はエラーを配列で返す(`[{"error": {...}}]`)。dict しか見ていなかった
    # ときは理由が落ちて、画面には「llm error 503」しか出なかった(実測)。
    if isinstance(doc, list):
        doc = doc[0] if doc and isinstance(doc[0], dict) else {}
    if not isinstance(doc, dict):
        return ""
    # FastAPI は detail に包む(CLI ブリッジがこれ)。OpenAI 互換は素で error を持つ。
    node = doc.get("detail", doc)
    if isinstance(node, str):
        return node.strip()[:REASON_MAX]
    if not isinstance(node, dict):
        return ""
    err = node.get("error")
    # OpenAI / Gemini / OpenRouter は {"error": {"message": ...}}、
    # CLI ブリッジは {"error": "claude failed", "exit_code": 1, "stderr": ...}。
    head = err.get("message") if isinstance(err, dict) else err
    # 終了コードも拾う。CLI が理由を何も書かずに落ちることがあり(実測: prompt 307KB の
    # 生成が 6 秒で exit 1、stderr も stdout も空)、そのとき残るのが「claude failed」
    # だけでは、落ちたのか断られたのかの区別も付かない。
    code = node.get("exit_code")
    code_text = f"exit {code}" if isinstance(code, int) else None
    parts = [p for p in (head, code_text, node.get("stderr")) if isinstance(p, str) and p.strip()]
    return " / ".join(p.strip() for p in parts)[:REASON_MAX]


def _note_failure(cfg: Settings, messages: list[dict], status: int, reason: str) -> None:
    """失敗を控えに残す(`app/ai_log.py`)。

    残す場所を呼び出しの側にしているのは、相手とプロンプトの大きさがここにしかないため。
    `_llm_error` / `_upstream_error` は応答を組むだけで、どの相手にどれだけ送ったかを知らない。
    """
    ai_log.record(
        backend=cfg.name,
        model=cfg.model,
        effort=cfg.effort,
        status=status,
        reason=reason,
        prompt_bytes=sum(len((m.get("content") or "").encode()) for m in messages),
    )


def _upstream_error(exc: Exception) -> HTTPException:
    """推論サーバ側の失敗を、Chiezo のエラー形式に翻訳する。

    例外の文言はそのまま返さない(ログには全部残す)。中身には接続先のホスト名や
    ポート、内部の解決失敗の詳細が入ることがあり、それを応答に載せると、認証の無い
    画面から内部構成が読めてしまう。呼び出し側が次の手を決めるのに要るのは
    「繋がらない」のか「遅い」のかの区別なので、そこだけ返す。
    """
    log.warning("llm request failed: %r", exc)
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(504, {"error": "llm timeout", "reason": type(exc).__name__})
    return HTTPException(502, {"error": "llm unreachable", "reason": type(exc).__name__})


def _llm_error(status: int, body: str, model: str = "") -> dict:
    """相手のエラーを Chiezo のエラー形式にする(理由が読めれば添える)。"""
    detail = {"error": f"llm error {status}"}
    if reason := _upstream_reason(body):
        detail["reason"] = reason
    if status == 404:
        # 404 はたいていモデル名。 相手のモデルは入れ替わるので、こちらの控えが
        # 古いままだと「その名前は無い」で 404 になる(実測: gemini-2.5-flash)
        detail["hint"] = (
            f"モデル名(`{model}`)が相手に無い可能性があります。"
            "会話画面のモデル選択で選び直すか、管理画面で保存し直してください"
        )
    return detail


# 混んでいるだけの失敗は引き直す。 Gemini は「いま混んでいる」を 503 で返し
# (`The model is overloaded`)、数秒後には通ることが多い。agent モードでは道具を
# 何度も引いた後に落ちるので、1 回の 503 でその手間ごと捨てるのは惜しい。
# 待ち時間は短く、回数も少なく —— 相手が本当に落ちているときに粘っても、
# 画面の前の人を待たせるだけ。
RETRY_STATUSES = (429, 503)
RETRY_WAITS = (1.0, 3.0)


async def _post_with_retry(client: httpx.AsyncClient, cfg: Settings, payload: dict):
    """混雑(429/503)だけ引き直す。他の失敗はそのまま返す(呼び出し側が翻訳する)。"""
    for wait in (*RETRY_WAITS, None):
        res = await client.post(cfg.endpoint, json=payload)
        if res.status_code not in RETRY_STATUSES or wait is None:
            return res
        log.info("llm %s; retrying in %.0fs", res.status_code, wait)
        await asyncio.sleep(wait)
    return res  # 到達しない(ループの最後で必ず返す)


def _record_usage(cfg: Settings, usage: dict | None) -> None:
    """1 往復ぶんを使用量に残す(`app/usage_store.py`)。

    相手がトークン数を言わなければ `None` のまま残す。 0 と書くと、数を返さない相手
    (CLI ブリッジ)が「0 トークンで動く相手」に見える —— 回数だけは確かなので、
    そちらは必ず 1 増える。

    失敗しても会話を止めない(記録側が例外を投げない作りにしてある)。
    """
    tokens = usage if isinstance(usage, dict) else {}

    def _count(*names: str) -> int | None:
        for name in names:
            value = tokens.get(name)
            if isinstance(value, int | float):
                return int(value)
        return None

    usage_store.record(
        cfg.name,
        model=cfg.model,
        kind="chat",
        # OpenAI 互換は prompt/completion。相手によっては input/output で名乗る。
        input_tokens=_count("prompt_tokens", "input_tokens"),
        output_tokens=_count("completion_tokens", "output_tokens"),
    )


async def complete_message(cfg: Settings, messages: list[dict], **extra) -> dict:
    """1 回の応答をメッセージまるごと取る。

    `_complete` が本文だけを返すのに対し、こちらは `tool_calls` を含む assistant
    メッセージをそのまま返す(agent モードは次のターンにこれを丸ごと積み直す必要がある)。
    """
    try:
        async with _llm_client(cfg) as client:
            res = await _post_with_retry(
                client, cfg, _payload(cfg, messages, stream=False, **extra)
            )
    except httpx.HTTPError as e:
        err = _upstream_error(e)
        _note_failure(cfg, messages, err.status_code, str(err.detail.get("reason", "")))
        raise err from None
    if res.status_code >= 400:
        # 相手の応答本文もそのまま返さない(上と同じ理由)。ログには残す。
        log.warning("llm error %s: %s", res.status_code, res.text[:500])
        detail = _llm_error(res.status_code, res.text, cfg.model)
        _note_failure(cfg, messages, res.status_code, detail.get("reason", detail["error"]))
        raise HTTPException(502, detail)
    try:
        body = res.json()
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise HTTPException(502, {"error": f"unexpected llm response: {e}"}) from None
    if not isinstance(message, dict):
        raise HTTPException(502, {"error": "unexpected llm response: message is not an object"})
    _record_usage(cfg, body.get("usage"))
    return message


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)
_ORPHAN_THINK_END = re.compile(r"^\s*</think>")


def content_of(message: dict) -> str:
    """assistant メッセージの本文を取り出し、思考タグの残骸を落とす。

    思考(reasoning)を出すモデルでは、推論サーバの設定次第で思考の中身や閉じタグだけが
    `content` に残る。実測: Qwen3 に `--reasoning-budget 0`(思考させない)を掛けると、
    本文の先頭に `</think>` だけが付いてきた。設定は Chiezo が握っていない
    (LAN 上の別サーバかもしれない)ので、受け側で落とす。
    """
    text = _THINK_BLOCK.sub("", message.get("content") or "")
    return _ORPHAN_THINK_END.sub("", text).strip()


async def _complete(cfg: Settings, messages: list[dict], **extra) -> str:
    """1 回の応答の本文だけを取る(クエリ生成・回答用)。"""
    return content_of(await complete_message(cfg, messages, **extra))


async def _stream(cfg: Settings, messages: list[dict], **extra) -> AsyncIterator[str]:
    """OpenAI 互換の SSE を読んで、本文の差分だけを順に返す。

    混雑(429/503)は流し始める前だけ引き直す。 1 文字でも返した後に引き直すと、
    画面に同じ答えが二重に出る。
    """
    for wait in (*RETRY_WAITS, None):
        try:
            async with _llm_client(cfg) as client, client.stream(
                "POST", cfg.endpoint, json=_payload(cfg, messages, stream=True, **extra)
            ) as res:
                if res.status_code in RETRY_STATUSES and wait is not None:
                    await res.aread()
                    log.info("llm %s; retrying in %.0fs", res.status_code, wait)
                    await asyncio.sleep(wait)
                    continue
                if res.status_code >= 400:
                    body = (await res.aread()).decode("utf-8", "replace")
                    log.warning("llm error %s: %s", res.status_code, body[:500])
                    detail = _llm_error(res.status_code, body, cfg.model)
                    _note_failure(cfg, messages, res.status_code,
                                  detail.get("reason", detail["error"]))
                    raise HTTPException(502, detail)
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
                # 流し切ったら 1 回ぶん残す。 差分の応答にトークン数は載らない
                # (`stream_options` を送れば載る相手もいるが、送ると 400 で断る相手がいる)
                # ので、回数だけを記録する。
                _record_usage(cfg, None)
                return
        except httpx.HTTPError as e:
            err = _upstream_error(e)
            _note_failure(cfg, messages, err.status_code, str(err.detail.get("reason", "")))
            raise err from None


# ---- 段 1: クエリ生成 -------------------------------------------------------


PLAN_SYSTEM = """\
あなたは全文検索のクエリを組み立てる補助システムです。ユーザーの質問に答えるために、
ローカル知識ベース「Chiezo」を検索するクエリを組み立ててください。

規則:
- 出力は次の形の JSON だけ。説明文やコードフェンスは書かない。
  {"queries": [{"source": "<ソース名>", "q": "<検索語>"}]}
- 検索語は質問文をそのまま入れない。名詞・固有名詞を 1〜3 語、空白区切りで書く
  (全文検索は空白区切りの各語の AND なので、語を増やすほど当たらなくなる)
- 3 文字以上の語を使う(2 文字以下は索引で引けない)
- クエリは最大 %d 件。関係のないソースは含めない
""" % MAX_QUERIES  # noqa: UP031 —— 本文に JSON の {"queries": …} が入るので format/f-string は使えない


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


def format_history(history: list[dict], limit: int = HISTORY_TURNS) -> str:
    """直前のやり取りを 1 つの文字列にする(会話の続きを検索語に反映するため)。

    「じゃあ京都のほうを詳しく」のような指示語は、直前の話が無いと検索語に直せない。
    全部載せずに直近だけにするのは、クエリ生成の 1 回目に長い文脈を渡す価値が薄いから。
    """
    recent = [m for m in history if m.get("role") in ("user", "assistant")][-limit:]
    if not recent:
        return ""
    lines = [
        f"{'ユーザー' if m['role'] == 'user' else 'あなた'}: {(m.get('content') or '')[:400]}"
        for m in recent
    ]
    return "これまでのやり取り:\n" + "\n".join(lines) + "\n\n"


def _plan_user_prompt(question: str, catalog: list[dict], history: list[dict] | None = None) -> str:
    lines = []
    for c in catalog:
        lang = f" / {c['lang']}" if c["lang"] else ""
        hint = f" — {c['hint']}" if c["hint"] else ""
        lines.append(f"- {c['name']}({c['kind']}{lang} / {c['docs']:,} 件){hint}")
    return (
        format_history(history or [])
        + "利用できるソース:\n" + "\n".join(lines)
        + f"\n\n質問: {question}"
    )


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
    cfg: Settings, request: Request, question: str, source: str | None,
    history: list[dict] | None = None,
) -> list[dict]:
    """質問から検索クエリを組み立てる。

    `source` を指定されたときもこの段は省かない。ソースを固定してもクエリ生成の
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
            {"role": "user", "content": _plan_user_prompt(question, catalog, history)},
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
    エンドポイントは既定値が `Query(...)` オブジェクトなので、全パラメータを明示的に渡す
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
            "url": doc_url(source, hit["doc_id"]),
        })
    return snippets, references


# ---- 段 3: 回答 -------------------------------------------------------------


# 回答方針は 2 つあり、`grounded` で切り替える。これは Chiezo の設計思想ではなく
# モデルの幻覚への対処なので、固定の制約にはしない(Chiezo は AI 用の知識ベースで、
# ローカル LLM はそれを使う側。持っている知識を封じるのが目的ではない)。
ANSWER_SYSTEM_GROUNDED = """\
あなたは AI アシスタントです。ローカル知識ベース「Chiezo」から抜き出した文章を渡すので、
それを根拠に日本語で簡潔に答えてください(Chiezo はあなたが引く知識であって、あなた自身ではありません)。

規則:
- 抜粋に書かれていないことは答えない。根拠が無ければ「抜粋からは分かりません」と言う
- 事実を述べた文には、根拠にした抜粋の番号を [1] の形で付ける
- 抜粋の丸写しではなく、質問に答える形にまとめる
"""

ANSWER_SYSTEM_OPEN = """\
あなたは AI アシスタントです。ローカル知識ベース「Chiezo」から取ってきた抜粋を渡すので、
それを踏まえて日本語で簡潔に答えてください(Chiezo はあなたが引く知識であって、あなた自身ではありません)。

規則:
- 抜粋に書かれていることは、根拠にした番号を [1] の形で付ける
- 抜粋で足りない部分は自分の知識で補ってよい。ただしその部分には番号を付けない
- 抜粋と自分の知識が食い違うときは抜粋を優先し、食い違い自体も述べる
"""

# grounded=1 なのに抜粋が 1 件も取れなかったときの答え。ここで LLM を呼ばないのは、
# 実測で小型モデル(gemma-3-1b)が「抜粋が空でも自分の知識で答えてしまう」ことを
# 確かめたため。守れない約束をプロンプトだけに委ねず、経路として断つ。
NO_CONTEXT_ANSWER = (
    "抜粋からは分かりません(Chiezo で該当する文書が見つかりませんでした)。"
    "検索語を変えるか、source を指定するか、grounded=0 で聞き直してください。"
)


def build_answer_messages(
    question: str, snippets: list[dict], grounded: bool = True,
    history: list[dict] | None = None,
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
    # 履歴は system と今回の質問のあいだに挟む(会話として自然な並びにする)。
    # 抜粋は毎回作り直すので、過去のターンの抜粋は積み直さない(文脈が際限なく伸びる)。
    past = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in (history or []) if m.get("role") in ("user", "assistant")
    ][-HISTORY_TURNS:]
    return [
        {"role": "system", "content": ANSWER_SYSTEM_GROUNDED if grounded else ANSWER_SYSTEM_OPEN},
        *past,
        {"role": "user", "content": f"# 抜粋\n{blocks}\n\n# 質問\n{question}"},
    ]


def has_no_basis(snippets: list[dict], grounded: bool) -> bool:
    """grounded なのに根拠が 1 件も無い状態(= LLM を呼ぶ意味がない)。"""
    return grounded and not snippets


# ---- 呼び出し口 -------------------------------------------------------------


async def prepare(
    cfg: Settings, request: Request, question: str, source: str | None,
    history: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """クエリ生成 → 取得 まで進め、(クエリ, 抜粋, 出典) を返す。"""
    queries = await plan_queries(cfg, request, question, source, history)
    snippets, references = await run_in_threadpool(gather_context, request, queries, cfg)
    return queries, snippets, references


async def answer(
    cfg: Settings,
    request: Request,
    question: str,
    source: str | None,
    grounded: bool = True,
    history: list[dict] | None = None,
) -> dict:
    """まとめて 1 つの JSON を返す(非ストリーミング)。"""
    queries, snippets, references = await prepare(cfg, request, question, source, history)
    if has_no_basis(snippets, grounded):
        text = NO_CONTEXT_ANSWER
    else:
        text = await _complete(
            cfg, build_answer_messages(question, snippets, grounded, history), temperature=0.2
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
    cfg: Settings, question: str, snippets: list[dict], grounded: bool = True,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    """回答本文を差分で返す(`/v1/ask?stream=1` の本体)。

    取得(`prepare`)は呼び出し側が先に済ませること。ストリーミング応答は
    ヘッダを送った後で失敗してもステータスコードを変えられないので、
    クエリ生成・検索の失敗は流し始める前に HTTP のエラーとして返す必要がある。
    """
    if has_no_basis(snippets, grounded):
        yield NO_CONTEXT_ANSWER
        return
    async for delta in _stream(
        cfg, build_answer_messages(question, snippets, grounded, history), temperature=0.2
    ):
        yield delta
