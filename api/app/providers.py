"""話せる AI の一覧（URL・表示名・モデル候補はここに決め打ち）。

**環境変数で相手を定義するのはやめた。** 以前は `CHIEZO_LLM_<名前>_URL` を並べる形だったが、
URL は相手ごとに 1 つに決まっていて、ユーザーが選ぶ余地は無い（Gemini の OpenAI 互換の口は
1 つしかないし、同居の推論サーバもブリッジもコンテナ名が compose で決まっている）。
決まっているものを設定にすると、書き間違いの余地を増やすだけで得が無い。

**ユーザーが決めるのは 3 つだけ**で、それらは管理画面から入れて `app/settings_store.py` に入る。
**同居の推論サーバ（`local`）も含めて全部同じ扱い**で、特別扱いする相手は無い。

- 使うか使わないか（on/off）
- API キー（要る相手だけ。要らない相手でも、認証を掛けた相手には任意で入れられる）
- どのモデルを使うか（会話のたびに選べる。ここの `models` は候補の控え）

URL だけは `url_env` を持つ相手に限り環境変数で上書きできる。**これは「別の URL を
選べるようにする設定」ではなく、コンテナ名で辿り着けない相手のための逃げ道**である
（`local` を LAN の別マシンで動かしている場合、IP は環境ごとに違って決め打ちにできない）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# API キーの要りかた。画面の出し分けと「on にできるか」の判定に使う。
KEY_REQUIRED = "required"  # 鍵が無ければ on にできない（Gemini・OpenRouter）
KEY_OPTIONAL = "optional"  # 無くても動くが、認証を掛けた相手には入れられる（推論サーバ）
KEY_NONE = "none"  # 鍵という概念が無い（いまは該当なし）


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    url: str
    key: str
    # 使えるようにするまでの手順（画面のヘルプに出す）
    setup: str
    # 課金の形（「無料枠か」「サブスクか」を一目で分かるように）
    billing: str
    # モデル候補の控え。相手の `/v1/models` が引ければそちらを優先し、
    # 引けない相手（CLI ブリッジなど）でこれを使う。
    models: tuple[str, ...] = ()
    # **立っていなければ on にできない相手**（同居のコンテナ）。有効にしても会話のたびに
    # 失敗するだけなので、管理画面は到達を確かめてからでないと押させない。
    # 外部のサービス（Gemini・OpenRouter）は落ちていても一時的なので確認しない。
    probe: bool = False
    # URL を上書きできる環境変数。コンテナ名で辿り着けない相手のための逃げ道で、
    # 設定として増やすものではない（いまは local だけが持つ）。
    url_env: str = ""
    # 画面に出す順
    order: int = 0


# 並び順は「同居のもの」→「鍵を入れれば使えるもの」→「コンテナを立てる必要があるもの」。
PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="local",
        label="推論サーバ",
        # compose 同梱の chiezo-llm（llama.cpp）。LAN の別マシンで動かしているなら
        # CHIEZO_LLM_URL で上書きする。
        url="http://chiezo-llm:7011/v1",
        key=KEY_OPTIONAL,
        billing="自前（電気代のみ）",
        setup="docker-compose.answer.yml を重ねて "
        "`docker compose -f docker-compose.yml -f docker-compose.answer.yml --profile answer up -d` "
        "で立ち上げてください。LAN の別マシンで動かしているなら、その URL を "
        "CHIEZO_LLM_URL に設定します（認証を掛けているなら API キーも入れてください）。",
        # llama-server は 1 プロセス 1 モデルなので、名乗る名前は相手に聞くのが正しい。
        models=(),
        probe=True,
        url_env="CHIEZO_LLM_URL",
        order=0,
    ),
    Provider(
        id="gemini",
        label="Gemini",
        # **この URL の直下が chat/completions**。末尾に /v1 は付けない。
        url="https://generativelanguage.googleapis.com/v1beta/openai",
        key=KEY_REQUIRED,
        billing="無料枠（課金を有効にしなければ従量課金は発生しない）",
        setup="Google AI Studio で API キーを発行して貼り付けてください。",
        models=("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"),
        order=10,
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        url="https://openrouter.ai/api/v1",
        key=KEY_REQUIRED,
        billing="無料モデル（モデル ID の末尾が :free のもの）",
        setup="openrouter.ai で API キーを発行して貼り付けてください。"
        "モデル ID の末尾が :free のものを選べば課金されません。",
        # 提供モデルは頻繁に入れ替わるので、ここは控えにとどめる
        # （実際の一覧は /v1/models から取る）。
        models=("deepseek/deepseek-r1:free", "qwen/qwen3-coder:free", "meta-llama/llama-4-scout:free"),
        order=20,
    ),
    Provider(
        id="claude",
        label="Claude Code",
        url="http://chiezo-bridge-claude:7013/v1",
        key=KEY_REQUIRED,
        billing="Claude のサブスクリプション（定額）",
        setup="docker-compose.yml の chiezo-bridge-claude のコメントを外して起動し、"
        "手元の端末で `claude setup-token` を実行して発行したトークンをここに登録してください。"
        "（ブリッジは設定 DB を読み取り専用でマウントしているので、"
        "登録すればブリッジの再起動なしで効きます。）",
        # CLI はエイリアスで受ける（正式名はモデルが変わるたびに動く）。
        models=("fable", "opus", "sonnet", "haiku"),
        probe=True,
        order=30,
    ),
    Provider(
        id="codex",
        label="Codex CLI",
        url="http://chiezo-bridge-codex:7013/v1",
        key=KEY_REQUIRED,
        billing="ChatGPT のサブスクリプション（定額）",
        setup="docker-compose.yml の chiezo-bridge-codex のコメントを外して起動し、"
        "手元で `codex login --device-auth` して作られる ~/.codex/auth.json の中身を"
        "そのままここに登録してください（API キー経路は従量課金になるので使いません）。",
        models=(),  # CLI の既定に任せる（/v1/models が返すものを使う）
        probe=True,
        order=40,
    ),
)

BY_ID = {p.id: p for p in PROVIDERS}


def all_providers() -> tuple[Provider, ...]:
    return tuple(sorted(PROVIDERS, key=lambda p: p.order))


def get(provider_id: str) -> Provider | None:
    return BY_ID.get((provider_id or "").strip().lower())


def url_of(spec: Provider) -> str:
    """その相手の URL。`url_env` を持つ相手だけ、環境変数があればそちらが勝つ。

    コンテナ名で辿り着けない相手（LAN の別マシンで動かしている推論サーバ）のための
    逃げ道であって、設定として増やすものではない。
    """
    if spec.url_env:
        override = os.environ.get(spec.url_env, "").strip()
        if override:
            return override
    return spec.url


def label_of(provider_id: str) -> str:
    """画面に出す名前。知らない ID はそのまま返す（過去の記録を消さないため）。"""
    p = get(provider_id)
    return p.label if p else provider_id
