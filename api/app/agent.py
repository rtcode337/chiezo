"""agent モード — 道具を LLM 自身に引かせる「答える」層のもう 1 つの経路。

`/v1/ask?mode=agent` の本体。rag モード(`app/answer.py`)が **search を 1 回叩いて
終わり**なのに対し、こちらは `filter` / `tags` / `links` を含む chiezo の道具一式を
LLM に渡し、何をどう引くかをモデルに決めさせる。「カテゴリ○○の記事は何件ある?」
(tags で正式な名前を確かめて filter の total を見る)のように、**1 回の検索では原理的に
答えられない問い**に届かせるための経路。

設計の要点:

- **道具の定義も実行も `app/mcp_server.py` から借りる**。MCP の `list_tools()` を
  OpenAI の function 形式へ写し、実行は `call_tool()` に投げる。書き写すと
  REST・MCP・agent の三重管理になって必ずずれるし、借りれば説明文(「タグの列挙は
  filter で」等、chiezo を正しく引くための知識)もそのまま付いてくる。
  結果として **Claude Code から MCP で使うときと同じ道具立て**になる。
- **渡すのは読み取り専用の道具だけ**(`AGENT_TOOLS`)。notes の `remember` は
  書き込みなので自動ループに任せない — 質問に答えた副作用でメモが増えるのは、
  利用者から見て予想外の変化になる。
- **上限は 3 つ**: ステップ数(`CHIEZO_AGENT_MAX_STEPS`)・ツール結果の長さ
  (`CHIEZO_AGENT_TOOL_CHARS`)・全体の締め切り(`CHIEZO_AGENT_TIMEOUT`)。
  ツール結果は毎ターン積み上がるので、上限が無いとモデルの窓も待ち時間も
  読めなくなる。必要な文脈長は **ステップ数 × ツール結果の長さ** で見積もれる。
- **同じ呼び出しは実行し直さず、前回の結果を返す**(`repeated_payload`)。モデルは
  1 回の応答に同じ呼び出しを 2 つ並べて出すことがあり、外へ 2 回出す必要はない。
  ここでエラーを返すと「失敗した」と受け取って別の検索を足しに行き、ステップを空費する。
- **最終回答はストリーミングしない**。ツール呼び出しは応答の途中まで読まないと
  「道具を呼んだのか答えたのか」が分からず、ストリームの断片から復元するのは
  壊れやすい。代わりに**ステップの進捗**(どの道具を何の引数で呼び、何件返ったか)を
  逐次流す — 数十秒待たせる画面で見たいのはそちらでもある。
- **出典は番号引用ではなくタイトル参照**。rag は抜粋に [1] を振って渡せるが、
  agent が見るのは道具の生の応答なので番号を振る先が無い。代わりに、道具の応答に
  出てきた文書を出現順に集めて `references` として返す(何を見て答えたかは残る)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import HTTPException, Request
from mcp.server.fastmcp.exceptions import ToolError

from app import answer, notes, websearch
from app.mcp_server import INSTRUCTIONS, build_mcp
from app.pages import browse_url, doc_url

log = logging.getLogger("chiezo.api")

# agent に渡す chiezo の道具(MCP に出しているものから借りる)。ここは読み取り専用。
KNOWLEDGE_TOOLS = ("sources", "search", "doc", "filter", "tags", "titles", "links")

# 「覚える」層の道具。**`remember` は chiezo で唯一の書き込み**なので分けてある。
# 当初は「質問の副作用でメモが増えるのは予想外の変化」として渡していなかったが、
# 会話で「覚えておいて」と**明示的に頼まれる**なら副作用ではない。代わりに
# ①やり取りごとに切れる(`notes` 引数・画面のトグル)②何を書いたかは step に出る、
# の 2 つで見えるようにしてある。
NOTE_TOOLS = ("remember", "recall")

# 「この名前は agent が実行してよい」の全体(実際に渡すかは呼び出しごとに決まる)
AGENT_TOOLS = KNOWLEDGE_TOOLS

# 出典として持ち帰る文書の上限(道具の応答には 50 件単位で並ぶので、そのまま
# 積むと出典欄が結果一覧になる)。
MAX_REFERENCES = 20

AGENT_SYSTEM_GROUNDED = """\
あなたは AI アシスタントです。ローカル知識ベース「chiezo」を上の道具で引けます
(chiezo はあなたが引く知識であって、あなた自身ではありません)。
引いて分かったことを日本語で簡潔に答えてください。

