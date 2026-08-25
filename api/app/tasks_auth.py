"""やること層の認証(Google OAuth)。**外に出す面はここだけが守る**。

知識ベース本体には認証が無い(LAN 内前提)。こちらは外に出すので、cc-tasks が
持っていた守りをそのまま移してある —— 許可メール 1 件、CSRF の二重送信、
ログイン試行のレート制限、リダイレクト URI のホスト検証。

## 開いていないことを既定にする

`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `ALLOWED_EMAIL` のどれかが欠けていたら、
**ログインを組み立てられないので `/api/**` は 401 を返し続ける**。「設定が無いから
素通しする」は外に出す面では致命的なので、足りないときは閉じる側に倒す。

## id_token を自分で検証しない

`code` の交換も利用者情報の取得も**サーバーからサーバーへ TLS で直接**行う
(`token` エンドポイント → `userinfo` エンドポイント)。ブラウザを経由しないので、
署名を検証する JWT を扱う必要がそもそも無い。pyjwt は依存にあるが、
**検証を省いた `jwt.decode` を書かずに済ませる**ほうが読み手に優しい。

## ヘッダを自分で読まない

レート制限のキーもリダイレクト URI のホストも `request` から取る。
`X-Forwarded-For` を自分で読んではいけない —— ヘッダは攻撃者が自由に付けられ、
プロキシは消さずに後ろへ追記するので、先頭を採ると毎リクエスト別バケットになって
制限が丸ごと無効化される。**リバースプロキシの後ろに置くときは uvicorn を
`--proxy-headers --forwarded-allow-ips=<プロキシのIP>` で起動すること**
(そうすると信頼できる値だけが `request.client` / `request.url` に入る)。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

log = logging.getLogger("chiezo.tasks")

AUTHORIZE_PATH = "/oauth2/authorization/google"
CALLBACK_PATH = "/login/oauth2/code/google"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

SESSION_COOKIE = "CHIEZOTASKSSESSION"
CSRF_COOKIE = "XSRF-TOKEN"
CSRF_HEADER = "X-XSRF-TOKEN"
LOGIN_COOKIE = "CHIEZOTASKSLOGIN"

SESSION_TTL = timedelta(days=30)
# 認可へ飛ばしてから戻ってくるまで。Google の画面で迷っても切れない程度に取る。
LOGIN_TTL = timedelta(minutes=10)

# 画像は Google のプロフィール画像だけ通す。form-action に accounts.google.com が
# 要るのは、ログインの遷移がフォーム POST を経由するため。
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://lh3.googleusercontent.com; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self' https://accounts.google.com; "
    "frame-ancestors 'none'"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _same(sent: str | None, expected: str | None) -> bool:
    """定数時間で突き合わせる。**バイト列にしてから比べる**。

    `secrets.compare_digest` は非 ASCII の str を渡すと TypeError を投げるので、
    外から来た値(`state`・CSRF トークン)をそのまま渡すと、非 ASCII を 1 文字
    混ぜられただけで 500 になる。UTF-8 に符号化すれば中身を問わず比べられる。
    """
    return secrets.compare_digest((sent or "").encode("utf-8"), (expected or "").encode("utf-8"))


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# ---- 設定 -------------------------------------------------------------------


class Config:
    """環境変数から読む。**プロセスの寿命ではなく都度読む**(テストが差し替えられる)。"""

    @property
    def client_id(self) -> str:
        return _env("GOOGLE_CLIENT_ID")

    @property
    def client_secret(self) -> str:
        return _env("GOOGLE_CLIENT_SECRET")

    @property
    def allowed_email(self) -> str:
        return _env("ALLOWED_EMAIL")

    @property
    def public_base_url(self) -> str:
        return _env("PUBLIC_BASE_URL").rstrip("/")

    @property
    def allowed_redirect_hosts(self) -> list[str]:
        raw = _env("ALLOWED_REDIRECT_HOSTS")
        return [h.strip() for h in raw.split(",") if h.strip()]

    @property
    def rate_limit_per_minute(self) -> int:
        try:
            value = int(_env("LOGIN_RATE_LIMIT_PER_MINUTE", "20"))
        except ValueError:
            return 20
        return value if value > 0 else 20

    @property
    def dev(self) -> bool:
        """認証を通さない開発モード。**本番で立ててはいけない**(既定は off)。"""
        return _env("CHIEZO_TASKS_DEV").lower() in {"1", "true", "yes"}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.allowed_email)


config = Config()


# ---- セッションの置き場 -----------------------------------------------------
#
# 設定(`app/settings_store.py`)とは別のファイルにする。あちらは CLI ブリッジの
# コンテナが読み取り専用でマウントするために `journal_mode=DELETE` に固定してあり、
# ログインのたびに書き換わるものを同居させたくない。

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    email        TEXT,
    name         TEXT,
    picture_url  TEXT,
    state        TEXT,
    code_verifier TEXT,
    redirect_uri TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""


def state_dir() -> Path | None:
    raw = _env("CHIEZO_STATE_DIR")
    return Path(raw) if raw else None


def db_path() -> Path | None:
    directory = state_dir()
    return directory / "tasks_sessions.db" if directory else None


def _connect() -> sqlite3.Connection | None:
    path = db_path()
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _sweep(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_iso(_now()),))


def _store(kind: str, ttl: timedelta, **fields) -> str | None:
    conn = _connect()
    if conn is None:
        return None
    session_id = secrets.token_urlsafe(32)
    try:
        with conn:
            _sweep(conn)
            conn.execute(
                "INSERT INTO sessions (id, kind, email, name, picture_url, state, code_verifier,"
                " redirect_uri, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, kind, fields.get("email"), fields.get("name"),
                    fields.get("picture_url"), fields.get("state"), fields.get("code_verifier"),
                    fields.get("redirect_uri"), _iso(_now()), _iso(_now() + ttl),
                ),
            )
    finally:
        conn.close()
    return session_id


def _load(session_id: str | None, kind: str) -> sqlite3.Row | None:
    if not session_id:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn:
            _sweep(conn)
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND kind = ?", (session_id, kind)
        ).fetchone()
    finally:
        conn.close()
    return row


def _drop(session_id: str | None) -> None:
    if not session_id:
        return
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    finally:
        conn.close()


# ---- ログイン試行のレート制限 -----------------------------------------------


class _RateLimiter:
    """IP あたりのトークンバケット。利用者 1 人・単一プロセス前提なのでメモリで足りる。"""

    MAX_TRACKED = 10_000

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, per_minute: int) -> bool:
        if len(self._buckets) > self.MAX_TRACKED:
            self._buckets.clear()
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(per_minute), now))
        tokens = min(float(per_minute), tokens + (now - last) * per_minute / 60.0)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


limiter = _RateLimiter()


# ---- リダイレクト URI -------------------------------------------------------


def resolve_redirect_uri(request: Request) -> str | None:
    """コールバックの絶対 URL。作れないときは None。

    `PUBLIC_BASE_URL` があればそれが最優先。無ければリクエストのホストから組むが、
    `ALLOWED_REDIRECT_HOSTS` を設定してあるならそこに載っているホストだけ採用する
    (Host ヘッダ注入で別のところへ飛ばされないようにするため)。
    """
    if config.public_base_url:
        return f"{config.public_base_url}{CALLBACK_PATH}"
    host = request.url.netloc
    allowed = config.allowed_redirect_hosts
    if allowed and host not in allowed:
        log.warning("許可されていないホストからのログイン開始: %s", host)
        return None
    if not allowed:
        log.warning(
            "PUBLIC_BASE_URL も ALLOWED_REDIRECT_HOSTS も未設定。"
            " リダイレクト URI をリクエストのホスト(%s)から組み立てる", host
        )
    return f"{request.url.scheme}://{host}{CALLBACK_PATH}"


# ---- ルート -----------------------------------------------------------------

router = APIRouter()


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


@router.get(AUTHORIZE_PATH)
async def start_login(request: Request):
    if config.dev:
        return RedirectResponse("/", status_code=302)
    if not config.configured:
        return _error(503, "not_configured",
                      "ログインが設定されていません(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / ALLOWED_EMAIL)")
    redirect_uri = resolve_redirect_uri(request)
    if redirect_uri is None:
        return _error(400, "bad_request", "このホストからはログインできません")

    state = secrets.token_urlsafe(24)
    # PKCE。認可コードを横取りされても、verifier が無ければ交換できない
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    login_id = _store("login", LOGIN_TTL, state=state, code_verifier=verifier, redirect_uri=redirect_uri)
    if login_id is None:
        return _error(503, "not_configured", "CHIEZO_STATE_DIR が未設定なのでログインを保持できません")

    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}", status_code=302)
    response.set_cookie(
        LOGIN_COOKIE, login_id, max_age=int(LOGIN_TTL.total_seconds()),
        httponly=True, samesite="lax", secure=_secure(request), path="/",
    )
    return response


@router.get(CALLBACK_PATH)
async def finish_login(request: Request):
    pending = _load(request.cookies.get(LOGIN_COOKIE), "login")
    _drop(request.cookies.get(LOGIN_COOKIE))
    if pending is None:
        return _error(400, "bad_request", "ログインの手続きが切れています。もう一度お試しください")
    # state はブラウザ経由で戻ってくるので、保存しておいた値と突き合わせる(CSRF 対策)
    if not _same(request.query_params.get("state"), pending["state"]):
        return _error(400, "bad_request", "ログインの検証に失敗しました")
    code = request.query_params.get("code", "")
    if not code:
        return _error(400, "bad_request", "ログインが中断されました")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_res = await client.post(GOOGLE_TOKEN_URL, data={
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": pending["redirect_uri"],
                "grant_type": "authorization_code",
                "code_verifier": pending["code_verifier"],
            })
            token_res.raise_for_status()
            access_token = token_res.json().get("access_token")
            if not access_token:
                return _error(403, "forbidden", "このアカウントではログインできません")
            user_res = await client.get(
                GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            user_res.raise_for_status()
            profile = user_res.json()
    except httpx.HTTPError:
        log.warning("Google との通信に失敗した", exc_info=True)
        return _error(503, "unavailable", "ログインに失敗しました。しばらくしてからお試しください")

    email = (profile.get("email") or "").strip()
    # **メールだけで人を同定する設計**なので、未検証のアドレスを受け入れると
    # 「他人のアドレスを名乗るアカウント」で入れてしまう
    if not profile.get("email_verified") or not email:
        return _error(403, "forbidden", "このアカウントではログインできません")
    if not _same(email.lower(), config.allowed_email.lower()):
        log.warning("許可されていないアカウントのログイン試行")
        return _error(403, "forbidden", "このアカウントではログインできません")

    session_id = _store(
        "user", SESSION_TTL,
        email=email, name=profile.get("name"), picture_url=profile.get("picture"),
    )
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE, session_id, max_age=int(SESSION_TTL.total_seconds()),
        httponly=True, samesite="lax", secure=_secure(request), path="/",
    )
    return response


@router.get("/api/me")
async def me(request: Request):
    user = getattr(request.state, "user", None)
    if user is None:
        return _error(401, "unauthorized", "ログインが必要です")
    return {"email": user["email"], "name": user["name"], "pictureUrl": user["pictureUrl"]}


@router.post("/api/logout", status_code=204)
async def logout(request: Request):
    _drop(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---- ミドルウェア -----------------------------------------------------------

# ログインを要らないことにする口。ログアウトは何度呼んでも 204 にしたいので入れる
# (ただし CSRF は免除しない —— 免除すると外から勝手にログアウトさせられる)。
# SPA の殻と PWA の資材はそもそも `/api/` の外なので、ここに並べる必要はない。
NO_AUTH_PATHS = frozenset({"/api/logout"})

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def install(app: FastAPI) -> None:
    """認証・CSRF・レート制限・セキュリティヘッダをアプリに仕込む。"""
    app.include_router(router)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path

        # ログインの乱打を IP 単位で抑える
        if path in {AUTHORIZE_PATH, CALLBACK_PATH}:
            client = request.client.host if request.client else "unknown"
            if not limiter.allow(f"login:{client}", config.rate_limit_per_minute):
                return _headers(_error(429, "rate_limited", "ログイン試行が多すぎます"), request)

        user = None
        session_id = request.cookies.get(SESSION_COOKIE)
        if config.dev:
            user = {"email": "dev@example.com", "name": "dev", "pictureUrl": None}
        else:
            row = _load(session_id, "user")
            if row is not None:
                user = {"email": row["email"], "name": row["name"], "pictureUrl": row["picture_url"]}
        request.state.user = user

        if path.startswith("/api/"):
            if user is None and path not in NO_AUTH_PATHS:
                return _headers(_error(401, "unauthorized", "ログインが必要です"), request)
            # CSRF は二重送信で見る。Cookie は JS から読める必要があるので HttpOnly にしない。
            # dev では外さないと curl で叩けない(cc-tasks の dev プロファイルと同じ)。
            if request.method not in SAFE_METHODS and not config.dev:
                sent = request.headers.get(CSRF_HEADER, "")
                expected = request.cookies.get(CSRF_COOKIE, "")
                if not expected or not _same(sent, expected):
                    return _headers(
                        _error(403, "forbidden", "このリクエストは許可されていません"), request
                    )

        response = await call_next(request)
        if not request.cookies.get(CSRF_COOKIE):
            response.set_cookie(
                CSRF_COOKIE, secrets.token_urlsafe(24),
                httponly=False, samesite="lax", secure=_secure(request), path="/",
            )
        return _headers(response, request)


def _headers(response, request: Request):
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if _secure(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
