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

# 「答える」層のコンテナ。**単体定義には載せない**(設定は載せる)—— 推論サーバと
# 検索エンジンは別サーバーのものを指せば済み、この環境で同居させる前提が無いため。
ANSWER_CONTAINERS = {"chiezo-llm", "searxng"}


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
    """単体定義は「答える」層のコンテナを持たないこと(設定だけを載せる)。"""
    doc = yaml.safe_load(STANDALONE.read_text(encoding="utf-8"))
    assert not (set(doc["services"]) & ANSWER_CONTAINERS), (
        "「答える」層のコンテナが単体定義に入っている。"
        "推論サーバ・検索エンジンは別サーバーのものを指すか、docker-compose.answer.yml を使うこと"
    )


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


@pytest.mark.parametrize("path", sorted(ROOT.glob("docker-compose*.yml")))
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
        p for p in ROOT.glob("docker-compose.*.yml")
        if p.name not in {"docker-compose.standalone.example.yml", "docker-compose.answer.yml"}
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
