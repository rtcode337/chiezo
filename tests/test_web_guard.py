"""ブラウザ越しの悪用(CSRF・DNS リバインディング)を止める関門のテスト(`app/web_guard.py`)。

Chiezo は認証を持たない LAN のサービスなので、その LAN の端末で開いている任意のページが
ブラウザを踏み台にして叩けてしまう。止めるのは「知らない名前の Host」と
「別のサイトからの書き込み」の 2 つで、**curl や他のアプリのサーバー側の HTTP クライアント、
MCP クライアントのような Origin を付けない相手はこれまでどおり通す**。
"""
import pytest
from fastapi.testclient import TestClient

from app import web_guard


@pytest.fixture()
def clean_env(monkeypatch):
    for key in ("CHIEZO_ALLOWED_HOSTS", "CHIEZO_MCP_ALLOWED_HOSTS", "CHIEZO_ALLOWED_ORIGINS"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestHost:
    """既定は検査しない。一覧を書いたら IP・localhost・ドットの無い名前と、書いた名前だけを通す。"""

    @pytest.mark.parametrize("host", [
        "192.0.2.10:7010", "192.0.2.10", "127.0.0.1:7010", "[::1]:7010", "[2001:db8::1]",
        "localhost", "localhost:7010", "chiezo-app:7010", "testserver",
    ])
    def test_the_default_lets_lan_style_hosts_in(self, clean_env, host):
        assert web_guard.host_allowed(host)

    @pytest.mark.parametrize("host", ["evil.example.com", "vpn-host.example.net:7010", "chiezo.local"])
    def test_without_a_list_any_name_is_let_in(self, clean_env, host):
        """既定は検査しない。VPN 越しの名前などで開いて、気づかないうちに締め出されないように。"""
        assert web_guard.host_allowed(host)

    @pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:7010", "", "chiezo.local"])
    def test_with_a_list_other_names_are_refused(self, clean_env, host):
        """名前を向け直す攻撃(DNS リバインディング)は名前で来る。一覧を書いたら知らない名前は断る。"""
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com")
        assert not web_guard.host_allowed(host)

    def test_with_a_list_lan_style_hosts_still_pass(self, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com")
        for host in ("192.0.2.10:7010", "localhost:7010", "chiezo-app:7010"):
            assert web_guard.host_allowed(host)

    def test_a_listed_name_is_allowed(self, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com, *.lan.example:7010")
        assert web_guard.host_allowed("chiezo.example.com")
        assert web_guard.host_allowed("chiezo.example.com:7010")
        assert web_guard.host_allowed("nas.lan.example:7010")
        assert not web_guard.host_allowed("nas.lan.example:8080")
        # `*.例` は `例` そのものを含まない(ぶら下がる名前だけ)
        assert not web_guard.host_allowed("lan.example:7010")
        # 前方一致・後方一致で別の名前を通さない
        assert not web_guard.host_allowed("chiezo.example.com.attacker.example")

    def test_the_mcp_list_counts_too(self, clean_env):
        """以前から /mcp の Host 検証に使っていた設定に名前を書いた環境を締め出さない。"""
        clean_env.setenv("CHIEZO_MCP_ALLOWED_HOSTS", "chiezo.example.com:7010,localhost:*")
        assert web_guard.host_allowed("chiezo.example.com:7010")
        assert not web_guard.host_allowed("chiezo.example.com:9999")

    def test_a_star_turns_the_check_off(self, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "*")
        assert web_guard.host_allowed("anything.example.com")


class TestCrossSiteWrites:
    HOST = "192.0.2.10:7010"

    def test_requests_without_browser_headers_pass(self, clean_env):
        """curl・サーバー側のクライアント・MCP クライアントは Origin を付けない。"""
        assert web_guard.refuse_reason("POST", {}, self.HOST) == ""

    def test_reading_is_never_refused_here(self, clean_env):
        headers = {"origin": "https://evil.example", "sec-fetch-site": "cross-site"}
        assert web_guard.refuse_reason("GET", headers, self.HOST) == ""

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_another_sites_page_is_refused(self, clean_env, method):
        assert web_guard.refuse_reason(method, {"sec-fetch-site": "cross-site"}, self.HOST)
        assert web_guard.refuse_reason(method, {"origin": "https://evil.example"}, self.HOST)
        # サンドボックスの iframe やファイルから開いたページは Origin: null
        assert web_guard.refuse_reason(method, {"origin": "null"}, self.HOST)

    def test_the_same_origin_passes(self, clean_env):
        headers = {"origin": "http://192.0.2.10:7010", "sec-fetch-site": "same-origin"}
        assert web_guard.refuse_reason("POST", headers, self.HOST) == ""

    def test_a_tls_proxy_in_front_still_counts_as_the_same_origin(self, clean_env):
        """TLS を終端する前段の裏では、ブラウザのオリジン(https)と Host(ポート省略)が並ぶ。"""
        assert web_guard.refuse_reason("POST", {"origin": "https://chiezo.example.com"},
                                       "chiezo.example.com") == ""
        # ポートが違えば別の場所
        assert web_guard.refuse_reason("POST", {"origin": "http://192.0.2.10:8080"}, self.HOST)

    def test_an_allowed_origin_passes(self, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_ORIGINS", "https://app.example.com/")
        headers = {"origin": "https://app.example.com", "sec-fetch-site": "cross-site"}
        assert web_guard.refuse_reason("POST", headers, self.HOST) == ""


class TestTheAppUsesIt:
    @pytest.fixture()
    def app_client(self, built_data_dir, clean_env):
        clean_env.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        return app

    def test_without_a_list_a_name_is_served(self, app_client):
        with TestClient(app_client, base_url="http://chiezo.example.com") as client:
            assert client.get("/healthz").status_code == 200

    def test_an_unknown_host_gets_421_json(self, app_client, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com")
        with TestClient(app_client, base_url="http://evil.example.com") as client:
            res = client.get("/healthz")
        assert res.status_code == 421
        assert "error" in res.json()

    def test_lan_hosts_keep_working(self, app_client):
        for base in ("http://192.0.2.10:7010", "http://chiezo-app:7010", "http://localhost:7010"):
            with TestClient(app_client, base_url=base) as client:
                assert client.get("/healthz").status_code == 200

    def test_a_cross_site_write_gets_403_json(self, app_client):
        with TestClient(app_client, base_url="http://192.0.2.10:7010") as client:
            res = client.post("/v1/ai/complete", json={},
                              headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
            assert res.status_code == 403
            assert "受け付けません" in res.json()["error"]
            # 同じ面から送ったもの・ブラウザ以外から送ったものは、本体まで届く
            same = client.post("/v1/ai/complete", json={},
                               headers={"Origin": "http://192.0.2.10:7010", "Sec-Fetch-Site": "same-origin"})
            plain = client.post("/v1/ai/complete", json={})
            assert same.status_code not in (403, 421)
            assert plain.status_code not in (403, 421)

    def test_mcp_is_behind_the_same_gate(self, app_client, clean_env):
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com")
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        with TestClient(app_client, base_url="http://evil.example.com") as client:
            assert client.post("/mcp/", headers=headers, json=body).status_code == 421
        with TestClient(app_client, base_url="http://192.0.2.10:7010") as client:
            assert client.post("/mcp/", headers=headers, json=body).status_code == 200


class TestMcpTransportSecurity:
    """MCP の SDK 側の検証も外さない。未設定なら本体と同じ判定を渡す。"""

    def test_it_is_on_by_default_with_the_same_rule(self, clean_env):
        from app import mcp_server

        settings = mcp_server._transport_security()
        assert settings.enable_dns_rebinding_protection
        # 一覧は判定（HostMatcher / OriginMatcher）で、`in` は部分文字列ではなく許可の判定を呼ぶ。
        # 文字列に `in` を当てているように読めないよう、判定を名指しで呼ぶ
        host_ok = settings.allowed_hosts.__contains__
        origin_ok = settings.allowed_origins.__contains__
        # 本体の一覧が空なら、本体と同じく名前も通す
        assert host_ok("vpn-host.example.net:7010")
        clean_env.setenv("CHIEZO_ALLOWED_HOSTS", "chiezo.example.com")
        assert host_ok("192.0.2.10:7010")
        assert host_ok("chiezo-app:7010")
        assert not host_ok("evil.example.com")
        assert origin_ok("http://192.0.2.10:7010")
        assert not origin_ok("https://evil.example.com")

    def test_an_explicit_list_still_narrows_it(self, clean_env):
        from app import mcp_server

        clean_env.setenv("CHIEZO_MCP_ALLOWED_HOSTS", "192.0.2.10:7010,localhost:*")
        settings = mcp_server._transport_security()
        assert settings.enable_dns_rebinding_protection
        assert settings.allowed_hosts == ["192.0.2.10:7010", "localhost:*"]
