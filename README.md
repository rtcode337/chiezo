# <img src="assets/icon.svg" width="40" alt="chiezo icon"> chiezo — ローカル知識サーバー

LAN 内の開発マシン(主に Claude Code)から使う、完全ローカルの知識検索 REST API です。
公式ダンプを SQLite (FTS5 trigram) に取り込み、外部 API のレート制限や負荷を気にせず参照できます。

- マルチソース設計: ソースごとに独立した SQLite ファイル 1 つ(`data/<source>.db`)
- 収録ソース:
  - `jawiki` — 日本語 Wikipedia(標準 XML ダンプ + wikitext 解析由来。CirrusSearch ダンプの
    text フィールドは折りたたみ(collapsible)セクションを検索インデックスから除外して
    いたため、この方式に切り替えた)。
    Wikipedia は **348 の言語版が定義済み**で(`enwiki` / `dewiki` / `zh_yuewiki` …)、
    使いたい言語だけを取り込みます。言語の選択は管理画面の `/admin` → `wikipedia` → 言語選択から
  - `osm_<国>` — OpenStreetMap の国別抽出(Geofabrik 由来の地名辞典 + POI 辞典。
    地名・行政区・自然地物に加え、病院・学校・店舗・観光地等の主要 POI と
    駅・空港・港・IC/SA 等の交通インフラ、およびそれらの座標)。
    Geofabrik にある **195 の国・地域が定義済み**で(`osm_japan` / `osm_france` / `osm_thailand` …)、
    使いたい国だけを取り込みます。国の選択は管理画面の `/admin` → `osm` → 国選択から
  - `geonames` — GeoNames 全世界地名辞典(約 400MB のダンプで約 1,200 万件。
    多言語別名を持つので「パリ」「ニューヨーク」のような日本語表記から引ける。
    wikidata の Q 番号も拾うので jawiki と突合できる。**店舗・営業時間は持たない**
    — そこは osm 系の担当)
