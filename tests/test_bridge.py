"""CLI ブリッジ(bridge/cli_bridge.py)のテスト。

CLI そのものは起動しない。確かめるのは「OpenAI 形式の入力を CLI の起動へどう変換するか」で、
そこが Chiezo 本体と CLI の間の唯一の接ぎ目だからである。認証が要る実行の側は
コンテナを立てて確かめる(docs/ai.md)。
"""
import importlib

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    """環境変数を差し替えてから読み込み直す(設定はモジュール読み込み時に確定するため)。"""
    def _load(**env):
        for key in ("CHIEZO_BRIDGE_CLI", "CHIEZO_BRIDGE_MODEL", "CHIEZO_BRIDGE_MCP_URL",
                    "CHIEZO_BRIDGE_MODEL_LABEL", "CHIEZO_BRIDGE_ALLOWED_TOOLS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import cli_bridge
        return importlib.reload(cli_bridge)
    return _load


class TestPrompt:
    def test_system_comes_first_and_turns_keep_their_order(self, bridge):
        server = bridge()
        text = server.build_prompt([
            server.Message(role="system", content="根拠:\n浅草寺は台東区にある"),
            server.Message(role="user", content="浅草寺はどこ?"),
            server.Message(role="assistant", content="台東区です"),
            server.Message(role="user", content="最寄り駅は?"),
        ])
        # 抜粋(根拠)は system に載る。ここが埋もれると答えが根拠から離れるので先頭に置く。
        assert text.startswith("根拠:")
        assert text.index("浅草寺はどこ?") < text.index("最寄り駅は?")

    def test_empty_messages_do_not_produce_an_empty_prompt(self, bridge):
        server = bridge()
        assert server.build_prompt([server.Message(role="user", content="  ")]).strip()


class TestCommand:
    def test_claude_gets_only_the_chiezo_tools(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MODEL="sonnet")
        cmd = server.build_command("/tmp/out.txt")
        assert cmd[:2] == ["claude", "-p"]
        # 組み込みの道具(Bash・Edit・Write…)は切る。知識ベースに答えるのに要らず、危ない。
        assert cmd[cmd.index("--tools") + 1] == ""
        # 手元の設定にある別の MCP を拾わせない
        assert "--strict-mcp-config" in cmd
        assert cmd[cmd.index("--allowed-tools") + 1] == "mcp__chiezo"
        # 非対話なので確認を出されると固まる
        assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
        assert cmd[cmd.index("--model") + 1] == "sonnet"

    def test_codex_runs_read_only_and_writes_the_answer_to_a_file(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="codex")
        cmd = server.build_command("/tmp/out.txt")
        assert cmd[:2] == ["codex", "exec"]
        # 生成されたシェルコマンドを実行させない
        assert cmd[cmd.index("-s") + 1] == "read-only"
        assert 'approval_policy="never"' in cmd
        assert cmd[cmd.index("-o") + 1] == "/tmp/out.txt"
        # プロンプトは標準入力から渡す(引数だと 128KiB の上限に当たる)
        assert cmd[-1] == "-"

    def test_model_is_omitted_when_not_configured(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        assert "--model" not in server.build_command("/tmp/out.txt")

    def test_antigravity_takes_the_prompt_as_an_argument(self, bridge):
        """**agy だけプロンプトを引数で取る**（claude/codex は標準入力から読む）。"""
        server = bridge(CHIEZO_BRIDGE_CLI="antigravity")
        assert server.build_command("/tmp/out.txt", "こんにちは") == ["agy", "-p", "こんにちは"]

    def test_antigravity_refuses_a_prompt_that_would_not_fit_in_argv(self, bridge):
        """引数渡しなので Linux の単一引数の上限(128KiB)に当たる。

        黙って E2BIG で落ちるより、理由を返して断るほうが原因を追える。
        """
        server = bridge(CHIEZO_BRIDGE_CLI="antigravity")
        with pytest.raises(server.PromptTooLong):
            server.build_command("/tmp/out.txt", "あ" * 100_000)

    def test_unknown_cli_is_rejected(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="gemini")
        with pytest.raises(RuntimeError):
            server.build_command("/tmp/out.txt")


class TestOptionalMcp:
    """MCP は任意。**Chiezo 専用の部品ではなく、道具の要らない用途でも使える**。"""

    def test_no_mcp_means_no_tools_at_all(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MCP_URL="")
        cmd = server.build_command("/tmp/out.txt")
        assert "--mcp-config" not in cmd
        assert "--allowed-tools" not in cmd
        # 組み込みの道具を切る指定と、他所の MCP を拾わない指定は残す
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in cmd

    def test_mcp_is_attached_when_configured(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MCP_URL="http://api.test:7010/mcp")
        cmd = server.build_command("/tmp/out.txt")
        assert "--mcp-config" in cmd
        assert cmd[cmd.index("--allowed-tools") + 1] == "mcp__chiezo"


class TestAntigravityCredential:
    def test_it_has_nothing_to_place(self, bridge):
        """API キー方式が無く、サインイン結果はホームのキャッシュにある。"""
        server = bridge(CHIEZO_BRIDGE_CLI="antigravity")
        assert server.apply_credential() == ""


class TestMcpConfig:
    def test_config_points_at_chiezo_over_streamable_http(self, bridge, tmp_path):
        import json

        server = bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MCP_URL="http://api.test:7010/mcp")
        server.MCP_CONFIG_PATH = str(tmp_path / "mcp.json")
        server._write_mcp_config()
        config = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
        assert config == {
            "mcpServers": {"chiezo": {"type": "http", "url": "http://api.test:7010/mcp"}}
        }


class TestModelLabel:
    def test_label_falls_back_to_the_cli_name(self, bridge):
        """Chiezo の見出し(`AI(…)と話す`)はこの名前を出す。"""
        assert bridge(CHIEZO_BRIDGE_CLI="codex").MODEL_LABEL == "Codex CLI"
        assert bridge(CHIEZO_BRIDGE_CLI="antigravity").MODEL_LABEL == "Antigravity CLI"
        assert bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MODEL="opus").MODEL_LABEL == "opus"
        assert bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_MODEL_LABEL="社内AI").MODEL_LABEL == "社内AI"
