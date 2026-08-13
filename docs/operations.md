# 運用

取り込み・更新・移行・配布と、運用上の前提(メモリ・セキュリティ)をまとめます。
なぜその方針なのかは [設計メモ](design-notes.md) が正です。

## ダンプ更新(ブルーグリーン)

ingest は毎回 `data/<source>-<date>.db` を新規構築し、検証が通ったらシンボリックリンク
`data/<source>.db` を差し替えます。旧世代は 1 つだけ残します。差し替えは chiezo-api が
数秒以内に自動検知して新しい DB へ切り替わるので、再起動も停止時間もありません。

```bash
docker compose --profile ingest run --rm chiezo-ingest
```

月次 cron の例:

```cron
0 3 1 * * cd /opt/chiezo && docker compose --profile ingest run --rm chiezo-ingest
```

中断しても運用 DB は壊れません(`.building` の一時ファイルに構築するため)。
再実行すれば最初からやり直します。

### ingest の環境変数

| 変数 | 説明 |
|---|---|
| `SOURCE` | 取り込むソース名(必須。compose 既定は `jawiki`。`-e SOURCE=osm_japan` 等で上書き) |
| `DUMP_DATE` | ダンプ日付 `YYYYMMDD` を固定(省略時は最新を自動検出。osm 系は常に latest を取得するため世代ラベルの上書きのみ) |
| `DUMP_FILE` | ダウンロードをスキップし既存ファイルを使う(カンマ区切りで複数シャード指定可) |
| `PAGEVIEW_PERIOD` | ページビュー突合対象の年月 `YYYY-MM` を固定(省略時は最新月を自動検出) |
| `MIN_DOCS` / `SAMPLE_TITLES` | 検証パラメータの上書き(小規模データでの動作確認用) |
| `OSM_AREA_ADMIN_LEVEL` | `extra.area` に入れる行政区の admin_level(既定 4 = 都道府県。`0` で境界パス省略) |
| `BUILD_PROFILE` | 構築プロファイル。既定は `low_memory` = **どのソースも 2GiB で構築できる**(構築用 SQLite キャッシュを絞り、osm のノード座標索引をディスクへ。osm は数倍〜10 倍遅い)。メモリの潤沢なビルド機では `fast` を実行時に明示すると速度優先になる([メモリについて](#メモリについて)) |
| `OSM_NODE_INDEX` | osm のノード座標索引の置き場(`sparse_mmap_array`〈RAM・速い〉/ `sparse_file_array`〈ディスク・省メモリ・遅い〉)。明示指定は `BUILD_PROFILE` より優先。未指定なら low_memory はディスク、fast はソースごとの既定(RAM 索引が 12GiB を超える国のみディスク) |
| `GEONAMES_ALT_LANGS` | geonames で取り込む別名の言語(カンマ区切り。既定 `ja,en`、`*` で全 400 言語超) |
| `GEONAMES_FEATURE_CLASSES` | geonames で取り込む feature class(既定 `AHLPSTUV` = 道路 `R` 以外すべて) |
| `BUILD_MEMORY_GB` / `SKIP_MEMORY_CHECK` | 構築前メモリ検査の必要量を上書き / 検査を無効化([メモリについて](#メモリについて)) |

### ソースごとの取り込みの注意

**Wikipedia 系**は標準 XML ダンプ(`<wiki_id>-<date>-pages-articles.xml.bz2`、jawiki で
確認時点 4.4GB)を取得し、wikitext をプレーンテキスト化して取り込みます
(CirrusSearch ダンプを使わない理由は
[設計メモ](design-notes.md#wikipedia-は-cirrussearch-ではなく-xml-ダンプから作る))。
記事本文に加えて Wikimedia の月次ページビューダンプ(全プロジェクト合算で圧縮 5〜6GB)も
ダウンロードし、`page_id` で突合して知名度(`rank_score`)と `extra.pageviews_month` に
します。この分、初回取り込みのダウンロード量・所要時間は見積もりよりやや増えます。

**OSM 系**(`osm_japan` 等)は Geofabrik の地域抽出
`https://download.geofabrik.de/<region>-latest.osm.pbf` を取り込みます。取り込むのは
`name` タグを持つ「名前付き地物」だけです(地名・行政界・主要な自然地物 + 主要 POI +
交通インフラ。住所補間・逆ジオコーディングはできません。内部の走査方法は
[設計メモ](design-notes.md#osm-は-pyosmium-で-3-パス読む))。
ファイルを 3 パス読むため、構築時間はダウンロード後さらに 2〜6 時間程度かかります
(既定の `low_memory` プロファイルではノード座標索引がディスクに置かれるため、
さらに数倍〜10 倍かかります)。メモリの潤沢なビルド機で `BUILD_PROFILE=fast` を付けると
索引が RAM 上(`sparse_mmap_array`)になり最速です(日本抽出で 5〜10GB 使います)。

## 既存 DB にタグ索引を足す(schema_version 2 → 3 → 4)

タグ絞り込み(`filter?tag=` / `tags`)には `schema_version` 3 以降が、`filter?bbox=` と
大きなカテゴリの並べ替えが実用的な速さになるには 4 以降が要ります(3 の DB でも遅い経路の
まま動きます。何がどう速くなるかは
[設計メモ](design-notes.md#読む量を該当件数に比例させる))。

足すのは既にある内容の射影(タグ転置表・タグ集計表・座標表・並び順の索引)だけなので、
**ダンプを取り直さずその場で移行できます**(jawiki の再取り込みは 2〜6 時間、この移行は
数分〜十数分。3 の DB を 4 にするだけなら jawiki で 1 分弱)。

```bash
docker compose stop chiezo-api        # 読み取り中の DB を書き換えないため
python3 scripts/add_tag_index.py data/jawiki.db data/osm_japan.db
docker compose start chiezo-api
```

シンボリックリンク(`jawiki.db`)を渡してよく(実体の世代ファイルを書き換えます)、
足りないステップだけを流すので、すでに 4 の DB には何もしません。中断しても、もう一度
実行すればやり直せます(`meta` の更新が最後の 1 ステップなので、中途半端に新しい版を
名乗る DB は残りません)。
次の取り込みからは新しいスキーマで構築されるため、この作業は 1 回だけです。

**非力なマシンでも実行できます**。メモリは文書数によらずほぼ一定で、実測で 100 万文書
(300 万タグ行)の展開でもピーク RSS は 24MiB でした(SQLite が数 MB のページキャッシュで
流し込むだけのため)。効いてくるのはディスクと時間です:

- 増えるディスクはタグ 1 行あたり約 50 バイト(jawiki で 1GB 前後、geonames で 2〜3GB)
- 時間は `docs` の全走査が支配的で、遅いディスクなら jawiki(42GB)で 10 分以上
- `DISTINCT` の並べ替えが一時ファイルに落ちるため、**20 万文書ずつに分割**して処理します
  (`--batch` で変更可。進捗が 1 バッチごとに出ます)。一時領域が厳しければ
  `--batch` を小さくするか `SQLITE_TMPDIR=/空きのある場所` を指定してください

運用 DB を直接書き換えるので、ロールバックジャーナルは無効化していません
(途中で kill されても壊れないことを速度より優先しています)。

## 既存 DB の rank_score を入れ直す

検索の並びに使う `rank_score` は「0.0〜1.0 に正規化した知名度」という約束ですが、
古い DB はこの約束を満たしていません:

- `<lang>wiki` — 全記事 0.0 固定(XML ダンプに人気度が無いため入れていなかった)。
  月間ページビューは `extra.pageviews_month` にあるので、そこから計算できます
- `geonames` — 人口の生値(最大 14 億)が入っています。`extra.population` から入れ直します
- `osm_<国>` — 元から 0.0〜1.0 なので対象外です

いずれも取り込み済みの `extra` を読み直すだけで、**ダンプの取り直しは要りません**。

```bash
docker compose stop chiezo-api
python3 scripts/refresh_rank_score.py data/jawiki.db data/geonames.db
docker compose start chiezo-api
```

jawiki は数分〜十数分かかります。SSH 越しに実行するなら、接続が切れても止まらないよう
`tmux` の中で回すか `nohup … &` にしてください(途中で落ちても、もう一度流し直せば
同じ結果になります)。

スキーマは変わらないので `schema_version` も上がりません。入れ直していない DB でも
API は壊れません(`rank_score` を 0〜1 に丸めてから使うので、実質 bm25 だけの並びに
戻るだけです)。何度実行しても結果は同じです。

## 地理データの守備範囲(geonames と osm の分担)

「全世界の地名に答える」用途と「特定の国を店舗レベルまで掘る」用途は、データ源を分けています。

| | `geonames` | `osm_<地域>` |
|---|---|---|
| 範囲 | **全世界**(約 1,200 万件) | 取り込んだ地域のみ |
| ダンプ | 約 400MB + 別名 191MB | 国単位で 2〜5GB(大陸単位は 32GB) |
| 持っているもの | 地名・座標・国/行政区・人口・多言語別名・timezone | 地名 + **店舗/施設/交通** の詳細、住所・電話・営業時間 |
| 持っていないもの | 店舗・レストラン・営業時間 | 取り込んでいない国のすべて |

大陸単位の OSM(`europe-latest.osm.pbf`)を 1 ソースとして取り込む案は**廃止しました**
(理由は [設計メモ](design-notes.md#地理データの守備範囲geonames-と-osm-の分担))。
推奨構成は:

- **全世界の問い合わせ** → `geonames`(1 ソースで賄う)
- **店舗レベルの詳細が要る国** → その国だけ `osm_<国>` を取り込む(管理画面 `/admin/osm` から選ぶか
  `-e SOURCE=osm_<国>`)。既定(`low_memory`)ではどの国もメモリ 2GiB で焼けます。
  メモリの潤沢なビルド機で `-e BUILD_PROFILE=fast` を付けると、多くの国は RAM 索引・
  12GiB 予算で数時間に短縮できます(フランス・ドイツ・カナダ・アメリカ・ロシアのような
  大きい国だけは fast でもディスク索引が既定です)。
- **事物の解説** → `jawiki`(座標と wikidata の Q 番号を持つ)

`geonames` と `jawiki` はどちらも `extra.wikidata` に Q 番号を持つので、
`filter?wikidata=Q90` で相互に引き当てられます。

## メモリについて

ingest は**開始前にメモリを検査し、足りなければダウンロードもせず即座に中止**します
(なぜ `mem_limit` で締めないのかは [設計メモ](design-notes.md#メモリ方針))。
取り込み中の巨大な対応表はディスク上の一時 SQLite に逃がしてあるので、常駐メモリは
コーパス規模によらず一定です。一時ファイルは終了・中断のいずれでも自動削除されます。

構築には速度優先とメモリ優先の 2 つのプロファイルがあり、環境変数 `BUILD_PROFILE` で
切り替えます:

- `low_memory`(既定) — メモリ優先。**どのソースも 2 GiB で構築できます**。構築用
  SQLite キャッシュを 64MiB に絞り、osm のノード座標索引をディスクに置きます。
  osm は数倍〜10 倍遅くなります(wikipedia / geonames はほぼ変わりません)。
  何も指定しなければこちらなので、メモリ 2 GiB 級のサーバや開発機でもそのまま取り込めます
- `fast` — 速度優先。必要メモリは下表のとおり(最大 12 GiB)。メモリの潤沢な
  ビルド機で焼くときに、実行時の引数として明示します(compose には書かず、
  `docker run -e BUILD_PROFILE=fast …` のようにその実行だけに付けるのがおすすめです。
  手順は[別マシンでビルドして .db を配布する](#別マシンでビルドして-db-を配布する))

`fast` での必要メモリの目安:

| ソース | 必要メモリの目安 | 内訳 |
|---|---|---|
| `jawiki` | 3 GiB | 巨大な対応表はディスクへ逃がしてあるので軽い。実測ピークは 1GiB 未満 |
| `osm_japan` | 12 GiB | ノード座標索引を RAM に持つため(実測 5〜10GB) |
| `osm_<他の国>` | 3〜12 GiB | pbf 1GB あたり RAM 索引 5GiB 見当。国ごとの目安は `/admin/osm` に出ます |
| `geonames` | 3 GiB | 別名(2,000 万行規模)はディスクへ逃がすため軽い |

RAM 索引が 12GiB に収まらない国(フランス・ドイツ・カナダ・アメリカ・ロシア)は、
`fast` でも**ディスク索引(`sparse_file_array`)が既定**です。なお `OSM_NODE_INDEX` の
明示指定はプロファイルよりも優先されるので、`OSM_NODE_INDEX=sparse_mmap_array` を付ければ
「既定の省メモリのまま、この 1 ソースだけ RAM 索引で速く」という使い方もできます。

足りない場合のメッセージと対処:

```
not enough memory to build osm_japan: 2.0 GiB available < 12.0 GiB required.
```

1. `BUILD_PROFILE=fast` を付けているなら外す(既定の `low_memory` は全ソース 2 GiB で
   構築できます)
2. メモリの多いマシンで焼いて `.db` をコピーする(下記)
3. `BUILD_MEMORY_GB=<n>` で必要量を上書き、`SKIP_MEMORY_CHECK=1` で検査を無効化(見積もりが
   実態と合わないと分かっている場合のみ)

**配信側はメモリ数百 MB で動きます。** chiezo-api は読み取り専用の immutable SQLite を開くだけなので、
1GB 級の小型機でも配信できます。効いてくるのはメモリではなくディスク(jawiki.db 約 42GB の空き)。

## 別マシンでビルドして .db を配布する

Chiezo の DB は**自己完結した単一の SQLite ファイル**なので、配布は「ファイルをコピーするだけ」です。
export/import も配信機でのビルドも要りません(SQLite のファイル形式は OS・CPU アーキ非依存です)。
メモリの少ない配信機と、メモリの多いビルド機を分けられます。

ビルド機に要るのは docker だけで、リポジトリも compose も設定ファイルも要りません。

```bash
# 1. イメージを最新にする。ローカルに古い latest が残っていると、それで焼いてしまう
#    (タグは latest のままなので、古いかどうかは見た目では分かりません)
docker pull ghcr.io/rtcode337/chiezo-ingest:latest

# 2. そのイメージが作る DB の schema_version を確かめる(取り込みを走らせずに聞けます)。
#    配信中の chiezo-api が期待するより古いと新機能が使えないので、数時間かける前にここで確認
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import core; print(core.SCHEMA_VERSION)"

# 3. 取り込む。数時間かかるので必ず -d(デタッチ)で回すこと。前景(--rm -it)だと
#    ターミナルを閉じた・スリープした瞬間に構築ごと消えます
docker run -d --name chiezo-build -e SOURCE=jawiki -e CHIEZO_DATA_DIR=/data \
  -v /path/to/chiezo-data:/data ghcr.io/rtcode337/chiezo-ingest:latest
docker logs -f chiezo-build                      # 進捗(Ctrl-C で抜けても構築は続く)
docker wait chiezo-build && docker rm chiezo-build

# 4. 出来た世代ファイルを配信機へコピーし、<ソース名>.db として見えるようにする
#    (リンクを差し替えれば chiezo-api が数秒以内に自動で読み込む。再起動は不要)
cp jawiki-20260701.db /path/to/chiezo/data/
ln -sfn jawiki-20260701.db /path/to/chiezo/data/jawiki.db
```

**jawiki 以外を焼くときに書き換えるのは次の 3 か所だけです**(他はそのままで動きます):

| | jawiki | osm_japan の場合 |
|---|---|---|
| 3. の `SOURCE=` | `jawiki` | `osm_japan`(`osm_france` 等、国名を変えるだけ) |
| 4. のコピー元ファイル名 | `jawiki-<日付>.db` | `osm_japan-<日付>.db` |
| 4. の `ln -sfn` の 2 つめの引数 | `jawiki.db` | `osm_japan.db` |

世代ファイル名の日付は取り込み時に決まるので、`ls /path/to/chiezo-data/*.db` で実際の名前を
確認してからコピーしてください。ビルド機にメモリの余裕があるなら `-e BUILD_PROFILE=fast` を
足すと速く焼けます(ソースによって必要メモリが違う点は上の
[メモリについて](#メモリについて)の表を参照。付けなければ既定の `low_memory` でどのソースも
2 GiB で焼けます)。`SOURCE` に渡せる名前(`osm_<国>` 195 件 + `<lang>wiki` 348 件 + geonames)は
ビルド機だけでも引けます:

```bash
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import sources; print('\n'.join(sources.ADAPTERS))" | grep france
```

ビルド機がインターネットに出られない・GHCR を使いたくない場合は、イメージをファイルで持ち込めます:

```bash
docker pull ghcr.io/rtcode337/chiezo-ingest:latest   # または docker-compose.build.yml を重ねてローカルビルド
mkdir -p handoff                                     # 持ち出す成果物の置き場(リポジトリ管理外)
docker save ghcr.io/rtcode337/chiezo-ingest:latest | gzip -1 > handoff/chiezo-ingest-image.tar.gz
# ビルド機で: docker load -i chiezo-ingest-image.tar.gz
```

詳しい手順(必要ディスク・所要時間、Docker Desktop/WSL2 のメモリ設定、後片付け、環境変数一覧)は
[`docs/build-on-another-machine.md`](build-on-another-machine.md) にまとめてあります
(取り込みが触るのは公開ダンプと指定した data フォルダだけで、認証情報や個人ファイルは読みません)。

## chiezo-trigger(管理画面からの初期化・再構築)

`chiezo-ingest` と同じイメージを使い回し、CMD だけ `server.py`(FastAPI)の起動に差し替えた
常駐コンテナです。`/data` に書き込み権限を持ち、`POST /run/{source}` で ingest の `run()` を
バックグラウンドスレッドで実行、`GET /status` で状態(`idle`/`running`/`done`/`error`)と
ログ tail を返します。同時に実行できるジョブは 1 つまでです。`GET /sources` は取り込める
ソースのカタログ(名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
wikipedia 言語版の表示名・自称・記事数)と、このイメージが焼くスキーマバージョン
(`schema_version`)を返し、管理画面の初期化一覧・国選択画面・言語選択画面・
「最新のスキーマバージョン」表示はこれを読んで組み立てます。

ホストへはポート公開せず、docker の内部ネットワーク経由で `chiezo-api` からのみ到達できます
(`chiezo-api` の環境変数 `CHIEZO_TRIGGER_URL=http://chiezo-trigger:8080`)。管理画面の
「初期化」ボタン(`POST /admin/init/{source}`)と「再構築」ボタン
(`POST /admin/rebuild/{source}`。登録済みソースのみ受け付ける)はこのサービスへのプロキシです。
`CHIEZO_TRIGGER_URL` が未設定なら管理画面の初期化・再構築機能は無効化されます(ボタンが押せません)。

`GET /sources` の結果は chiezo-api が `CHIEZO_CATALOG_TTL` 秒(既定 300)キャッシュします。
大半はイメージに焼かれた静的な表ですが、プラグインはマウントで実行時に足せるので、
永久に持つと足したソースが管理画面に出ません。取り直しに失敗したときは**古いカタログを
そのまま使い続けます**(trigger が一時的に落ちただけで一覧が空になるのを避けるため)。
0 以下にすると取り直しません。この値と `CHIEZO_RESCAN_INTERVAL`・`CHIEZO_MCP_ALLOWED_HOSTS` は
compose が `.env` から受け取ります(`.env.example` の「1. Chiezo 本体」)。

新ソースは `ingest/sources/__init__.py` の `ADAPTERS` に追加するだけで管理画面にも出ます
(`chiezo-api` は ingest のコードを import しませんが、上記の `GET /sources` 経由で名前を受け取ります)。
`api/app/known_sources.py` の `KNOWN_SOURCES` は、`chiezo-trigger` が未設定・到達不能なときに
管理画面を空にしないための控えです(必須の複製ではありません)。

## `.env` を置けない環境で起動する(単体定義)

管理画面に YAML を貼り付けて起動するタイプの実行環境では、`.env` もシェルの環境変数も
無いため `${...}` を解決できず、`--profile` も付けられません。この場合は値を直接書いた
[`docker-compose.standalone.example.yml`](../docker-compose.standalone.example.yml) を使います。

```bash
# 中身をコピーして、先頭の置き場を実際の絶対パスに書き換えてから貼り付ける
cp docker-compose.standalone.example.yml docker-compose.standalone.yml   # コピー先は .gitignore 済み
cat docker-compose.standalone.yml
```

編集するのは先頭の 2 行(`x-data-dir` / `x-notes-dir`)だけです。**ホスト側は絶対パスで
書きます** —— 貼り付けて登録する環境では相対パス(`./data`)の基準が読めないためです。
どちらのディレクトリも**あらかじめ作っておいてください**(取り込んだ `.db` は data に置く)。

起動するのは `chiezo-api` と `chiezo-trigger` の 2 つで、通常の `docker-compose.yml` と同じく
trigger はホストへ公開されず、管理画面の初期化ボタンから内部ネットワーク経由で叩かれます。
**「答える」層は設定だけを載せてあり、コンテナは含みません** —— 推論サーバと検索エンジンは
別サーバーで動かしているものを指せば済み(chiezo-api は OpenAI 互換の口を叩くだけ)、
貼り付けて動かすような実行環境で同居させる前提も無いためです。同じホストに立てるなら、
上書きを重ねられる環境で `docker-compose.answer.yml` を使ってください。

`cpu_shares` のように受け付けない実行環境がある項目は、行ごと消しても動きます。
**`docker-compose.yml` を変えたらこのファイルも追従させてください**(値が直書きのぶん、
黙って古くなります)。追従漏れは `tests/test_compose_files.py` が検知します —— 本体が
`chiezo-api` に渡している設定が、コメントとしてでもこのファイルに出てくるかを照合します。

## ソースの追加・削除

`data/` に `<source>.db` を置く(または消す)だけです(chiezo-api が数秒以内に自動で検知します)。
新しい種類のソースの取り込み方は [adding-a-source.md](adding-a-source.md) を参照してください。

**このリポジトリに入れられないソース**(公開できないプライベートな情報など)は、
別リポジトリのプラグインとして書いて足せます。**プラグインは別イメージ・別コンテナの
サービス**で、本体からは HTTP で見えます(取得と整形はプラグイン、DB の構築は本体)。

```yaml
# 別リポジトリ側の docker-compose.plugin.yml(Chiezo の compose に重ねる)
services:
  my-plugin:
    image: ghcr.io/<自分のアカウント>/my-plugin:latest   # 本体とは別に pull する
    restart: unless-stopped
  chiezo-trigger:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080     # カンマ区切りで複数可
    depends_on: [my-plugin]
  chiezo-ingest:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080
```

```bash
# .env
COMPOSE_FILE=docker-compose.yml:../my-plugin/docker-compose.plugin.yml
```

プラグインが落ちていても本体は動きます(そのソースが一覧に出なくなるだけ)。

これで管理画面の「初期化」「再構築」ボタンからも回せます。Chiezo 側にはコードもデータも
入りません。そもそも**配信側はソース種別を知らない**ので、管理画面から回す必要が無ければ
「別リポジトリで `.db` を焼いて `data/` に置く」だけで動きます(設定も不要)。
手順は [adding-a-source.md のケース 3](adding-a-source.md) が正です。

## セキュリティ

認証はありません。LAN 内利用が前提です。ルーターでポート 7010 を外部に開放しないでください。
必要ならホストの LAN インターフェースのみに bind するよう compose の `ports` を
`"<LAN の IP>:7010:7010"` の形式に変更してください。

`/mcp` は既定で Host ヘッダの検証(DNS リバインディング対策)を**無効**にしています。
MCP SDK の既定は「localhost 系の Host しか受け付けない」で、そのままだと LAN の別マシンから
繋いだ時点で 421 になり、このサービスの使い方が成立しないためです(REST 側も認証なし・
LAN 内前提なので方針は揃っています)。絞りたい場合は `chiezo-api` の環境変数
`CHIEZO_MCP_ALLOWED_HOSTS` に許可する Host をカンマ区切りで指定してください
(例: `<LAN の IP>:7010,localhost:*`。末尾 `:*` でポート任意)。

「使う」層(`/v1/ask`・`/v1/chat`・`/ai/chat`)も同じ方針です。推論コンテナ `chiezo-llm` は
ホストへポートを公開せず、chiezo-api からのみ到達できます。ただし到達できる相手は誰でも
推論を起動できる(比較的重い処理を認証なしで回せる)点は、初期化ボタンと同様に留意してください。
質問文が外部へ送られることはありません(推論も検索もローカルで完結します)。

`chiezo-trigger` はホストへポートを公開していません(`docker-compose.yml` に `ports:` の記載なし)。
`chiezo-api` からのみ内部ネットワーク経由で到達できます。管理画面の初期化ボタンは
`chiezo-api` にも認証を課していないため、`/admin` に到達できる相手は誰でも初期化を起動できます
(LAN 内前提のこのサービス全体の認証なし方針と同一線上ですが、ダウンロード・構築という
比較的重い処理を誰でも起動できる点は留意してください)。

`notes`(唯一書き込めるソース)も同じで、`/v1/notes` に到達できる相手は誰でも書けます。
