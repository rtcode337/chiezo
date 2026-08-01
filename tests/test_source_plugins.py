"""外部プラグイン(CHIEZO_SOURCE_PLUGINS)でソースを足せることのテスト。

公開できないプライベートな情報を、取得コードごと別リポジトリに置いたまま、
Chiezo のイメージを継承して足せる、という経路を固定する。
"""
import importlib
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

PLUGIN_SOURCE = textwrap.dedent(
    '''
    """テスト用のプラグイン。ダンプを読まず、その場で Doc を作る。"""
    from core import Doc


    class MemoAdapter:
        source = "memo"
        source_kind = "memo"
        lang = "ja"
        min_docs = 2
        sample_titles = ["回線の契約内容"]
        min_build_memory_gb = 0.1

        def fetch(self, workdir):
            return workdir, "20260731"

        def iter_docs(self, path):
            yield Doc(
                doc_id=1,
                title="回線の契約内容",
                opening="上下 1Gbps の光回線。IPv6 は有効。",
                body="上下 1Gbps の光回線。IPv6 は有効。契約は 2 年ごとの自動更新。",
                tags=["契約"],
                rank_score=1.0,
                extra={"area": "拠点 A"},
            )
            yield Doc(
                doc_id=2,
                title="バックアップの運用",
                opening="バックアップは 1 日 1 回。",
                body="バックアップは 1 日 1 回。世代は 30 日ぶん残す。",
                tags=["運用"],
                rank_score=0.5,
            )


    ADAPTERS = {"memo": MemoAdapter}
    '''
)


@pytest.fixture()
def plugin_module(tmp_path, monkeypatch):
    """一時ディレクトリにプラグインを書き、import できる状態にして名前を返す。"""
    (tmp_path / "chiezo_memo.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "chiezo_memo"
    sys.modules.pop("chiezo_memo", None)


def write_plugin(tmp_path, monkeypatch, name: str, body: str) -> str:
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop(name, None)
    return name


class TestLoading:
    def test_no_plugins_configured_is_a_no_op(self):
        import sources

        assert sources.load_plugin_adapters("") == {}
        assert sources.load_plugin_adapters("  ,  ") == {}

    def test_loads_adapters_from_a_module(self, plugin_module):
        import sources

        loaded = sources.load_plugin_adapters(plugin_module)
        assert list(loaded) == ["memo"]
        adapter = loaded["memo"]()
        assert adapter.source == "memo"
        assert adapter.source_kind == "memo"

    def test_env_var_registers_the_source(self, plugin_module, monkeypatch):
        """実際の経路(環境変数 → import 時に ADAPTERS へ)を通す。"""
        import sources

        monkeypatch.setenv("CHIEZO_SOURCE_PLUGINS", plugin_module)
        reloaded = importlib.reload(sources)
        try:
            assert "memo" in reloaded.ADAPTERS
            assert reloaded.get_adapter("memo").source == "memo"
            # 組み込みのソースは消えない
            assert "jawiki" in reloaded.ADAPTERS
            assert "osm_japan" in reloaded.ADAPTERS
        finally:
            monkeypatch.delenv("CHIEZO_SOURCE_PLUGINS")
            importlib.reload(sources)

    def test_unknown_source_message_lists_plugin_sources(self, plugin_module, monkeypatch):
        import sources

        monkeypatch.setenv("CHIEZO_SOURCE_PLUGINS", plugin_module)
        reloaded = importlib.reload(sources)
        try:
            with pytest.raises(SystemExit) as e:
                reloaded.get_adapter("nosuch")
            assert "memo" in str(e.value)
        finally:
            monkeypatch.delenv("CHIEZO_SOURCE_PLUGINS")
            importlib.reload(sources)


class TestRejectsBrokenPlugins:
    """壊れた指定は黙って無視せず落とす(指定したのに入っていない状態を作らない)。"""

    def test_unimportable_module(self):
        import sources

        with pytest.raises(SystemExit) as e:
            sources.load_plugin_adapters("chiezo_no_such_module")
        assert "cannot import" in str(e.value)

    def test_module_without_adapters(self, tmp_path, monkeypatch):
        import sources

        name = write_plugin(tmp_path, monkeypatch, "chiezo_empty", "X = 1\n")
        with pytest.raises(SystemExit) as e:
            sources.load_plugin_adapters(name)
        assert "ADAPTERS" in str(e.value)

    def test_name_collision_with_a_builtin_is_rejected(self, tmp_path, monkeypatch):
        """jawiki を影で差し替えられると、間違ったダンプが jawiki.db に焼かれる。"""
        import sources

        name = write_plugin(
            tmp_path, monkeypatch, "chiezo_shadow",
            "ADAPTERS = {'jawiki': lambda: None}\n",
        )
        with pytest.raises(SystemExit) as e:
            sources.load_plugin_adapters(name)
        assert "redefines an existing source" in str(e.value)

    def test_hyphen_in_source_name_is_rejected(self, tmp_path, monkeypatch):
        """`<source>-<date>.db` の区切りと衝突し、切り替えの段で壊れるため先に止める。"""
        import sources

        name = write_plugin(
            tmp_path, monkeypatch, "chiezo_hyphen",
            "ADAPTERS = {'net-map': lambda: None}\n",
        )
        with pytest.raises(SystemExit) as e:
            sources.load_plugin_adapters(name)
        assert "invalid source name" in str(e.value)

    def test_non_callable_factory_is_rejected(self, tmp_path, monkeypatch):
        import sources

        name = write_plugin(
            tmp_path, monkeypatch, "chiezo_notcallable",
            "ADAPTERS = {'memo2': 'not a factory'}\n",
        )
        with pytest.raises(SystemExit) as e:
            sources.load_plugin_adapters(name)
        assert "must be callable" in str(e.value)


class TestEndToEnd:
    """プラグインのソースが、取り込み → 配信までそのまま通ること。"""

    @pytest.fixture()
    def plugin_data_dir(self, plugin_module, tmp_path_factory):
        import main as ingest_main

        import sources

        adapter = sources.load_plugin_adapters(plugin_module)["memo"]()
        data_dir = tmp_path_factory.mktemp("plugin_data")
        building = data_dir / "memo-20260731.db.building"
        ingest_main.build_db(adapter, data_dir, "20260731", building)
        ingest_main.validate_db(adapter, building)
        ingest_main.switch_db(data_dir, "memo", "20260731", building)
        return data_dir

    def test_api_serves_a_plugin_source_without_any_change(self, plugin_data_dir, monkeypatch):
        """api はソース種別を知らないので、コアスキーマの .db を置くだけで登録される。"""
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(plugin_data_dir))
        from app.main import app

        with TestClient(app) as client:
            listed = client.get("/v1/sources").json()["sources"]
            assert [s["name"] for s in listed] == ["memo"]
            assert listed[0]["kind"] == "memo"

            hits = client.get("/v1/memo/search", params={"q": "バックアップ"}).json()
            assert [r["title"] for r in hits["results"]] == ["バックアップの運用"]

            doc = client.get("/v1/memo/doc", params={"title": "回線の契約内容"}).json()
            assert "IPv6" in doc["body"]
            assert doc["tags"] == ["契約"]

            # 属性・タグの索引も組み込みソースと同じように効く
            tagged = client.get("/v1/memo/filter", params={"tag": "運用"}).json()
            assert [r["title"] for r in tagged["results"]] == ["バックアップの運用"]

            # ブラウズ画面も汎用なのでそのまま開ける
            assert client.get("/search/memo/").status_code == 200
