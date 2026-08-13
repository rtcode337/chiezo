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
        # 組み込みの道具(Bash・Read・WebFetch…)は名前を挙げて塞ぐ。
        denied = cmd[cmd.index("--disallowed-tools") + 1].split(",")
        assert {"Bash", "Read", "Write", "WebFetch", "Agent"} <= set(denied)
        # **`--tools ""` は使わない**（MCP の道具まで消えてしまう）
        assert "--tools" not in cmd
        # **ToolSearch は塞がない**（MCP の道具はここから読み込まれる）
        assert "ToolSearch" not in denied
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
        # 組み込みの道具を塞ぐ指定と、他所の MCP を拾わない指定は残す
        assert "--disallowed-tools" in cmd
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


class TestRunsAsNonRoot:
    """**イメージが非 root で動くこと。**

    claude は権限確認を飛ばす指定を root では拒む
    (`--dangerously-skip-permissions cannot be used with root/sudo privileges`)。
    非対話で動かす以上その指定は外せないので、root に戻すと生成が必ず失敗する。
    しかも `claude auth status` は root でも通るため、管理画面の「接続を試す」は
    成功したまま生成だけが 502 になる —— 気づきにくいので、ここで見張る。
    """

    def test_dockerfile_switches_to_an_unprivileged_user(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "bridge" / "Dockerfile").read_text(
            encoding="utf-8"
        )
        users = [ln.split()[1] for ln in text.splitlines() if ln.startswith("USER ")]
        assert users, "bridge/Dockerfile に USER が無い(root で動いてしまう)"
        assert users[-1] not in {"root", "0"}, f"最後の USER が root: {users[-1]}"


