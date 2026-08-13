"""管理画面の「話す相手」節（一覧の描画と、on/off・認証情報の受け口）。

**設定は環境変数ではなくここから入れる。** URL と表示名は `app/providers.py` に
決め打ちしてあり、ユーザーが決めるのは on/off・認証情報・既定のモデルだけ。
保存先は `app/settings_store.py`（`state/settings.db`）。

on にできる条件を画面側でも守る:

- 認証情報の要る相手… **未登録なら on にできない**
- CLI ブリッジ（Claude Code / Codex CLI）… **コンテナが立っていなければ on にできない**
  （compose のコメントを外していなければ立っていない。立っていない相手を有効にしても
  会話のたびに失敗するだけなので、押させない）

どちらも `app/answer.py` 側でも弾く（設定を直に書き換えられた場合の保険）。
"""
from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import answer, providers, settings_store
from app.pages import CHAT_PATH, esc

router = APIRouter()


def _rows() -> list[dict]:
    """一覧に出す 1 行ぶんずつ。

    **画面を描くときに相手へ問い合わせない。** 以前は毎回ブリッジの到達確認をしていたが、
    立っていない相手の数だけ表示が遅れるうえ、「到達できる」と「話せる」は別物だった
    （認証情報が間違っていても到達はする）。いまは**「接続を試す」が通った記録**だけを見る。
    """
    stored = settings_store.load_all()

    rows = []
    for spec in providers.all_providers():
        st = stored.get(spec.id, settings_store.ProviderSetting(spec.id))
        blocked = ""
        if spec.credential == providers.CRED_REQUIRED and not st.has_credential:
            blocked = "認証情報が未登録"
        elif not st.verified:
            blocked = "未確認（接続を試す）"
        rows.append(
            {
                "spec": spec,
                "enabled": st.enabled,
                "has_credential": st.has_credential,
                # 認証情報の欄を出すか（要る相手と、任意で入れられる相手）
                "takes_credential": spec.credential != providers.CRED_NONE,
                # 認証情報が無いと on にできないか（任意の相手は無くても on にできる）
                "credential_required": spec.credential == providers.CRED_REQUIRED,
                "model": st.model,
                "updated_at": st.updated_at[:19],
                # 「接続を試す」が通った記録。**これが on を押せる条件**
                "verified": st.verified,
                "verified_at": st.verified_at[:19],
                # can_enable が偽なら on のボタンを押させない
                "can_enable": not blocked,
                "blocked": blocked,
                "runnable": st.enabled and not blocked,
            }
        )
    return rows


