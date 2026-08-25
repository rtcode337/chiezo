"""やること層の認証のテスト。**外に出す面なので、閉じていることを重点的に見る。**"""
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fresh_limiter():
    """レート制限はモジュール変数なので、試験間で持ち越さないように空にする。"""
    from app import tasks_auth

    tasks_auth.limiter._buckets.clear()
    yield


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_EMAIL", "someone@example.com")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ALLOWED_REDIRECT_HOSTS", raising=False)
    monkeypatch.delenv("CHIEZO_TASKS_DEV", raising=False)
    return monkeypatch


@pytest.fixture()
def client(env):
    from app.tasks_app import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


@pytest.fixture()
def logged_in(client):
    from app import tasks_auth

    session_id = tasks_auth._store(
        "user", tasks_auth.SESSION_TTL, email="someone@example.com", name="Someone"
    )
    client.cookies.set(tasks_auth.SESSION_COOKIE, session_id)
    client.cookies.set(tasks_auth.CSRF_COOKIE, "csrf")
    client.headers[tasks_auth.CSRF_HEADER] = "csrf"
    return client


class TestClosedByDefault:
    def test_api_needs_a_session(self, client):
        res = client.get("/api/tasks")
        assert res.status_code == 401 and res.json()["error"]["code"] == "unauthorized"

    def test_every_api_path_is_guarded(self, client):
        for path in ["/api/tasks", "/api/projects", "/api/rules", "/api/me", "/api/tasks/export"]:
            assert client.get(path).status_code == 401, path

    def test_missing_credentials_does_not_open_the_door(self, tmp_path, monkeypatch):
        """設定が無いときに素通しすると、外に出した瞬間に丸見えになる。"""
        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        for name in ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ALLOWED_EMAIL", "CHIEZO_TASKS_DEV"]:
            monkeypatch.delenv(name, raising=False)
        from app.tasks_app import create_app

        with TestClient(create_app(), follow_redirects=False) as c:
            assert c.get("/api/tasks").status_code == 401
            assert c.get("/oauth2/authorization/google").status_code == 503

    def test_dev_mode_is_off_by_default(self, env):
        from app import tasks_auth

        assert tasks_auth.config.dev is False

    def test_a_stale_session_id_is_rejected(self, client):
        from app import tasks_auth

        client.cookies.set(tasks_auth.SESSION_COOKIE, "no-such-session")
        assert client.get("/api/tasks").status_code == 401


class TestCsrf:
    def test_post_without_the_header_is_403(self, logged_in):
        del logged_in.headers["X-XSRF-TOKEN"]
        res = logged_in.post("/api/tasks", json={"title": "あ"})
        assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"

    def test_a_mismatched_token_is_403(self, logged_in):
        logged_in.headers["X-XSRF-TOKEN"] = "another-token"
        assert logged_in.post("/api/tasks", json={"title": "あ"}).status_code == 403

    def test_get_does_not_need_it(self, logged_in):
        del logged_in.headers["X-XSRF-TOKEN"]
        assert logged_in.get("/api/tasks").status_code == 200

    def test_logout_needs_it_too(self, logged_in):
        """免除すると、外のページから勝手にログアウトさせられる。"""
        del logged_in.headers["X-XSRF-TOKEN"]
        assert logged_in.post("/api/logout").status_code == 403

    def test_the_cookie_is_handed_out_and_readable_by_js(self, client):
        res = client.get("/healthz")
        cookie = res.headers.get("set-cookie", "")
        assert "XSRF-TOKEN=" in cookie and "HttpOnly" not in cookie


class TestSession:
    def test_me_returns_the_profile(self, logged_in):
        assert logged_in.get("/api/me").json() == {
            "email": "someone@example.com", "name": "Someone", "pictureUrl": None
        }

    def test_logout_drops_the_session(self, logged_in):
        assert logged_in.post("/api/logout").status_code == 204
        logged_in.headers["X-XSRF-TOKEN"] = "csrf"
        assert logged_in.get("/api/me").status_code == 401

    def test_an_expired_session_stops_working(self, client):
        from datetime import timedelta

        from app import tasks_auth

        session_id = tasks_auth._store(
            "user", timedelta(seconds=-1), email="someone@example.com", name="Someone"
        )
        client.cookies.set(tasks_auth.SESSION_COOKIE, session_id)
        assert client.get("/api/me").status_code == 401


