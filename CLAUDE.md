# Chiezo — AI を使うのに要るものを貸し出すサーバー

**AI を使うのに要るものを 1 か所で持ち、貸し出す。** 持つのは、知識・記憶・話せる AI・
鍵・計算資源・時計・置き場。**持たないのは目的**で、それは呼ぶ側が持ってくる。
**推論も同居させない**(話す相手は全部既定 off)—— 考えるのは常に呼ぶ側の AI で、
Chiezo はそれが持てないものを預かる側にいる。

**借りに来るのは 2 種類いる。ここを取り違えない。**

| 借りる側 | 口 | 主に借りるもの |
|---|---|---|
| **AI**(Claude Code など) | **MCP**(`/mcp`) | 溜めた知識、覚えたことの置き場、絵や音を作る道具 |
| **アプリ**(LAN 内の別のアプリ) | **REST**(`/v1/...`) | **話せる AI そのもの**(鍵は Chiezo が握る)、知識、生成 |
| 人 | 画面(`/admin` ほか) | 設定と、動いているかの確認 |

**アプリが AI を借りに来る面を落とさないこと。** 出発点は「AI が知識を引く」だったが、
いまは `/v1/ai/complete` に自前のプロンプトと材料を持ち込む使い方が同じくらい重い ——
呼ぶ側は API キーも課金の関係も持たずに済む。設計や文書を「AI が引く」だけで組み立てると、
この面が毎回抜ける。

**機能を足すかどうかの線引き:**

> **各アプリが自前で持つと重複して重い、かつ目的に依らないもの** → Chiezo が持つ
> **目的に依るもの**(何を知りたいか・何を判断するか) → 呼ぶ側が持つ

判断や好みを Chiezo に持たせない、が守りたい一線。持たせ始めると「もう 1 つの AI
フレームワーク」になり、上の表の 2 種類に対して中立でいられなくなる。

## 詳しい記述の置き場(`.claude/rules/`)

このファイルには、どの変更にも効く原則・地図・コマンド・約束事だけを置く。
層ごとの詳しい記述(何がどこにあり、なぜその形か)は `.claude/rules/<名前>.md` に分けてある。

- 各ファイルは先頭の `paths` に挙げたファイルを触ると、Claude Code が自動で読み込む
- 該当するファイルを触らずにその層を相談・設計するときは、下の表から選んで**明示的に読む**
  (自動では読み込まれないので、読まずに推測で答えない)
- 層をまたぐ全体像(貸し出すものの一覧と、各層の位置づけ)は `overview.md`

| ルールファイル | 層 | 自動で読み込まれる主なファイル |
|---|---|---|
| `overview.md` | 持っているものの全体像 | `README.md`・`docs/**` |
| `knowledge-api.md` | アプリの組み立て・知識の REST・登録・接続・FTS・ブラウズ画面 | `app/main.py`・`registry.py`・`db.py`・`fts.py`・`deps.py`・`known_sources.py`・`logs.py`・`web_guard.py`・`views/browse.py` |
| `mcp-claude-config.md` | MCP(`/mcp`・`/mcp/knowledge`)と Claude Code 用の設定生成 | `app/mcp_server.py`・`claude_config.py`・`hooks/**`・`scripts/gen_claude_config.sh` |
| `notes-tasks.md` | 短期記憶(`chiezo_memory`・`/v1/stores`)と ToDo | `app/notes.py`・`tasks.py`・`views/todo.py` |
| `collect.md` | 集める層の本体(素材・直す回・墓標・変更履歴・収集の面・時計・差し込み口・辞典への引き当てと書式) | `app/collect.py`・`collect_log.py`・`machine_store.py`・`ingest_queue.py`・`resolve.py`・`formats.py`・`ingest/sources/collect.py` |
| `collect-sweeps.md` | 巡回(`Sweep`)・割り込み(`Focus`)・区画 | `app/collect.py`・`partition.py`・`repartition_job.py` |
| `collect-tools.md` | 収集の道具(フィード・Pi-hole・サムネイル・リンク確認・手で回す巡回・検索文) | `app/feeds.py`・`outbound.py`・`pihole.py`・`thumbs.py`・`link_checks.py`・`handoff.py`・`search_queries.py` |
| `collect-extract.md` | 機械で名簿を作る `extract` | `app/extract.py` |
| `workers.md` | ワーカー(誰に頼むかを名前で外へ出す) | `app/workers.py`・`views/ai_workers.py` |
| `ai-backends.md` | 話せる相手・鍵・素の口(`/v1/ai/*`)・走っている依頼・会話 | `app/providers.py`・`anthropic_api.py`・`gemini_api.py`・`settings_store.py`・`views/ai_settings.py`・`ai_inflight.py`・`ai_transcript.py`・`talk.py` |
| `ai-usage.md` | 使用量・失敗の控え・依頼の履歴 | `app/usage.py`・`usage_store.py`・`ai_log.py`・`views/ai_usage.py`・`views/ai_history.py` |
| `answer-agent.md` | 「使う」層(`/v1/ask`・`/v1/chat`・`/ai/chat`・agent・web 検索) | `app/answer.py`・`agent.py`・`websearch.py`・`views/chat.py` |
| `bridge.md` | CLI ブリッジと会話ブリッジ | `bridge/**` |
| `media.md` | 生成(絵・音・動画・声・文章)・見比べ・`/v1/capabilities` | `app/media*.py`・`capabilities.py`・`views/media_*.py` |
| `admin-ui.md` | 管理画面の組み立て・面・共通の殻・日時の書式 | `app/views/**`・`pages.py`・`jst.py`・`build_info.py` |
| `ingest.md` | 取り込み(chiezo-ingest / chiezo-trigger・アダプタ)とメモリ方針 | `ingest/**`・`tests/test_ingest*.py`・`scripts/gen_*.py` |
| `dev-tooling.md` | 補助スクリプト・テスト・`pyproject.toml`・依存の書き方 | `scripts/**`・`tests/conftest.py`・`pyproject.toml`・`requirements*` |
| `deploy.md` | compose の分け方・CI・Dependabot | `docker-compose*.yml`・`**/Dockerfile`・`.github/**` |

