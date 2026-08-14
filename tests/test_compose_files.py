"""compose ファイル同士の食い違いを見張るテスト。

Chiezo の compose は「本体 + 上書き」に分けてあり、上書き側は重ねるだけなので
構造上ずれない。**ずれるのは単体定義(docker-compose.standalone.example.yml)だけ**で、
これは `${...}` も profile も使えない環境向けに値を直書きしたコピーだから、
本体を変えたときに手で追従させるしかない。実際に 2 回取り残された
(web 検索の設定一式と、回答パイプラインの調整)。

そこで「本体の chiezo-api に渡している環境変数が、単体定義にも(コメントとしてでも)
出てきているか」を照合する。**コメントでよい**ことにしてあるのは、単体定義では
任意の設定をコメントアウトで並べておくのが作法だから。

docker や PyYAML の外部コマンドには依存しない(YAML として読むのは本体だけで、
単体定義は文字列として走査する)。
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docker-compose.yml"
STANDALONE = ROOT / "docker-compose.standalone.example.yml"
ANSWER = ROOT / "docker-compose.answer.yml"

# 単体定義にはあえて載せていないもの。ここに足すときは「なぜ単体定義に要らないか」を書くこと。
STANDALONE_EXEMPT = {
    # イメージのタグは単体定義では直書きする(${...} が解決できないため)
    "CHIEZO_API_IMAGE",
    "CHIEZO_INGEST_IMAGE",
}

# 「答える」層の上書き(docker-compose.answer.yml)が持つコンテナ。**推論サーバだけ** ——
# 検索エンジン(SearXNG)は本体側にある(web 検索と推論は独立していて、相手が Gemini や
# Claude Code でも検索は要るのに、以前は検索のために推論サーバまで立ち上がっていた)。
ANSWER_CONTAINERS = {"chiezo-llm"}

# **単体定義には載せないコンテナ。** 推論サーバはモデルの置き場(数 GB)と GPU の設定が
# 環境ごとに違い、別サーバーのものを指せば済むため。
# (SearXNG は設定を焼き込んだイメージにしたので、単体定義にも載せてある)
STANDALONE_EXCLUDED_CONTAINERS = {"chiezo-llm"}

# **リポジトリが持たない compose は見ない。** 単体定義に実値を書いた
# docker-compose.standalone.yml は .gitignore 済みで、置くかどうかも中身も
# 環境ごとに違う（ホストの絶対パスが入る）。手元にそれがある環境でだけテストが
# 落ちるのは、見張りたいものと関係がない。
LOCAL_ONLY = {"docker-compose.standalone.yml"}


def _compose_files() -> list[Path]:
    """リポジトリが持っている compose ファイル。"""
    return sorted(p for p in ROOT.glob("docker-compose*.yml") if p.name not in LOCAL_ONLY)


def _service_env_keys(compose_path: Path, service: str) -> set[str]:
    """compose の 1 サービスに渡している CHIEZO_* の環境変数名を集める。"""
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    env = doc["services"][service].get("environment") or []
    # 本体は list 形式(KEY=値)、単体定義は dict 形式。どちらでも名前だけ取る。
    names = env.keys() if isinstance(env, dict) else (item.split("=", 1)[0] for item in env)
    return {n for n in names if n.startswith("CHIEZO_")}


def test_standalone_covers_base_env():
    """本体が chiezo-api に渡す設定は、単体定義にも出てくること。"""
    base = _service_env_keys(BASE, "chiezo-api") - STANDALONE_EXEMPT
    text = STANDALONE.read_text(encoding="utf-8")
    missing = sorted(k for k in base if k not in text)
    assert not missing, (
        "docker-compose.yml にあって単体定義に無い設定: "
        f"{missing}。docker-compose.standalone.example.yml に追従させること"
        "(使わないなら STANDALONE_EXEMPT に理由付きで足す)"
    )


def test_standalone_has_no_answer_containers():
    """単体定義は推論サーバ・検索エンジンを持たないこと(設定だけを載せる)。"""
    doc = yaml.safe_load(STANDALONE.read_text(encoding="utf-8"))
    assert not (set(doc["services"]) & STANDALONE_EXCLUDED_CONTAINERS), (
        "推論サーバ・検索エンジンのコンテナが単体定義に入っている。"
        "別サーバーのものを指すか、リポジトリを置ける環境で docker-compose.yml を使うこと"
    )


def test_websearch_container_is_in_the_base():
    """SearXNG は本体側にあること(推論サーバと同居させない)。

    web 検索と推論は独立している —— 話す相手が Gemini や Claude Code でも検索は要るのに、
    「答える」層の上書きに置いていた頃は、検索を使いたいだけで数 GB の推論サーバまで
    立ち上げることになっていた。
    """
    base = yaml.safe_load(BASE.read_text(encoding="utf-8"))
    assert "searxng" in base["services"]
    # profile を付けない(本体を上げれば一緒に立つ)
    assert not base["services"]["searxng"].get("profiles")
    # **設定はマウントではなくイメージに焼き込む** —— マウントだと、リポジトリを置けない
    # 環境(単体定義)では立てられない
    assert not base["services"]["searxng"].get("volumes")
    assert "chiezo-searxng" in base["services"]["searxng"]["image"]


def test_standalone_has_the_websearch_container():
    """単体定義でも SearXNG が立つこと(設定を焼き込んだイメージなので置ける)。"""
    doc = yaml.safe_load(STANDALONE.read_text(encoding="utf-8"))
    assert "searxng" in doc["services"]
    assert not doc["services"]["searxng"].get("volumes")


def test_answer_overlay_defines_only_containers():
    """「答える」層の上書きが持つのはコンテナだけで、chiezo-api の設定は本体側にあること。

    推論を LAN の別マシンに任せる使い方では、コンテナは要らず設定だけが要る。
    設定をこちらへ移すと、その使い方でこのファイルを重ねる羽目になる。
    """
    doc = yaml.safe_load(ANSWER.read_text(encoding="utf-8"))
    assert set(doc["services"]) == ANSWER_CONTAINERS
    assert "CHIEZO_LLM_URL" in _service_env_keys(BASE, "chiezo-api"), (
        "「答える」層の機能フラグ(CHIEZO_LLM_URL)は本体側に置くこと"
    )


@pytest.mark.parametrize("path", _compose_files())
def test_compose_files_parse(path: Path):
    """どの compose ファイルも YAML として読めること(貼り付けミスの検知)。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "services" in doc, f"{path.name} に services が無い"


def test_overrides_do_not_duplicate_base():
    """上書きファイルが本体の設定を写していないこと。

    以前 docker-compose.build.yml が本体の完全なコピーで、そのぶん取り残された。
    上書きは「本体との違い」だけを書く —— 行数で機械的に見張る。
    """
    overrides = [
        p for p in _compose_files()
        if p.name not in {"docker-compose.yml", "docker-compose.standalone.example.yml",
                          "docker-compose.answer.yml"}
    ]
    base_lines = len(BASE.read_text(encoding="utf-8").splitlines())
    for path in overrides:
        body = [
            ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert len(body) < base_lines // 2, (
            f"{path.name} が本体を写している疑い(実質 {len(body)} 行)。"
            "上書きには本体との違いだけを書くこと"
        )
