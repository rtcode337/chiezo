# <img src="assets/icon.svg" width="40" alt="chiezo icon"> chiezo — ローカル知識サーバー

**AI のための知識ベースです**。公開ダンプ(Wikipedia / OpenStreetMap / GeoNames)を
ローカルの SQLite (FTS5) に取り込んで索引し、AI が引ける形で出します。完全ローカルなので、
外部 API のレート制限も、問い合わせ内容が外に出ることもありません。

- **ためる** — `ingest` が公式ダンプを取り込み、ソースごとに独立した 1 つの SQLite ファイル
  (`data/<source>.db`)にする。更新はブルーグリーン(別ファイルに構築 → 切り替え)
- **取り出す** — AI からの引き口は 2 経路。**MCP**(`/mcp`、Streamable HTTP)と
  **REST**(`search` / `doc` / `filter` / `tags` …)。Claude Code 向けには「どんなときに
  chiezo を使うか」を書いた CLAUDE.md ブロックも自動生成します
- **答える(任意)** — ローカル LLM を同居させれば、ためた知識で回答まで返せます
  (未実装。既定では起動しない構成にする予定)

人間が中身を確かめるための簡易ブラウズ画面と管理画面(`/admin`)も付いています。

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

# 日本語・スペース等を含むパラメータ(q/title/prefix/area/feature/tag)は、URL に直接埋め込む
# のではなく `-G --data-urlencode` で渡す(生の UTF-8 はサーバーに弾かれるため)
curl -s "$BASE/v1/sources"                                          # ソース一覧
curl -sG "$BASE/v1/jawiki/search?limit=5" --data-urlencode "q=浅草寺"                    # 全文検索
curl -sG "$BASE/v1/jawiki/doc?fields=title,opening,tags" --data-urlencode "title=浅草寺" # 文書概要
curl -sG "$BASE/v1/jawiki/doc?max_chars=8000" --data-urlencode "title=浅草寺"            # 文書全文(切り詰め)
curl -sG "$BASE/v1/jawiki/doc?fields=title,extra" --data-urlencode "title=浅草寺"        # ページビュー等の付加情報
curl -sG "$BASE/v1/jawiki/titles" --data-urlencode "prefix=浅草"                         # タイトル前方一致
curl -sG "$BASE/v1/jawiki/links" --data-urlencode "title=浅草寺"                         # リンク先一覧
curl -s "$BASE/v1/jawiki/random?limit=3"                            # ランダム文書