- API: FastAPI + uvicorn(ポート 9000)、認証なし・LAN 内前提
- 管理画面(`/admin`)から未初期化ソースの取り込みを起動できます(内部専用の `chiezo-trigger`
  サービス経由。ホストへポート公開せず、`chiezo-api` からのみ到達可能)

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
#     (定義済みの国は管理画面の /admin/osm、または GET /sources[chiezo-trigger] で一覧できます)
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 2''. geonames を取り込む(全世界の地名。ダンプ約 400MB + 別名 191MB。
#      OSM の大陸抽出と違い 1 ソースで全世界を賄える)
docker compose --profile ingest run --rm -e SOURCE=geonames chiezo-ingest

# 3. 新しい DB を読み込ませる
docker compose restart chiezo-api
```

## API の使い方

```bash
BASE=http://<サーバーIP>:9000

# 日本語・スペース等を含むパラメータ(q/title/prefix/area/feature)は、URL に直接埋め込む
# のではなく `-G --data-urlencode` で渡す(生の UTF-8 はサーバーに弾かれるため)
curl -s "$BASE/v1/sources"                                          # ソース一覧
curl -sG "$BASE/v1/jawiki/search?limit=5" --data-urlencode "q=浅草寺"                    # 全文検索
curl -sG "$BASE/v1/jawiki/doc?fields=title,opening,tags" --data-urlencode "title=浅草寺" # 文書概要
curl -sG "$BASE/v1/jawiki/doc?max_chars=8000" --data-urlencode "title=浅草寺"            # 文書全文(切り詰め)
curl -sG "$BASE/v1/jawiki/doc?fields=title,extra" --data-urlencode "title=浅草寺"        # ページビュー等の付加情報
curl -sG "$BASE/v1/jawiki/titles" --data-urlencode "prefix=浅草"                         # タイトル前方一致
curl -sG "$BASE/v1/jawiki/links" --data-urlencode "title=浅草寺"                         # リンク先一覧
curl -s "$BASE/v1/jawiki/random?limit=3"                            # ランダム文書

curl -sG "$BASE/v1/osm_japan/search?limit=5" --data-urlencode "q=富士山"                 # 地名・POI検索(同一エンドポイント)
curl -sG "$BASE/v1/osm_japan/doc?fields=title,extra" --data-urlencode "title=京都市"     # 座標・OSMタグ等

# 属性での一括抽出(全文検索ではなく等価・範囲条件。Overpass API 相当)
curl -sG "$BASE/v1/osm_japan/filter?limit=200" \
  --data-urlencode "feature=amenity=place_of_worship" --data-urlencode "area=京都府"
curl -sG "$BASE/v1/osm_japan/filter?bbox=34.9,135.6,35.1,135.9" --data-urlencode "feature=tourism=museum"
curl -s "$BASE/v1/jawiki/filter?wikidata=Q17221&fields=title,extra" # Q 番号 → 記事の逆引き
```

ブラウザで `http://<サーバーIP>:9000/`(`/admin` へ自動リダイレクト)を開くと、登録済みソース
(文書数・dump_date・構築日時など)の一覧に加えて、未初期化ソース(`chiezo-trigger` 側の
既知ソース一覧に載っているが `/data` にまだ `.db` が無いもの)向けの「初期化」ボタンが見られます。
ボタンを押すと `chiezo-trigger`(内部専用サービス。後述)にジョブが積まれ、進行状況(ログ tail 込み)
が管理画面に表示されます(実行中は自動でリロードされます)。ジョブが完了したら
`docker compose restart chiezo-api` で新しい DB を読み込ませてください(自動再起動はしません)。

OSM の国別ソース(`osm_<国>`、195 件)と Wikipedia の言語版(`<lang>wiki`、348 件)は、
そのまま並べると他のソースが埋もれるため、一覧ではそれぞれ `osm` / `wikipedia` の 1 行に
まとめてあります。`osm` 行の「国を選ぶ」から国選択の画面(`/admin/osm`)が開き、
大陸ごとに畳まれた一覧から国を選んで初期化できます。各国の pbf サイズと必要メモリの目安、
構築済みかどうかもそこに出ます(国名・`region` での絞り込み可)。同様に `wikipedia` 行の
「言語を選ぶ」から言語選択の画面(`/admin/wikipedia`)が開き、記事数の階層ごとに畳まれた
一覧から言語を選んで初期化できます(言語名・コードでの絞り込み可)。

さらに、各ソース名は `/{source}/` にリンクしています。トップは検索フォームのみで(jawiki のような
大規模ソースだと rank_score 順の全件一覧はフルスキャンになりタイムアウトしうるため、未検索時は
一覧を出しません)、検索すると結果一覧が表示されます。`/{source}/doc/{doc_id}` で文書詳細
(本文・tags・links・extra)をブラウザで閲覧できます(`/v1/...` の JSON API を人間向け HTML で
薄くラップしたものです)。

主な仕様:

- `search` — `limit` 既定 10・最大 50。3 文字以上の語が無いクエリは自動的にタイトル前方一致へ
  フォールバックし、レスポンスの `"mode"` が `"title_prefix"` になります(通常は `"fts"`)。
  `filter` と同じ `area` / `feature` / `bbox` を併用でき、同名の別地物を掴む取り違えを避けられます
  (例: `search?q=八坂神社&area=京都府`)。
- `doc` — `title` 完全一致 → リダイレクト(alias)解決 → 見つからなければ 404 と近似候補 5 件。
  `fields`(既定 `title,opening,body,tags,updated_at`)と `max_chars` で応答サイズを制御できます。
  同名の別地物が他にもある場合は `alternatives`(`doc_id` / `title` / `feature` / `area` /
  `lat` / `lon` を最大 5 件)を併記するので、取り違えにその場で気づけます。`area` / `feature` /
  `bbox` で最初から絞り込むこともできます(例: `doc?title=博多駅&feature=railway%3Dstation`)。
- `filter` — 属性での絞り込み一括抽出。`feature`(`amenity=place_of_worship` 形式。カンマ区切りで
  複数可)・`area`(所属行政区名)・`bbox`(`min_lat,min_lon,max_lat,max_lon`)・`wikidata`(Q 番号)を
  AND で組み合わせます。1 つ以上の条件が必須(無指定は 400)。`limit` 既定 50・最大 500、応答の
  `total` と `offset` でページングできます。実体は `docs` の生成列(`feature` / `area` / `lat` /
  `lon` / `wikidata`)への索引付き検索で、これは `schema_version` 2 以降の DB にしかありません
  (1 のまま残っている DB では 409 を返すので、取り込みをやり直してください)。
- `extra` フィールド(jawiki) — 座標を持つ記事(`{{Coord}}` テンプレートや駅・空港・施設の
  Infobox の `緯度度`/`経度度` 系引数から抽出)には `{"lat": ..., "lon": ...}` が入ります。
  ページビューを突合できた記事には
  `{"pageviews_month": <月間閲覧数>, "pageviews_period": "YYYY-MM"}` が入ります(Wikimedia の
  `pageview_complete` 月次ダンプ由来、bot 除外・全アクセス種別合算)。突合できなかった記事は `null`。
  `page_props` ダンプから wikidata の Q 番号が取れた記事には `{"wikidata": "Q17221"}` も入り、
  `filter?wikidata=` で逆引きできます(OSM 側の `wikidata` タグと突き合わせられます)。
- `extra` フィールド(osm_japan) — `{"osm_type": "node|way|relation", "osm_id": ..., "lat": ...,
  "lon": ..., "feature": "place=city", "tags": {<OSM 生タグ>}, ...}`。way / relation の座標は
  構成ノードの平均(近似重心)で、行政境界は admin_centre / label ノードを優先します。
  同名地物はタイトルを「名前 (node:123)」形式で弁別し、元の名前は alias として引けます。
  POI(`feature` が `amenity=*` / `shop=*` / `tourism=*` / `leisure=*` / `historic=*` /
  `craft=*` / `office=*` / `healthcare=*`)では、住所(`addr:*` 由来)が取れれば `address`、
  電話・サイト・営業時間が取れれば `phone` / `website` / `opening_hours` も入ります。
  交通インフラ(`feature` が `railway=station|halt|tram_stop` / `aeroway=aerodrome|terminal|
  helipad|heliport` / `aerialway=station` / `public_transport=station` /
  `highway=services|rest_area|motorway_junction|toll_gate` / `man_made=bridge|lighthouse|pier|tower`。
  港は `amenity=ferry_terminal`)も取り込み対象で、同名の店舗より上位に来るよう `rank_score` を
  高く設定しています(「博多駅」で同名のラーメン店を掴む取り違えの防止)。
  地名(place/boundary/natural)・POI・交通インフラは同一ソース内に混在し、`search` は
  すべてをヒットさせます。
  座標を持つ地物には所属する行政区を `area`(日本では都道府県。`admin_level=4` の境界 relation を
  ポリゴンとして組み立て、点内包判定で決定)として付けます。bbox 近似ではないので県境をまたいだ
  取りこぼし・混入はありません。取り込み時の判定粒度は環境変数 `OSM_AREA_ADMIN_LEVEL` で変更でき、
  `0` を指定すると境界パスごとスキップします(`area` は付かなくなります)。
- 全クエリ 5 秒タイムアウト(超過は 504)。エラーは `{"error": "..."}` 形式。

### Claude Code から使う(設定ファイル自動生成)

各アプリの環境で動く Claude に「chiezo に載っている知識が必要なら chiezo を使う」よう
促す CLAUDE.md ブロックを、稼働中の chiezo の設定生成 API
(`GET /admin/claude-config.txt`)に問い合わせて自動生成できます。登録済み
(初期化済み)ソースだけを、実データの文書数・実在タイトルを使った具体例つきで列挙します。
ブロック内の curl 例のベース URL は、chiezo 側が「スクリプトがアクセスしてきた URL の
プロトコル・ホスト名・ポート」から導出するため、`--base-url` に指定した到達可能な URL が
そのまま生成物に載ります(リバースプロキシ越し・非標準ポートでも可)。
`curl` だけで動き追加インストールは不要(Python 不要。既存 settings への権限マージにのみ
jq を使います)。
**既定の書き込み先は `~/.claude/CLAUDE.md`**(全プロジェクトの Claude に効く推奨の使い方)。
あわせて既定で、書き込み先に対応する Claude Code 設定(`--user` なら
`~/.claude/settings.json`、`--project`/`--target` なら `.claude/settings.local.json`)に
chiezo への `curl` を許可するルールを追記するため、生成後は chiezo への `curl` が
毎回の許可プロンプトなしに実行できます(`--no-permissions` で無効化可)。

```bash
# ~/.claude/CLAUDE.md を更新・localhost:9000 を参照
/path/to/chiezo/scripts/gen_claude_config.sh

# chiezo が LAN 上の別ホストにある場合は場所を指定(環境変数 CHIEZO_URL でも可)
scripts/gen_claude_config.sh --base-url http://<サーバーIP>:9000

scripts/gen_claude_config.sh --project     # ~/.claude ではなく ./CLAUDE.md にする
scripts/gen_claude_config.sh --print       # 書き込まず内容だけ確認
```

既存 CLAUDE.md との共存:

- 既定(`--merge markers`)は `<!-- BEGIN chiezo -->`〜`<!-- END chiezo -->` で囲んだ
  ブロックだけを冪等に差し替えます。既存の記述は壊さず、再実行でソース一覧が最新化されます。
- 既存内容との統合に人間的な判断が要る場合は `--merge headless` で Claude Code の
  ヘッドレスモード(`claude -p`)にマージを任せられます(`claude` CLI が必要)。

主なオプション: `--base-url/-u`(chiezo の場所)、`--user`(既定・`~/.claude/CLAUDE.md`)、
`--project`(`./CLAUDE.md`)、`--target/-o`(書き込み先をパス指定)、`--merge {markers,headless}`、
`--print`、`--no-permissions`(既定で行う上記の権限追記を無効化)。
生成は chiezo 本体が行うため、稼働中の chiezo が必要です(旧 `--offline --sources` は廃止)。
生成される文面の要点は「まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)」です。