規則:
- **必ず道具で調べてから答える**。道具で取れなかったことは答えず、
  「chiezo からは分かりません」と言う
- 道具は 1 ステップに必要な分だけ呼ぶ。同じ呼び出しを繰り返さない
  (0 件だったら、検索語・ソース・絞り込みのどれかを変える)
- 件数を答えるときは filter の total を使う(search の結果件数は上位数件でしかない)
- 十分に分かった時点で道具を呼ぶのをやめ、答えを書く
- 根拠にした文書のタイトルを本文に書く(出典一覧は chiezo 側で付ける)
"""

AGENT_SYSTEM_OPEN = """\
あなたは AI アシスタントです。ローカル知識ベース「chiezo」を上の道具で引けます
(chiezo はあなたが引く知識であって、あなた自身ではありません)。
引いて分かったことを日本語で簡潔に答えてください。会話として自然に応じてかまいません。

規則:
- **まず道具で調べる**。そのうえで足りない部分は自分の知識で補ってよい
- 道具は 1 ステップに必要な分だけ呼ぶ。同じ呼び出しを繰り返さない
  (0 件だったら、検索語・ソース・絞り込みのどれかを変える)
- 件数を答えるときは filter の total を使う(search の結果件数は上位数件でしかない)
- chiezo から取れたことと自分の知識で補ったことは、区別が分かるように書く
- 根拠にした文書のタイトルを本文に書く(出典一覧は chiezo 側で付ける)
"""

# web 検索が有効なときだけ足す使い分け。**chiezo が先**という順番をここで固定する
# (chiezo の存在理由が「外部 API を先に叩かせない」ことなので、道具が増えても順番は変えない)。
WEB_SEARCH_POLICY = """
web_search も使える場合:
- **まず chiezo を引く**。web は chiezo に無いものだけ(取り込んだダンプより新しい出来事、
  いま現在の状態、ローカルに収録していない話題)に使う
- web から得たことは、chiezo から得たことと**区別が分かるように書く**
  (「web で調べた限り」など)。出典一覧にも web として並ぶ