# タグ(Wikipedia のカテゴリ)。カテゴリの全記事を列挙するのはこちらで、
# 本文の全文検索で "Category:" 行を拾ってはいけない(後述の取りこぼしがある)
curl -sG "$BASE/v1/jawiki/tags" --data-urlencode "contains=ラーメン"                     # タグ名を文書数つきで探す
curl -sG "$BASE/v1/jawiki/filter?limit=200" --data-urlencode "tag=ラーメン店"            # そのカテゴリの記事を全件

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
  並び順は 2 段で、**タイトルが検索語と完全一致する文書**が先、残りが
  **関連度(bm25)× 知名度(`rank_score`)の順**です(理由は
  [設計メモ](docs/design-notes.md#検索の並び順))。`rank_score` が入っていない古い DB では
  2 段目が bm25 だけになります(後述の「既存 DB の rank_score を入れ直す」)。
  `filter` と同じ `area` / `feature` / `bbox` を併用でき、同名の別地物を掴む取り違えを避けられます
  (例: `search?q=八坂神社&area=京都府`)。
- `doc` — `title` 完全一致 → リダイレクト(alias)解決 → 見つからなければ 404 と近似候補 5 件。
  `fields`(既定 `title,opening,body,tags,updated_at`)と `max_chars` で応答サイズを制御できます。
  同名の別地物が他にもある場合は `alternatives`(`doc_id` / `title` / `feature` / `area` /
  `lat` / `lon` を最大 5 件)を併記するので、取り違えにその場で気づけます。`area` / `feature` /
  `bbox` で最初から絞り込むこともできます(例: `doc?title=博多駅&feature=railway%3Dstation`)。
- `filter` — 属性での絞り込み一括抽出。`feature`(`amenity=place_of_worship` 形式。カンマ区切りで
  複数可)・`area`(所属行政区名)・`bbox`(`min_lat,min_lon,max_lat,max_lon`)・`wikidata`(Q 番号)・
  `tag`(タグ = Wikipedia のカテゴリ等。カンマ区切りで複数可、その中は OR)を
  AND で組み合わせます。1 つ以上の条件が必須(無指定は 400)。`limit` 既定 50・最大 500、応答の
  `total` と `offset` でページングできます。並びは `rank_score` の降順です。
  `schema_version` 2 以降が必要で(1 の DB には 409)、`tag` は 3 以降、`bbox` と
  大きな該当件数の並べ替えは 4 以降が実用的な速さになります。古い DB は
  後述の `scripts/add_tag_index.py` でその場で移行できます
  (どの版で何が速くなるかは [設計メモ](docs/design-notes.md#読む量を該当件数に比例させる))。
- `tags` — タグ名を文書数つきで列挙します(`prefix` = 前方一致 / `contains` = 部分一致 /
  無指定 = 文書数の多い順)。`filter?tag=` はタグ名の**完全一致**なので、Wikipedia の
  カテゴリのように表記の揺れがあるものは、まずここで実在する名前を確かめてから引くのが
  確実です(`schema_version` 3 以降。4 以降は集計表を引くので部分一致も速い)。
- **カテゴリの全記事を列挙したいときは、本文の全文検索(`search?q=Category:ラーメン店`)ではなく
  `filter?tag=` を使ってください**。全文検索だとソートキー付きのカテゴリを静かに
  取りこぼします(実データで 115 件中 16 件が漏れていました。
  [設計メモ](docs/design-notes.md#カテゴリは本文検索で列挙してはいけない))。
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
  POI(`amenity` / `shop` / `tourism` / `leisure` / `historic` / `craft` / `office` /
  `healthcare`)では、住所・電話・サイト・営業時間が取れれば `address` / `phone` /
  `website` / `opening_hours` も入ります。地名・POI・交通インフラ(駅・空港・港・IC/SA 等)は
  同一ソース内に混在し、`search` はすべてをヒットさせます(駅が同名の店舗より上に来るよう
  `rank_score` を高くしてあります)。座標を持つ地物には所属行政区が `area` として付きます
  (日本では都道府県。粒度は `OSM_AREA_ADMIN_LEVEL` で変更可)。
- 全クエリ 5 秒タイムアウト(超過は 504)。エラーは `{"error": "..."}` 形式。

### MCP から使う(`/mcp`)

REST と同じ機能を MCP(Model Context Protocol)のツールとしても提供しています。
**chiezo-api 自身が MCP サーバー**なので、クライアント側に何もインストールせずに繋がります。

```bash
claude mcp add --transport http chiezo http://<サーバーIP>:9000/mcp
```

公開しているツールは `sources` / `search` / `doc` / `filter` / `tags` / `titles` / `links` の
7 つで、引数は REST のクエリパラメータと同じです。実体も REST のエンドポイント関数そのものなので、
挙動が二重管理になることはありません。REST と違うのは `doc` / `filter` の `max_chars` が
既定で 4000 字に切られる点だけです(MCP の応答はそのままモデルのコンテキストに載るため。
全文が要るときは `max_chars` を明示的に上げてください)。

トランスポートは Streamable HTTP(ステートレス)です。Streamable HTTP に未対応の
クライアントからは `mcp-remote` 経由で繋いでください。

Claude Code では**従来の curl + CLAUDE.md 方式も引き続き使えます**(前節)。大量取得では
curl の方がトークン効率が良い場面があるため、どちらかに寄せる必要はありません。

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
`--print`、`--no-permissions`(既定で行う上記の権限追記を無効化)、
`--with-hook`(下記の自動許可フックを設置。既定では設置しない)。
生成は chiezo 本体が行うため、稼働中の chiezo が必要です(旧 `--offline --sources` は廃止)。
生成される文面の要点は「まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)」です。

#### 大量取得でプロンプトが出てしまう場合(`--with-hook`)

上の権限ルールは Claude Code の仕様上**コマンド文字列の前方一致**でしか判定できません。
そのため単発の `curl` には効きますが、

```bash
for t in 東京都 浅草寺 多摩川; do curl -sG ".../doc" --data-urlencode "title=$t"; done
curl -sG ".../search" --data-urlencode "q=多摩川" | jq -r '.hits[].title'
```

のように `curl` が先頭に来ない形になると 1 本もマッチせず、**大量取得のときだけ**
毎回プロンプトが出ます。これを解消したい場合は `--with-hook` を付けると、
前方一致ではなく**コマンドの構造**で判定する `PreToolUse` フックを併せて設置します
(`<設定ディレクトリ>/hooks/chiezo-autoallow.py` + `settings` の `hooks.PreToolUse`)。

フックが自動許可するのは「**chiezo だけを読む、読み取り専用のコマンド**」だけです
(登場する URL が全て chiezo / 実行されるコマンドが `curl`・`jq` 等の許可リスト内 /
`$(...)`・`eval` 等でコマンド位置を隠していない / ディスクへ書かない)。条件を外れたときは
何も出力せず通常の許可フローに戻る、判定に迷ったら黙る設計です。

ただしこれは **Claude が打つ Bash を毎回検査して自動承認しうる**仕掛けで、
権限ルールより影響範囲が広くなります。中身を読んで納得してから入れられるよう
**既定では設置せず**、`--with-hook` を明示したときだけ設置します。
判定ロジックの全文は設置前に確認できます:

```bash
curl -s http://<サーバーIP>:9000/admin/claude-config.hook.py   # フック本体
# 管理画面 /admin/claude-config でもプレビューできます
```

設置には `python3`(フックの実行)と `jq`(settings のマージ)が必要です。
何度実行しても `hooks.PreToolUse` は重複せず、設置先を変えた場合も古いエントリは掃除されます。
反映には Claude Code の再起動(または一度 `/hooks` を開く)が必要な場合があります。

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

### 既存 DB にタグ索引を足す(schema_version 2 → 3 → 4)

タグ絞り込み(`filter?tag=` / `tags`)には `schema_version` 3 以降が、`filter?bbox=` と
大きなカテゴリの並べ替えが実用的な速さになるには 4 以降が要ります(3 の DB でも遅い経路の
まま動きます。何がどう速くなるかは
[設計メモ](docs/design-notes.md#読む量を該当件数に比例させる))。

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

### 既存 DB の rank_score を入れ直す

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

スキーマは変わらないので `schema_version` も上がりません。入れ直していない DB でも
API は壊れません(`rank_score` を 0〜1 に丸めてから使うので、実質 bm25 だけの並びに
戻るだけです)。何度実行しても結果は同じです。

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

Wikipedia 系ソースは標準 XML ダンプ(`<wiki_id>-<date>-pages-articles.xml.bz2`、jawiki で
確認時点 4.4GB)を取得し、wikitext をプレーンテキスト化して取り込みます
(CirrusSearch ダンプを使わない理由は
[設計メモ](docs/design-notes.md#wikipedia-は-cirrussearch-ではなく-xml-ダンプから作る))。

記事本文に加えて Wikimedia の月次ページビューダンプ(全プロジェクト合算で圧縮 5〜6GB)も
ダウンロードし、`page_id` で突合して知名度(`rank_score`)と `extra.pageviews_month` に
します。この分、初回取り込みのダウンロード量・所要時間は冒頭の見積もりよりやや増えます。

### 地理データの守備範囲(geonames と osm の分担)

「全世界の地名に答える」用途と「特定の国を店舗レベルまで掘る」用途は、データ源を分けています。

| | `geonames` | `osm_<地域>` |
|---|---|---|
| 範囲 | **全世界**(約 1,200 万件) | 取り込んだ地域のみ |
| ダンプ | 約 400MB + 別名 191MB | 国単位で 2〜5GB(大陸単位は 32GB) |
| 持っているもの | 地名・座標・国/行政区・人口・多言語別名・timezone | 地名 + **店舗/施設/交通** の詳細、住所・電話・営業時間 |
| 持っていないもの | 店舗・レストラン・営業時間 | 取り込んでいない国のすべて |

大陸単位の OSM(`europe-latest.osm.pbf`)を 1 ソースとして取り込む案は**廃止しました**
(理由は [設計メモ](docs/design-notes.md#地理データの守備範囲geonames-と-osm-の分担))。
推奨構成は:

- **全世界の問い合わせ** → `geonames`(1 ソースで賄う)
- **店舗レベルの詳細が要る国** → その国だけ `osm_<国>` を取り込む(管理画面 `/admin/osm` から選ぶか
  `-e SOURCE=osm_<国>`)。多くの国は pbf 数百 MB〜2GB で、RAM 索引のまま 12GiB 予算・数時間で焼けます
  (フランス・ドイツ・カナダ・アメリカ・ロシアのような大きい国だけは既定がディスク索引になり、
  2GiB で焼ける代わりに数倍遅くなります)。
- **事物の解説** → `jawiki`(座標と wikidata の Q 番号を持つ)

`geonames` と `jawiki` はどちらも `extra.wikidata` に Q 番号を持つので、
`filter?wikidata=Q90` で相互に引き当てられます。

OSM 系ソース(`osm_japan` 等)は Geofabrik の地域抽出
`https://download.geofabrik.de/<region>-latest.osm.pbf` を取り込みます。取り込むのは
`name` タグを持つ「名前付き地物」だけです(地名・行政界・主要な自然地物 + 主要 POI +
交通インフラ。住所補間・逆ジオコーディングはできません。内部の走査方法は
[設計メモ](docs/design-notes.md#osm-は-pyosmium-で-3-パス読む))。

ファイルを 3 パス読むため、構築時間はダウンロード後さらに 2〜6 時間程度かかります。
ノード座標インデックスは既定で RAM 上(`sparse_mmap_array`)に置き、日本抽出で 5〜10GB
使います。メモリの少ない環境では `OSM_NODE_INDEX=sparse_file_array` でディスクへ逃がせます
(必要メモリは 2GiB まで下がる代わりに数倍〜10 倍遅くなります)。

中断しても運用 DB は壊れません(`.building` の一時ファイルに構築するため)。再実行すれば最初からやり直します。

#### メモリについて

ingest は**開始前にメモリを検査し、足りなければダウンロードもせず即座に中止**します
(なぜ `mem_limit` で締めないのかは [設計メモ](docs/design-notes.md#メモリ方針))。
取り込み中の巨大な対応表はディスク上の一時 SQLite に逃がしてあるので、常駐メモリは
コーパス規模によらず一定です。一時ファイルは終了・中断のいずれでも自動削除されます。

| ソース | 必要メモリの目安 | 内訳 |
|---|---|---|
| `jawiki` | 3 GiB | 巨大な対応表はディスクへ逃がしてあるので軽い。実測ピークは 1GiB 未満 |
| `osm_japan` | 12 GiB | ノード座標索引を RAM に持つため(実測 5〜10GB) |
| `osm_<他の国>` | 3〜12 GiB | pbf 1GB あたり RAM 索引 5GiB 見当。国ごとの目安は `/admin/osm` に出ます |
| `geonames` | 3 GiB | 別名(2,000 万行規模)はディスクへ逃がすため軽い |

RAM 索引が 12GiB に収まらない国(フランス・ドイツ・カナダ・アメリカ・ロシア)は、
**ディスク索引(`sparse_file_array`)が既定**です。必要メモリは 2 GiB に下がる代わりに
数倍遅くなります(`OSM_NODE_INDEX=sparse_mmap_array` を明示すれば RAM 索引に戻せます)。

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

# 4. 出来た世代ファイルを配信機へコピーし、<ソース名>.db として見えるようにして再起動
cp jawiki-20260701.db /path/to/chiezo/data/
ln -sfn jawiki-20260701.db /path/to/chiezo/data/jawiki.db
docker compose restart chiezo-api
```

**jawiki 以外を焼くときに書き換えるのは次の 3 か所だけです**(他はそのままで動きます):

| | jawiki | osm_japan の場合 |
|---|---|---|
| 3. の `SOURCE=` | `jawiki` | `osm_japan`(`osm_france` 等、国名を変えるだけ) |
| 4. のコピー元ファイル名 | `jawiki-<日付>.db` | `osm_japan-<日付>.db` |
| 4. の `ln -sfn` の 2 つめの引数 | `jawiki.db` | `osm_japan.db` |

世代ファイル名の日付は取り込み時に決まるので、`ls /path/to/chiezo-data/*.db` で実際の名前を
確認してからコピーしてください。ソースによって必要メモリが違う(上の「メモリについて」の表)点と、
`osm_<国>` では `-e OSM_NODE_INDEX=sparse_file_array` を足せばメモリ 2 GiB でも焼ける
(その代わり遅い)点にだけ注意してください。`SOURCE` に渡せる名前(`osm_<国>` 195 件 +
`<lang>wiki` 348 件 + geonames)はビルド機だけでも引けます:

```bash
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import sources; print('\n'.join(sources.ADAPTERS))" | grep france
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

`/mcp` は既定で Host ヘッダの検証(DNS リバインディング対策)を**無効**にしています。
MCP SDK の既定は「localhost 系の Host しか受け付けない」で、そのままだと LAN の別マシンから
繋いだ時点で 421 になり、このサービスの使い方が成立しないためです(REST 側も認証なし・
LAN 内前提なので方針は揃っています)。絞りたい場合は `chiezo-api` の環境変数
`CHIEZO_MCP_ALLOWED_HOSTS` に許可する Host をカンマ区切りで指定してください
(例: `192.168.0.3:9000,localhost:*`。末尾 `:*` でポート任意)。

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

なぜこの形なのか(土台に SQLite + FTS5 を選んだ理由、検索の並び順、索引の形、
geonames と osm の分担、メモリ方針など)は
**[docs/design-notes.md](docs/design-notes.md)** にまとめています。
実測して方針が変わったものは、数字と一緒にそちらへ残す方針です。

- [FTS トークナイザの評価](docs/fts-tokenizer-evaluation.md) — 形態素解析への差し替えを
  見送った経緯と実測値
- [ソースの追加手順](docs/adding-a-source.md) — 新しい種類のデータを足すとき

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
