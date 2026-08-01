# <img src="assets/icon.svg" width="40" alt="chiezo icon"> chiezo — ローカル知識サーバー

**AI のための知識ベースです**。公開ダンプ(Wikipedia / OpenStreetMap / GeoNames)を
ローカルの SQLite (FTS5) に取り込んで索引し、AI が引ける形で出します。

- **ためる** — `ingest` が公式ダンプを取り込み、ソースごとに独立した 1 つの SQLite ファイル
  (`data/<source>.db`)にする。更新はブルーグリーン(別ファイルに構築 → 切り替え)
- **取り出す** — AI からの引き口は 2 経路。**MCP**(`/mcp`、Streamable HTTP)と
  **REST**(`search` / `doc` / `filter` / `tags` …)。Claude Code 向けには「どんなときに
  chiezo を使うか」を書いた CLAUDE.md ブロックも自動生成します
- **覚える** — `/v1/notes` に書いたことは `notes` ソースとして溜まり、あとから
  `recall` で引けます。**常時コンテキストに載るのはツール定義だけ**なので、
  件数が増えても AI 側の負担が増えません
- **答える(任意)** — ローカル LLM を繋ぐと、chiezo を引いて回答まで返します
  (`/v1/ask` と ブラウザの `/localllm/chat`)。Claude Code と同じ知識をローカルの LLM からも
  使う口です。**既定では無効**で、推論は別プロセスに置きます

人間が中身を確かめるための簡易ブラウズ画面と管理画面(`/admin`)も付いています。

### 目的

**AI に外部の公開 API を直接叩かせるのをやめ、手元のコピーを引かせるため**に作りました。
外部 API のままだと、次の 3 つが避けられません。

- **相手に迷惑をかける** — 地図・辞書・統計といった公共データの API は、無料で
  コミュニティ運営されているものが多く、フェアユースを前提に提供されています。
  AI に調べさせると人手では起こらない頻度で叩くことになり、運営の負担になります
- **問い合わせ内容と、意図しない個人情報が外に出る** — 何を調べたかが相手のログに残ります。
  さらに厄介なのが、**API の利用規約が「連絡先を明示すること」を求めている場合**です。
  AI はその規約に素直に従おうとして、git の設定や環境変数から拾った**開発者のメール
  アドレスを `User-Agent` に載せて送ってしまう**ことがあります。取り込んだデータを
  ローカルで引く限り、そもそも送る先がありません
- **遅い・失敗する** — レート制限で待たされ、ネットワークの不調や相手側の障害で止まります。
  ローカルの SQLite なら数 ms〜数十 ms で返り、オフラインでも動きます

裏返すと、**chiezo が向かないのは「いま現在の状態」を知りたいとき**です。取り込んだ
ダンプのスナップショットなので、リアルタイム性が要る用途は外部 API のままが適切です。

### ためられるソース

- `<lang>wiki` — Wikipedia。一般知識・人物・作品・出来事など。**348 の言語版が定義済み**で
  (`jawiki` / `enwiki` / `zh_yuewiki` …)、使いたい言語だけを取り込みます
  (管理画面の `/admin` → `wikipedia` → 言語選択から)
- `osm_<国>` — OpenStreetMap の国別抽出(Geofabrik 由来の地名辞典 + POI 辞典)。
  地名・行政区・自然地物に加え、病院・学校・店舗・観光地等の主要 POI と
  駅・空港・港・IC/SA 等の交通インフラ、およびそれらの座標。Geofabrik にある
  **195 の国・地域が定義済み**で(`osm_japan` / `osm_france` …)、使いたい国だけを
  取り込みます(`/admin` → `osm` → 国選択から)
- `geonames` — GeoNames 全世界地名辞典(約 400MB のダンプで約 1,200 万件)。
  多言語別名を持つので「パリ」「ニューヨーク」のような日本語表記から引けます。
  wikidata の Q 番号も拾うので jawiki と突合できます。**店舗・営業時間は持たない**
  — そこは osm 系の担当です