各層のテスト(`tests/test_<層>*.py`)も、その層のファイルに含めてある。正確な一覧は各ファイルの `paths`。

## アーキテクチャ(地図)

- `app/` — **chiezo-app**: FastAPI + uvicorn の常駐コンテナ。`/data` の `*.db` をソースとして登録し、
  MCP(`/mcp`)と REST(`/v1/...`)で出す。画面(`/admin` ほか)も持つ
  - 知識の引き口 —— `main.py`(口とアプリの組み立て)・`registry.py`・`db.py`・`fts.py`・`deps.py` → `knowledge-api.md`
  - MCP と Claude Code 用の設定生成 —— `mcp_server.py`・`claude_config.py`・`hooks/` → `mcp-claude-config.md`
  - 覚える・ToDo —— `notes.py`・`tasks.py` → `notes-tasks.md`
  - 集める / 育てる —— `collect.py`・`partition.py`・`extract.py`・`resolve.py`(辞典への引き当て)・
    `formats.py`(脇書きの書式)・`feeds.py`・`thumbs.py` / `wiki_zim.py`(絵の縮小版とオフライン版)・`workers.py` ほか
    → `collect.md`・`collect-sweeps.md`・`collect-tools.md`・`collect-extract.md`・`workers.md`
  - 話せる AI と鍵 —— `providers.py`・`settings_store.py`・`talk.py`・`ai_inflight.py` → `ai-backends.md`
  - 使用量と失敗の控え —— `usage.py`・`ai_log.py` → `ai-usage.md`
  - 「使う」層 —— `answer.py`・`agent.py`・`websearch.py` → `answer-agent.md`
  - 生成と見比べ —— `media.py`・`media_backends.py`・`media_providers.py`・`capabilities.py` → `media.md`
  - 画面 —— `views/`・`pages.py`・`jst.py` → `admin-ui.md`(各節の中身はそれぞれの層)
- `bridge/` — CLI ブリッジ・会話ブリッジ(別イメージ) → `bridge.md`
- `ingest/` — **chiezo-ingest**(ワンショット構築バッチ)と **chiezo-trigger**(常駐) → `ingest.md`
- `scripts/`・`tests/`・`pyproject.toml`・`*/requirements.*` → `dev-tooling.md`(`gen_claude_config.sh` は `mcp-claude-config.md`)
- `docker-compose*.yml`・`.github/` → `deploy.md`
- `assets/` — プロジェクトアイコン → `dev-tooling.md`

## コマンド

起動は README、テスト・依存・CI は `docs/development.md`、取り込み・運用(ダンプ更新、別マシンでのビルド、既存 DB の移行)の
手順と ingest の環境変数一覧は **`docs/operations.md` が正**。
ここには開発時にしか使わないものだけ置く。