"""

# ステップ予算・締め切りを使い切ったときに最後の 1 回だけ足す指示。
# 道具を渡さずにこれを送るので、モデルはここで必ず答えを書くことになる。
FORCED_ANSWER_NOTICE = (
    "ここまでで調べる時間を使い切りました。これ以上道具は使えません。"
    "いま手元にある情報だけで、分かる範囲の答えを日本語で書いてください。"
    "足りない部分は「chiezo からは分かりませんでした」と正直に書いてかまいません。"
)

# 一度も道具が結果を返さないまま grounded で答えようとした場合の定型文。
# rag 側の NO_CONTEXT_ANSWER と同じ趣旨(根拠が無いなら推論に委ねない)。
NO_EVIDENCE_ANSWER = (
    "chiezo からは分かりません(道具で根拠になる情報を取れませんでした)。"
    "質問を具体的にするか、source を指定するか、grounded=0 で聞き直してください。"
)


# ---- 道具(MCP から借りる) --------------------------------------------------


def _mcp(app):
    """このアプリに紐づく MCP サーバー(道具の定義と実体を持っている)。

    通常は lifespan が組み立てて `app.state.mcp` に置いたものを使う。
    無いのは lifespan を通っていない経路だけなので、そのときだけ作って覚える。
    """
    mcp = getattr(app.state, "mcp", None)
    if mcp is None:
        mcp = build_mcp(app)
        app.state.mcp = mcp
    return mcp


async def tool_specs(app, web: bool = False, notes: bool = False) -> list[dict]:
    """MCP のツール定義を OpenAI の function 形式へ写す。

    説明文(description)も入力スキーマも MCP のものをそのまま使う。ここで書き直すと
    「MCP 経由では正しく引けるのに agent では引けない」というずれが必ず生まれる。
    """
    allowed = set(KNOWLEDGE_TOOLS) | (set(NOTE_TOOLS) if notes else set())
    tools = await _mcp(app).list_tools()
    specs = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in tools
        if t.name in allowed
    ]
    # web 検索は chiezo の道具ではないので MCP には出しておらず、ここで足す
    # (使うときだけ。使わないなら道具ごと見せない = 使えないものを文脈に並べない)。
    if web:
        specs.append(websearch.TOOL_SPEC)
    return specs


def notes_allowed(requested: bool | None) -> bool:
    """このやり取りで「覚える・思い出す」を使わせるか。

    `CHIEZO_NOTES_DIR` が未設定なら notes ごと無効なので、頼まれても使えない。
    有効な場合は**呼び出しごとに切れる**(書き込みを伴うので、切れることが要る)。
    """
    return notes.is_enabled() and requested is not False


def web_allowed(requested: bool | None) -> bool:
    """このやり取りで web 検索を使わせるか。

    サーバー側で設定されていなければ、頼まれても使えない(道具が無い)。
    設定されている場合は**呼び出しごとに切れる** — 画面のトグルや API の `web=0` で、
    「いまは chiezo だけで答えてほしい」を選べるようにするため。
    """
    return websearch.is_enabled() and requested is not False


def _payload_of(result: Any) -> Any:
    """FastMCP の戻り値を素の Python 値に均す。

    mcp 1.x の `call_tool(convert_result=True)` は表示用の ContentBlock 列を返す
    (中身は道具が返した dict の JSON)。版によっては構造化結果との組で返るので、
    その場合は構造化側を採る。どちらでもなければ本文を JSON として読む
    (読めなければ文字列のまま)。
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
        structured = result[1]
        # dict を返さない道具は {"result": ...} に包まれて来る
        return structured["result"] if set(structured) == {"result"} else structured
    if isinstance(result, dict):
        return result
    text = "".join(getattr(b, "text", "") or "" for b in (result or []))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _tool_error_payload(text: str) -> dict:
    """ToolError の文言から、エンドポイントが返した JSON を取り出す。

    `mcp_server._call` は HTTPException の中身を JSON 文字列にして ToolError にするが、
    FastMCP がさらに "Error executing tool search: " を前置きするので素直には読めない。
    404 の candidates や 409 の移行案内はモデルが次の手を決めるのに要るので、
    前置きを剥がしてでも拾う(それも駄目なら文言をそのままエラーにする)。
    """
    for candidate in (text, text[text.find("{"):] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"error": text}


async def execute(
    app, name: str, arguments: dict, web: bool = False, notes_ok: bool = False
) -> tuple[bool, Any]:
    """道具を 1 つ実行する。戻りは (成功したか, 応答)。

    失敗も**モデルに返す**(例外にしない)。404 の candidates や 409 の移行案内には
    次の手を決めるのに要る情報が入っているし、1 回の失敗でループを落とす必要もない。
    """
    if name == websearch.TOOL_NAME:
        if not web:
            return False, {"error": "web search is disabled"}
        payload = await websearch.search(str(arguments.get("q", "")).strip())
        return "error" not in payload, payload
    if name in NOTE_TOOLS and not notes_ok:
        return False, {"error": "notes are disabled for this conversation"}
    if name not in KNOWLEDGE_TOOLS and name not in NOTE_TOOLS:
        return False, {"error": f"unknown tool: {name}"}
    try:
        raw = await _mcp(app).call_tool(name, arguments)
    except ToolError as e:
        return False, _tool_error_payload(str(e))
    except Exception as e:  # noqa: BLE001 - 道具の失敗でループを落とさない
        # 例外の文言は**返さない**(種別だけ)。step はそのまま画面へ流れるので、
        # 内部の詳細をブラウザまで運ばないようにする。ログには全部残す。
        log.exception("agent tool %s failed", name)
        return False, {"error": f"tool failed: {type(e).__name__}"}
    return True, _payload_of(raw)