## 運用

### ダンプ更新(ブルーグリーン)

ingest は毎回 `data/<source>-<date>.db` を新規構築し、検証が通ったらシンボリックリンク
`data/<source>.db` を差し替えます。旧世代は 1 つだけ残します。API の停止時間は再起動の数秒のみです。

```bash
docker compose --profile ingest run --rm chiezo-ingest && docker compose restart chiezo-api
```

月次 cron の例:

```cron
0 3 1 * * cd /opt/chiezo && docker compose --profile ingest run --rm chiezo-ingest && docker compose restart chiezo-api
```

ingest の環境変数:

| 変数 | 説明 |
|---|---|
| `SOURCE` | 取り込むソース名(必須。compose 既定は `jawiki`。`-e SOURCE=osm_japan` 等で上書き) |
| `DUMP_DATE` | ダンプ日付 `YYYYMMDD` を固定(省略時は最新を自動検出。osm 系は常に latest を取得するため世代ラベルの上書きのみ) |
| `DUMP_FILE` | ダウンロードをスキップし既存ファイルを使う(カンマ区切りで複数シャード指定可) |
| `PAGEVIEW_PERIOD` | ページビュー突合対象の年月 `YYYY-MM` を固定(省略時は最新月を自動検出) |
| `MIN_DOCS` / `SAMPLE_TITLES` | 検証パラメータの上書き(小規模データでの動作確認用) |
| `OSM_AREA_ADMIN_LEVEL` | `extra.area` に入れる行政区の admin_level(既定 4 = 都道府県。`0` で境界パス省略) |
| `OSM_NODE_INDEX` | osm のノード座標索引の置き場。既定はソースごと(小さい国は `sparse_mmap_array`〈RAM・速い〉、RAM 索引が 12GiB を超える国は `sparse_file_array`〈ディスク・省メモリ・遅い〉) |
| `GEONAMES_ALT_LANGS` | geonames で取り込む別名の言語(カンマ区切り。既定 `ja,en`、`*` で全 400 言語超) |
| `GEONAMES_FEATURE_CLASSES` | geonames で取り込む feature class(既定 `AHLPSTUV` = 道路 `R` 以外すべて) |
| `BUILD_MEMORY_GB` / `SKIP_MEMORY_CHECK` | 構築前メモリ検査の必要量を上書き / 検査を無効化(「メモリについて」参照) |