geonames と osm の使い分けは
[運用ドキュメント](docs/operations.md#地理データの守備範囲geonames-と-osm-の分担)を参照してください。

API は FastAPI + uvicorn(ポート 9000)、認証なし・LAN 内前提。未初期化ソースの取り込みは
管理画面から起動できます(内部専用の `chiezo-trigger` サービス経由。ホストへポート公開せず、
`chiezo-api` からのみ到達可能)。

## セットアップ

```bash
# 1. API を起動(DB が無い間もソース 0 件で起動する)。
#    イメージは GHCR から自動で pull される(ビルド不要。更新は docker compose pull)
docker compose up -d
curl -s http://localhost:9000/healthz

# 2. jawiki を取り込む(ダンプ 5〜6GB DL + 構築 2〜6 時間、ディスク空き 80GB 以上推奨)
docker compose --profile ingest run --rm chiezo-ingest

# 2'. osm_japan を取り込む(ダンプ 2〜3GB DL [.osm.pbf] + 構築 1〜4 時間、
#     POI を含むため DB は数 GB 規模)。他の国は SOURCE=osm_france のように国名を変えるだけ
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 2''. geonames を取り込む(全世界の地名。ダンプ約 400MB + 別名 191MB)
docker compose --profile ingest run --rm -e SOURCE=geonames chiezo-ingest

# 3. 取り込みが終われば chiezo-api が数秒以内に自動で新しい DB を読み込む(再起動は不要)
curl -s http://localhost:9000/v1/sources
```

取り込みは**開始前にメモリを検査**し、足りなければダウンロードもせず中止します。
既定(`BUILD_PROFILE=low_memory`)ならどのソースもメモリ 2 GiB で構築できます
([メモリについて](docs/operations.md#メモリについて))。

## 使う

```bash
BASE=http://<サーバーIP>:9000

# 日本語・スペース等を含むパラメータは -G --data-urlencode で渡す
curl -s  "$BASE/v1/sources"                                                              # ソース一覧
curl -sG "$BASE/v1/jawiki/search?limit=5" --data-urlencode "q=浅草寺"                    # 全文検索
curl -sG "$BASE/v1/jawiki/doc?fields=title,opening,tags" --data-urlencode "title=浅草寺" # 文書
curl -sG "$BASE/v1/jawiki/filter?limit=200" --data-urlencode "tag=ラーメン店"            # カテゴリ全件
curl -sG "$BASE/v1/osm_japan/filter?limit=200" \
  --data-urlencode "feature=amenity=place_of_worship" --data-urlencode "area=京都府"     # Overpass 相当
```

ブラウザで `http://<サーバーIP>:9000/` を開くと管理画面(`/admin`)に、
各ソース名から簡易ブラウズ画面(`/search/{source}/`)に辿れます。

パラメータの一覧・`extra` フィールドの中身・MCP・Claude Code 連携・画面の仕様は
**[API リファレンス](docs/api-reference.md) が正です**。押さえておくと事故らない点だけ挙げると:

- 3 文字未満の語は全文検索できず、タイトル前方一致にフォールバックします
  (応答の `mode` が `title_prefix` になります)
- **カテゴリの全記事列挙は `filter?tag=`** を使ってください。本文の全文検索
  (`search?q=Category:…`)で代用すると、ソートキー付きの記事を静かに取りこぼします
- **504 は「該当 0 件」ではなく「取れなかった」を意味します**。空の結果として先へ進むと
  そのまとまりを丸ごと取りこぼします

### MCP / Claude Code から使う

chiezo-api 自身が MCP サーバーなので、クライアント側に何もインストールせずに繋がります。
Claude Code 用の設定(CLAUDE.md ブロック・権限ルール・MCP 登録)は、稼働中の chiezo に
問い合わせて生成できます。

```bash
claude mcp add --transport http chiezo http://<サーバーIP>:9000/mcp   # 手動で登録する場合
scripts/gen_claude_config.sh -u http://<サーバーIP>:9000              # 一式まとめて生成(MCP 登録も既定で行う)
```

オプション・自動許可フック・既存 CLAUDE.md とのマージ方法は
[API リファレンス](docs/api-reference.md#claude-code-から使う設定ファイル自動生成)にあります。

## 覚える(notes)

「これ覚えておいて」と言われたこと、調べた結果、決めたこと。**chiezo で唯一書き込めるソース**が
`notes` です。溜めたものは `recall` で新しい順に引けます。

```bash
curl -s "$BASE/v1/notes" -H 'Content-Type: application/json' \
  -d '{"text":"開発環境を WSL2 へ移行する","tags":"環境,決定"}'
curl -s "$BASE/v1/notes/recall"                                  # 新しい順に 20 件
```

**なぜ CLAUDE.md や記憶ファイルではなく chiezo なのか** — 常時コンテキストに載るかどうかが
違います。CLAUDE.md や AI の記憶ファイルは毎セッション全部が読み込まれるので、件数が増える
ほど関係ない話のときにもトークンを払い続けます。chiezo に置けば**常駐するのは MCP の
ツール定義(数百字)だけ**で、中身は引いたときにしか載りません。100 件でも 1000 件でも
常駐コストは変わりません。

compose では既定で有効です(`./notes` に SQLite が 1 つできます)。`CHIEZO_NOTES_DIR` を
空にすると機能ごと無効になります。**認証はありません** — `/v1/notes` に到達できる相手は
誰でも書けます。

- API の詳細 → [API リファレンス](docs/api-reference.md#notes唯一書き込めるソースの-rest)
- なぜこの形か → [設計メモ](docs/design-notes.md#覚えるnotesはなぜ-chiezo-に置くのか)

## 答える(ローカル LLM。既定では無効)

**chiezo を引ける AI と、ブラウザから話せます**(`/localllm/chat`)。1 問 1 答の口(`/v1/ask`)と
会話の口(`/v1/chat`)があり、根拠にした文書は出典として併記します。推論は chiezo-api の中では
動かさず、**OpenAI 互換 API を喋る別プロセス**に任せます(配信側が数百 MB で動く前提を
壊さないため)。有効になるのは `CHIEZO_LLM_URL` を設定したときだけです。

```bash
cp .env.example .env
# .env の CHIEZO_LLM_URL=http://chiezo-llm:8080/v1 の行のコメントを外す
docker compose --profile answer up -d
```

`mode=agent` を付けると、`search` / `doc` / `filter` / `tags` / `links` を**モデル自身に**
引かせます(MCP と同じ道具立て)。「カテゴリ○○の記事は何件?」のように 1 回の検索では
原理的に答えられない問いに届きます。ツール呼び出しが安定するモデル(8B 級以上)と GPU が
実質の前提なので、既定は `rag` のままです。

使い方・パラメータ・agent モード・web 検索・GPU 設定・環境変数の一覧は
**[ローカル LLM ドキュメント](docs/local-llm.md) が正です**。

## 運用

```bash
docker compose --profile ingest run --rm chiezo-ingest   # ダンプ更新(ブルーグリーン)
```

ingest は毎回 `data/<source>-<date>.db` を新規構築し、検証が通ったらシンボリックリンク
`data/<source>.db` を差し替えます(旧世代は 1 つ保持)。差し替えは chiezo-api が数秒以内に
自動検知するので、再起動も停止時間もありません。

取り込みの環境変数・スキーマ移行・rank_score の入れ直し・メモリ方針・別マシンでのビルドと
配布・`chiezo-trigger`・セキュリティは
**[運用ドキュメント](docs/operations.md) が正です**。
よく使うものだけ挙げると:

| したいこと | 参照 |
|---|---|
| 古い DB にタグ索引・座標表を足す(再取り込みなし) | [schema_version 2 → 3 → 4](docs/operations.md#既存-db-にタグ索引を足すschema_version-2--3--4) |
| 検索の並びが効かない DB を直す | [rank_score を入れ直す](docs/operations.md#既存-db-の-rank_score-を入れ直す) |
| メモリの多い別マシンで焼いて配る | [.db を配布する](docs/operations.md#別マシンでビルドして-db-を配布する) |
| 外に開けてよいか確かめる | [セキュリティ](docs/operations.md#セキュリティ) |

## 開発

```bash
scripts/run_tests.sh                          # 全テスト
scripts/run_tests.sh tests/test_notes.py -v   # 引数はそのまま pytest へ渡る
```

依存が揃っていればそのまま、無ければ CI と同じ Python 3.12 のイメージを組み立てて
Docker で実行します(リポジトリはバインドマウントするだけなので `data/` の大きさは
関係ありません)。手元に環境を作るなら **Python 3.12** を使ってください
(`api/` と `ingest/` のイメージ・CI と同じ系列。依存に C 拡張が含まれるため、
別のバージョンでは import から落ちます):

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r api/requirements.txt -r ingest/requirements.txt pytest
```

`.venv` があれば `scripts/run_tests.sh` はそちらを優先します。

テストは同梱の小型フィクスチャ(`tests/fixtures/mini_jawiki.xml.gz` 12 文書、
`tests/fixtures/mini_osm.osm.pbf` 12 ノード + 2 way + 2 relation、
`tests/fixtures/mini_geonames.zip` ほか geonames 一式)から実際に
DB を構築して全エンドポイントを検証します。ネットワーク・実データは不要です。

CI(`.github/workflows/ci.yml`)は push / PR でこのテストを実行し、main への push で
`ghcr.io/rtcode337/chiezo-api` / `ghcr.io/rtcode337/chiezo-ingest` のマルチアーキ
(amd64 / arm64)イメージを GHCR へ公開します。`docker-compose.yml` はこのイメージを
pull して使います。`api/` や `ingest/` を変更してローカルで動作確認するときは
ビルド版を使ってください:

```bash
docker compose -f docker-compose.build.yml up -d --build
```

### アイコンを変えたとき

原本は `assets/icon.svg` で、`api/app/pages.py` に 2 つの派生物を埋め込んであります
(api イメージのビルドコンテキストは `api/` のみで `assets/` を含まないため)。
原本を変えたら両方を作り直してください:

- `FAVICON_DATA_URI` — SVG を最小化した data URI(ブラウザタブ用)
- `APPLE_TOUCH_ICON_PNG` — iPhone「ホーム画面に追加」用の 180×180 PNG
  (`/apple-touch-icon.png` で配信)。iOS は SVG や data URI のファビコンを
  ホームアイコンに使わないため PNG が別に要ります。角丸マスクは iOS が自前で掛ける
  (透過部分は黒く塗られる)ので、**角丸なし・全面塗り**でラスタライズします:

```bash
pip install cairosvg
python - <<'EOF'
import base64, cairosvg, pathlib, textwrap
svg = pathlib.Path("assets/icon.svg").read_text().replace('rx="56"', 'rx="0"', 1)
png = cairosvg.svg2png(bytestring=svg.encode(), output_width=180, output_height=180)
print("\n".join(textwrap.wrap(base64.b64encode(png).decode(), 96)))
EOF
```

## ドキュメント

| | 中身 |
|---|---|
| [設計メモ](docs/design-notes.md) | **なぜこの形なのか**。SQLite + FTS5 を選んだ理由、検索の並び順、索引の形、メモリ方針。実測して方針が変わったものは数字と一緒にここへ残します |
| [API リファレンス](docs/api-reference.md) | REST の全パラメータ、`extra` の中身、MCP、Claude Code 連携、管理画面・ブラウズ画面 |
| [ローカル LLM](docs/local-llm.md) | 「答える」層の使い方・agent モード・web 検索・GPU・環境変数 |
| [運用](docs/operations.md) | 取り込みの環境変数、スキーマ移行、メモリ、別マシンでのビルドと配布、セキュリティ |
| [ソースの追加手順](docs/adding-a-source.md) | 新しい種類のデータを足すとき |
| [FTS トークナイザの評価](docs/fts-tokenizer-evaluation.md) | 形態素解析への差し替えを見送った経緯と実測値 |

## ライセンス

このリポジトリのコードは [MIT License](LICENSE) です。

取り込んで構築した DB の**中身(データ)は各ソースのライセンスに従います**。
リポジトリ自体にはデータを含みませんが、構築した DB や検索結果を配布・公開する場合は
以下の帰属表示・ライセンス継承が必要です。

| ソース | データ提供元 | ライセンス |
|---|---|---|
| `<lang>wiki` | [Wikimedia ダンプ](https://dumps.wikimedia.org/) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.ja)(© Wikipedia 各記事の執筆者) |
| `osm_<国>` | [Geofabrik](https://download.geofabrik.de/)(OpenStreetMap 抽出) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/)(© OpenStreetMap contributors) |
| `geonames` | [GeoNames](https://www.geonames.org/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) |