# ---- ステップの記録 ---------------------------------------------------------


def summarize(payload: Any) -> str:
    """ステップ表示用の 1 行。応答そのものは長いので、形だけ分かる要約にする。"""
    if isinstance(payload, dict):
        if "error" in payload:
            return f"エラー: {payload['error']}"
        if isinstance(payload.get("total"), int):
            return f"total={payload['total']}"
        for key in ("results", "notes", "tags", "titles", "links", "sources"):
            value = payload.get(key)
            if isinstance(value, list):
                return f"{len(value)} 件"
        if payload.get("title"):
            return str(payload["title"])
    return "取得"


def has_content(ok: bool, payload: Any) -> bool:
    """この応答が「根拠になった」と言えるか(grounded の判定に使う)。

    件数を数える `tags` / `filter` の total のように、文書そのものを返さなくても
    根拠になる応答があるので、出典(references)の有無では判定しない。
    """
    if not ok or not isinstance(payload, dict):
        return False
    if isinstance(payload.get("total"), int):
        return payload["total"] > 0
    for key in ("results", "notes", "tags", "titles", "links", "sources"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
    return bool(payload.get("title"))


def collect_references(refs: list[dict], source: str, payload: Any) -> None:
    """道具の応答に出てきた文書を出典として積む(出現順・重複なし・上限あり)。

    `doc` の応答は既定のフィールドに doc_id を含まないので、その場合は
    タイトルで検索する URL に落とす(出典の行き先が無いよりはよい)。
    """
    if not isinstance(payload, dict):
        return
    source = payload.get("source") or source
    # 道具ごとに並びのキーが違う(search / filter は results、recall は notes)
    rows = next(
        (payload[key] for key in ("results", "notes") if isinstance(payload.get(key), list)),
        None,
    )
    if rows is None:
        rows = [payload] if payload.get("title") else []
    found = []
    if not source:
        return  # どのソースの文書か分からなければ、リンクを組み立てられない
    for row in rows:
        if not isinstance(row, dict) or not row.get("title"):
            continue
        title, doc_id = str(row["title"]), row.get("doc_id")
        url = (
            doc_url(source, doc_id) if isinstance(doc_id, int)
            else browse_url(source) + f"?q={quote(title)}"
        )
        found.append({
            "source": source, "title": title,
            "doc_id": doc_id if isinstance(doc_id, int) else None, "url": url,
        })
    add_references(refs, found)


def add_references(refs: list[dict], found: list[dict]) -> None:
    """出典を積む(出現順・重複なし・上限あり)。番号はここで振る。

    chiezo の文書と web の結果が同じ一覧に並ぶので、`source` は消さない
    (`web` かどうかが出典を見た人に分かる必要がある)。
    """
    for item in found:
        if len(refs) >= MAX_REFERENCES:
            return
        if any(r["source"] == item["source"] and r["title"] == item["title"] for r in refs):
            continue
        refs.append({"n": len(refs) + 1, **item})


def repeated_payload(payload: Any) -> Any:
    """同じ引数で 2 回目に呼ばれたときに返すもの: **前回の結果 + 一言**。

    エラーを返してはいけない。実測でモデルは **1 回の応答に同じ呼び出しを 2 つ並べて**
    出してくる(0 件だったので投げ直した、ではない)。そこにエラーを返すと、手元に
    結果があるのに「失敗した」と受け取って別の検索を足しに行き、ステップを空費する。
    外へは出さずに前回の結果をそのまま返し、繰り返しであることだけ添える。
    """
    if isinstance(payload, dict):
        return {
            **payload,
            "note": "同じ引数で既に呼ばれたので、前回の結果をそのまま返した"
                    "(結果は変わらない。次は別の引数にするか、分かったことで答えること)",
        }
    return payload


# ---- ループ本体 -------------------------------------------------------------


def _system_prompt(
    catalog: list[dict], source: str | None, grounded: bool, web: bool = False
) -> str:
    """道具の使い方(MCP の instructions)+ ソース一覧 + 回答方針。

    使い方の説明を MCP から借りるのは道具の定義と同じ理由で、chiezo を正しく引くための
    知識(「列挙は filter」「短い語は前方一致に落ちる」等)を二重に書かないため。
    """
    lines = []
    for c in catalog:
        lang = f" / {c['lang']}" if c["lang"] else ""
        hint = f" — {c['hint']}" if c["hint"] else ""
        lines.append(f"- {c['name']}({c['kind']}{lang} / {c['docs']:,} 件){hint}")
    fixed = (
        f"\n\n**このやり取りでは {source} だけを引くこと**(他のソースは使わない)。"
        if source else ""
    )
    # web 検索を使わせるときだけ、その使い分けを足す(使わないなら道具ごと出していない)。
    web_policy = WEB_SEARCH_POLICY if web else ""
    return (
        INSTRUCTIONS
        + "\n利用できるソース:\n" + "\n".join(lines) + fixed + "\n\n"
        + (AGENT_SYSTEM_GROUNDED if grounded else AGENT_SYSTEM_OPEN)
        + web_policy
    )


def _parse_arguments(raw: Any) -> tuple[dict | None, str]:
    """tool_calls の arguments(JSON 文字列)を読む。読めなければ理由を返す。"""
    if isinstance(raw, dict):
        return raw, ""
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return None, f"arguments is not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return None, "arguments must be a JSON object"
    return parsed, ""


def prepare_catalog(request: Request, source: str | None) -> list[dict]:
    """プロンプトに載せるソース一覧を作る。指定されたソースが無ければ 404。

    ループを回し始める前に済ませられる唯一の検査なので、ストリーミングの
    呼び出し側(`main._agent_events`)はここだけ先に通してから流し始める
    (SSE はヘッダを送った後でステータスコードを変えられないため)。
    """
    catalog = answer.source_catalog(request)
    known = [c["name"] for c in catalog]
    if not known:
        raise HTTPException(503, {"error": "no sources registered"})
    if source is None:
        return catalog
    if source not in known:
        raise HTTPException(404, {"error": f"unknown source: {source}", "sources": known})
    return [c for c in catalog if c["name"] == source]


async def stream(
    cfg: answer.Settings,
    request: Request,
    question: str,
    source: str | None,
    grounded: bool = True,
    history: list[dict] | None = None,
    web: bool | None = None,
    notes: bool | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """agent ループを回し、進捗と結果を (イベント名, 中身) で流す。

    イベントは 3 種:
      - `step`       … 道具を 1 つ実行した(name / arguments / ok / summary)
      - `references` … ループが終わり、出典が確定した
      - `delta`      … 回答本文(ここでは 1 個だけ。上の設計メモを参照)
    非ストリーミングの `answer()` も、これを集めて 1 つの JSON にするだけ。
    """
    app = request.app
    catalog = prepare_catalog(request, source)
    use_web = web_allowed(web)
    use_notes = notes_allowed(notes)
    tools = await tool_specs(app, use_web, use_notes)
    # 履歴は本文だけを積み直す(過去のターンの道具のやり取りまで積むと文脈が際限なく伸び、
    # モデルが古い検索結果を根拠にし始める。何を引くかは毎ターン引き直させる)。
    past = [
        {"role": m["role"], "content": m.get("content") or ""}
        for m in (history or []) if m.get("role") in ("user", "assistant")
    ][-answer.HISTORY_TURNS:]
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(catalog, source, grounded, use_web)},
        *past,
        {"role": "user", "content": question},
    ]
    references: list[dict] = []
    # 呼んだ道具と、その結果(同じ引数で呼び直されたら実行せずこれを返す)
    called: dict[str, tuple[bool, Any]] = {}
    evidence = 0
    # 予算(`agent_max_steps`)は LLM のターン数だが、1 ターンで複数の道具を呼べるので
    # 表示用の番号は**実行した道具の通し番号**にする(同じ番号が並ぶと追えない)。
    executed = 0
    deadline = asyncio.get_running_loop().time() + cfg.agent_timeout
    final = ""

    for step in range(cfg.agent_max_steps):
        if asyncio.get_running_loop().time() > deadline:
            log.info("agent: out of time after %d step(s)", step)
            break
        message = await answer.complete_message(
            cfg, messages, tools=tools, temperature=0.2
        )
        calls = message.get("tool_calls") or []
        if not calls:
            final = answer.content_of(message)
            break
        # assistant メッセージは tool_calls ごとそのまま積み直す(次のターンで
        # tool メッセージと対応が取れなくなるため、内容を作り変えない)
        messages.append(message)
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            arguments, why = _parse_arguments(fn.get("arguments"))
            repeated = False
            if arguments is None:
                ok, payload = False, {"error": why}
            elif (key := f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}") in called:
                # 同じ呼び出しは**実行し直さない**(外へも出さない)。前回の結果を返す。
                repeated = True
                ok, payload = called[key]
                payload = repeated_payload(payload)
            else:
                ok, payload = await execute(app, name, arguments, use_web, use_notes)
                called[key] = (ok, payload)
            if ok:
                if name == websearch.TOOL_NAME:
                    add_references(references, websearch.references_from(payload))
                else:
                    # notes の道具(remember / recall)は source 引数を持たない
                    default_source = "notes" if name in NOTE_TOOLS else ""
                    collect_references(
                        references,
                        (arguments or {}).get("source") or default_source,
                        payload,
                    )
                if has_content(ok, payload):
                    evidence += 1
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": _tool_content(payload, cfg.agent_tool_chars),
            })
            executed += 1
            yield "step", {
                "step": executed, "turn": step + 1, "tool": name,
                "arguments": arguments or {}, "ok": ok, "repeated": repeated,
                "summary": summarize(payload) + ("(前回と同じ)" if repeated else ""),
            }
    else:
        log.info("agent: step budget (%d) exhausted", cfg.agent_max_steps)

    if not final:
        # ステップ予算か締め切りを使い切った場合。道具を渡さずにもう 1 回だけ聞く
        # (ここで打ち切ると、調べただけで何も答えないまま終わってしまう)。
        messages.append({"role": "user", "content": FORCED_ANSWER_NOTICE})
        last = await answer.complete_message(cfg, messages, temperature=0.2)
        final = answer.content_of(last)

    if grounded and evidence == 0:
        # 根拠を 1 件も取れていないなら、モデルの記憶で答えさせない(rag 側と同じ判断)
        final = NO_EVIDENCE_ANSWER
        references = []

    yield "references", {"references": references}
    yield "delta", {"text": final}


def _tool_content(payload: Any, limit: int) -> str:
    """道具の応答をモデルに返す形(JSON 文字列)にし、長ければ切る。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + f"…(応答が長いので {limit} 字で切った。必要なら絞り込んで引き直すこと)"


async def answer_question(
    cfg: answer.Settings,
    request: Request,
    question: str,
    source: str | None,
    grounded: bool = True,
    history: list[dict] | None = None,
    web: bool | None = None,
    notes: bool | None = None,
) -> dict:
    """agent ループをまとめて 1 つの JSON にする(非ストリーミング)。"""
    steps: list[dict] = []
    references: list[dict] = []
    text = ""
    async for event, data in stream(
        cfg, request, question, source, grounded, history, web, notes
    ):
        if event == "step":
            steps.append(data)
        elif event == "references":
            references = data["references"]
        elif event == "delta":
            text += data["text"]
    return {
        "question": question,
        "answer": text.strip(),
        "references": references,
        "steps": steps,
        "mode": "agent",
        "grounded": grounded,
        "web": web_allowed(web),
        "notes": notes_allowed(notes),
        "model": cfg.model,
    }
