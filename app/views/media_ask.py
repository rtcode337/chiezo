"""画面から絵と音を頼む口（管理画面の「見比べ」の中）。

なぜ要るか
----------
**会話できない相手には、画面から頼む道が無かった。** ComfyUI と ElevenLabs は
話せる相手ではないので、会話の道具に混ぜても届かない —— MCP か REST を自分で叩く
しかなく、画面を開いている人には手が出せなかった。

**「画面に課金の走る操作を置かない」は、ここには当てはまらない。** あれは
やること層（認証なしで外に開く面）の決めごとで、管理画面はもともと
「Chiezo を操作している人だけが開ける」前提。実際、会話も収集の即時実行も
押せば枠を使う —— 生成だけを外しておく理由が無かった。

決めごと
--------
- **選べる相手は `media.backends` が返すものだけ。** 使えない相手を並べて、
  押してから断られるのは手間が増えるだけ
- **押した人の判断でしか動かない。** ここは頼む口で、AI に判断を渡す話とは別
  （会話の相手に作らせる道は `app/agent.py` 側にある）
- 頼んだら**見比べへ戻す**。job は後から引くものなので、待たせない
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import media, media_providers
from app.pages import esc

router = APIRouter()

# 頼める種類。**動画と読み上げは出さない** —— 前者は 1 本で数十 MB あって
# 押し間違いが重く、後者は声を選ばないと使いものにならない（欄が増える）。
KINDS = (
    (media_providers.KIND_IMAGE, "絵"),
    (media_providers.KIND_AUDIO, "音"),
)


async def _usable(kind: str) -> list[dict]:
    return [b for b in await media.backends(kind) if b.get("usable")]


def _options(items: list[tuple[str, str]], selected: str = "") -> str:
    return "".join(
        f'<option value="{esc(value)}"{" selected" if value == selected else ""}>'
        f"{esc(label)}</option>"
        for value, label in items
    )


async def section_html() -> str:
    """「作ってもらう」節。**相手ごとに選べるサイズが違う**ので、まとめて添える。"""
    if not media.is_enabled():
        return ""
    forms = []
    for kind, label in KINDS:
        usable = await _usable(kind)
        if not usable:
            continue
        backends = [(b["id"], b["label"]) for b in usable]
        sizes = sorted({s for b in usable for s in (b.get("sizes") or [])})
        size_field = (
            '<label>大きさ<br><select name="size">'
            f"{_options([(s, s) for s in sizes], '1024x1024')}</select></label>"
            if kind == media_providers.KIND_IMAGE else ""
        )
        sound_field = (
            '<label>種類<br><select name="sound">'
            f'{_options([("sfx", "効果音"), ("music", "曲")])}</select></label>'
            if kind == media_providers.KIND_AUDIO else ""
        )
        forms.append(
            f'<form method="post" action="/admin/media/ask" class="ask-form">'
            f'<input type="hidden" name="kind" value="{esc(kind)}">'
            f"<h3>{esc(label)}を作ってもらう</h3>"
            '<p><label>依頼文<br>'
            '<textarea name="prompt" rows="4" required '
            'placeholder="何を作ってほしいか（日本語で書く）"></textarea></label></p>'
            '<p><label>相手<br><select name="backend">'
            f"{_options(backends)}</select></label>"
            f"{size_field}{sound_field}"
            '<label>組の名前<br><input type="text" name="group" '
            'placeholder="何案か並べたいとき（任意）"></label></p>'
            "<p><button type=\"submit\">頼む</button> "
            '<span class="muted">出来たら、この画面に組として並びます</span></p>'
            "</form>"
        )
    if not forms:
        return ""
    return (
        "<h2>作ってもらう</h2>"
        '<p class="muted">会話できない相手（自前の GPU など）にも、ここから直接頼めます。'
        "<strong>押すと相手の枠を使います。</strong>"
        "待たずに job が立つので、出来たら下の組に並びます。</p>"
        + "".join(forms)
    )


@router.post("/admin/media/ask")
async def admin_media_ask(
    request: Request,
    kind: str = Form(...),
    prompt: str = Form(...),
    backend: str = Form(""),
    size: str = Form("1024x1024"),
    sound: str = Form("sfx"),
    group: str = Form(""),
):
    """頼んで見比べへ戻る。**待たない**（job は後から引くもの）。

    断られた理由はそのまま画面に出す —— サイズや相手の選び違いは、
    書き直せば通るものなので、何が悪かったのかが読めることが要る。
    """
    from fastapi import HTTPException

    common = {"backend": backend.strip(), "group": group.strip(),
              "requested_by": "管理画面"}
    try:
        if kind == media_providers.KIND_AUDIO:
            media.start_audio_job(prompt, sound=sound, **common)
        else:
            media.start_image_job(prompt, size=size, **common)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"error": str(e.detail)}
        body = (
            "<h1>頼めませんでした</h1>"
            f'<p class="stale">{esc(str(detail.get("error") or ""))}</p>'
            f'<p class="muted">{esc(str(detail.get("hint") or ""))}</p>'
            '<p><a href="/admin/media">← 見比べへ戻る</a></p>'
        )
        return HTMLResponse(content=body, status_code=e.status_code)
    return RedirectResponse("/admin/media", status_code=303)
