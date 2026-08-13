"""管理画面の「話す相手」節（一覧の描画と、on/off・API キーの受け口）。

**設定は環境変数ではなくここから入れる。** URL と表示名は `app/providers.py` に
決め打ちしてあり、ユーザーが決めるのは on/off・API キー・既定のモデルだけ。
保存先は `app/settings_store.py`（`state/settings.db`）。

on にできる条件を画面側でも守る:

- API キーの要る相手（Gemini / OpenRouter）… **鍵が未登録なら on にできない**
- CLI ブリッジ（Claude Code / Codex CLI）… **コンテナが立っていなければ on にできない**
  （compose のコメントを外していなければ立っていない。立っていない相手を有効にしても
  会話のたびに失敗するだけなので、押させない）

どちらも `app/answer.py` 側でも弾く（設定を直に書き換えられた場合の保険）。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import answer, providers, settings_store
from app.pages import CHAT_PATH, esc

router = APIRouter()


async def _rows() -> list[dict]:
    """一覧に出す 1 行ぶんずつ。ブリッジの到達確認は並行に行う。

    直列に待つと、立っていない相手の数だけ管理画面の表示が遅れる
    （到達確認は 3 秒でタイムアウトするので、2 つで最大 6 秒になる）。
    """
    stored = settings_store.load_all()
    specs = providers.all_providers()

    async def probe(spec: providers.Provider) -> bool:
        if not spec.probe:
            return True
        return await answer.reachable(providers.url_of(spec))

    reachable = await asyncio.gather(*(probe(s) for s in specs))

    rows = []
    for spec, ok in zip(specs, reachable, strict=True):
        st = stored.get(spec.id, settings_store.ProviderSetting(spec.id))
        blocked = ""
        if spec.key == providers.KEY_REQUIRED and not st.has_key:
            blocked = "API キーが未登録"
        elif spec.probe and not ok:
            blocked = "起動していない"
        rows.append(
            {
                "spec": spec,
                "enabled": st.enabled,
                "has_key": st.has_key,
                # 鍵の欄を出すか（要る相手と、任意で入れられる相手）
                "takes_key": spec.key != providers.KEY_NONE,
                # 鍵が無いと on にできないか（任意の相手は無くても on にできる）
                "key_required": spec.key == providers.KEY_REQUIRED,
                "model": st.model,
                "updated_at": st.updated_at[:19],
                # can_enable が偽なら on のボタンを押させない
                "can_enable": not blocked,
                "blocked": blocked,
                "runnable": st.enabled and not blocked,
            }
        )
    return rows


async def section_html() -> str:
    """管理画面に差し込む「話す相手」節。

    見た目は**管理画面の素っ気なさに合わせる**（表と details だけ。JS も持たない）。
    会話画面は毎日触るので作り込んであるが、こちらは設定を一度入れたら開かない場所である。
    """
    if not settings_store.is_enabled():
        return (
            "<h2>話す相手</h2>\n"
            '<p class="muted">設定の保存先がありません。書き込み可能なディレクトリを'
            " <code>CHIEZO_STATE_DIR</code> に設定すると、ここから話す相手を追加できます"
            "(compose では <code>./state:/state</code> をマウント済み)。</p>"
        )

    on = settings_store.answer_enabled()
    master = (
        '<div class="job-status">'
        f'<strong>「答える」層: {"有効" if on else "停止中"}</strong> '
        '<form method="post" action="/admin/ai/layer" class="init-form">'
        f'<input type="hidden" name="enabled" value="{"0" if on else "1"}">'
        f'<button type="submit">{"停止する" if on else "有効にする"}</button></form>'
        '<p class="muted">元栓。止めると、下で有効にしてある相手があっても'
        " <code>/v1/ask</code>・<code>/ai/chat</code> は 503 になる"
        "(相手を 1 つずつ切って回らずに、機能ごと止めたいとき用)。</p></div>"
    )

    rows = []
    for r in await _rows():
        spec = r["spec"]
        state = "使える" if r["runnable"] else (r["blocked"] or "無効")

        if not r["takes_key"]:
            # **「鍵が要らない」のではなく「chiezo-api が持てない」。** CLI の認証情報は
            # ブリッジのコンテナが使うもので、別コンテナへ環境変数を注入する手段が無いため
            # .env から compose 経由で渡す。「不要」とだけ書くと何も要らないと読めてしまう。
            key_cell = '<span class="muted">ブリッジ側（.env）</span>'
        elif not r["key_required"]:
            key_cell = "登録済み" if r["has_key"] else "未登録"
            key_cell += '<br><span class="muted">認証を掛けているときだけ</span>'
        elif r["has_key"]:
            key_cell = (
                f"登録済み<br><span class=\"muted\">{esc(r['updated_at'])}</span>"
                f'<form method="post" action="/admin/ai/key" class="init-form"'
                f" onsubmit=\"return confirm('API キーを削除して無効にします。よろしいですか?')\">"
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="hidden" name="action" value="delete">'
                f"<button type=\"submit\">削除</button></form>"
            )
        else:
            key_cell = "未登録"

        # 入力欄は details に畳む。常設すると、鍵の要る相手の数だけ表が縦に伸びる。
        if r["takes_key"]:
            key_cell += (
                f"<details><summary>{'更新' if r['has_key'] else '登録'}する</summary>"
                f'<p class="muted">{esc(spec.setup)}</p>'
                f'<form method="post" action="/admin/ai/key">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="text" name="api_key" placeholder="API キー" required>'
                f"<button type=\"submit\">保存</button></form>"
                f'<p class="muted">保存しても有効にはなりません(有効化は右のボタン)。'
                f"値は画面に二度と表示しません。</p></details>"
            )

        toggle = (
            f'<form method="post" action="/admin/ai/enabled" class="init-form">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f'<input type="hidden" name="enabled" value="{"0" if r["enabled"] else "1"}">'
            f"<button type=\"submit\"{'' if (r['enabled'] or r['can_enable']) else ' disabled'}>"
            f"{'無効にする' if r['enabled'] else '話せるようにする'}</button></form>"
        )
        # 手順は鍵の欄の details に出ているので、ここには繰り返さない
        # （同じ長文が 1 行に 2 回出て、表が読めない高さになる）。
        rows.append(
            f"<tr><td>{esc(spec.label)}</td><td>{esc(state)}</td>"
            f"<td>{key_cell}</td><td>{toggle}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    return f"""<h2>話す相手</h2>
{master}
<details>
<summary>この節について</summary>
<p>Chiezo にためた知識を引ける AI をここで増やす。<strong>相手の URL は決まっているので設定に出さない</strong>
(<code>api/app/providers.py</code> に決め打ち)—— 入れるのは API キーと、使うかどうかだけ。</p>
<p>どのモデルを使うかは<strong>会話のたびに選べる</strong>(<a href="{CHAT_PATH}">AI と話す</a>の画面)。</p>
<p><strong>Claude Code / Codex CLI は CLI なので、別コンテナ(ブリッジ)を立てて使う。</strong>
<code>docker-compose.yml</code> の該当サービスのコメントを外して起動すると、ここが押せるようになる。
<strong>認証情報はこの画面から登録する</strong> —— ブリッジが設定 DB を読み取り専用でマウントして
読むので、登録すればブリッジの再起動なしで効く。</p>
<p class="stale">⚠️ Chiezo は認証なし・LAN 内前提。ここに入れた API キーは、この画面を開ける人なら
誰でも差し替えられる(値は表示しないが、書き換えは防げない)。</p>
</details>
<table class="ai-settings">
<thead><tr><th>AI</th><th>状態</th><th>API キー</th><th>使う</th><th>課金の形</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
"""


def _require_provider(provider: str) -> providers.Provider:
    spec = providers.get(provider)
    if spec is None:
        raise HTTPException(404, {"error": f"unknown provider: {provider}"})
    return spec


@router.post("/admin/ai/key")
async def set_key(provider: str = Form(...), api_key: str = Form(""), action: str = Form("")):
    """API キーの登録・削除。

    削除を同じ入口にまとめてあるのは、鍵を消したら同時に無効にする必要があるため
    （鍵の無い相手を有効のまま残すと、会話のたびに失敗するだけになる）。
    """
    spec = _require_provider(provider)
    settings_store.require_path()
    if spec.key == providers.KEY_NONE:
        raise HTTPException(400, {"error": f"「{spec.label}」は API キーを使いません"})
    if action == "delete":
        settings_store.clear_api_key(spec.id)
    else:
        value = api_key.strip()
        if not value:
            raise HTTPException(400, {"error": "API キーが空です"})
        settings_store.set_api_key(spec.id, value)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/enabled")
async def set_enabled(provider: str = Form(...), enabled: str = Form("0")):
    """on/off の切り替え。**on にできない相手は on にしない**（画面側の抑止の裏打ち）。"""
    spec = _require_provider(provider)
    settings_store.require_path()
    want = enabled == "1"
    if want:
        st = settings_store.load(spec.id)
        if spec.key == providers.KEY_REQUIRED and not st.has_key:
            raise HTTPException(400, {"error": f"先に「{spec.label}」の API キーを登録してください"})
        if spec.probe and not await answer.reachable(providers.url_of(spec)):
            raise HTTPException(400, {"error": f"「{spec.label}」に到達できません", "hint": spec.setup})
    settings_store.set_enabled(spec.id, want)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/layer")
async def set_layer(enabled: str = Form("1")):
    """「答える」層そのものの on/off（元栓）。"""
    settings_store.require_path()
    settings_store.set_answer_enabled(enabled == "1")
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/ai/models")
async def list_models(request: Request, backend: str = ""):
    """会話画面がモデルのセレクトを組み立てるために引く。

    相手に聞けたらその一覧、聞けなければ `app/providers.py` の控えを返す。
    """
    name = answer.normalize_backend(backend)
    if name not in answer.backend_names():
        raise HTTPException(404, {"error": f"unknown backend: {name}"})
    return {"backend": name, "models": await answer.available_models(name)}
