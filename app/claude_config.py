"""Claude Code 連携用の CLAUDE.md ブロックを生成する(生成の正はここ)。

同一プロセスから DB を直接引いて組み立てる(HTTP プローブを打たないので速く正確)。
`scripts/gen_claude_config.sh` はこの出力(`GET /admin/claude-config.txt`、
`GET /admin/claude-config.permissions.json`、`GET /admin/claude-config.hook.py`、
`GET /admin/claude-config.hook.json`)を取得してクライアント側のファイルへ
書き込むだけの薄いクライアント。管理画面は同じ出力を「いま設定を吐き出したら
何が出るか」のプレビューとして表示する。

curl 例・許可ルールのベース URL は呼び出し側(main.request_origin)が
「アクセス元 URL のプロトコル・ホスト名・ポート」から導出して渡す。
実ファイル(クライアント側の ~/.claude/CLAUDE.md 等)は API からは見えないので触らない。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app import capabilities, db, jst, notes
from app.registry import Source

BEGIN_MARK = "<!-- BEGIN chiezo (auto-generated) -->"
END_MARK = "<!-- END chiezo -->"

# クライアント側に置くフック本体のファイル名。settings.json の中身を書き換える
# ときに「以前入れた Chiezo のフック」を見分ける鍵にもなるので、変えると
# 古いエントリが残る(gen_claude_config.sh 側の掃除条件もこの名前を見ている)。
HOOK_FILENAME = "chiezo-autoallow.py"
HOOK_PATH_PLACEHOLDER = "{{HOOK_PATH}}"

# MCP サーバー登録名。Claude 側のツール名の接頭辞(mcp__chiezo__search 等)になるので、
# 変えると既存環境の登録と二重になる。
MCP_SERVER_NAME = "chiezo"

_HOOK_SOURCE = Path(__file__).parent / "hooks" / "chiezo_autoallow.py"
_ORIGIN_LINE_RE = re.compile(r'^CHIEZO_ORIGIN = "[^"]*"$', re.MULTILINE)

# links の有無を判定するために読む docs の行数(_has_links 参照)。
_LINKS_SAMPLE_ROWS = 200_000

# フッターの生成時刻に使う。人が読む行なので JST(CLAUDE.md「日時は JST で見せる」)。
# 書式ごと `app/jst.py` に集めてある —— 実行環境のローカル時刻に任せると、app コンテナの
# TZ 次第で表記が変わる。
JST = jst.JST


def _sample(src: Source) -> tuple[str, set[str], list[str]]:
    """例示に使うサンプルのタイトルと、その doc の extra のキー集合・tags。"""
    try:
        rows = db.query(src.path, "SELECT title, extra, tags FROM docs LIMIT 1")
    except sqlite3.Error:
        return "<タイトル>", set(), []
    if not rows:
        return "<タイトル>", set(), []
    title = rows[0]["title"] or "<タイトル>"
    keys: set[str] = set()
    raw = rows[0]["extra"]
    if raw:
        try:
            keys = set(json.loads(raw).keys())
        except (ValueError, TypeError):
            keys = set()
    try:
        tags = json.loads(rows[0]["tags"]) if rows[0]["tags"] else []
    except (ValueError, TypeError):
        tags = []
    return title, keys, tags


def _quotable(src: Source) -> bool:
    """このソースの中身(タイトル・タグ)を例示に引き写してよいか。

    例示は DB の実データから採る。公開ダンプ由来のソース(wikipedia・osm・geonames…)なら
    生成物に載るのは公開情報の引き写しにすぎないが、notes はユーザーが手元で書いたメモで、
    機密が混じりうる。CLAUDE.md ブロックはリポジトリ側(`--project`)にも生成できるので、
    メモの見出しやタグがそのまま載るとコミットされて意図せず共有される。
    書き込めるソース(= 中身が手元で作られるソース)は実データを引かず、
    `<タイトル>` のようなプレースホルダーだけで例示する。
    """
    return not src.mutable


def _has_value(src: Source, column: str) -> bool:
    """生成列(v2 のみ)に非 NULL の行が 1 件でもあるか。索引があるので速い。

    column は呼び出し側の固定リテラル('lat'/'wikidata')のみ。ユーザー入力は渡さない。
    """
    try:
        rows = db.query(src.path, f"SELECT 1 FROM docs WHERE {column} IS NOT NULL LIMIT 1")
    except sqlite3.Error:
        return False
    return bool(rows)


def _has_links(src: Source) -> bool:
    """links(出リンク先タイトルの配列)が入っているソースか。

    links は生成列でも索引付きでもないので、`WHERE links IS NOT NULL LIMIT 1` を
    そのまま打つと、入っていないソースほど遅い(1 件も無いと全表を舐める。
    実測: geonames 13,391,482 件で 3.2 秒。ここは判定 1 個のために払う額ではない)。
    知りたいのは「このソースは links を持つ設計か」だけなので、先頭の一定件数だけ
    見て決める。links を作るソース(wikipedia)は全記事に入るし、一部にしか入らない
    ソース(osm は wikipedia タグのある地物だけ)でもこの範囲に必ず現れる。
    """
    try:
        rows = db.query(
            src.path,
            "SELECT 1 FROM (SELECT links FROM docs LIMIT ?)"
            " WHERE links IS NOT NULL LIMIT 1",
            (_LINKS_SAMPLE_ROWS,),
        )
    except sqlite3.Error:
        return False
    return bool(rows)


def _short(name: str) -> str:
    """相手の名前から但し書きを落とす(`ElevenLabs(声・効果音・曲・絵・動画)` → `ElevenLabs`)。

    表の名前は「どの相手か」を見分けるためのもので、**この節では節そのものが用途を
    言っている**ので繰り返しになる。ブロックは毎回のコンテキストに乗るので短く保つ。
    """
    return name.split("(")[0].strip() or name


def _who(usable: dict[str, set[str]] | None, *kinds: tuple[str, str]) -> str:
    """「いま使えるのは A・B」の 1 文。相手が分からない・いなければ空。

    使えない相手は書かない。 勧められて呼んでから断られるのでは、
    読んだ側は何が悪いのか分からない。

    分類を複数渡せるのは、1 つの節が 2 つの分類を扱うため(音の節は曲と効果音、
    声の節は読み上げと文字起こし)。相手が同じなら 1 文にまとめ、違えば書き分ける
    —— 「曲は作れるが効果音は作れない」相手がいるので、まとめると嘘になる。
    """
    # **相手の名前は焼き込まない。** ソースと同じ理由 —— 生成した瞬間の写しになり、
    # 相手を足したり止めたりするたびに古くなる。古い名前は、いない相手へ投げさせ、
    # **いる相手を使わせない**。どこで聞けるかだけ書く。
    if not usable:
        return ""
    found = [(label, [_short(n) for n in capabilities.providers_for(usable, cap)])
             for cap, label in kinds]
    ready = [label for label, names in found if names]
    if not ready:
        return ""
    return (f"**{'・'.join(ready)}を頼める相手は、そのときどきで変わる。** "
            "名前を覚えず、毎回一覧で確かめること。")


# 手分けの相手に出さない相手。 このブロックを読むのは Claude Code なので、
# Chiezo 越しにまた Claude Code へ投げても同じサブスクの枠を食うだけで、
# 手分けの目的(自分の枠を空けて、別の視点も入れる)から外れる。
# 名指しで頼みたいときは `/v1/ai/backends` に出ているので、経路は塞がない。
HELPER_EXCLUDED = frozenset({"claude"})


def _chat_helpers(usable: dict[str, set[str]] | None) -> list[str]:
    """手分けを頼める相手の ID(画面と同じ並び。自分自身は除く)。"""
    if not usable:
        return []
    return [
        pid for pid in capabilities.all_provider_ids()
        if capabilities.CHAT in usable.get(pid, set()) and pid not in HELPER_EXCLUDED
    ]


def permission_rules(base_url: str) -> list[str]:
    """settings.json の permissions.allow に追記される Chiezo への curl 許可ルール。

    Bash ルールはコマンド文字列の前方一致なので、実際に打たれる形のぶんだけ変種が要る:
    `-s`(単純 GET)/`-sG`(--data-urlencode 併用)× URL のクォート有無 の 4 本。
    `/v1/` 個別ルールは `/` 前方一致に包含されるので出さない。
    スクリプトはこれをソート・重複排除しながら既存 allow に足す(jq の `unique` と
    python3 の `sorted(set(...))` で同じ結果になる)ので、ここでもソート済みで返す。
    """
    base = base_url.rstrip("/")
    return sorted(
        f"Bash(curl {flags} {quote}{base}/:*)"
        for flags in ("-s", "-sG")
        for quote in ("", '"')
    )


def permission_json(base_url: str) -> str:
    """権限ファイル(新規作成時)の中身。既存ファイルには allow へ追記マージされる。"""
    return (
        json.dumps(
            {"permissions": {"allow": permission_rules(base_url)}},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def mcp_servers_json(base_url: str) -> str:
    """MCP サーバー登録の断片(`.mcp.json` の中身そのもの)。

    プロジェクト用 `.mcp.json` には新規作成ならこのまま、既存があれば
    `mcpServers.chiezo` としてマージされる。ユーザースコープは Claude Code CLI
    (`claude mcp add --scope user`)経由で登録するので、この JSON は使わず
    URL(`<base>/mcp`)だけを使う(いずれも gen_claude_config.sh 側の仕事)。
    """
    base = base_url.rstrip("/")
    return (
        json.dumps(
            {
                "mcpServers": {
                    MCP_SERVER_NAME: {"type": "http", "url": f"{base}/mcp"},
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def hook_script(base_url: str) -> str:
    """クライアントへ配る PreToolUse フック本体(`app/hooks/chiezo_autoallow.py`)。

    前方一致の `permissions.allow` は curl が先頭に来る形にしか効かないので、
    ループやパイプで大量に引くとき(= いちばん許可したい場面)には 1 本もマッチ
    しない。フックはコマンドを構造で見て「Chiezo だけを読む」ものを自動許可する。
    ここでは配信時にベース URL を埋め込むだけで、判定ロジックはフック側にある。
    """
    base = base_url.rstrip("/")
    src = _HOOK_SOURCE.read_text(encoding="utf-8")
    replaced, n = _ORIGIN_LINE_RE.subn(f"CHIEZO_ORIGIN = {json.dumps(base)}", src)
    if n != 1:
        # 差し替え対象を見失ったまま配ると localhost 宛てのフックを配ることになる。
        raise RuntimeError(f"CHIEZO_ORIGIN を {_HOOK_SOURCE} に見つけられない(置換 {n} 件)")
    return replaced


def hook_settings_json() -> str:
    """settings.json の `hooks` へマージされる断片。

    フック本体の設置先はクライアント側(`--user` か `--project` か)で変わり
    app からは見えないので、コマンドは `{{HOOK_PATH}}` のままにしておき、
    絶対パスへの差し替えは gen_claude_config.sh が行う。
    """
    return (
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": HOOK_PATH_PLACEHOLDER,
                                    "timeout": 10,
                                }
                            ],
                        }
                    ]
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _spaced(word: str) -> str:
    """欧文の語を日本語の地の文に入れるときは前後に空白を置く。

    「依頼文はEnglishで書く」は詰まって読みにくい(日本語は分かち書きしないので、
    欧文だけが単語の切れ目を失う)。全角の語はそのままでよい。
    """
    return f" {word} " if word.isascii() else word


def build_block(
    sources: dict[str, Source],
    base_url: str,
    now: datetime | None = None,
    hook: bool = False,
    mcp: bool = False,
    media: bool = False,
    usable: dict[str, set[str]] | None = None,
    prompt_language: str = "",
) -> str:
    """CLAUDE.md に貼る Chiezo ブロック(マーカー込み)を組み立てて返す。

    `hook=True` のときだけ、自動許可フックに載る書き方の指示を足す。フックは
    クライアント側で `--with-hook` を指定したときにしか入らないので、既定で
    書いてしまうと入れていない環境には嘘になる(gen_claude_config.sh が
    フックを設置するときだけ `?hook=1` で取りに来る)。
    `mcp=True` も同じ理屈で、MCP サーバーを登録した環境にだけ使い分けの指示を足す。
    こちらはスクリプト側の既定が「登録する」なので、通常は `?mcp=1` で来て、
    `--no-mcp` を指定されたときだけ `?mcp=0` になる。

    `media=True` はこの Chiezo で絵・音・動画・声を扱えるときだけ(置き場があり「答える」層が
    有効)。道具は MCP 経由なので `mcp=True` と揃ったときにしか書かない ——
    登録していない環境に「`image_generate` を使え」と書いても呼べない。

    `usable`(`app/capabilities.py` の `usable_now()`)はいま実際に頼める相手。
    渡すと相手を名指しで書く —— 「外部の生成 AI を選べる」と抽象的に書くより、
    「いま使えるのは ElevenLabs / Codex CLI」と書いたほうが読んだ側が動ける。
    名指しするのは使える相手だけ(鍵の無い相手を勧めると、呼んで断られるまで
    分からない)。話せる相手がいれば手分けの節も足す。
    """
    base = base_url.rstrip("/")
    when = jst.format(now or datetime.now(JST))

    out: list[str] = [
        BEGIN_MARK,
        # 見出しは「何を借りられるか」を並べる。知識だけを名乗っていた頃は、
        # 同じブロックに載っている「手分けして調べる」「絵を作る」に読み手が気づかなかった
        "## Chiezo(LAN 内の知識・AI・生成サーバー)",
        "",
        "LAN 内に読み取り専用の知識検索 API「Chiezo」がある。下記ソースに載っている情報が"
        "必要になったら、**Web 検索や外部 API より先に Chiezo を使うこと**"
        "(オフライン・レート制限なし・高速)。",
        f"ベース URL: `{base}`",
        "",
        "使い方の要点:",
        "- まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)。"
        "いきなり全文を取らない。",
        "- 3 文字未満の語はタイトル前方一致にフォールバックする"
        "(レスポンスの `mode` が `title_prefix` になる)。",
        "- 日本語・スペース等を含むパラメータ(`q`/`title`/`prefix`/`area`/`feature`/`tag`)は"
        "下記例のとおり `curl -sG --data-urlencode` で渡す"
        "(URL に直接埋め込むとサーバーに Invalid HTTP request で弾かれる)。",
        '- 応答は JSON。エラーは `{"error": "..."}` 形式。全クエリ 5 秒でタイムアウト(超過は 504)。'
        "**504 は「0 件」ではなく「取れなかった」を意味する**ので、空の結果として先へ進まず、"
        "`limit` を小さくして取り直す(そのまま続けると静かに取りこぼす)。",
        f'- ソース一覧(最新の登録状況): `curl -s "{base}/v1/sources"`',
    ]

    if helpers := _chat_helpers(usable):
        # 知識を引く話とは別物なので節を分ける。ここも短く保つ ——
        # 毎回のコンテキストに乗るぶんが増えるので、手順は 1 行ずつにとどめ、
        # 相手ごとの細かい話は `/v1/ai/backends` に聞かせる。
        named = "・".join(f"{capabilities.label_of(pid)}(`{pid}`)" for pid in helpers)
        # 例示の本文は json.dumps で作る。 手で組むとクォートの入れ子で必ず崩れ、
        # 崩れた curl をそのまま打たれる(読む側はコピーして使う)。
        payload = json.dumps(
            {"backend": helpers[0],
             # **名乗りは必ず入れて配る。** 口としては任意だが、無人で回る層と
             # 外のアプリが同じ表に並ぶので、名乗りの無い依頼は後から誰のものか
             # 辿れない(待たされている人が「これは自分のか」を判断できない)
             "requested_by": "<あなたの名乗り。例: claude-code>",
             "messages": [{"role": "user", "content": "<調べてほしいこと>"}]},
            ensure_ascii=False,
        )
        out += [
            "",
            "### 手分けして調べる(このサーバー越しに別の AI へ頼める)",
            "",
            "調べものが広いとき・別の観点でも当たりたいときは、**自分だけで抱えずに "
            "Chiezo 越しに別の AI へ手分けして頼む**。鍵はサーバー側にあるので、"
            "こちらは相手を選んで投げるだけでよい。"
            f"**いまこのサーバーで頼めるのは {named}**。",
            f'- 頼む → `curl -s "{base}/v1/ai/complete" -H \'Content-Type: application/json\' '
            f"-d '{payload}'`"
            "(**渡した文がそのまま相手に届く**。知識ベースの抜粋は混ざらない)",
            f'- 相手・モデル・web 検索の有無 → `curl -s "{base}/v1/ai/backends"`',
            '- **外の最新情報が要るなら `"web": true` を足す**'
            "(相手自身の web 検索が開く。持たない相手には 400 で断られる)",
            "- **`requested_by` は必ず入れる**(何を名乗るかは自由。サーバーは必須に"
            "していないが、無名の依頼は管理画面の依頼元が空欄になり、"
            "誰が枠を食っているのか後から辿れない)",
            "- **1 回 1 往復**で履歴は持たない。長い調査は問いを分けて**並行に投げ**、"
            "突き合わせはこちらでやる",
            "- **相手は数十秒〜数分かかる**(CLI を包んだ相手はとくに)。"
            "投げたら他の作業を進める",
            f'- 枠の残りが気になるときは `curl -s "{base}/v1/ai/usage"`'
            "(相手が枠を出す場合のみ。使いすぎている相手は避ける)",
        ]

    if mcp and media:
        # 知識を引く話とは別物なので、節を分けて短く書く。ここに長い説明を積むと、
        # 毎回のコンテキストに乗るぶんが増えるだけで、実際に呼ぶときの手順は道具の
        # description に書いてある
        out += [
            "",
            "### 絵を作る(このサーバーで生成できる)",
            "",
            "画像が要るとき(ゲーム素材・図版・アイコン)は、**外のサービスを探しに行く前に"
            f"`mcp__{MCP_SERVER_NAME}__image_generate` を使う**。自前の GPU か外部の"
            "生成 AI を選べて、鍵はサーバー側にある。"
            + _who(usable, (capabilities.IMAGE, "画像")),
            f"- 頼む → `mcp__{MCP_SERVER_NAME}__image_generate`(job が返る。**待たない**)",
            f"- 仕上がり → `mcp__{MCP_SERVER_NAME}__image_status`(**パスと URL** が返る。"
            "画像そのものは返らないので、要るならそのパスを読む)",
            f"- 相手とモデル → `mcp__{MCP_SERVER_NAME}__image_backends`"
            "(使えない相手は理由つきで出る)",
            "- **サイズは `image_backends` の一覧から選ぶ**(自前の GPU は一覧以外を断る)。"
            "アイコンのような小さい素材も、一覧のサイズで描いてから手元で縮小する",
            "- **同じ絵を作り直すときは seed を指定する**(自前の GPU のときだけ再現できる)。"
            "キャラの別ポーズのように「揃えたい」場面で効く",
            # 名乗りは絵だけの話ではないので、1 か所で言って「どれも同じ」と添える。
            # 種類ごとに繰り返すと、毎回のコンテキストに乗るぶんが 4 倍になる
            "- **`requested_by` に必ず名乗る**(例 `requested_by=\"claude-code\"`)。"
            "サーバーは必須にしていないが、無名の依頼は管理画面の依頼元が空欄になり、"
            "誰が枠を食ったのか後から辿れない。**絵・音・動画・声のどれも同じ**",
            *(
                [f"- **依頼文は{_spaced(prompt_language)}で書く**(このサーバーの設定。"
                 "絵・音・動画・声のどれも同じ)"]
                if prompt_language else []
            ),
            "",
            "### 音を作る(このサーバーで生成できる)",
            "",
            "効果音や BGM が要るときも、**外のサービスを探しに行く前に"
            f"`mcp__{MCP_SERVER_NAME}__audio_generate` を使う**。作りは絵と同じ。"
            + _who(usable, (capabilities.MUSIC, "曲"), (capabilities.SFX, "効果音")),
            f"- 頼む → `mcp__{MCP_SERVER_NAME}__audio_generate`"
            "(`sound=\"sfx\"` で効果音、`sound=\"music\"` で曲。job が返る。**待たない**)",
            f"- 仕上がり → `mcp__{MCP_SERVER_NAME}__audio_status`(**パスと URL** が返る)",
            f"- 相手と上限 → `mcp__{MCP_SERVER_NAME}__audio_backends`"
            "(**頼める種類と長さの上限は相手ごとに違う**。効果音を作れない相手もいる)",
            "- **長さ(`seconds`)は上限を超えると断られる**。黙って短くはしないので、"
            "長い曲が要るときは上限の大きい相手を選ぶ",
            "- **BGM は `lyrics` を空のままにする**(器楽で作る)。歌が入ると台詞と喧嘩する",
            "",
            "### 何案か作って選んでもらう",
            "",
            "**どれがいいか人に決めてほしいときは、`group` に同じ名前を付けて何案か頼む**"
            "(例: `group=\"ホームランの音\"`)。同じ名前の依頼は管理画面の「見比べ」に"
            "まとまって並び、依頼文つきで見比べ・聴き比べできる。"
            "**音は自分では聴けないので、聴いてもらう手段はここしかない**。",
            f"- 選ばれたものを知る → `mcp__{MCP_SERVER_NAME}__media_picks`"
            "(`group` で絞れる。添えられた一言も返る)",
            "- **会話で「何番がいい?」と聞かない。** 選ぶ人と自分が別のやり取りにいると"
            "拾えないので、印は画面で付けてもらい、ここを見に来る",
            "- 1 組から選ばれるのは 1 つ。選び直されたら前の印は外れている",
            "",
            "### 動画を作る(このサーバーで生成できる)",
            "",
            f"動画が要るときも `mcp__{MCP_SERVER_NAME}__video_generate` を使う。作りは絵と同じだが、"
            "**待ち時間の桁が違う**(数分〜十数分)ので、頼んだら他の仕事をして後から引きに来る。"
            + _who(usable, (capabilities.VIDEO, "動画")),
            f"- 頼む → `mcp__{MCP_SERVER_NAME}__video_generate`(job が返る。**待たない**)",
            f"- 仕上がり → `mcp__{MCP_SERVER_NAME}__video_status`(**パスと URL** が返る)",
            f"- 相手と尺 → `mcp__{MCP_SERVER_NAME}__video_backends`",
            "- **尺(`seconds`)は一覧にある値だけ**(Sora は 4/8/12、Veo は 4/6/8)。"
            "近い値に丸めたりはしないので、一覧を見てから頼む",
            "- **1 本で数十 MB あり、置き場は 14 日で掃除される**。要るものは早めに引き取る",
            "",
            "### 声を扱う(このサーバーでできる)",
            "",
            f"読み上げは `mcp__{MCP_SERVER_NAME}__speech_generate`、"
            f"文字起こしは `mcp__{MCP_SERVER_NAME}__transcribe`。"
            f"相手と声は `mcp__{MCP_SERVER_NAME}__voice_backends` で引く。"
            + _who(usable, (capabilities.SPEECH, "読み上げ"), (capabilities.TRANSCRIBE, "文字起こし")),
            "- **読み上げは job**(パスと URL が返る)、**文字起こしはその場で文字が返る**",
            "- **自前の GPU に読み上げは頼めない**(ComfyUI 本体に TTS のノードが無い)",
            "- 文字起こしに渡せるのは **chiezo が作ったもの(`/media/…`)か、"
            "サーバーから届く URL** だけ。手元のファイルは "
            f"`POST {base}/v1/media/transcribe` に multipart で送る",
        ]

    if mcp:
        out.append(
            f"- MCP ツール(`mcp__{MCP_SERVER_NAME}__search` / `doc` / `filter` 等)も"
            "同じ機能を提供している。**単発の参照は MCP ツールを優先**してよい"
            "(引数が構造化されていて URL エンコードの失敗が無い)。"
            "**大量取得・後処理があるときは curl** — MCP の応答は必ずコンテキストを"
            "通るので、ページングや突合はファイルに落とせる curl のほうが向く。"
        )

    if hook:
        out.append(
            "- 大量に引くときは `for` ループやパイプにまとめてよい(許可プロンプトは出ない)。"
            "ただし Chiezo 以外のホストを混ぜる・`$(...)`・`eval`・`sed`/`awk`・"
            "ファイルへの書き出し(`curl -o`、`> file`)を使うと自動許可から外れて"
            "毎回プロンプトになるので、取得は `curl`→`jq` の読み取りだけで完結させる。"
        )

    # **ソースの一覧は焼き込まない。** 以前は 23 ソースぶんの curl 例を並べていたが、
    # あれは生成した瞬間の写しで、**増えるたびに人が再生成しないと古いまま**になる。
    # 古い一覧は害のほうが大きい —— 無いものへ投げるし、**あるのに使わない**。
    # 引き方だけ書いて、何があるかは毎回聞かせる。
    out += [
        "",
        "### どのソースがあるかは、毎回聞く",
        "",
        f'**一覧 → `curl -s "{base}/v1/sources"`。** ここに載っていないソースは無い。'
        "返るのは名前・種類・件数に加えて、**そのソースで何ができるか**(`can`)と"
        "**一言**(`note`)、例示に使える実在の値(`sample`)。"
        "`can` に入っていない引き方は、そのソースでは通らない。",
        "",
        "| `can` | 引き方（`<source>` は一覧の `name`） |",
        "|---|---|",
        f'| `search` | `curl -sG "{base}/v1/<source>/search?limit=5" --data-urlencode "q=<語>"` |',
        f'| `doc` | `curl -sG "{base}/v1/<source>/doc?fields=title,opening,tags"'
        ' --data-urlencode "title=<タイトル>"`（本文は `max_chars=8000`） |',
        f'| `titles` | `curl -sG "{base}/v1/<source>/titles" --data-urlencode "prefix=<語>"` |',
        f'| `tags` | `curl -sG "{base}/v1/<source>/tags?limit=20" --data-urlencode "contains=<語>"`'
        "（実在するタグ名を件数つきで。`filter` は完全一致なので先にここで確かめる） |",
        f'| `filter` | `curl -sG "{base}/v1/<source>/filter?limit=200" --data-urlencode "tag=<タグ>"`'
        "（`total` で件数、`offset` でページング） |",
        f'| `bbox` | `curl -sG "{base}/v1/<source>/filter?bbox=<南>,<西>,<北>,<東>"`'
        "（座標で範囲抽出。**ジオコーディング API を叩く必要はない**） |",
        f'| `wikidata` | `curl -s "{base}/v1/<source>/filter?wikidata=Q17221&fields=title,extra"`'
        "（Q 番号 → 文書。**wikidata.org を叩く必要はない**） |",
        f'| `links` | `curl -sG "{base}/v1/<source>/links" --data-urlencode "title=<タイトル>"`'
        "（**出リンクのみ**で、被リンク(この記事を指している記事)は取れない。"
        "重複と `記事名#節名` が混じるので、`doc` に渡す前に重複を落として"
        "`#` の前で切ること） |",
        f'| `pageviews` | `curl -sG "{base}/v1/<source>/doc?fields=title,extra"'
        ' --data-urlencode "title=<タイトル>"`（`extra.pageviews_month` が知名度の目安。'
        "**Wikimedia の API を叩く必要はない**） |",
        "",
        "- **同名の別物があるときは `doc` の応答に `alternatives` が付く。**"
        ' `--data-urlencode "area=<行政区>"` や `--data-urlencode "feature=<種別>"` で絞る',
        "- **カテゴリの列挙は `filter?tag=` を使う。** 本文の全文検索で `Category:` 行を"
        "探してはいけない —— ソートキー付きの記事(`[[Category:X|よみがな]]`)は"
        "本文側にカテゴリ名が残らず、静かに取りこぼす",
    ]

    out.append("")
    # 再生成の案内には今回のフラグを引き継がせる(既定と違う選択をした環境で
    # フラグ無しで再生成すると、実際の設置状態と本文の指示がずれるため)。
    # MCP は既定で登録するので、引き継ぐのは opt-out した `--no-mcp` のほう。
    regen = f"scripts/gen_claude_config.sh --base-url {base}"
    if not mcp:
        regen += " --no-mcp"
    if hook:
        regen += " --with-hook"
    out.append(
        f"<sub>この一覧は {when} 時点の Chiezo(`{base}`)の登録ソースから自動生成。"
        f"再生成: `{regen}`</sub>"
    )
    out.append(END_MARK)
    return "\n".join(out) + "\n"


# ソースごとの一言。**kind から引く**（ソースが増えても書き足さずに済む）。
_SOURCE_NOTES = {
    "wikipedia": "一般知識・人物・作品・地名・用語・出来事など",
    "osm": "地名・行政区・自然地物に加え、病院/学校/店舗/観光地などの施設、"
           "駅・空港・港・IC/SA などの交通インフラと座標",
    "overture": "飲食店・宿・小売・寺社・駅などの POI と座標。"
                "**OSM 辞典より件数が多く、店の URL と電話番号が入っていることが多い**"
                "（代わりに住所は自由記述）",
    "geonames": "全世界の地名と座標",
    notes.SOURCE_KIND: "手元で書いた短期記憶。**中身は例示に引き写さない**",
    "collect": "AI が定期的に集めて焼いたもの。何が入っているかは中身を見る",
}


_SOURCE_HINTS = {
    "osm": ["`links` が返すのは `wikipedia` タグから作った対応記事のタイトル。"
            "言語プレフィックスは外してあるので、Wikipedia 側の `doc?title=` に"
            "そのまま渡せる（タグの無い地物は空配列）"],
    "overture": ["カテゴリ名は独自なので、**推測で書かずに `tags?contains=` で確かめる**"],
    notes.SOURCE_KIND: ["手元で書いたもの。**中身を例示に引き写さない**（`sample` は空で返る）"],
}


def describe(src: Source) -> dict:
    """そのソースの説明と、**何ができるか**。

    **設定ファイルに焼き込まない。** 以前は 23 ソースぶんの curl 例を CLAUDE.md に
    並べていたが、あれは生成した瞬間の写しで、**ソースが増えるたびに人が再生成しないと
    古いまま**になる。古い一覧は害のほうが大きい —— 無いものへ投げるし、
    **あるのに使わない**。一覧は引くもの、という形へ寄せる。

    返すのは「何ができるか」（`can`）と一言（`note`）と、例示に使える実在の値
    （`sample`）まで。**URL の組み立て方は設定側に置く** —— そちらは
    ソースが増えても変わらない知識なので、焼き込む価値がある。
    """
    title, extra_keys, sample_tags = _sample(src)
    if not _quotable(src):
        title, sample_tags = "", []
    can = ["search", "doc", "tags", "titles", "filter"]
    if _has_value(src, "lat"):
        can.append("bbox")
    if "wikidata" in extra_keys:
        can.append("wikidata")
    if _has_links(src):
        can.append("links")
    if "pageviews_month" in extra_keys:
        can.append("pageviews")
    return {
        "note": _SOURCE_NOTES.get(src.kind, ""),
        # **そのソースだけの注意。** 引き方の一般則は設定側にあるので、
        # ここに置くのは「この kind でだけ効く癖」に限る
        "hints": _SOURCE_HINTS.get(src.kind, []),
        "can": can,
        "extra": sorted(extra_keys),
        "sample": {"title": title, "tag": (sample_tags or [""])[0]},
    }