Wikipedia 系ソースは標準 XML ダンプ(`https://dumps.wikimedia.org/<wiki_id>/<date>/
<wiki_id>-<date>-pages-articles.xml.bz2`、jawiki で確認時点 4.4GB)を取得し、
`xml.etree.ElementTree`(標準ライブラリ)でストリーミング解析、記事本文の wikitext は
`mwparserfromhell`(wikipedia 系ソースのみの例外的依存)でプレーンテキスト化します。以前は CirrusSearch
ダンプの `text` フィールドを直接使っていましたが、このフィールドは Wikipedia の折りたたみ
(collapsible)セクション(`{{hidden begin}}`〜`{{hidden end}}` 等)を検索インデックスから
除外しており、例えば「ブラタモリ」の放送回一覧表のような内容が本文に一切含まれない欠落が
あったため、XML ダンプ + wikitext 解析に切り替えました。折りたたみテンプレートは通常の
テンプレート呼び出しとして扱われるため、中身(表を含む)が自然に本文へ含まれます。
`{{Dts|年|月|日}}`のようなテンプレートは完全展開はせずパラメータ値の連結として残るため
(例:「2015 4 11」)、日付表示は整形されませんが検索対象としては機能します。
リダイレクトは XML 上ではリダイレクト元ページの `<redirect>` タグとして表現されるため、
`ingest/sources/osm.py` と同じ 2 パス走査(パス1: リダイレクト元→対象の対応収集、
パス2: 本体の Doc 生成)で aliases に変換しています。XML ダンプには CirrusSearch の
`popularity_score` 相当の人気度指標が無いため `rank_score` は `0.0` 固定になります
(ページビューは従来どおり `extra` に格納されます)。