class TestStartLogin:
    def test_redirects_to_google_with_state_and_pkce(self, client):
        res = client.get("/oauth2/authorization/google")
        assert res.status_code == 302
        params = parse_qs(urlparse(res.headers["location"]).query)
        assert params["client_id"] == ["test-client"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["state"] and params["code_challenge"]
        assert res.headers["location"].startswith("https://accounts.google.com/")

    def test_public_base_url_wins(self, client, env):
        env.setenv("PUBLIC_BASE_URL", "https://tasks.example.com/")
        res = client.get("/oauth2/authorization/google")
        params = parse_qs(urlparse(res.headers["location"]).query)
        assert params["redirect_uri"] == ["https://tasks.example.com/login/oauth2/code/google"]

    def test_an_unlisted_host_cannot_start_a_login(self, client, env):
        """Host ヘッダ注入で別のところへ飛ばされないようにするための関門。"""
        env.setenv("ALLOWED_REDIRECT_HOSTS", "tasks.example.com")
        res = client.get("/oauth2/authorization/google")
        assert res.status_code == 400

    def test_a_listed_host_is_accepted(self, client, env):
        env.setenv("ALLOWED_REDIRECT_HOSTS", "testserver")
        assert client.get("/oauth2/authorization/google").status_code == 302

    def test_login_is_rate_limited(self, client, env):
        env.setenv("LOGIN_RATE_LIMIT_PER_MINUTE", "3")
        codes = [client.get("/oauth2/authorization/google").status_code for _ in range(5)]
        assert codes.count(429) >= 1
        assert client.get("/oauth2/authorization/google").json()["error"]["code"] == "rate_limited"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Google の token / userinfo の代わり。**サーバー間の 2 回の呼び出しだけ**を模す。"""

    profile: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, data=None):
        return _FakeResponse({"access_token": "token"})

    async def get(self, url, headers=None):
        return _FakeResponse(type(self).profile)


class TestCallback:
    @pytest.fixture()
    def pending(self, client):
        from app import tasks_auth

        login_id = tasks_auth._store(
            "login", tasks_auth.LOGIN_TTL, state="s-t-a-t-e", code_verifier="v",
            redirect_uri="http://testserver/login/oauth2/code/google",
        )
        client.cookies.set(tasks_auth.LOGIN_COOKIE, login_id)
        return client

    def _with_profile(self, monkeypatch, profile):
        from app import tasks_auth

        _FakeClient.profile = profile
        monkeypatch.setattr(tasks_auth.httpx, "AsyncClient", _FakeClient)

    def test_without_a_pending_login_it_is_400(self, client):
        assert client.get("/login/oauth2/code/google", params={"state": "x", "code": "y"}).status_code == 400

    def test_a_mismatched_state_is_400(self, pending):
        res = pending.get("/login/oauth2/code/google", params={"state": "another", "code": "y"})
        assert res.status_code == 400

    def test_a_non_ascii_state_is_rejected_cleanly(self, pending):
        """クエリは非 ASCII を運べる。secrets.compare_digest に str のまま渡すと
        TypeError になり、1 文字混ぜられただけで 500 に落ちる。
        """
        res = pending.get("/login/oauth2/code/google", params={"state": "違う値", "code": "y"})
        assert res.status_code == 400

    def test_the_allowed_account_gets_a_session(self, pending, env):
        self._with_profile(env, {"email": "someone@example.com", "email_verified": True, "name": "S"})
        res = pending.get("/login/oauth2/code/google", params={"state": "s-t-a-t-e", "code": "c"})
        assert res.status_code == 302 and res.headers["location"] == "/"
        cookie = res.headers["set-cookie"]
        assert "CHIEZOTASKSSESSION=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    def test_another_account_is_refused(self, pending, env):
        self._with_profile(env, {"email": "someone.else@example.com", "email_verified": True})
        res = pending.get("/login/oauth2/code/google", params={"state": "s-t-a-t-e", "code": "c"})
        assert res.status_code == 403

    def test_an_unverified_address_is_refused(self, pending, env):
        """メールだけで人を同定する設計なので、未検証を通すと名乗り放題になる。"""
        self._with_profile(env, {"email": "someone@example.com", "email_verified": False})
        res = pending.get("/login/oauth2/code/google", params={"state": "s-t-a-t-e", "code": "c"})
        assert res.status_code == 403

    def test_the_pending_login_is_single_use(self, pending, env):
        self._with_profile(env, {"email": "someone@example.com", "email_verified": True})
        pending.get("/login/oauth2/code/google", params={"state": "s-t-a-t-e", "code": "c"})
        again = pending.get("/login/oauth2/code/google", params={"state": "s-t-a-t-e", "code": "c"})
        assert again.status_code == 400


class TestSecurityHeaders:
    def test_headers_are_present(self, client):
        headers = client.get("/healthz").headers
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "same-origin"

    def test_headers_are_on_error_responses_too(self, client):
        assert client.get("/api/tasks").headers["x-frame-options"] == "DENY"
