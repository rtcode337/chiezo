"""CLI ブリッジへ渡す認証情報の写し(`settings_store.export_bridge_credential`)。

ブリッジの中で動くのは道具を持ったエージェントなので、**設定 DB(全部の相手の鍵が
平文で入っている)を渡さない**。登録のたびに相手ごとの写しを
`<state>/bridge/<相手>/credential` へ書き出し、ブリッジにはそのディレクトリだけを
マウントする。
"""
import os
import stat

import pytest

from app import settings_store


@pytest.fixture()
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"


def _copy(state, provider):
    return state / "bridge" / provider / "credential"


class TestExport:
    def test_registering_writes_only_that_providers_copy(self, state):
        settings_store.set_credential("claude", "tok-claude")
        settings_store.set_credential("gemini", "key-gemini")

        assert _copy(state, "claude").read_text(encoding="utf-8") == "tok-claude"
        # ブリッジで包まない相手の鍵は書き出さない(どのブリッジにも要らない)
        assert not (state / "bridge" / "gemini").exists()
        assert "key-gemini" not in "".join(
            p.read_text(encoding="utf-8") for p in (state / "bridge").rglob("*") if p.is_file()
        )

    def test_clearing_removes_the_copy(self, state):
        settings_store.set_credential("codex", '{"tokens": {}}')
        settings_store.clear_credential("codex")

        assert not _copy(state, "codex").exists()

    def test_the_copy_is_not_world_readable(self, state):
        """ブリッジは別のユーザーで動くので、グループ(compose の group_add)で読ませる。"""
        settings_store.set_credential("claude", "tok")
        mode = stat.S_IMODE(os.stat(_copy(state, "claude")).st_mode)

        assert mode & stat.S_IROTH == 0
        assert mode & stat.S_IWOTH == 0
        assert mode & stat.S_IRGRP

    def test_startup_catches_up_with_older_registrations(self, state):
        """写しの仕組みより前に登録された鍵も、起動時に揃う(DB が正)。"""
        settings_store.set_credential("claude", "tok-old")
        _copy(state, "claude").unlink()

        settings_store.export_bridge_credentials()

        assert _copy(state, "claude").read_text(encoding="utf-8") == "tok-old"

    def test_a_failure_to_write_does_not_stop_registration(self, state, monkeypatch):
        """写しはブリッジのための控えで、正は設定 DB。書けなくても登録は通す。"""
        monkeypatch.setattr(settings_store.os, "replace", _boom)
        settings_store.set_credential("claude", "tok")

        assert settings_store.load("claude").credential == "tok"


def _boom(*_args, **_kwargs):
    raise PermissionError("read-only")


class TestTheBridgeReadsIt:
    def test_the_bridge_reads_the_exported_copy(self, state, monkeypatch):
        """書き出した写しを、ブリッジがそのまま読めること(2 つのイメージの約束)。"""
        import importlib

        settings_store.set_credential("claude", "tok-from-admin")
        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "claude")
        monkeypatch.setenv("CHIEZO_BRIDGE_CREDENTIAL_FILE", str(_copy(state, "claude")))
        monkeypatch.setenv("CHIEZO_BRIDGE_STATE_DB", str(state / "not-mounted.db"))
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        import cli_bridge

        server = importlib.reload(cli_bridge)
        server._PROBED = True

        assert server.stored_credential() == "tok-from-admin"
