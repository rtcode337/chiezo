"""各 AI の使用量 —— 相手が言う枠と、Chiezo が使ったぶん。

2 つを別々に出す。混ぜると読めなくなる。

| | 何の数か | 分かること | 聞ける相手 |
|---|---|---|---|
| 枠(quota) | 相手の勘定 | 残り(使用率と、いつ戻るか) | 下の表の 3 つだけ |
| 使ったぶん(spent) | Chiezo の勘定 | 回数とトークン。残りは分からない | 全部 |

枠を聞ける相手と、その聞き方(`app/providers.py` の `usage`):

- Codex CLI …… ブリッジの `/usage`(`codex app-server` の `account/rateLimits/read`)。
  手元に控えた auth.json は期限切れになる —— CLI に聞けば更新はあちらがやる
- Antigravity CLI …… ブリッジの `/usage`(`agy` の print モード)。
  残クレジットを取る RPC はあるが、外から叩ける口としては公開されていない
- OpenRouter …… `GET /api/v1/key`。クレジットの使用額と残高がそのまま返る

Gemini・OpenAI・推論サーバには口が無い(Gemini の残量は Google Cloud の Quotas API 側、
OpenAI の使用量は Admin キーが要る)。「出せない」と画面に書く ——
空欄にすると「使っていない」と読めてしまう。

Claude Code CLI もここに入る。 CLI に出口が無く(サブコマンドが無く、`/usage` は
対話画面の中だけ)、CLI 自身が叩いている口は `user:profile` を要求するのに対し、
Chiezo が預かるのは `claude setup-token` の長期トークン —— 推論だけに絞られていて
このスコープを持たない(実測 HTTP 403)。取れないものをエラーとして出し続けるより、
「出さない相手」と書くほうが正しい(詳しくは docs/ai.md)。

枠を取るのは押されたときだけ。 管理画面は描画のたびに相手へ問い合わせない
(「接続を試す」と同じ流儀)—— 落ちている相手がいると、その数だけ画面が遅れる。
控えてある値と「いつ取ったか」を出し、取り直しはボタンで。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from app import media_providers, providers, settings_store, usage_store

log = logging.getLogger("chiezo.usage")

# Claude のサブスクの枠を引く口。Claude Code の `/usage` が叩いているのと同じで、
# 公開ドキュメントには載っていない(CLI の中から見つけたもの)。相手の都合で消えうるので、
# 取れなかった理由をそのまま画面に出す作りにしてある。

# 相手へ聞きに行くときの上限秒数。短くする —— 押した人を待たせる操作で、
# しかも 1 つ落ちていても他は出したい。
TIMEOUT = 10.0

# Chiezo が使ったぶんを数える窓。先頭は 5 時間 —— サブスクの「セッション」が
# その長さなので、枠の数字と並べたときに同じ期間の話になる。
SPENT_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("5h", timedelta(hours=5)),
    ("24h", timedelta(days=1)),
    ("7d", timedelta(days=7)),
)


@dataclass
class Window:
    """枠 1 つぶん。相手ごとに単位が違うので、割合と実数の両方を持てる形にしてある。

    - 使用率で言う相手(Claude・Codex)…… `used_percent`
    - 金額で言う相手(OpenRouter)…… `used` / `limit` / `unit`
    """

    id: str
    label: str
    used_percent: float | None = None
    resets_at: str = ""
    used: float | None = None
    limit: float | None = None
    unit: str = ""

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, round(100.0 - self.used_percent, 1))

    def as_dict(self) -> dict:
        return {**asdict(self), "remaining_percent": self.remaining_percent}


@dataclass
class Quota:
    """相手から聞いた枠の控え。"""

    supported: bool = False
    fetched_at: str = ""
    error: str = ""
    windows: list[Window] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "supported": self.supported,
            "fetched_at": self.fetched_at,
            "error": self.error,
            "windows": [w.as_dict() for w in self.windows],
        }


class UsageError(Exception):
    """枠を取れなかった理由(そのまま画面に出る)。"""


def _window_from(raw: dict) -> Window | None:
    """控えから窓を組み直す。知らないキーは捨てる —— 版が変わって項目が増えても、
    古い控えを読んだ瞬間に画面ごと落ちないようにするため。"""
    fields = set(Window.__dataclass_fields__)
    known = {k: v for k, v in raw.items() if k in fields}
    if not known.get("id"):
        return None
    return Window(**known)


def _client(headers: dict[str, str] | None = None, timeout: float = TIMEOUT) -> httpx.AsyncClient:
    """相手へ聞きに行くための HTTP クライアント。

    テストはここを差し替える(`app/answer.py` の `_llm_client` と同じ流儀)——
    偽のサーバを立てずに、応答の読み方まで通しで確かめられるようにするため。
    """
    return httpx.AsyncClient(timeout=timeout, headers=headers or {})


def _percent(value) -> float | None:
    """0〜100 の使用率として読む。読めない値は None(0 と混ぜない)。"""
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _iso(value) -> str:
    """相手が返す時刻を ISO に揃える。unix 秒でも ISO 文字列でも受ける。"""
    if value in (None, ""):
        return ""
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC).isoformat(timespec="seconds")
    return str(value)


def _window_label(minutes: float | None, fallback: str) -> str:
    """窓の長さから名前を作る。相手は名前を持たない(primary / secondary としか
    言わない)ので、長さで呼ぶ —— どちらが 5 時間でどちらが週かは、そこにしか無い。"""
    if not minutes:
        return fallback
    if minutes % (60 * 24) == 0:
        return f"直近 {int(minutes // (60 * 24))} 日"
    if minutes % 60 == 0:
        return f"直近 {int(minutes // 60)} 時間"
    return f"直近 {int(minutes)} 分"


# ---- 相手ごとの聞き方 -------------------------------------------------------


async def _elevenlabs(spec, credential: str) -> list[Window]:
    """ElevenLabs の枠。声・効果音・曲・絵・動画が同じ 1 つの残量を食う。

    `GET /v1/user/subscription` は鍵だけで引ける —— 生成も会話もしないので、
    確かめるたびに枠が減ることはない。
    """
    url = f"{media_providers.url_of(spec).rstrip('/')}/user/subscription"
    try:
        async with _client({"xi-api-key": credential}) as client:
            res = await client.get(url)
    except httpx.HTTPError as e:
        raise UsageError(f"つながりません: {e}") from None
    if res.status_code in (401, 403):
        raise UsageError(f"認証情報が受け付けられませんでした(HTTP {res.status_code})")
    if res.status_code >= 400:
        raise UsageError(f"HTTP {res.status_code}: {res.text[:300]}")
    try:
        body = res.json()
    except ValueError:
        raise UsageError("応答を JSON として読めませんでした") from None

    # 相手は文字数(`character_*`)と呼ぶが、料金表とダッシュボードの呼び名はクレジット。
    # 効果音や曲も同じ勘定から引かれるので、画面ではクレジットと書く。
    used = _amount(body.get("character_count"))
    limit = _amount(body.get("character_limit"))
    if used is None and limit is None:
        raise UsageError(f"枠の項目が見当たりません: {str(body)[:200]}")
    tier = str(body.get("tier") or "").strip()
    return [Window(
        id="credits",
        label=f"クレジット({tier})" if tier else "クレジット",
        used_percent=round(min(100.0, used / limit * 100.0), 1) if used is not None and limit else None,
        resets_at=_iso(body.get("next_character_count_reset_unix")),
        used=used,
        limit=limit,
        unit="クレジット",
    )]


def _amount(value) -> float | None:
    """数として読む。読めない値は None(0 と混ぜない)。"""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def _openrouter(spec: providers.Provider, credential: str) -> list[Window]:
    """OpenRouter のクレジット。使った額と、上限があれば残高。

    上限が無い鍵もある(従量課金)ので、`limit` が null のときは使用額だけ出す ——
    残りを 0 と書くと、使い切ったように読める。
    """
    url = f"{providers.url_of(spec).rstrip('/')}/key"
    try:
        async with _client({"Authorization": f"Bearer {credential}"}) as client:
            res = await client.get(url)
    except httpx.HTTPError as e:
        raise UsageError(f"つながりません: {e}") from None
    if res.status_code in (401, 403):
        raise UsageError(f"認証情報が受け付けられませんでした(HTTP {res.status_code})")
    if res.status_code >= 400:
        raise UsageError(f"HTTP {res.status_code}: {res.text[:200]}")
    try:
        data = res.json().get("data") or {}
    except ValueError:
        raise UsageError("応答を JSON として読めませんでした") from None

    used = data.get("usage")
    limit = data.get("limit")
    used_f = float(used) if isinstance(used, int | float) else None
    limit_f = float(limit) if isinstance(limit, int | float) else None
    percent = None
    if used_f is not None and limit_f:
        percent = round(min(100.0, used_f / limit_f * 100.0), 1)
    return [
        Window(
            id="credits",
            label="クレジット" + ("(上限なし)" if limit_f is None else ""),
            used_percent=percent,
            used=used_f,
            limit=limit_f,
            unit="USD",
        )
    ]


async def _bridge(spec: providers.Provider) -> list[Window]:
    """CLI ブリッジに聞く(ブリッジが CLI に聞く)。

    ブリッジが立っていなければ取れない。 枠は CLI の中にしか無いので、
    ここは「相手が立っていること」が前提になる —— 立っていないことも理由として出す。
    """
    base = providers.url_of(spec).rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        async with _client(timeout=max(TIMEOUT, 45.0)) as client:
            res = await client.get(f"{base}/usage")
    except httpx.HTTPError as e:
        raise UsageError(f"ブリッジにつながりません: {e}") from None
    if res.status_code == 404:
        raise UsageError("このブリッジは使用量を出せません(CLI に聞く口がない)")
    try:
        body = res.json()
    except ValueError:
        raise UsageError(f"HTTP {res.status_code}: 応答を JSON として読めませんでした") from None
    if res.status_code >= 400 or body.get("error"):
        raise UsageError(_bridge_error(body, res.status_code))

    windows = []
    for entry in body.get("windows") or []:
        if not isinstance(entry, dict):
            continue
        minutes = entry.get("window_minutes")
        windows.append(
            Window(
                id=str(entry.get("id") or "window"),
                label=str(entry.get("label") or "")
                or _window_label(minutes, str(entry.get("id") or "枠")),
                used_percent=_percent(entry.get("used_percent")),
                resets_at=_iso(entry.get("resets_at")),
                used=entry.get("used") if isinstance(entry.get("used"), int | float) else None,
                limit=entry.get("limit") if isinstance(entry.get("limit"), int | float) else None,
                unit=str(entry.get("unit") or ""),
            )
        )
    if not windows:
        raise UsageError(str(body.get("reason") or "CLI が使用量を返しませんでした")[:300])
    return windows


def _bridge_error(body: dict, status: int) -> str:
    """ブリッジが返した理由を取り出す。

    FastAPI は `HTTPException` の中身を `detail` に包むので、そこまで見ないと
    「HTTP 401」としか出せない —— 打つ手が分かるのは中の文言のほう。
    """
    detail = body.get("detail")
    if isinstance(detail, dict):
        detail = detail.get("error") or detail
    reason = body.get("error") or detail
    return (str(reason) if reason else f"HTTP {status}")[:300]


async def fetch(spec: providers.Provider) -> list[Window]:
    """その相手の枠を取りに行く。取れなければ `UsageError`。"""
    if not spec.usage:
        raise UsageError("この相手は使用量を出しません")
    credential = settings_store.load(spec.id).credential
    if spec.usage == providers.USAGE_OPENROUTER:
        if not credential:
            raise UsageError("認証情報が未登録です")
        return await _openrouter(spec, credential)
    if spec.usage == providers.USAGE_BRIDGE:
        return await _bridge(spec)
    if spec.usage == media_providers.USAGE_ELEVENLABS:
        if not credential:
            raise UsageError("認証情報が未登録です")
        return await _elevenlabs(spec, credential)
    raise UsageError(f"未対応の聞き方: {spec.usage}")


def spec_of(provider_id: str):
    """枠を聞ける相手を引く。 絵と音だけの相手(ElevenLabs)も同じ入口で扱う ——
    呼ぶ側が「どちらの表にいるか」を知らずに済むようにするため。"""
    return providers.get(provider_id) or media_providers.get(provider_id)


async def refresh(provider_id: str) -> Quota:
    """1 相手ぶん取り直して控える。失敗も控える(理由を画面に出すため)。"""
    spec = spec_of(provider_id)
    if spec is None or not spec.usage:
        return Quota(supported=False)
    try:
        windows = await fetch(spec)
    except UsageError as e:
        usage_store.save_quota(spec.id, [], str(e))
        stored = usage_store.load_quota().get(spec.id, {})
        return Quota(
            supported=True,
            fetched_at=stored.get("fetched_at", ""),
            error=str(e),
            windows=_windows_from(stored.get("windows", [])),
        )
    usage_store.save_quota(spec.id, [asdict(w) for w in windows])
    return Quota(supported=True, fetched_at=_now_iso(), windows=windows)


def _windows_from(raw: list) -> list[Window]:
    out = [_window_from(w) for w in raw if isinstance(w, dict)]
    return [w for w in out if w is not None]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---- まとめて出す -----------------------------------------------------------


def _stored_quota(provider_id: str, spec_usage: str, stored: dict) -> Quota:
    """控えてある枠を読む(取りに行かない)。"""
    if not spec_usage:
        return Quota(supported=False)
    row = stored.get(provider_id)
    if row is None:
        return Quota(supported=True)
    return Quota(
        supported=True,
        fetched_at=row.get("fetched_at", ""),
        error=row.get("error", ""),
        windows=_windows_from(row.get("windows", [])),
    )


def _spent_all() -> dict[str, dict[str, usage_store.Spent]]:
    """窓ごとに「相手 → 使ったぶん」を集める。"""
    now = datetime.now(UTC)
    out: dict[str, dict[str, usage_store.Spent]] = {}
    for name, span in SPENT_WINDOWS:
        for provider, value in usage_store.spent(now - span).items():
            out.setdefault(provider, {})[name] = value
    return out


def rows() -> list[dict]:
    """画面と API が使う 1 行ぶんずつ。相手へは問い合わせない(控えを読むだけ)。

    絵と音だけの相手(自前の GPU・ElevenLabs)も並べる —— あちらも呼べば回数が
    増えるので、話す相手だけ出すと「頼んだはずの回数が出てこない」ことになる。
    """
    stored = usage_store.load_quota()
    spent = _spent_all()
    settings = settings_store.load_all()

    out = []
    for spec in providers.all_providers():
        st = settings.get(spec.id)
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "billing": spec.billing,
                "enabled": bool(st and st.enabled),
                "quota": _stored_quota(spec.id, spec.usage, stored),
                "spent": spent.get(spec.id, {}),
            }
        )
    for pid, label in media_providers.standalone_labels().items():
        st = settings.get(pid)
        out.append(
            {
                "id": pid,
                "label": label or pid,
                "billing": "",
                "enabled": bool(st and st.enabled),
                # 枠を聞ける相手はここにもいる(ElevenLabs)。自前の GPU は口が無い。
                "quota": _stored_quota(
                    pid, getattr(media_providers.get(pid), "usage", ""), stored
                ),
                "spent": spent.get(pid, {}),
            }
        )
    return out


async def refresh_all() -> None:
    """枠を聞ける相手を並行に取り直す。直列だと落ちている相手の数だけ待つ。"""
    targets = [p.id for p in providers.all_providers() if p.usage]
    targets += [pid for pid in media_providers.standalone_labels()
                if getattr(media_providers.get(pid), "usage", "")]
    await asyncio.gather(*(refresh(pid) for pid in targets), return_exceptions=True)
