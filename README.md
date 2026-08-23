# <img src="assets/icon.svg" width="40" alt="Chiezo icon"> Chiezo — ローカル知識サーバー

AI が使う知識を AI の外に置くための知識ベース。知識をローカルの SQLite (FTS5) に
索引して置き、AI は必要になったときに必要なぶんだけ引く。

| はたらき | 内容 |
|---|---|
| ためる | ソースごとに独立した 1 つの SQLite ファイル(`data/<source>.db`)にする。取得元は公開ダンプ(Wikipedia / OpenStreetMap / GeoNames)のほか、公開リポジトリに置けないプライベートな情報も、別リポジトリのアダプタとして差し込める。更新はブルーグリーン(別ファイルに構築 → 切り替え) |
| 取り出す | AI からの引き口は 2 経路。MCP(`/mcp`、Streamable HTTP)と REST(`search` / `doc` / `filter` / `tags` …)。Claude Code 向けには「どんなときに Chiezo を使うか」を書いた CLAUDE.md ブロックも生成できる |
| 覚える | `/v1/notes` に書いたものは `notes` ソースとして溜まり、`recall` で引ける。書き手は人でも AI でもよい |

人間が中身を確かめるための簡易ブラウズ画面と管理画面(`/admin`)も持つ。

知識ベースとしての中身は上の 3 つ。それとは別に「使う」側も同梱している。Chiezo を
どう引けば当たるかを知っているのはこのリポジトリなので、Claude Code 向けの設定を生成する
スクリプトと、Chiezo を引ける AI 用の道具立て・プロンプトを置いてある
(後者は[使う](#使うローカル-llm既定では無効)。既定では無効)。

### 性質

AI に知識を持たせる方法はほかに 3 つある。モデルの中(学習済みの知識)、コンテキストの中
(CLAUDE.md や記憶ファイル)、外部 API。Chiezo はそのどれとも違う置き場で、性質は次のとおり。

- コンテキストを消費しない。CLAUDE.md や記憶ファイルは毎セッション全部が読み込まれるため
  量に比例してトークンを払うが、Chiezo で常駐するのは道具の定義(数百字)だけで、
  中身は引いたときにしか載らない。全世界の地名を入れても常駐コストは変わらない
- ローカルの SQLite なので数 ms〜数十 ms で返り、オフラインでも動く。外部 API のような
  レート制限や相手側の障害による停止が無い
- 問い合わせ内容が外に出ない。外部 API では何を調べたかが相手のログに残る。利用規約が
  連絡先の明示を求めている場合、AI が git の設定や環境変数から拾った開発者のメール
  アドレスを `User-Agent` に載せて送ってしまうこともある
- 公共データの API(地図・辞書・統計)を AI に叩かせると、人手では起こらない頻度になり
  運営の負担になる。取り込んだコピーを引く限りその問題は生じない
- 取り込んだ時点のスナップショットを引くので、いま現在の状態を知りたい用途には向かない

### ためられるソース

| ためる情報 | ソース名 | 内容 |
|---|---|---|
| Wikipedia | `<lang>wiki` | 一般知識・人物・作品・出来事など。348 の言語版が定義済みで(`jawiki` / `enwiki` / `zh_yuewiki` …)、使いたい言語だけを取り込む(管理画面の `/admin` → `wikipedia` → 言語選択から) |
| OpenStreetMap | `osm_<国>` | 国別抽出(Geofabrik 由来の地名辞典 + POI 辞典)。地名・行政区・自然地物に加え、病院・学校・店舗・観光地等の主要 POI と駅・空港・港・IC/SA 等の交通インフラ、およびそれらの座標。Geofabrik にある 195 の国・地域が定義済みで(`osm_japan` / `osm_france` …)、使いたい国だけを取り込む(`/admin` → `osm` → 国選択から) |
| GeoNames | `geonames` | 全世界地名辞典(約 400MB のダンプで約 1,200 万件)。多言語別名を持つので「パリ」「ニューヨーク」のような日本語表記から引ける。wikidata の Q 番号も拾うので jawiki と突合できる。店舗・営業時間は持たない(そこは osm 系の担当) |
| AI 自身が書いたメモ | `notes` | 取り込みは要らず、書いた端から引ける(後述) |

公開リポジトリに置けないプライベートな情報は、取得コードごと別リポジトリに置いたまま
[アダプタとして差し込める](docs/operations.md#ソースの追加削除)。
geonames と osm の使い分けは
[運用ドキュメント](docs/operations.md#地理データの守備範囲geonames-と-osm-の分担)を参照。

API は FastAPI + uvicorn(ポート 7010)、認証なし・LAN 内前提。未初期化ソースの取り込みは
管理画面から起動できる(内部専用の `chiezo-trigger` サービス経由。ホストへポート公開せず、
`chiezo-api` からのみ到達可能)。

## セットアップ

```bash
# 1. API を起動(DB が無い間もソース 0 件で起動する)。
#    イメージは GHCR から自動で pull される(ビルド不要。更新は docker compose pull)
docker compose up -d
curl -s http://localhost:7010/healthz

# 2. jawiki を取り込む(ダンプ 5〜6GB DL + 構築 2〜6 時間、ディスク空き 80GB 以上推奨)
docker compose --profile ingest run --rm chiezo-ingest

# 2'. osm_japan を取り込む(ダンプ 2〜3GB DL [.osm.pbf] + 構築 1〜4 時間、
#     POI を含むため DB は数 GB 規模)。他の国は SOURCE=osm_france のように国名を変えるだけ
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 2''. geonames を取り込む(全世界の地名。ダンプ約 400MB + 別名 191MB)
docker compose --profile ingest run --rm -e SOURCE=geonames chiezo-ingest

# 3. 取り込みが終われば chiezo-api が数秒以内に自動で新しい DB を読み込む(再起動は不要)
curl -s http://localhost:7010/v1/sources
```

取り込みは開始前にメモリを検査し、足りなければダウンロードもせず中止する。
既定(`BUILD_PROFILE=low_memory`)ならどのソースもメモリ 2 GiB で構築できる
([メモリについて](docs/operations.md#メモリについて))。

## 引く

```bash
BASE=http://<サーバーIP>:7010

# 日本語・スペース等を含むパラメータは -G --data-urlencode で渡す
curl -s  "$BASE/v1/sources"                                                              # ソース一覧
curl -sG "$BASE/v1/jawiki/search?limit=5" --data-urlencode "q=浅草寺"                    # 全文検索
curl -sG "$BASE/v1/jawiki/doc?fields=title,opening,tags" --data-urlencode "title=浅草寺" # 文書
curl -sG "$BASE/v1/jawiki/filter?limit=200" --data-urlencode "tag=ラーメン店"            # カテゴリ全件
curl -sG "$BASE/v1/osm_japan/filter?limit=200" \
  --data-urlencode "feature=amenity=place_of_worship" --data-urlencode "area=京都府"     # Overpass 相当
```

ブラウザで `http://<サーバーIP>:7010/` を開くと管理画面(`/admin`)に、
各ソース名から簡易ブラウズ画面(`/search/{source}/`)に辿れる。

パラメータの一覧・`extra` フィールドの中身・MCP・Claude Code 連携・画面の仕様は
[API リファレンス](docs/api-reference.md)にある。挙動が直感に反する点は 2 つ。

- 3 文字未満の語は全文検索できず、タイトル前方一致にフォールバックする
  (応答の `mode` が `title_prefix` になる)
- カテゴリの全記事列挙は `filter?tag=` を使う。本文の全文検索
  (`search?q=Category:…`)で代用すると、ソートキー付きの記事を静かに取りこぼす

### MCP / Claude Code から使う

chiezo-api 自身が MCP サーバーなので、クライアント側に何もインストールせずに繋がる。
Claude Code 用の設定(CLAUDE.md ブロック・権限ルール・MCP 登録)は、稼働中の Chiezo に
問い合わせて生成できる。

```bash
claude mcp add --transport http chiezo http://<サーバーIP>:7010/mcp   # 手動で登録する場合
scripts/gen_claude_config.sh -u http://<サーバーIP>:7010              # 一式まとめて生成(MCP 登録も既定で行う)
```

オプション・自動許可フック・既存 CLAUDE.md とのマージ方法は
[API リファレンス](docs/api-reference.md#claude-code-から使う設定ファイル自動生成)にある。

## 覚える(notes)

`notes` は Chiezo で唯一書き込めるソース。「これ覚えておいて」と言われたこと、調べた結果、
決めたことを溜め、`recall` で新しい順に引く。書き手は人でも AI でもよく、MCP
クライアントからは `remember` / `recall` / `update` / `forget` の 4 つの道具として見える
(書き換えは渡した項目だけの差し替え。削除は取り消せない)。

```bash
curl -s "$BASE/v1/notes" -H 'Content-Type: application/json' \
  -d '{"text":"開発環境を WSL2 へ移行する","tags":"環境,決定"}'
curl -s "$BASE/v1/notes/recall"                                  # 新しい順に 20 件
curl -sG "$BASE/v1/notes/recall" -d since=2026-07-01 --data-urlencode "q=移行"
```

引くときは期間(`since`/`until`)・キーワード(`q`)・タグで絞れる。本文は既定で先頭
400 文字までで、切れたメモには `truncated` が付く(全文は `doc` で取り直す)。

CLAUDE.md や記憶ファイルとの違いは、常時コンテキストに載るかどうか。それらは毎セッション
全部が読み込まれるため件数に比例してトークンを払うが、Chiezo で常駐するのは MCP の
ツール定義(数百字)だけで、中身は引いたときにしか載らない。

compose では既定で有効(`./notes` に SQLite が 1 つできる)。`CHIEZO_NOTES_DIR` を
空にすると機能ごと無効になる。認証は無いので、`/v1/notes` に到達できる相手は誰でも書ける。

- API の詳細 → [API リファレンス](docs/api-reference.md#notes唯一書き込めるソースの-rest)
- なぜこの形か → [設計メモ](docs/design-notes.md#覚えるnotesはなぜ-chiezo-に置くのか)

## 使う(ローカル LLM。既定では無効)

Chiezo を引ける AI とブラウザから話せる(`/ai/chat`)。1 問 1 答の口(`/v1/ask`)と
会話の口(`/v1/chat`)があり、根拠にした文書は出典として併記する。

これは知識ベース本体の機能ではなく、Chiezo を使う側をこのリポジトリが用意しているもの
(Claude Code 向けの設定を生成するスクリプトと同じ位置づけ)。どう引けば当たるか
—— 短い語は前方一致に落ちる、カテゴリの列挙は `filter?tag=` —— を知っているのはここなので、
道具立てとプロンプトもここで持つ。推論そのものは chiezo-api の中では動かさず、
OpenAI 互換 API を喋る別プロセスに任せる(配信側が数百 MB で動く前提を崩さないため)。
有効になるのは `CHIEZO_LLM_URL` を設定したときだけ。

```bash
cp .env.example .env
# .env の CHIEZO_LLM_URL=http://chiezo-llm:7011/v1 の行のコメントを外す
docker compose -f docker-compose.yml -f docker-compose.answer.yml --profile answer up -d
```

推論サーバのコンテナは `docker-compose.answer.yml` に分けてある
(LAN の別マシンの LLM を指すだけなら重ねなくてよい)。**検索エンジン(SearXNG)は本体側**で、
`docker compose up -d` で一緒に立つ(設定を焼き込んだ `chiezo-searxng` を使うので、
リポジトリを置けない環境でも立てられる) —— 話す相手が Gemini や Claude Code でも web 検索は
使えるようにするため。使うかどうかは `CHIEZO_WEB_SEARCH_URL` を書くかで決まる。

**絵・音・動画・声を作らせることもできる**(MCP の `image_generate` / `audio_generate` /
`video_generate` / `speech_generate` / `transcribe`)。ゲーム素材や図版・効果音・BGM・
短い動画・読み上げを、自分の GPU(ComfyUI)か外部(Gemini / OpenAI / ElevenLabs)を選んで
作る —— 知識を引くのとは別の仕事だが、**MCP の登録先を増やさない**ために同じサーバーに
載せてある。詳しくは [docs/ai.md](docs/ai.md)「絵を描かせる」以降。

**各 AI の使用量も見られる**(管理画面の「使用量」節と `GET /v1/ai/usage`)。
相手が言う枠の残り —— Claude Code の 5 時間・週、Codex の窓、OpenRouter のクレジット ——
と、Chiezo がその相手を呼んだ回数・トークン数を並べて出す。**枠を聞けない相手もいる**
(Gemini・OpenAI)ので、そこは「出せない」と書いてある。

`mode=agent` を付けると、`search` / `doc` / `filter` / `tags` / `links` をモデル自身に
引かせる(MCP と同じ道具立て)。「カテゴリ○○の記事は何件?」のように 1 回の検索では
原理的に答えられない問いに届く。ツール呼び出しが安定するモデル(8B 級以上)と GPU が
実質の前提なので、既定は `rag` のまま。

使い方・パラメータ・agent モード・web 検索・GPU 設定・環境変数の一覧は
[AI と話すドキュメント](docs/ai.md)にある。

## 運用

```bash
docker compose --profile ingest run --rm chiezo-ingest   # ダンプ更新(ブルーグリーン)
```

ingest は毎回 `data/<source>-<date>.db` を新規構築し、検証が通ったらシンボリックリンク
`data/<source>.db` を差し替える(旧世代は 1 つ保持)。差し替えは chiezo-api が数秒以内に
自動検知するので、再起動も停止時間も要らない。

取り込みの環境変数・スキーマ移行・rank_score の入れ直し・メモリ方針・別マシンでのビルドと
配布・`chiezo-trigger`・セキュリティは[運用ドキュメント](docs/operations.md)にある。
よく使うものは次の 5 つ。

| したいこと | 参照 |
|---|---|
| 古い DB にタグ索引・座標表を足す(再取り込みなし) | [schema_version 2 → 3 → 4](docs/operations.md#既存-db-にタグ索引を足すschema_version-2--3--4) |
| 検索の並びが効かない DB を直す | [rank_score を入れ直す](docs/operations.md#既存-db-の-rank_score-を入れ直す) |
| メモリの多い別マシンで焼いて配る | [.db を配布する](docs/operations.md#別マシンでビルドして-db-を配布する) |
| どのコミットが動いているか確かめる | [動いているビルドを確かめる](docs/operations.md#動いているビルドを確かめる) |
| 外に開けてよいか確かめる | [セキュリティ](docs/operations.md#セキュリティ) |
| `.env` を置けない環境で起動する | [単体定義](docs/operations.md#env-を置けない環境で起動する単体定義) |

## 開発

```bash
scripts/run_tests.sh                          # 全テスト
scripts/run_tests.sh tests/test_notes.py -v   # 引数はそのまま pytest へ渡る
ruff check .                                  # lint(設定は pyproject.toml)
```

依存が揃っていればそのまま、無ければ CI と同じ Python 3.12 のイメージを組み立てて
Docker で実行する(リポジトリはバインドマウントするだけなので `data/` の大きさは
関係しない)。手元に環境を作るなら Python 3.12 を使う(`api/` と `ingest/` のイメージ・
CI と同じ系列。依存に C 拡張が含まれるため、別のバージョンでは import から落ちる)。

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

`.venv` があれば `scripts/run_tests.sh` はそちらを優先する。

依存は `requirements.in`(直接の依存を範囲で指定)から `requirements.txt`(版とハッシュを
固定したロック)を作る形にしてある。`.in` を編集したら `scripts/lock_requirements.sh` で
ロックを作り直す。イメージが再現する代わりに上流の新版は自動では入らないので、
破壊的変更への追従は CI の週次ジョブ(最新の依存でテストを回す)が受け持つ。

テストは同梱の小型フィクスチャ(`tests/fixtures/mini_jawiki.xml.gz` 12 文書、
`tests/fixtures/mini_osm.osm.pbf` 12 ノード + 2 way + 2 relation、
`tests/fixtures/mini_geonames.zip` ほか geonames 一式)から実際に
DB を構築して全エンドポイントを検証する。ネットワーク・実データは要らない。

CI(`.github/workflows/ci.yml`)は push / PR でこのテストを実行し、main への push で
`ghcr.io/rtcode337/chiezo-api` / `ghcr.io/rtcode337/chiezo-ingest` のマルチアーキ
(amd64 / arm64)イメージを GHCR へ公開する。`docker-compose.yml` はこのイメージを
pull して使うので、`api/` や `ingest/` を変更してローカルで動作確認するときは
ビルド版を使う。

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

compose は「本体 + 上書き」の形にしてある。本体(`docker-compose.yml`)が検索 API・MCP で、
上書きは足したいものだけを数行ずつ:`build`(手元ビルド)・`answer`(推論と検索エンジン)・
`cuda`(GPU)・`lan`(「答える」層を別ホストへ公開)。重ねる順はこの並びどおり。

### アイコンを変えたとき

原本は `assets/icon.svg` で、`api/app/pages.py` に 2 つの派生物を埋め込んである
(api イメージのビルドコンテキストは `api/` のみで `assets/` を含まないため)。
原本を変えたら両方を作り直す。

- `FAVICON_DATA_URI` — SVG を最小化した data URI(ブラウザタブ用)
- `APPLE_TOUCH_ICON_PNG` — iPhone「ホーム画面に追加」用の 180×180 PNG
  (`/apple-touch-icon.png` で配信)。iOS は SVG や data URI のファビコンを
  ホームアイコンに使わないため PNG が別に要る。角丸マスクは iOS が自前で掛ける
  (透過部分は黒く塗られる)ので、角丸なし・全面塗りでラスタライズする。

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
| [設計メモ](docs/design-notes.md) | なぜこの形なのか。SQLite + FTS5 を選んだ理由、検索の並び順、索引の形、メモリ方針。実測して方針が変わったものは数字と一緒にここへ残す |
| [API リファレンス](docs/api-reference.md) | REST の全パラメータ、`extra` の中身、MCP、Claude Code 連携、管理画面・ブラウズ画面 |
| [AI と話す](docs/ai.md) | 「使う」層の使い方・話す相手の増やし方・CLI ブリッジ・agent モード・web 検索・GPU・環境変数 |
| [運用](docs/operations.md) | 取り込みの環境変数、スキーマ移行、メモリ、別マシンでのビルドと配布、セキュリティ |
| [別マシンで DB を焼く](docs/build-on-another-machine.md) | メモリの多いマシンで `.db` を作って配信機へ渡す手順。これ 1 枚で完結する |
| [ソースの追加手順](docs/adding-a-source.md) | 新しい種類のデータを足すとき |
| [FTS トークナイザの評価](docs/fts-tokenizer-evaluation.md) | 形態素解析への差し替えを見送った経緯と実測値 |

## ライセンス

このリポジトリのコードは [MIT License](LICENSE)。

取り込んで構築した DB の中身(データ)は各ソースのライセンスに従う。リポジトリ自体には
データを含まないが、構築した DB や検索結果を配布・公開する場合は以下の帰属表示・
ライセンス継承が必要になる。

| ソース | データ提供元 | ライセンス |
|---|---|---|
| `<lang>wiki` | [Wikimedia ダンプ](https://dumps.wikimedia.org/) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.ja)(© Wikipedia 各記事の執筆者) |
| `osm_<国>` | [Geofabrik](https://download.geofabrik.de/)(OpenStreetMap 抽出) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/)(© OpenStreetMap contributors) |
| `geonames` | [GeoNames](https://www.geonames.org/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) |