```bash
# テスト(引数はそのまま pytest へ。依存が手元に無ければ Docker で回す)
scripts/run_tests.sh
scripts/run_tests.sh tests/test_notes.py -v

# lint(CI と同じ設定。設定は pyproject.toml、版は requirements-dev.txt で固定)
ruff check .
ruff check --fix .

# 依存のロックを作り直す(.in を編集したあと。--upgrade で範囲の中の最新へ)
scripts/lock_requirements.sh

# フィクスチャ再生成
python tests/fixtures/make_fixture.py
python tests/fixtures/make_osm_fixture.py
python tests/fixtures/make_geonames_fixture.py

# app/ ingest/ を変更したときのローカルビルドでの動作確認
# (docker-compose.yml は GHCR の公開イメージを pull するので、こちらを重ねないと反映されない)
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build

# 「答える」層(推論サーバ + 検索エンジン)も立てる。GPU なら cuda を後ろに重ねる
docker compose -f docker-compose.yml -f docker-compose.llm.yml --profile llm up -d
```

実データを落とさずに取り込みを試すなら、`DUMP_FILE`(ダウンロードを飛ばして手元のファイルを使う)
と `MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータを緩める)を使う。`tests/` はこの経路を
フィクスチャで自動化したもの。

`SOURCE` に渡せる名前や、そのイメージが焼く `schema_version` はイメージ単体に聞ける
(`docs/operations.md`「別マシンでビルドして .db を配布する」節。ローカルビルド版なら
`chiezo-chiezo-ingest:latest` に置き換える)。

メモリ方針(取り込み系コンテナに `mem_limit` を課さない理由・構築プロファイル・ビルド機と配信機の分離)は `.claude/rules/ingest.md` の「メモリ方針」。

## 実装上の約束事

- **Python は 3.12 に留める**(app/ingest の Dockerfile・CI・開発環境で揃える)。現行の最新は
  3.14 で、**2026-08 に実測した限りテストは 3.14 でも全件通る**が、依存のうち
  `mwparserfromhell` だけ cp314 の wheel がまだ無く、sdist からのビルドになる
  (純 Python のフォールバックに落とすと wikitext 解析が桁で遅くなるので、C 拡張は死守する)。
  ingest イメージにビルド道具の出し入れを常設することになり、イメージは +7MB(276→283MB)で
  済むがビルド時間を 2 アーキぶん恒久的に払う。**上流が cp314 wheel を出した時点で上げる** —
  そのときは `FROM` の行と CI の `python-version` を変えるだけで済む(dependabot が週次で見ている)。
  app 側の依存は 3.14 でも全部 wheel があるので、待っているのは ingest の都合だけ。
- コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。ソース固有情報は `docs.extra`(JSON)へ。
  変更は最終手段で、`schema_version` を上げる。app が読むのは `SUPPORTED_SCHEMA_VERSIONS` の版だけで、
  上げるときは配信中の DB が焼き直されるまで新旧を両方読めるようにし、焼き直し終えたら古い版の
  対応は外す(古い版の分岐を抱え続けない。古い版の DB は登録せず、取り込み直しを促す)。
- ソース間で JOIN しない。API はソース種別を意識せず docs/aliases/docs_fts のみ参照する。
- FTS5 は trigram。ユーザー入力は必ずフレーズエスケープしてから MATCH に渡す(`app/fts.py` 経由)。
  **形態素トークナイザへの差し替えは 2026-07 に候補 2 つを実測して見送っている**
  (`docs/fts-tokenizer-evaluation.md` に数字と再現手順)。索引 −72%・2 文字クエリが
  引けるという利得は本物だが、**軽いほうは遅すぎ、速いほうは重すぎる**という状態:
  - `sqlite-vaporetto` — 速い(索引化は trigram より速い)が、モデルを SQLite の
    **接続ごと**に持つ。接続 1 本で +395MiB・8 本で +926MiB になり、`app/db.py` が
    スレッドごとに接続を張る以上「配信機は数百 MB」の前提を壊す
  - `lindera-sqlite` — 辞書を静的リンクで共有するのでメモリは軽い(1 本 +95MiB)が、
    **索引化が文書長に対してほぼ O(n²)**。1 万字級の Wikipedia 記事で 1 件 195ms、
    jawiki 全体では数十時間規模になり取り込みが成立しない
    (おまけに現行 SQLite ではロード自体が失敗する。上流のバグ 2 つを直して測った)
  再挑戦の条件は評価ドキュメントの「採用できる条件」に 3 つ整理してある。
- 運用 DB は読み取り専用(`immutable=1`)。更新はブルーグリーン(別ファイル構築 → シンボリックリンク差し替え)のみ。
  **例外は `chiezo_memory` の 1 ソースだけ**(`app/notes.py`)。書き込みは `CHIEZO_NOTES_DIR` 配下に
  閉じ、`/data` は read-only マウントのまま保つこと。そのソースだけ読み手も `mode=ro` に落とす。
  差し替えは app が自動検知する: lifespan の常駐タスクが `CHIEZO_RESCAN_INTERVAL` 秒(既定 5)ごとに
  `/data` の指紋(`registry.data_dir_fingerprint`)を見て、変わっていれば再走査(`main.refresh_sources`)。
  スレッドローカルの接続キャッシュも `db.get_connection` がリンク先の inode を見て開き直すので再起動不要。
  `/data` への書き込み権限を持つのは chiezo-ingest(one-shot)と chiezo-trigger(常駐)だけで、
  chiezo-app は引き続き read-only マウント。
- エラーレスポンスは `{"error": "..."}` 形式。**例外の文言や相手の応答本文をそのまま
  載せない**(接続先のホスト名などが内部構成の手がかりになる。認証が無いぶん、
  読めてよいのは「繋がらない/遅い/相手が 500」の区別まで)。詳細は `log` に残し、
  応答には種別(`reason` = 例外クラス名)だけ返す。agent の step も画面へ流れるので同じ扱い。
- **ドキュメントの強調は、`**` が括弧・句読点と隣り合わないようにする。** CommonMark の
  flanking rule では、`**` の外側が文字で内側が記号だと強調として認識されず、`*` がそのまま
  表示される。日本語は句点で終え鉤括弧で括る書き方が自然なので、両側とも踏みやすい:
  - **閉じ側**: `**…です。**次の文` は閉じられない(直前が句点)。`**…です**。次の文` と書く。
    `**…「無制限」**と` も同じ(直前が閉じ括弧)。括弧を強調の内側に収めて `**…「無制限」と解釈する**` にする
  - **開始側**: `より**「…」…**` は開始できない(直前が文字・直後が開き括弧)。`より、**「…」…**`
    のように直前を句読点か空白にする
  - 検出は markdown をレンダリングして本文に `*` が残るかを見る(`cmarkgfm` で段落ごとに描画し、
    code span を除いてから探す。**行単位で描画しないこと** —— 強調が複数行にまたがる書き方が多く、
    行で切ると全部が誤検出になる)
- **製品名は `Chiezo`(大文字始まり)、識別子は `chiezo`(小文字)**。散文・見出し・画面の
  文言・LLM へのプロンプト・エラーメッセージは `Chiezo` で書く。小文字のまま据え置くのは
  **値として意味を持つもの**だけ: サービス名(`chiezo-app` / `chiezo-ingest` / `chiezo-trigger` /
  `chiezo-llm`)、イメージ名、環境変数の接頭辞 `CHIEZO_`、MCP 登録名(`claude_config.MCP_SERVER_NAME`
  = `"chiezo"`)、CLAUDE.md ブロックのマーカー(`<!-- BEGIN chiezo … -->`。変えると既存の
  埋め込みを差し替えられなくなる)、`User-Agent`(`chiezo-ingest/0.1` と揃えた機械可読トークン)、
  モデルを選ばなかったときに送る印(`answer.PLACEHOLDER_MODEL`)。**日本語名は付けない**(表記は `Chiezo` に一本化する)。
- **名前の検査は全体一致で行う**(`fullmatch`)。`re.match(r"^…$")` の `$` は末尾の改行 1 つの
  手前でも一致するので、`"news\n"` のような名前が通る(名前はファイル名・ソース名・URL になる)。
- **CLI ブリッジは `chiezo-ai` ネットワークにだけ置く**(chiezo-app は default と両方)。
  ブリッジの中で動くのは道具を持ったエージェントなので、届くのは chiezo-app と外だけにし、
  長期記憶へ書ける chiezo-trigger には届かせない。設定 DB も渡さない(相手ごとの写しだけ)。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。chiezo-trigger・chiezo-llm・searxng は
  ホストへポート公開せず、chiezo-app からのみ内部ネットワーク経由で到達可能にすること
  (**GPU の要るもの**= chiezo-llm・chiezo-image だけは、別ホストの chiezo-app から使う
  ために `docker-compose.lan.yml` で開ける。**searxng はそこに入れない** ——
  本体の compose にあって chiezo-app と必ず同居できるので、別ホストのものを指す理由が無い)。
- **待ち受けは 7010 = API・7011 = 推論・7012 = 検索エンジン・7013 = CLI ブリッジ・
  7014 = 絵と音の生成(ComfyUI)・7016 = 会話ブリッジで、コンテナの内と外を同じ番号にする**。
  番号が食い違うと URL を書くたびにどちらか迷う。既定が違うイメージは環境変数で寄せる ——
  searxng は granian で動くので `GRANIAN_PORT`(SearXNG の `SEARXNG_PORT` は設定ファイルの
  値で待ち受けには使われない)。ComfyUI は `CLI_ARGS` の `--port`(既定は 8188)。
- **「使う」層は既定で無効のまま保つ**。推論を chiezo-app の中で動かさない(配信側が
  数百 MB で動く前提)。LLM を呼ぶコードは `app/answer.py` と `app/agent.py` に閉じ、
  検索・文書取得は `app/main.py` の関数(agent は MCP の道具)を再利用する。compose では
  `docker-compose.llm.yml` を重ねて profile `llm` を付けたときだけコンテナが
  起動する状態を崩さない。
  **素の既定は `rag` + `grounded=1`**(小さな機械でも安全に動く側)。潤沢な環境では
  `CHIEZO_ASK_DEFAULT_MODE` / `_GROUNDED` で倒せるが、**素の既定は変えない**。
- **外へ出るのは「使う」層だけ**。Chiezo 本体(知識ベース)は ingest がダンプを取る以外
  外を叩かない。**例外は読む側のアプリに外を叩かせないための取り次ぎ**(`POST /v1/thumbs` =
  Wikimedia の絵の縮小版・`GET /v1/bookmarks` = はてなの件数。`app/bookmarks.py`)で、
  Wikimedia の絵は Wikipedia のオフライン版(`app/wiki_zim.py`)にあればそこから引いて外へ出ない。
  取りに行ける相手は決め打ちに絞る(読む側から URL を受け取る口が、外を叩かせる踏み台にならないように)。
  web 検索は使う側の機能として `app/websearch.py` に閉じ、既定は無効のまま保つ。
  有効にしたときも「どれが web 由来か分かる」ことを崩さない(出典の `source` が `web`)。
- **会話の状態を chiezo-app に持たせない**。`/v1/chat` は履歴を毎回まるごと受け取る。
  セッションを持つと read-only・複数ワーカーの前提が崩れる(MCP をステートレスにしたのと同じ)。
  **続きものの会話(`/v1/talk`)の状態は会話ブリッジ(別コンテナ)が持つ** ——
  会話の id はブリッジのスレッドの id に相手の名前を付けただけで、chiezo-app は
  どのワーカーが受けても同じブリッジへ流すだけ。ここに表やキャッシュを足さないこと。
  キャラ設定は短期記憶か、アプリの名前付きの置き場(`store`)に置いた見出しで指す
- **compose の分け方**(本体 + 上書き・「答える」層・searxng・ComfyUI・GPU・単体定義)の約束事は
  `.claude/rules/deploy.md` にある。compose ファイルを触るときは自動で読み込まれる
- コード(app/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(入口。概要・起動・引き方・運用の要約とリンク)と、対応する docs/ の詳細
  (`overview.md` = 各機能の説明 / `api-reference.md` = API 仕様と画面 / `ai.md` = 「使う」層 /
  `operations.md` = 取り込みと運用 / `development.md` = テスト・依存・CI /
  `design-notes.md` = なぜこの形か)、および本ファイル
  (CLAUDE.md、アーキテクチャの地図)と `.claude/rules/` の該当ファイル(層ごとの詳しい記述)も
  あわせて更新すること。新しいファイルを足したら、どのルールファイルの `paths` に入るかも確かめる
  (どこにも入らないと、触っても詳しい記述が読み込まれない)。**README には詳細を書き戻さない**
  (人に読ませる入口として 1 画面で全体像がつかめる長さを保つ)。
  ドキュメントだけを別コミット・別対応に先送りしない。