Wikipedia 系ソースは記事本文のダンプに加えて、Wikimedia の月次ページビューダンプ
(`other/pageview_complete/monthly/`、全プロジェクト合算で圧縮 5〜6GB)もダウンロードし、
`page_id`(XML ダンプの `<page><id>`)で突合して `docs.extra` に月間閲覧数を格納します。
`WIKI_DOMAIN`(`ingest/sources/wikipedia.py`)に対応表が無い wiki_id ではこの突合を
スキップします。この分、初回取り込みのダウンロード量・所要時間は README 冒頭の見積もりより
やや増えます。

### 地理データの守備範囲(geonames と osm の分担)

「全世界の地名に答える」用途と「特定の国を店舗レベルまで掘る」用途は、データ源を分けています。

| | `geonames` | `osm_<地域>` |
|---|---|---|
| 範囲 | **全世界**(約 1,200 万件) | 取り込んだ地域のみ |
| ダンプ | 約 400MB + 別名 191MB | 国単位で 2〜5GB(大陸単位は 32GB) |
| 持っているもの | 地名・座標・国/行政区・人口・多言語別名・timezone | 地名 + **店舗/施設/交通** の詳細、住所・電話・営業時間 |
| 持っていないもの | 店舗・レストラン・営業時間 | 取り込んでいない国のすべて |

大陸単位の OSM(`europe-latest.osm.pbf`)を 1 ソースとして取り込む案は**廃止しました**。pbf だけで
32GB、ノード座標索引が 100GB 超、構築に 1 日以上かかるうえ、実測で `osm_japan` の内訳は
**73% が店舗・施設の裾**(地名・行政界・自然・観光は合計 27%)で、「全世界の地名」を得る手段としては
過剰だったためです。逆に地名だけに絞れば、それは GeoNames が 80 分の 1 のサイズで既に提供している
ものになります。

