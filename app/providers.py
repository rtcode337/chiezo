"""話せる AI の一覧（URL・表示名・モデル候補はここに決め打ち）。

環境変数で相手を定義するのはやめた。 以前は `CHIEZO_LLM_<名前>_URL` を並べる形だったが、
URL は相手ごとに 1 つに決まっていて、ユーザーが選ぶ余地は無い（Gemini の OpenAI 互換の口は
1 つしかないし、同居の推論サーバもブリッジもコンテナ名が compose で決まっている）。
決まっているものを設定にすると、書き間違いの余地を増やすだけで得が無い。

ユーザーが決めるのは 3 つだけで、それらは管理画面から入れて `app/settings_store.py` に入る。
同居の推論サーバ（`local`）も含めて全部同じ扱いで、特別扱いする相手は無い。

- 使うか使わないか（on/off）
- 認証情報（要る相手だけ。API キー・OAuth トークン・auth.json と、相手によって中身が違う）
- どのモデルを使うか（会話のたびに選べる。ここの `models` は候補の控え）

URL だけは `url_env` を持つ相手に限り環境変数で上書きできる。**これは「別の URL を
選べるようにする設定」ではなく、コンテナ名で辿り着けない相手のための逃げ道**である
（`local` を LAN の別マシンで動かしている場合、IP は環境ごとに違って決め打ちにできない）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# 認証情報の要りかた。「API キー」と呼ばない —— API キーなのは Gemini と OpenRouter だけで、
# Claude Code は OAuth トークン、Codex は auth.json の中身が入る。画面の出し分けと「on にできるか」の判定に使う。
CRED_REQUIRED = "required"  # 認証情報が無ければ on にできない
CRED_OPTIONAL = "optional"  # 無くても動くが、認証を掛けた相手には入れられる（推論サーバ）
CRED_NONE = "none"  # 渡すものが無い（Antigravity。認証はコンテナ内のサインイン結果）

# 枠(使用量と残り)の聞き方。 相手ごとに口が違い、持たない相手のほうが多い。
# 実装は `app/usage.py`。ここに書くのは「どの口で聞くか」だけ。
USAGE_NONE = ""  # 聞く口が無い（Gemini・OpenAI・推論サーバ）
USAGE_OPENROUTER = "openrouter"  # OpenRouter の /api/v1/key（クレジットの使用額と残高）
USAGE_BRIDGE = "bridge"  # CLI ブリッジの /usage（CLI 自身に聞かせる）


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    url: str
    credential: str
    # 使えるようにするまでの手順（画面のヘルプに出す）
    setup: str
    # 課金の形（「無料枠か」「サブスクか」を一目で分かるように）
    billing: str
    # モデル候補の控え。相手の `/v1/models` が引ければそちらを優先し、
    # 引けない相手（CLI ブリッジなど）でこれを使う。
    models: tuple[str, ...] = ()
    # モデルを必ず指定しないといけない相手か。 API は指定が要る（Gemini に
    # モデル無しで投げても通らない）が、CLI ブリッジと 1 プロセス 1 モデルの推論サーバは
    # 相手が自分で決められる。False の相手では、画面で「既定」を選べば何も送らない。
    model_required: bool = True
    # 選べる「エフォート」（考える量）。相手に聞く口が無いので決め打ちする。
    # 空なら画面に出さない —— 確かめていない相手には出さない。
    # 送ると `reasoning_effort` として相手に渡る（CLI ブリッジは `--effort` に直す）。
    efforts: tuple[str, ...] = ()
    # モデルの名前に考える量が埋まっている相手か（Antigravity の
    # `gemini-3.8-flash-high` など）。**True の相手では、モデルを選んだときに
    # 考える量を送らない** —— 両方渡すと食い違う組み合わせを作れてしまい、
    # どちらが勝つかは相手次第でこちらからは読めない。
    # モデルを選ばなかったときは今までどおり送る（相手の既定モデルに効く）。
    model_carries_effort: bool = False
    # その相手に MCP（Chiezo の道具）を引かせられるか。 引けない相手では agent モードに
    # 意味が無い（道具を渡す先が無く、モデルの知識だけで答える）ので、rag に倒す。
    can_use_mcp: bool = True
    # この CLI ブリッジ（bridge/）で包んでいる相手か。 「接続を試す」の確かめ方が変わる
    # —— ブリッジは `/health?check=1` を持っていて CLI に直接聞ける（`claude auth status` 等）。
    # それ以外は OpenAI 互換の `/models` を引いて確かめる。
    bridge: bool = False
    # 枠の聞き方（`USAGE_*`。空なら聞く口が無い）。課金の形とは別物 ——
    # サブスクの相手でも枠を出す口があるとは限らない（Antigravity は CLI にしか無い）。
    usage: str = USAGE_NONE
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
        credential=CRED_OPTIONAL,
        billing="自前（電気代のみ）",
        model_required=False,
        setup="docker-compose.llm.yml を重ねて "
        "`docker compose -f docker-compose.yml -f docker-compose.llm.yml --profile llm up -d` "
        "で立ち上げてください。LAN の別マシンで動かしているなら、その URL を "
        "CHIEZO_LLM_URL に設定します（認証を掛けているなら API キーも入れてください）。",
        # llama-server は 1 プロセス 1 モデルなので、名乗る名前は相手に聞くのが正しい。
        models=(),
        # 推論サーバはブリッジではない（OpenAI 互換サーバそのもの）。
        url_env="CHIEZO_LLM_URL",
        order=0,
    ),
    Provider(
        id="gemini",
        label="Gemini",
        # この URL の直下が chat/completions。末尾に /v1 は付けない。
        url="https://generativelanguage.googleapis.com/v1beta/openai",
        credential=CRED_REQUIRED,
        billing="無料枠（課金を有効にしなければ従量課金は発生しない）",
        setup="Google AI Studio で API キーを発行して貼り付けてください。",
        # 先頭が既定。 実測(2026-08)で 2.5 系は chat/completions が 404 を返すように
        # なっていた —— 相手の一覧には残っているので、一覧に出るかどうかでは判断できない。
        # 動くことを確かめたものだけ並べる。
        models=("gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"),
        # 枠を聞く口は無い。残量は Google Cloud の Quotas API 側にあり、
        # API キー 1 本では引けない（GCP のプロジェクトと別の認証が要る）。
        order=10,
    ),
    Provider(
        id="openai",
        label="OpenAI",
        url="https://api.openai.com/v1",
        credential=CRED_REQUIRED,
        billing="従量課金(無料枠は無い)",
        setup="platform.openai.com で API キーを発行して貼り付けてください。"
        "**画像生成(image_generate の openai)でも同じ鍵を使います** —— "
        "画像だけに使うなら、話す相手としては off のままでよいです。",
        # 提供モデルは入れ替わるので控えにとどめる(実際の一覧は /v1/models から取る)。
        models=(),
        order=15,
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        url="https://openrouter.ai/api/v1",
        credential=CRED_REQUIRED,
        billing="無料モデル（モデル ID の末尾が :free のもの）",
        setup="openrouter.ai で API キーを発行して貼り付けてください。"
        "モデル ID の末尾が :free のものを選べば課金されません。",
        # 提供モデルは頻繁に入れ替わるので、ここは控えにとどめる
        # （実際の一覧は /v1/models から取る）。
        models=("deepseek/deepseek-r1:free", "qwen/qwen3-coder:free", "meta-llama/llama-4-scout:free"),
        usage=USAGE_OPENROUTER,
        order=20,
    ),
    Provider(
        id="claude",
        label="Claude Code CLI",
        url="http://chiezo-bridge-claude:7013/v1",
        # **無くても動く。** 登録が空なら、コンテナの中でサインインした結果を CLI が
        # 自分で使う（`bridge/cli_bridge.py` の `apply_credential`）。
        # **required にしてはいけない** —— 枠を取るには登録を空にする必要があるのに、
        # 空にすると二度と有効にできなくなる（実際に詰んだ）。
        credential=CRED_OPTIONAL,
        billing="Claude のサブスクリプション（定額）",
        model_required=False,
        setup="ブリッジのコンテナ **chiezo-bridge-claude** を立ててから、"
        "**どちらか一方**を選んでください。\n"
        "\n"
        "**枠（使用量）まで見たいなら、コンテナの中でサインインします。**\n"
        "ここは**登録しないまま**にしてください ——\n"
        "`docker exec -it chiezo-bridge-claude claude` を実行して `/login`。\n"
        "**登録があるとそちらが優先される**ので、登録したままサインインしても枠は出ません。\n"
        "サインイン結果を残すため、`/srv/bridge/home` を名前付きボリュームにしておくこと。\n"
        "\n"
        "**会話だけでよいなら、トークンを登録します。** 手元の端末で\n"
        "`claude setup-token` を実行し、発行されたトークンをここに登録してください。\n"
        "この長期トークンは推論だけに絞られているので、**枠は出せません**。\n"
        "\n"
        "**コンテナ名はこのとおりにすること**（Chiezo はこの名前で呼びに行きます）。\n"
        "compose があるなら docker-compose.yml の該当サービスのコメントを外すだけです。\n"
        "\n"
        "**compose のファイルが無い環境**では docker run で立てます。\n"
        "条件は「コンテナ名」と「chiezo-app と同じネットワークに繋ぐ」の 2 つだけです。\n"
        "`docker run -d --name chiezo-bridge-claude --network <chiezo と同じ> "
        "-v <state のパス>:/state:ro -e CHIEZO_BRIDGE_CLI=claude "
        "-e CHIEZO_BRIDGE_MCP_URL=http://chiezo-app:7010/mcp --restart unless-stopped "
        "ghcr.io/rtcode337/chiezo-bridge:latest`\n"
        "\n"
        "ブリッジは設定 DB を読み取り専用でマウントして読むので、"
        "登録すればブリッジの再起動なしで効きます。",
        # CLI はエイリアスで受ける（正式名はモデルが変わるたびに動く）。
        # 先頭は CLI の既定に揃える（claude の既定は claude-sonnet-5。実測）——
        # 何も選ばなかったときにここが使われるので、ずらすと黙って別のモデルになる。
        models=("sonnet", "fable", "opus", "haiku"),
        # `claude --help` の --effort（実測で 5 つとも通る）。
        efforts=("low", "medium", "high", "xhigh", "max"),
        # 枠はブリッジの `/usage` から聞く（`claude -p "/usage" --output-format json`）。
        # 実測の `result`:
        #   You are currently using your subscription to power your Claude Code usage
        #   Current session: 5% used · resets Sep 16, 7:10pm (UTC)
        #   Current week (all models): 24% used · resets Sep 20, 6am (UTC)
        #   Current week (Fable): 0% used · resets Sep 20, 6am (UTC)
        #
        # **かつては「出せない相手」だった。** 当時の観測は
        # `Total cost: $0.0000 / Usage: 0 input, 0 output`（`num_turns` も 0）で、
        # 締めの集計しか入っていなかった —— **CLI の版が上がって出るようになった**。
        # 別に試した `app.anthropic.com` の `/api/oauth/usage` は今も塞がっている
        # （あの口は `user:profile` を要求するが、預かっているのは `claude setup-token` の
        # 長期トークンで、推論だけに絞られている。実測で HTTP 403）。
        #
        # **どちらの認証情報で通ったかは、まだ突き止めていない。** 測ったときの
        # コンテナは、登録済みのトークン（依頼のたびに環境変数で置く）と、
        # 手で `/login` したぶんの両方がありうる状態だった。ブリッジが聞くときは
        # 前者しか置かないので、**画面の「取り直す」で 1 回確かめること**。
        usage=USAGE_BRIDGE,
        bridge=True,
        order=30,
    ),
    Provider(
        id="antigravity",
        label="Antigravity CLI",
        url="http://chiezo-bridge-antigravity:7013/v1",
        # API キー方式が無い。 コンテナ内で 1 回サインインした結果をホーム配下の
        # キャッシュから読むので、画面から登録する秘密は無い。
        credential=CRED_NONE,
        billing="Google AI サブスクリプション（定額）",
        model_required=False,
        setup="**API キーでは動きません。** 鍵を渡しても Google アカウントの"
        "サインインを求められます（実測）。手順は 2 段階です。\n"
        "\n"
        "**(1) コンテナ chiezo-bridge-antigravity を立てる。**\n"
        "コンテナ名はこのとおりにし、chiezo-app と同じネットワークに繋ぎ、"
        "**書き込めるホームを渡します**（サインイン結果がそこに残るので、"
        "コンテナを作り直しても消えません）。\n"
        "**ホストのディレクトリではなく名前付きボリュームを使ってください** —— "
        "ブリッジは非 root(uid 1000) で動くのに、バインド先が無いと Docker は "
        "root 所有で作るため、サインインの保存が `permission denied` で落ちます。\n"
        "`docker run -d --name chiezo-bridge-antigravity --network <chiezo と同じ> "
        "-v chiezo-antigravity-home:/srv/bridge/home -e CHIEZO_BRIDGE_CLI=antigravity "
        "-e CHIEZO_BRIDGE_MCP_URL=http://chiezo-app:7010/mcp --restart unless-stopped "
        "ghcr.io/rtcode337/chiezo-bridge:latest`\n"
        "どうしてもホストのパスに置くなら、先に作って "
        "`sudo chown -R 1000:1000 <ディレクトリ>` してからバインドします。\n"
        "\n"
        "**(2) コンテナの中で `agy` を対話で 1 回実行してサインインする。**\n"
        "`docker exec -it chiezo-bridge-antigravity agy`\n"
        "表示される URL を手元のブラウザで開き、出てきた認証コードを貼り戻します。\n"
        "コンテナ管理画面しか無い環境では、その画面のコンソール機能から同じことをします。",
        # `agy models` が返す slug（実測）。**考える量が slug に埋まっている** ——
        # だから、モデルを選んだときは `--effort` を送らない
        # (`model_carries_effort`)。両方渡すと食い違う組み合わせを作れてしまい、
        # どちらが勝つかは agy 次第でこちらからは読めない。
        #
        # **Claude と GPT は Gemini と別の枠**（週/5 時間がそれぞれに立つ）。
        # Gemini 側を使い切ったときの逃げ先になる。ただし同じ Google AI の契約なので、
        # 契約ごと替えたいときは claude や codex の相手のほうが効く。
        models=(
            "gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low",
            "gemini-3.7-flash-high", "gemini-3.7-flash-medium", "gemini-3.7-flash-low",
            "gemini-3.6-flash-high", "gemini-3.6-flash-medium", "gemini-3.6-flash-low",
            "gemini-3.1-pro-high", "gemini-3.1-pro-low",
            "claude-sonnet-4-6", "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ),
        # `agy --help` の --effort。claude と違い xhigh / max は無い。
        # **モデルを選ばなかったときだけ効く**（選んだら slug のほうが持っている）。
        efforts=("low", "medium", "high"),
        model_carries_effort=True,
        # 枠は CLI に聞くしかない。 残クレジットを取る RPC は持っているが、
        # 外から叩ける口としては公開されていない（画面の中で使われるだけ）。
        usage=USAGE_BRIDGE,
        bridge=True,
        order=36,
    ),
    Provider(
        id="codex",
        label="Codex CLI",
        url="http://chiezo-bridge-codex:7013/v1",
        credential=CRED_REQUIRED,
        billing="ChatGPT のサブスクリプション（定額）",
        model_required=False,
        setup="ブリッジのコンテナ chiezo-bridge-codex を立ててから、"
        "手元で `codex login --device-auth` して作られる ~/.codex/auth.json の中身を"
        "そのままここに登録してください（API キー経路は従量課金になるので使いません）。",
        models=(),  # CLI の既定に任せる（/v1/models が返すものを使う）
        # `codex exec --help` に --effort は無い（設定キーはあるが確かめていないので出さない）。
        efforts=(),
        # codex exec では MCP の呼び出しが必ずキャンセルされる（非対話では答えられない
        # 確認の経路に入る。`user cancelled MCP tool call`。openai/codex#16685、
        # 2026-08 時点で未修正）。こちらの設定では回避できないので agent を選ばせず rag に倒す
        # —— rag なら Chiezo が抜粋を集めてプロンプトに載せるので、道具が無くても根拠が付く。
        can_use_mcp=False,
        # 枠は CLI に聞く。 `codex app-server` の `account/rateLimits/read` が
        # 5 時間・週の使用率を返す（モデルを呼ばないので枠を食わない）。
        # 手元に控えた auth.json を直に使わないのは、access_token が期限切れになるため
        # —— CLI に聞けば、更新はあちらがやる。
        usage=USAGE_BRIDGE,
        bridge=True,
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


def efforts_of(provider_id: str) -> tuple[str, ...]:
    """その相手が**受け取れる**エフォート。

    検証に使う値（`answer.normalize_effort`）なので、画面に出さない相手のぶんも
    残す —— 既に保存されている設定から飛んでくるものを弾かないため。
    """
    p = get(provider_id)
    return p.efforts if p else ()


def selectable_efforts(provider_id: str) -> tuple[str, ...]:
    """その相手で**選ばせる**エフォート（空なら画面に出さない）。

    **モデルの名前に考える量が埋まっている相手では空**（Antigravity の
    `gemini-3.8-flash-high` など）。あそこで選べても、モデルを選んだ時点で
    送らなくなる（`model_carries_effort`）ので、**選べるのに効かない欄**になる。
    選んだつもりが効いていないのは、欄が無いより悪い。
    """
    p = get(provider_id)
    if p is None or p.model_carries_effort:
        return ()
    return p.efforts


def bridge_hostnames() -> tuple[tuple[str, str], ...]:
    """CLI ブリッジの (相手の id, ホスト名)。**そこから来た依頼を見分けるために使う。**

    ブリッジ越しの相手はシェルを持っている(`--dangerously-skip-permissions` /
    `danger-full-access`)ので、**MCP の道具を取り上げても行き先は消えない** ——
    `curl` でも Python でも REST の口を直接叩ける(実際に叩かれた: 依頼元が
    `Python-urllib/3.11` の生成が、こちらの依頼文を英訳した内容で立った)。
    塞ぐなら経路ではなく入口。
    """
    found = []
    for spec in PROVIDERS:
        if not spec.bridge:
            continue
        host = urlsplit(url_of(spec)).hostname
        if host:
            found.append((spec.id, host))
    return tuple(found)