async def section_html(request: Request | None = None) -> str:
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

    # 「接続を試す」の結果。**画面に残すのは 1 回だけ**（リロードで消える）ので、
    # クエリで受け渡す —— セッションを持たない作りに合わせる。
    banner = ""
    q = request.query_params if request is not None else {}
    if tested := q.get("tested"):
        label = esc(providers.label_of(tested))
        if q.get("ok") == "1":
            banner = f'<p class="note">✅ {label} と話せます。</p>'
        else:
            banner = f'<p class="stale">⚠️ {label} と話せません: {esc(q.get("why", ""))}</p>'

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
    for r in _rows():
        spec = r["spec"]
        if r["runnable"]:
            state = "使える"
        elif r["blocked"]:
            state = r["blocked"]
        else:
            state = f"確認済み（{r['verified_at'][:10]}）" if r["verified"] else "無効"

        if not r["takes_credential"]:
            # **「要らない」のではなく「渡すものが無い」。** Antigravity は API キー方式も
            # 持たず、コンテナ内で 1 回サインインした結果を使う。ここで「不要」とだけ書くと
            # 何もしなくてよいと読めてしまうので、何をすればよいかを添える。
            cred_cell = (
                '<span class="muted">渡すものが無い</span>'
                f'<details><summary>使えるようにするには</summary>'
                f'<p class="muted">{esc(spec.setup)}</p></details>'
            )
        elif not r["credential_required"]:
            cred_cell = "登録済み" if r["has_credential"] else "未登録"
            cred_cell += '<br><span class="muted">認証を掛けているときだけ</span>'
        elif r["has_credential"]:
            cred_cell = (
                f"登録済み<br><span class=\"muted\">{esc(r['updated_at'])}</span>"
                f'<form method="post" action="/admin/ai/key" class="init-form"'
                f" onsubmit=\"return confirm('認証情報を削除して無効にします。よろしいですか?')\">"
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="hidden" name="action" value="delete">'
                f"<button type=\"submit\">削除</button></form>"
            )
        else:
            cred_cell = "未登録"

        # 入力欄は details に畳む。常設すると、鍵の要る相手の数だけ表が縦に伸びる。
        if r["takes_credential"]:
            cred_cell += (
                f"<details><summary>{'更新' if r['has_credential'] else '登録'}する</summary>"
                f'<p class="muted">{esc(spec.setup)}</p>'
                f'<form method="post" action="/admin/ai/key">'
                f'<input type="hidden" name="provider" value="{spec.id}">'
                f'<input type="text" name="credential" placeholder="認証情報" required>'
                f"<button type=\"submit\">保存</button></form>"
                f'<p class="muted">保存しても有効にはなりません(有効化は右のボタン)。'
                f"値は画面に二度と表示しません。</p></details>"
            )

        test_btn = (
            f'<form method="post" action="/admin/ai/test" class="init-form">'
            f'<input type="hidden" name="provider" value="{spec.id}">'
            f"<button type=\"submit\">接続を試す</button></form>"
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
            f"<td>{cred_cell}</td><td>{toggle}{test_btn}</td>"
            f'<td class="muted">{esc(spec.billing)}</td></tr>'
        )

    return f"""<h2>話す相手</h2>
{banner}
{master}
<details>
<summary>この節について</summary>
<p>Chiezo にためた知識を引ける AI をここで増やす。<strong>相手の URL は決まっているので設定に出さない</strong>
(<code>api/app/providers.py</code> に決め打ち)—— 入れるのは認証情報と、使うかどうかだけ。</p>
<p>どのモデルを使うかは<strong>会話のたびに選べる</strong>(<a href="{CHAT_PATH}">AI と話す</a>の画面)。</p>
<p><strong>Claude Code / Codex CLI は CLI なので、別コンテナ(ブリッジ)を立てて使う。</strong>
<code>docker-compose.yml</code> の該当サービスのコメントを外して起動すると、ここが押せるようになる。
<strong>認証情報はこの画面から登録する</strong> —— ブリッジが設定 DB を読み取り専用でマウントして
読むので、登録すればブリッジの再起動なしで効く。</p>
<p class="stale">⚠️ Chiezo は認証なし・LAN 内前提。ここに入れた認証情報は、この画面を開ける人なら
誰でも差し替えられる(値は表示しないが、書き換えは防げない)。</p>
</details>
<table class="ai-settings">
<thead><tr><th>AI</th><th>状態</th><th>認証情報</th><th>使う</th><th>課金の形</th></tr></thead>
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
async def set_credential(provider: str = Form(...), credential: str = Form(""), action: str = Form("")):
    """認証情報の登録・削除。

    削除を同じ入口にまとめてあるのは、認証情報を消したら同時に無効にする必要があるため
    （認証情報の無い相手を有効のまま残すと、会話のたびに失敗するだけになる）。
    """
    spec = _require_provider(provider)
    settings_store.require_path()
    if spec.credential == providers.CRED_NONE:
        raise HTTPException(400, {"error": f"「{spec.label}」は認証情報を受け取りません"})
    if action == "delete":
        settings_store.clear_credential(spec.id)
    else:
        value = credential.strip()
        if not value:
            raise HTTPException(400, {"error": "認証情報が空です"})
        settings_store.set_credential(spec.id, value)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/enabled")
async def set_enabled(provider: str = Form(...), enabled: str = Form("0")):
    """on/off の切り替え。**on にできない相手は on にしない**（画面側の抑止の裏打ち）。"""
    spec = _require_provider(provider)
    settings_store.require_path()
    want = enabled == "1"
    if want:
        st = settings_store.load(spec.id)
        if spec.credential == providers.CRED_REQUIRED and not st.has_credential:
            raise HTTPException(400, {"error": f"先に「{spec.label}」の認証情報を登録してください"})
        # **「接続を試す」が一度でも通っていないと on にできない。** 到達できるだけでは
        # 話せる保証にならず（認証情報が間違っていても到達はする）、会話して初めて失敗する。
        if not st.verified:
            raise HTTPException(
                400,
                {"error": f"先に「{spec.label}」の「接続を試す」を通してください", "hint": spec.setup},
            )
    settings_store.set_enabled(spec.id, want)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/layer")
async def set_layer(enabled: str = Form("1")):
    """「答える」層そのものの on/off（元栓）。"""
    settings_store.require_path()
    settings_store.set_answer_enabled(enabled == "1")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/ai/test")
async def test_connection(provider: str = Form(...)):
    """「接続を試す」。**会話は 1 往復もせず**、相手に軽く聞くだけで確かめる。

    - CLI ブリッジ … ブリッジの `/health?check=1`（CLI に `claude auth status` 等を聞かせる）
    - それ以外 … OpenAI 互換の `/models` を引く

    登録の有無ではなく「いま使えるか」を見る。打ち間違えた認証情報や期限切れは前者では
    分からず、会話して初めて失敗する（本番でそれが 502 として出た）。
    """
    spec = _require_provider(provider)
    ok, why = False, ""

    if spec.bridge:
        url = providers.url_of(spec).rstrip("/")
        base = url[: -len("/v1")] if url.endswith("/v1") else url
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.get(f"{base}/health", params={"check": "1"})
            body = res.json()
            ok, why = bool(body.get("authenticated")), str(body.get("reason") or "")
        except (httpx.HTTPError, ValueError) as e:
            ok, why = False, f"ブリッジにつながりません: {e}"
    else:
        cfg = answer.load_settings(spec.id, require_enabled=False)
        if cfg is None:
            ok, why = False, "認証情報が未登録です"
        else:
            ok, why = await answer.check_credential(cfg)

    # **結果を残す。** これが on を押せるかどうかの根拠になる。
    settings_store.set_verified(spec.id, ok)
    params = {"tested": spec.id, "ok": "1" if ok else "0"}
    if why:
        params["why"] = why[:300]
    return RedirectResponse(url="/admin?" + urlencode(params), status_code=303)


@router.get("/ai/models")
async def list_models(request: Request, backend: str = ""):
    """会話画面がモデルとエフォートのセレクトを組み立てるために引く。

    モデルは相手に聞けたらその一覧、聞けなければ `app/providers.py` の控え。
    エフォートは**聞く口が無い**ので控えだけ（持たない相手では空）。
    """
    name = answer.normalize_backend(backend)
    if name not in answer.backend_names():
        raise HTTPException(404, {"error": f"unknown backend: {name}"})
    spec = providers.get(name)
    return {
        "backend": name,
        "models": await answer.available_models(name),
        "efforts": list(providers.efforts_of(name)),
        # CLI ブリッジかどうか（会話画面のトグルの出し分けに使う）
        "bridge": bool(spec and spec.bridge),
    }
