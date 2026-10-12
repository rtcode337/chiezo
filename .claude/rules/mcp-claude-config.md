---
paths:
  - "app/mcp_server.py"
  - "app/claude_config.py"
  - "app/hooks/**"
  - "scripts/gen_claude_config.sh"
  - "tests/test_mcp.py"
  - "tests/test_claude_hook.py"
---

# MCP と Claude Code 連携の設定生成

MCP サーバー(`/mcp`・`/mcp/knowledge`)と、Claude Code 向けの設定(CLAUDE.md ブロック・権限・フック・MCP 登録)の生成。

## MCP サーバー

- `app/mcp_server.py` — **MCP サーバー(`/mcp`、Streamable HTTP・ステートレス)**。
  使い方は `docs/api-reference.md`「MCP から使う」節。ツールの実体は `app/main.py` のエンドポイント関数
  そのもので、MCP 用に処理を書き直していない(別実装にすると片方だけ直されて必ずずれる)。
  そのぶん**踏み抜きやすい罠が 3 つ**あるので、触るときは順に確認すること:
  - FastAPI のエンドポイントは既定値が `Query(...)` オブジェクトなので、Python から
    直接呼ぶときに**全パラメータを明示的に渡す**必要がある。渡し忘れると Query
    インスタンスが値として入り、`if tag:` が常に真になる等、例外にならず静かに壊れる。
    `tests/test_mcp.py::TestStaysInSyncWithRest` がシグネチャを突き合わせて落とす
  - MCPServer は**同期のツール関数をイベントループ上で直接呼ぶ**(await するのは async
    関数だけ)。Chiezo のクエリは最大 5 秒ブロックしうるので、必ず `run_in_threadpool`
    に逃がす。でないと重いクエリ 1 本で API 全体が止まる
  - `TransportSecuritySettings` の既定は「localhost 系の Host しか受け付けない」。
    そのままだと LAN の別マシンから叩いた時点で 421 になる。**未設定なら本体と同じ判定
    (`web_guard.HostMatcher`。`CHIEZO_ALLOWED_HOSTS` が空なら何でも通す)を渡す** —— SDK の一覧は完全一致と `名前:*` しか
    書けず「IP なら何でも」を表せないので、`in` に答えるだけの判定を `model_construct` で
    検証を通さずに入れる(SDK は `in` で確かめるだけ)。`CHIEZO_MCP_ALLOWED_HOSTS` を書けば
    今までどおりその一覧だけで絞る
  セッションマネージャは lifespan の中で `run()` する必要があり(python-sdk#1367)、かつ
  1 インスタンス 1 回しか呼べないため、**起動ごとに `build_mcp()` で作り直して**
  `app.state.mcp_asgi` に置き、マウント先(`main._mcp_asgi`)がそれを見る形にしている
  (モジュール読み込み時に作り置きすると、同一プロセスで二度起動するテストが落ちる)。
  **ステートレス・待ち受けパス・Host 検証は `build_mcp_app()`(= `streamable_http_app()`)
  側の設定**で、mcp 2.x でサーバー本体の引数から移った(1.x の `FastMCP(...)` に
  まとめて渡していた頃の書き方は通らない)

## Claude Code 連携の設定生成

- `/admin/claude-config`(GET/HTML)・`/admin/claude-config.txt`(GET/text/plain)・
  `/admin/claude-config.permissions.json`(GET/application/json)・
  `/admin/claude-config.mcp.json`(GET/application/json)・
  `/admin/claude-config.hook.py`(GET/text/x-python)・
  `/admin/claude-config.hook.json`(GET/application/json) —
  Claude 連携設定の生成 API。現在の登録ソースから CLAUDE.md ブロックと
  **権限ファイル(`settings.json`/`settings.local.json` の `permissions.allow`)**、
  および任意で入れる**自動許可フック**を生成して配信する(実ファイルは書き換えない。
  ホームの `~/.claude/CLAUDE.md` 等はクライアント側にあり app からは見えないため)。
  `gen_claude_config.sh` はこれらのエンドポイントを取得して書き込む。
  HTML はプレビュー + コピーボタン付き。
  curl 例・許可ルールのベース URL はアクセス元 URL のプロトコル・ホスト名・ポート
  (`request_origin`: スキーム=`X-Forwarded-Proto`(あれば)、ホスト=`X-Forwarded-Host`
  (あれば)→無ければ `Host` ヘッダ。`Host` はポートを保持する)から導出するので、
  リバースプロキシ越し・非標準ポートでもそのまま到達可能な URL になる
  - `.txt` の `?hook=1` は「自動許可フックを入れる前提の書き方の指示」を本文に足す。
    フックはクライアント側で `--with-hook` を指定したときしか入らないので、
    既定で書くと入れていない環境には嘘になる。設置するときだけ付けて取りに来る。
    `?mcp=1` は MCP 登録済み環境向けの使い分けの指示(単発の参照は MCP・大量取得は
    curl)を足す。**MCP 登録はスクリプトの既定**なのでこちらは通常付いてくる
    (`--no-mcp` のときだけ落ちる。HTML プレビューも既定に合わせて `mcp=True` で出す)
  - `.mcp.json` は MCP サーバー登録の断片(`mcpServers.chiezo` → `<base>/mcp`)。
    プロジェクト用 `.mcp.json` へのマージやユーザースコープの `claude mcp add` は
    スクリプト側の仕事(登録名の正は `claude_config.MCP_SERVER_NAME`)
- `app/hooks/chiezo_autoallow.py` — 上記フックの実体(`PreToolUse` フック)。
  Claude Code の `permissions.allow` は**コマンド文字列の前方一致**でしか判定できず、
  `for … do curl … done` やパイプに包まれた curl には 1 本もマッチしない。
  大量取得は必ずその形になるので、いちばん許可したい場面でルールが効かない。
  このフックは前方一致ではなく**構造**(登場する URL が全て Chiezo か・コマンド位置に
  来る語が読み取り専用の許可リスト内か・`$(…)`/`eval` 等でコマンド位置を隠していないか)
  で判定し、条件を満たすときだけ `permissionDecision: "allow"` を返す。
  外れたら**何も出力しない**(= 通常の許可プロンプトに戻る)ので、判定に迷ったら
  黙るのが正。app プロセスからは import も実行もされず、`claude_config.hook_script()`
  がソースを読んで `CHIEZO_ORIGIN` の行だけ差し替えて配信する。文字列テンプレートに
  せず実ファイルで持つのは、通常の Python として lint・テストできるようにするため
  (`tests/test_claude_hook.py` が判定の許可/拒否を両側から固定している)
- `app/claude_config.py` — 上記ブロックの生成ロジック。**生成の正はここ(app 側)に置く**。
  同一プロセスの DB を schema_version と索引付きの `WHERE lat IS NOT NULL LIMIT 1` 等で
  直接引くので、HTTP プローブと違い巨大ソース(jawiki)でも timeout の false-negative が出ない。
  ただし**索引の無い列を同じ形で探ってはいけない**: `links` は生成列でも索引付きでもないので
  `WHERE links IS NOT NULL LIMIT 1` は 1 件も無いソースほど全表を舐める(geonames 1300 万件で
  3.2 秒)。`_has_links()` は先頭 `_LINKS_SAMPLE_ROWS` 行だけ見て判定する
  - **書き込めるソース(`Source.mutable` = notes)の中身は例示に引き写さない**(`_quotable()`)。
    例示のタイトル・タグは DB の実データから採るが、notes はユーザーが手元で書いたメモで
    機密が混じりうる。ブロックは `--project` でリポジトリ側にも生成できるので、
    見出しやタグが載るとコミットされて意図せず共有される。`<タイトル>`・`<タグ名>` に落とす
  - **文書数は載せない**。取り込みと notes への書き込みで変わり、ブロックを貼り替えない限り
    古い数字が残る。正確な件数はブロック自身が案内している `/v1/sources` で引ける
  - 生成時刻のフッターは **JST 固定**(`JST` 定数)。人が読む行なので、app コンテナの
    TZ 次第で表記が変わらないようにする

## CLI ブリッジ向けの MCP

- `/mcp/knowledge`(POST) — **CLI ブリッジ向けの MCP。読むだけの道具しか出さない**
  (`query` で SQL も引けるが、通すのは SELECT だけ)
  (生成の道具も、短期記憶へ書く remember / update / forget も出さない。recall は出す)
  (`build_mcp(app, with_media=False)`)。**出すと、Chiezo が絵を頼んだ相手が
  Chiezo に絵を頼み返す** —— 1 枚頼んだだけで、頼まれた側が依頼文を英語に言い換えて
  別の相手へ 2 本、こちらの依頼文をそのまま 1 本、計 3 本を積んだ(どれも組名が無く、
  サイズだけがこちらの指定を引き継いでいたので足が付いた)。枠を余計に食うだけでなく、
  頼まれた側が描く前に別の生成を待つので**時間も伸びる**。**道具を選ばせるつまみは
  接続先しか無い** —— 道具を名前で絞れるのは claude だけ(`--allowed-tools`)で、
  codex と antigravity は起動時の `mcp add`(`bridge/entrypoint.sh`)で丸ごと繋がる
  (引数の形は CLI ごとに違い、codex は `--url`、agy は位置引数で URL を取る)。`/mcp` より**先に**
  マウントすること(後にすると `/mcp` が配下ごと拾う)。
  **接続先だけに頼らない**(`mcp_server._refuse_bridge`)—— 設定を 1 か所間違えれば
  ループが戻る(配備では実際に `/mcp` へ繋がっていた)。**作る道具は、呼んできた
  相手がブリッジなら断る** —— REST の口は住所を見て断っているが
  (`main._refuse_bridge_for`)、**MCP の道具はその検査を通らない**(道具は REST の
  関数を直に呼ぶので、本物のリクエストの無い器を渡している)。断り方は REST と同じで、
  **別の相手へ回すのと、生成中の相手は断り、会話している相手自身は通す**

## 生成する CLAUDE.md ブロックの中身

- **生成する CLAUDE.md ブロックにも絵と音の節を足す**(`claude_config.build_block` の
  `media=True`)。**MCP を登録していて、かつこのサーバーで作れるときだけ** ——
  呼べない道具を勧めない
- **「どれに頼むのがよいか」は種類ごとの表に持つ**(`media_providers.PREFERENCE`)。
  **画面の並び(`order`)とは別物** —— あちらは設定を探すための並びで、
  頼む順で並べ替えると、いつも同じ場所にあった行が動く。
  **`all_providers(kind)` はこの順で返す**ので、`default_backend()`(名指ししなかった
  ときの相手)も道具の一覧もここに従う。**かつては自前の GPU が既定だった**
  (外へ出さず枠も食わない)が、**出来が違う**うえ、相手を名指ししない呼び出しが
  いちばん多い —— 枠を使いたくないときは `comfyui` を名指しする。
  **順位は相手ごとではなく種類ごと** —— 相手ごとに 1 つにすると、音で先頭にした相手が
  絵でも先頭になる(ElevenLabs は曲がよくても絵の相手としては選びたくない)。
  **表に無い相手は最後尾へ黙って回る**ので、`tests/test_media.py` が欠けを見張っている
- **ブロックは「いま頼める相手」を名指しする**(`build_block(usable=…)`。
  出どころは `capabilities.usable_now()`)。「外部の生成 AI を選べる」と抽象的に
  書くより、「いま使えるのは ElevenLabs」と書いたほうが読んだ側が動ける。
  **名指しするのは使える相手だけ** —— 鍵の無い相手を勧めると、呼んで断られるまで
  分からない。**曲と効果音・読み上げと文字起こしは相手が違いうる**ので、
  違えば書き分ける(`_who`)。名前の但し書き(`ElevenLabs(声・…)`)は落とす ——
  ブロックは毎回のコンテキストに乗るので短く保つ
- **話せる相手がいれば「手分けして調べる」の節も足す**(`/v1/ai/complete` を curl で
  叩く手順)。**頼み先に `claude` は出さない**(`HELPER_EXCLUDED`)——
  このブロックを読むのは Claude Code なので、Chiezo 越しにまた Claude Code へ
  投げても**同じサブスクの枠を食うだけ**で、手分けの目的(自分の枠を空けて、
  別の視点も入れる)から外れる。経路は塞がない(`/v1/ai/backends` には出る)。**MCP の登録は要らない**(道具ではなく普通の POST なので、
  生成される許可ルール `curl -s "<base>/` の前方一致にそのまま載る)。
  調べものが広いときに 1 人で抱えないための節で、**枠の残りの見方
  (`/v1/ai/usage`)まで書く** —— 使い切っている相手を避けられるように。
  **例示の JSON は `json.dumps` で組む** —— 手で書くとクォートの入れ子で崩れ、
  崩れた curl をそのままコピーされる

## scripts/gen_claude_config.sh

- `gen_claude_config.sh` — Chiezo 連携用の Claude 設定生成器。**使い方・オプション一覧は
  `docs/api-reference.md`「Claude Code から使う(設定ファイル自動生成)」節が正**で、
  ここには実装側の要点だけ置く:
  - `curl` + POSIX ツールのみで動く(**既存 JSON へのマージにだけ jq か python3 が要る**。
    どちらでも同じ結果になるよう両方の実装を持ち、jq を優先する)。
    稼働中の Chiezo の `/admin/claude-config.*` を取得して書き込むだけの薄いクライアントで、
    **生成の正は app 側 `app/claude_config.py`**。ベース URL はサーバーがアクセス元 URL から
    導出するので、接続に使った URL がそのまま生成物の curl 例・許可ルールになる
  - `--with-hook` を付けたときだけ自動許可フックも設置する。**既定では設置しない** —
    Claude が打つ Bash を毎回検査して自動承認しうる仕掛けで権限ルールより影響が広く、
    中身を読んで納得してから入れられるようにするため。フック本体が Python スクリプトなので
    設置には python3 が要り、欠けていれば落とす
  - settings のマージは「コマンドが `chiezo-autoallow.py` を含む既存エントリ」を落としてから
    足すので、再実行しても重複せず、設置先を変えたときも古いパスのエントリが残らない
  - MCP サーバーの登録も**既定で行う**(`--no-mcp` で無効)。**claude CLI があれば
    `--user` / `--project` どちらのスコープも `claude mcp add --scope {user,project}` に
    任せる**(remove → add で冪等)。設定ファイルの構造を自前で知らずに済み、jq も要らない
    ため。CLI の無い環境(VS Code 拡張のみ等)では `~/.claude.json` / `.mcp.json` の
    `mcpServers` へ直接マージする(新規なら API 応答をそのまま置く)。
    **CLI 任せの副作用**: `claude mcp add --scope project` は `.mcp.json` を書き直すので、
    `mcpServers` 以外の独自キーを書いていた場合は落ちる(このファイルの仕様は
    `mcpServers` だけなので許容している)
  - **権限と MCP は「入れられないなら黙って飛ばさず落とす」**(`die`)。どちらも既定で
    入れる設定なので、飛ばすと「設定が入ったつもり」で使い始めることになる。
    外したいときは明示的に `--no-permissions` / `--no-mcp` を付ける。
    前提の検査は取得より前にまとめてあり、**実際にマージが要るときだけ**落とす
    (書き込み先が無ければ API 応答を `cp` するだけなので jq も python3 も要らない)。
    `--print` は何も書き込まないので検査しない
  - **フックと違って既定で入れる**理由: フックが opt-in なのは Bash を自動承認しうる
    security 上の判断で、MCP 登録にはその性質が無い。ツール定義は常時コンテキストに
    載るが(知識を引く 7 ツールだった頃で約 4.4k 字)、既定で入れている CLAUDE.md ブロック(約 4.3k 字)と
    同程度で、Chiezo を設定する時点で使う前提なのだから片方だけ渋る理由が無い