したがって推奨構成は:

- **全世界の問い合わせ** → `geonames`(1 ソースで賄う)
- **店舗レベルの詳細が要る国** → その国だけ `osm_<国>` を取り込む(管理画面 `/admin/osm` から選ぶか
  `-e SOURCE=osm_<国>`)。多くの国は pbf 数百 MB〜2GB で、RAM 索引のまま 12GiB 予算・数時間で焼けます
  (フランス・ドイツ・カナダ・アメリカ・ロシアのような大きい国だけは既定がディスク索引になり、
  2GiB で焼ける代わりに数倍遅くなります)。
- **事物の解説** → `jawiki`(座標と wikidata の Q 番号を持つ)

`geonames` と `jawiki` はどちらも `extra.wikidata` に Q 番号を持つので、
`filter?wikidata=Q90` で相互に引き当てられます。

OSM 系ソース(`osm_japan` 等)は Geofabrik の地域抽出
`https://download.geofabrik.de/<region>-latest.osm.pbf` をダウンロードし、pyosmium
(libosmium バインディング)でストリーミング解析します(Geofabrik が 2026 年に `.osm.bz2` の
配布を終了し `.osm.pbf` のみになったため、標準ライブラリの `xml.etree` では読めなくなった。
osm 系ソースに限り pyosmium への依存を許容している)。取り込むのは「名前付き地物」です:
`place=*`、`boundary=administrative`、主要な `natural=*`(山頂・湖沼・島・湾など)に加えて、
主要 POI(`amenity=*` `shop=*` `tourism=*` `leisure=*` `historic=*` `craft=*` `office=*`
`healthcare=*`)と、交通インフラ(`railway` `aeroway` `aerialway` `public_transport` `highway`
`man_made` のうち「地点を指す値」だけを列挙。`railway=rail` や `highway=residential` のような
名前付きの線形地物は除外)。いずれも `name` タグ必須です。住所補間・逆ジオコーディングはできません
(それが必要な場合は公式の [nominatim-docker](https://github.com/mediagis/nominatim-docker)
を別途立ててください)。ファイルは relation メンバーの把握(パス1)→ 行政境界ポリゴンの構築
(パス2。`extra.area` の点内包判定用。`OSM_AREA_ADMIN_LEVEL=0` で省略可)→ ノード座標解決込みの
node/way/relation 走査(パス3)の 3 パスで読むため、構築時間はダウンロード後さらに
2〜6 時間程度かかります。パス3 のノード座標インデックスは既定で RAM 上(`sparse_mmap_array`)に
置きます。参照ノードぶんの座標を抱えるため日本抽出で 5〜10GB 使いますが、これがいちばん速く、
潤沢メモリのマシンで取り込む前提だからです(足りるかどうかは開始前に検査されます)。
メモリの少ない環境では `OSM_NODE_INDEX=sparse_file_array` でディスク上のファイル
(`data/dumps/<source>.nodeloc.idx`、終了・中断のいずれでも自動削除)へ逃がせます。
必要メモリは 2GiB まで下がる代わりに、ノード解決がランダム読みになり数倍〜10 倍遅くなります。

中断しても運用 DB は壊れません(`.building` の一時ファイルに構築するため)。再実行すれば最初からやり直します。

#### メモリについて

取り込み中に参照する巨大な対応表(リダイレクト・ページビュー・wikidata の Q 番号。jawiki では
それぞれ 160〜190 万件)は、メモリではなく `data/dumps/*.lookup.db` / `*.redirects.db`(一時 SQLite)に
持ちます。常駐メモリは SQLite のページキャッシュ上限(約 32MiB/表)に固定され、コーパス規模に
よらず一定です(これらを素朴に dict で抱えていた版では合計 GB 級になり、メモリ 8GB 級のホストで
他コンテナごと OOM で落ちる事故がありました)。これらの一時ファイルは取り込み終了・中断のいずれでも
自動削除されます。

**設計方針: 取り込みは「メモリが足りることを確認できたときだけ」実行する。** 取り込み系コンテナに
`mem_limit` は課していません。上限で締めると、足りないときに OOM killer が数時間かけた構築を
最後に殺すだけだからです(実際に 1GiB で締めて osm のノード座標索引が入りきらず落ちました)。
代わりに ingest は**開始前にメモリを検査し、足りなければダウンロードもせず即座に中止**します。

| ソース | 必要メモリの目安 | 内訳 |
|---|---|---|
| `jawiki` | 3 GiB | 巨大な対応表はディスクへ逃がしてあるので軽い。実測ピークは 1GiB 未満 |
| `osm_japan` | 12 GiB | ノード座標索引を RAM に持つため(実測 5〜10GB) |
| `osm_<他の国>` | 3〜12 GiB | pbf 1GB あたり RAM 索引 5GiB 見当。国ごとの目安は `/admin/osm` に出ます |
| `geonames` | 3 GiB | 別名(2,000 万行規模)はディスクへ逃がすため軽い |

RAM 索引が 12GiB に収まらない国(フランス・ドイツ・カナダ・アメリカ・ロシア)は、
**ディスク索引(`sparse_file_array`)が既定**です。必要メモリは 2 GiB に下がる代わりに数倍遅くなります
(「既定設定ではどのソースも 12GiB のマシンで構築できる」という方針を保つための切り替えで、
`OSM_NODE_INDEX=sparse_mmap_array` を明示すれば潤沢メモリのマシンで RAM 索引に戻せます)。

足りない場合のメッセージと対処:

```
not enough memory to build osm_japan: 2.0 GiB available < 12.0 GiB required.
```

1. メモリの多いマシンで焼いて `.db` をコピーする(下記「別マシンでビルドして .db を配布する」)
2. `OSM_NODE_INDEX=sparse_file_array` でノード座標索引をディスクへ逃がす(osm 系のみ。要件は
   2 GiB まで下がるが、ノード解決がランダム読みになり**数倍〜10 倍遅い**)
3. `BUILD_MEMORY_GB=<n>` で必要量を上書き、`SKIP_MEMORY_CHECK=1` で検査を無効化(見積もりが
   実態と合わないと分かっている場合のみ)

**配信側はメモリ数百 MB で動きます。** chiezo-api は読み取り専用の immutable SQLite を開くだけなので、
1GB 級の小型機でも配信できます。効いてくるのはメモリではなくディスク(jawiki.db 約 42GB の空き)。

### 別マシンでビルドして .db を配布する

chiezo の DB は**自己完結した単一の SQLite ファイル**なので、配布は「ファイルをコピーするだけ」です。
export/import も配信機でのビルドも要りません(SQLite のファイル形式は OS・CPU アーキ非依存なので、
Windows で焼いて Linux で読ませてよい)。メモリの少ない配信機と、メモリの多いビルド機を分けられます。

```bash
# 1. ビルド機(メモリ潤沢)で GHCR からイメージを pull して取り込む。
#    リポジトリも compose も設定ファイルも不要
docker run --rm -it -e SOURCE=jawiki -e CHIEZO_DATA_DIR=/data \
  -v /path/to/chiezo-data:/data ghcr.io/rtcode337/chiezo-ingest:latest

# 2. 出来た世代ファイルを配信機へコピーし、<ソース名>.db として見えるようにして再起動
cp jawiki-20260701.db /path/to/chiezo/data/
ln -sfn jawiki-20260701.db /path/to/chiezo/data/jawiki.db
docker compose restart chiezo-api
```

ビルド機がインターネットに出られない・GHCR を使いたくない場合は、イメージをファイルで持ち込めます:

```bash
docker pull ghcr.io/rtcode337/chiezo-ingest:latest   # または docker-compose.build.yml でローカルビルド
docker save ghcr.io/rtcode337/chiezo-ingest:latest | gzip -1 > handoff/chiezo-ingest-image.tar.gz
# ビルド機で: docker load -i chiezo-ingest-image.tar.gz
```

詳しい手順(必要ディスク・所要時間、Docker Desktop/WSL2 のメモリ設定、後片付け、環境変数一覧)は
[`handoff/BUILD-ON-ANOTHER-MACHINE.md`](handoff/BUILD-ON-ANOTHER-MACHINE.md) にまとめてあります
(取り込みが触るのは公開ダンプと指定した data フォルダだけで、認証情報や個人ファイルは読みません)。

### chiezo-trigger(管理画面からの初期化)

`chiezo-ingest` と同じイメージを使い回し、CMD だけ `server.py`(FastAPI)の起動に差し替えた
常駐コンテナです。`/data` に書き込み権限を持ち、`POST /run/{source}` で ingest の `run()` を
バックグラウンドスレッドで実行、`GET /status` で状態(`idle`/`running`/`done`/`error`)と
ログ tail を返します。同時に実行できるジョブは 1 つまでです。`GET /sources` は取り込める
ソースのカタログ(名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
wikipedia 言語版の表示名・自称・記事数)を返し、管理画面の初期化一覧・国選択画面・
言語選択画面はこれを読んで組み立てます。

ホストへはポート公開せず、docker の内部ネットワーク経由で `chiezo-api` からのみ到達できます
(`chiezo-api` の環境変数 `CHIEZO_TRIGGER_URL=http://chiezo-trigger:8080`)。管理画面の
「初期化」ボタン(`POST /admin/init/{source}`)はこのサービスへのプロキシです。
`CHIEZO_TRIGGER_URL` が未設定なら管理画面の初期化機能は無効化されます(ボタンが押せません)。

新ソースは `ingest/sources/__init__.py` の `ADAPTERS` に追加するだけで管理画面にも出ます
(`chiezo-api` は ingest のコードを import しませんが、上記の `GET /sources` 経由で名前を受け取ります)。
`api/app/known_sources.py` の `KNOWN_SOURCES` は、`chiezo-trigger` が未設定・到達不能なときに
管理画面を空にしないための控えです(必須の複製ではありません)。

### ソースの追加・削除

`data/` に `<source>.db` を置いて(または消して)`docker compose restart chiezo-api` するだけです。
新しい種類のソースの取り込み方は [docs/adding-a-source.md](docs/adding-a-source.md) を参照してください。

### セキュリティ

認証はありません。LAN 内利用が前提です。ルーターでポート 9000 を外部に開放しないでください。
必要ならホストの LAN インターフェースのみに bind するよう compose の `ports` を
`"192.168.x.x:9000:9000"` の形式に変更してください。

`chiezo-trigger` はホストへポートを公開していません(`docker-compose.yml` に `ports:` の記載なし)。
`chiezo-api` からのみ内部ネットワーク経由で到達できます。管理画面の初期化ボタンは
`chiezo-api` にも認証を課していないため、`/admin` に到達できる相手は誰でも初期化を起動できます
(LAN 内前提のこのサービス全体の認証なし方針と同一線上ですが、ダウンロード・構築という
比較的重い処理を誰でも起動できる点は留意してください)。

## 開発

```bash
python -m venv .venv && .venv/bin/pip install -r api/requirements.txt -r ingest/requirements.txt pytest
.venv/bin/python -m pytest tests/ -v
```

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

## 設計メモ

- SQLite + FTS5 (trigram) 採用。読み取り専用・少数クライアントなら数 ms〜数十 ms で十分。
  「ソース = 1 ファイル」が世代管理・ブルーグリーンとよく噛み合います。
- 割り切り: 3 文字未満の語は FTS 不可(前方一致へ自動フォールバック)、ランキングは簡易(bm25 + 人気度)。
- 移行トリガー: 検索精度に不満 → Meilisearch / 同時接続・書き込み要件 → PostgreSQL + PGroonga。
  API 層があるため DB だけ差し替え可能です。

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