class TestModelSelection:
    """会話ごとにモデルを選べること。

    以前は起動時の CHIEZO_BRIDGE_MODEL に固定で、要求の `model` は**捨てていた** ——
    画面にモデル選択があるのに何も変わらない状態だった。
    """

    def test_it_advertises_the_models_it_accepts(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        assert server.MODELS == ("sonnet", "fable", "opus", "haiku")

    def test_the_first_model_is_the_cli_default(self, bridge):
        """見出しは一覧の先頭を名乗るので、CLI の既定(claude-sonnet-5)に揃える。"""
        assert bridge(CHIEZO_BRIDGE_CLI="claude").MODELS[0] == "sonnet"

    def test_it_does_not_guess_ids_for_clis_without_a_list(self, bridge):
        """確かめていない ID を並べない(選べるのに必ず失敗する選択肢になるため)。"""
        assert bridge(CHIEZO_BRIDGE_CLI="codex").MODELS == ()
        assert bridge(CHIEZO_BRIDGE_CLI="antigravity").MODELS == ()

    def test_the_list_can_be_given_from_outside(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="codex", CHIEZO_BRIDGE_MODELS="gpt-x, gpt-y ")
        assert server.MODELS == ("gpt-x", "gpt-y")

    def test_the_requested_model_reaches_the_cli(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        cmd = server.build_command("/tmp/out.txt", "q", server.resolve_model("haiku"))
        assert cmd[cmd.index("--model") + 1] == "haiku"

    def test_codex_takes_it_with_its_own_flag(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="codex")
        cmd = server.build_command("/tmp/out.txt", "q", server.resolve_model("gpt-x"))
        assert cmd[cmd.index("-m") + 1] == "gpt-x"

    def test_no_choice_leaves_the_cli_default_alone(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        for requested in (None, "", "  ", "Claude Code", "chiezo"):
            assert server.resolve_model(requested) == ""
            assert "--model" not in server.build_command("/tmp/out.txt", "q", "")

    def test_it_refuses_a_name_that_would_become_a_flag(self, bridge):
        """**引数として渡すので、`-` で始まる名前は CLI のフラグになる。**"""
        import fastapi

        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        with pytest.raises(fastapi.HTTPException) as got:
            server.resolve_model("--dangerously-skip-permissions")
        assert got.value.status_code == 400

    def test_it_accepts_a_full_model_name(self, bridge):
        """一覧に無くても通す(CLI は正式名も受け付ける。間違いは CLI が言う)。"""
        assert bridge(CHIEZO_BRIDGE_CLI="claude").resolve_model("claude-fable-5") == "claude-fable-5"


class TestEffortSelection:
    """考える量（エフォート）を会話ごとに選べること。"""

    def test_it_offers_what_the_cli_accepts(self, bridge):
        assert bridge(CHIEZO_BRIDGE_CLI="claude").EFFORTS == (
            "low", "medium", "high", "xhigh", "max",
        )
        # agy に xhigh / max は無い
        assert bridge(CHIEZO_BRIDGE_CLI="antigravity").EFFORTS == ("low", "medium", "high")
        assert bridge(CHIEZO_BRIDGE_CLI="codex").EFFORTS == ()

    def test_it_reaches_the_cli(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        cmd = server.build_command("/tmp/out.txt", "q", "", server.resolve_effort("xhigh"))
        assert cmd[cmd.index("--effort") + 1] == "xhigh"

    def test_no_choice_leaves_the_cli_default_alone(self, bridge):
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        assert server.resolve_effort(None) == ""
        assert "--effort" not in server.build_command("/tmp/out.txt", "q", "", "")

    def test_it_refuses_a_value_the_cli_would_swallow(self, bridge):
        """**CLI が間違いを教えてくれない。**

        claude は `--effort bogus` をエラーにも警告にもせず、黙って既定で動く（実測）。
        通すと「選んだのに効いていない」ことに気づけないので、ここで弾く。
        """
        import fastapi

        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        with pytest.raises(fastapi.HTTPException) as got:
            server.resolve_effort("bogus")
        assert got.value.status_code == 400
        # 段階の名前が違う CLI では、その CLI に無いものも弾く
        agy = bridge(CHIEZO_BRIDGE_CLI="antigravity")
        with pytest.raises(fastapi.HTTPException):
            agy.resolve_effort("xhigh")

    def test_a_cli_without_efforts_refuses_all_of_them(self, bridge):
        import fastapi

        server = bridge(CHIEZO_BRIDGE_CLI="codex")
        with pytest.raises(fastapi.HTTPException):
            server.resolve_effort("high")


class TestBuiltinTools:
    """**MCP の道具を残したまま**、組み込みの道具だけを塞ぐ。

    以前は `--tools ""`（組み込みを全部切る指定）を渡していたが、これは MCP の道具まで
    消す。つまり「Chiezo の知識を引かせる」というブリッジの目的が黙って働いておらず、
    CLI は自分の知識だけで答えていた（本番で発覚）。
    """

    def test_it_does_not_use_the_flag_that_kills_mcp(self, bridge):
        cmd = bridge(CHIEZO_BRIDGE_CLI="claude").build_command("/tmp/out.txt")
        assert "--tools" not in cmd
        assert cmd[cmd.index("--mcp-config") + 1]

    def test_tool_search_stays_open(self, bridge):
        """MCP の道具は ToolSearch から読み込まれるので、塞ぐと引けなくなる。"""
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        assert "ToolSearch" not in server.DEFAULT_DISALLOWED

    def test_the_list_can_be_given_from_outside(self, bridge):
        """組み込みは CLI の版が上がるたびに増えるので、外から差し替えられる。"""
        server = bridge(CHIEZO_BRIDGE_CLI="claude", CHIEZO_BRIDGE_DISALLOWED_TOOLS="Bash,Read")
        cmd = server.build_command("/tmp/out.txt")
        assert cmd[cmd.index("--disallowed-tools") + 1] == "Bash,Read"

    def test_notices_do_not_leak_into_the_answer(self, bridge):
        """塞ぐ道具の名前がずれると CLI が注意書きを吐く。**答えに混ぜない。**"""
        server = bridge(CHIEZO_BRIDGE_CLI="claude")
        raw = (
            'Permission deny rule "SlashCommand" matches no known tool — check for typos.\n'
            "浅草寺は東京都台東区にあります。"
        )
        assert server.strip_cli_notices(raw) == "浅草寺は東京都台東区にあります。"
        assert server.strip_cli_notices("答えだけ") == "答えだけ"


class TestWebTools:
    """CLI 自身の web 検索は、頼まれたときだけ開ける。

    引く先は Chiezo の SearXNG ではなく**提供元の検索**なので、既定では塞いだまま。
    """

    def test_it_is_closed_by_default(self, bridge):
        cmd = bridge(CHIEZO_BRIDGE_CLI="claude").build_command("/tmp/out.txt")
        denied = cmd[cmd.index("--disallowed-tools") + 1].split(",")
        assert "WebSearch" in denied
        assert "WebFetch" in denied

    def test_asking_opens_it(self, bridge):
        cmd = bridge(CHIEZO_BRIDGE_CLI="claude").build_command("/tmp/out.txt", "q", web=True)
        denied = cmd[cmd.index("--disallowed-tools") + 1].split(",")
        assert "WebSearch" not in denied
        assert "WebFetch" not in denied
        # 他は塞いだまま
        assert "Bash" in denied

