# API リファレンス

Chiezo から知識を**取り出す**口の詳細仕様です。経路は 3 つあり、どれも中身は同じ関数を呼びます。

- [REST](#rest) — `curl` と人間向け HTML 画面
- [MCP](#mcp-から使うmcp) — Streamable HTTP。chiezo-api 自身が MCP サーバー
- [Claude Code 連携](#claude-code-から使う設定ファイル自動生成) — CLAUDE.md ブロック・権限・MCP 登録の自動生成

なぜこの形なのかは [設計メモ](design-notes.md) が正です。

## REST

```bash
BASE=http://<サーバーIP>:7010

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

### エンドポイントごとの仕様

- `search` — `limit` 既定 10・最大 50。3 文字以上の語が無いクエリは自動的にタイトル前方一致へ
  フォールバックし、レスポンスの `"mode"` が `"title_prefix"` になります(通常は `"fts"`)。
  並び順は 2 段で、**タイトルが検索語と完全一致する文書**が先、残りが
  **関連度(bm25)× 知名度(`rank_score`)の順**です(理由は
  [設計メモ](design-notes.md#検索の並び順))。`rank_score` が入っていない古い DB では
  2 段目が bm25 だけになります([既存 DB の rank_score を入れ直す](operations.md#既存-db-の-rank_score-を入れ直す))。
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
  [その場で移行できます](operations.md#既存-db-にタグ索引を足すschema_version-2--3--4)
  (どの版で何が速くなるかは [設計メモ](design-notes.md#読む量を該当件数に比例させる))。
- `tags` — タグ名を文書数つきで列挙します(`prefix` = 前方一致 / `contains` = 部分一致 /
  無指定 = 文書数の多い順)。`filter?tag=` はタグ名の**完全一致**なので、Wikipedia の
  カテゴリのように表記の揺れがあるものは、まずここで実在する名前を確かめてから引くのが
  確実です(`schema_version` 3 以降。4 以降は集計表を引くので部分一致も速い)。
- `titles` / `links` / `random` — タイトル前方一致 / その文書から出ているリンク先の一覧 /
  ランダム抽出。`links` は**出リンクのみ**で、被リンク(その文書を指している文書)は取れません。
- 全クエリ 5 秒タイムアウト(超過は 504)。エラーは `{"error": "..."}` 形式。
  **504 は「該当 0 件」ではなく「取れなかった」を意味します**。`filter` でページングしながら
  全件集める用途では、空の結果として扱うとそのまとまりを丸ごと取りこぼすので、
  `limit` を小さくして取り直してください。

> **カテゴリの全記事を列挙したいときは、本文の全文検索(`search?q=Category:ラーメン店`)ではなく
> `filter?tag=` を使ってください。** 全文検索だとソートキー付きのカテゴリを静かに
> 取りこぼします(実データで 115 件中 16 件が漏れていました。
> [設計メモ](design-notes.md#カテゴリは本文検索で列挙してはいけない))。

### `extra` フィールドの中身

ソース固有の情報はすべて `docs.extra`(JSON)に入ります。コアスキーマは全ソース共通なので、
API 側はソース種別を意識しません。

**jawiki(Wikipedia 系)**

- 座標を持つ記事(`{{Coord}}` テンプレートや、駅・空港・施設の Infobox の `緯度度`/`経度度` 系、
  会社の Infobox(`基礎情報 会社`)の `本社緯度度`/`本店緯度度` 系の引数から抽出)には
  `{"lat": ..., "lon": ...}` が入ります。
- ページビューを突合できた記事には
  `{"pageviews_month": <月間閲覧数>, "pageviews_period": "YYYY-MM"}` が入ります(Wikimedia の
  `pageview_complete` 月次ダンプ由来、bot 除外・全アクセス種別合算)。突合できなかった記事は `null`。
- `page_props` ダンプから wikidata の Q 番号が取れた記事には `{"wikidata": "Q17221"}` も入り、
  `filter?wikidata=` で逆引きできます(OSM 側の `wikidata` タグと突き合わせられます)。

**osm_japan(OSM 系)**

`{"osm_type": "node|way|relation", "osm_id": ..., "lat": ..., "lon": ...,
"feature": "place=city", "tags": {<OSM 生タグ>}, ...}`。

- way / relation の座標は構成ノードの平均(近似重心)で、行政境界は admin_centre / label
  ノードを優先します。
- 同名地物はタイトルを「名前 (node:123)」形式で弁別し、元の名前は alias として引けます。
- POI(`amenity` / `shop` / `tourism` / `leisure` / `historic` / `craft` / `office` /
  `healthcare`)では、住所・電話・サイト・営業時間が取れれば `address` / `phone` /
  `website` / `opening_hours` も入ります。
- 地名・POI・交通インフラ(駅・空港・港・IC/SA 等)は同一ソース内に混在し、`search` は
  すべてをヒットさせます(駅が同名の店舗より上に来るよう `rank_score` を高くしてあります)。
- 座標を持つ地物には所属行政区が `area` として付きます(日本では都道府県。粒度は
  `OSM_AREA_ADMIN_LEVEL` で変更可)。

**geonames**

`{"lat": ..., "lon": ..., "feature": "P=PPLC", "country_code": "FR"}` が常に入り、
取れたものだけ `area`(1 次行政区。無ければ国名)・`country`・`population`・`timezone`・
`elevation`・`wikidata` が加わります。`extra.feature` は OSM と同じ `<class>=<code>` 形式に
揃えてあるので、`filter?feature=` の使い方は共通です。GeoNames は本文を持たないため、
`opening` / `body` には「名前(行政区, 国)— コード / 分類、人口 N」の 1 行を組み立てて
入れています(FTS を効かせるため)。

### notes(唯一書き込めるソース)の REST

```bash
curl -s "$BASE/v1/notes" -H 'Content-Type: application/json' \
  -d '{"text":"開発環境を WSL2 へ移行する","tags":"環境,決定"}'

curl -s "$BASE/v1/notes/recall"                                  # 新しい順に 20 件
curl -sG "$BASE/v1/notes/recall" --data-urlencode "q=移行"        # 全文検索
curl -sG "$BASE/v1/notes/recall" -d since=2026-07-01             # 期間で絞る
curl -sG "$BASE/v1/notes/recall" --data-urlencode "tag=決定"      # タグで絞る
curl -sG "$BASE/v1/notes/recall" -d fields=title,updated_at      # 当たりを付ける(本文を載せない)
curl -sG "$BASE/v1/notes/recall" -d max_chars=0                  # 本文を切らずに返す
curl -s -X PATCH "$BASE/v1/notes/3" -H 'Content-Type: application/json' \
  -d '{"tags":"環境,決定,完了"}'                                  # 書き換え(渡した項目だけ)
curl -s -X DELETE "$BASE/v1/notes/3"                             # 取り消し
```

書き換え(PATCH)は**渡した項目だけ**を差し替えます(`text` / `title` / `tags`)。
`tags` はカンマ区切りの丸ごと置き換えで、空文字を渡すと全部外れます。
`updated_at` が現在時刻になるので、書き換えたメモは `recall` の先頭に浮きます。

タグには**定番の語彙**があります(`todo` = いつかやるが今ではない作業、`決定`、`runbook`、
`環境`、`本番`、`設計メモ`、`トラブルシュート`。プロジェクトはリポジトリ名を小文字で)。
語彙は `api/app/notes.py` の `CANONICAL_TAGS` が 1 か所で持ち、MCP の `remember` の
ツール定義として配られます —— 書き手が変わっても同じ意味に同じ表記が付くようにするためで、
ここに無いタグも自由に付けられます。curl で書くときもこの表記に合わせてください。

**本文は既定で先頭 400 文字までしか返りません**(`max_chars`)。切れたメモには
`truncated: true` が付くので、全文が要るものだけ `/v1/notes/doc/{doc_id}` で取り直します。
当たった件数ぶんの全文が会話のコンテキストに載るのを避けるためで、他ソースの
`search`(冒頭だけ)→ `doc`(全文)と同じ二段構えです。`fields` で項目を選べば
本文そのものを外せます(`doc_id` / `title` / `text` / `tags` / `updated_at` / `url`)。

専用の口は追記・書き換え・削除・時系列の想起だけです。読み出しはコアスキーマなので
`/v1/notes/search`・`doc`・`filter`・`tags` と `/search/notes/`(ブラウズ画面)もそのまま効きます。

| 変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_NOTES_DIR` | `/notes`(compose) | 書き込み可能なディレクトリ。**空にすると機能ごと無効**(`/v1/notes` は 503、MCP の道具も出ない) |

compose では既定で有効で、`./notes` に SQLite が 1 つできます(初回アクセス時に自動生成
されるので、取り込みを回す必要はありません)。`/data` は読み取り専用マウントのままです。
notes を別ディレクトリに置いているのは、`/data` の変化を監視して全ソースを再走査する
仕組みと干渉させないためで、その理由となぜ Chiezo に置くのかは
[設計メモ](design-notes.md#覚えるnotesはなぜ-chiezo-に置くのか)にあります。

**認証はありません。** `/v1/notes` に到達できる相手は誰でも書けます(LAN 内前提という
このサービス全体の方針と同じですが、書き込みができる唯一の口である点は留意してください)。

## 人間向けの画面

### 管理画面(`/admin`)

ブラウザで `http://<サーバーIP>:7010/`(`/admin` へ自動リダイレクト)を開くと、登録済みソース
(文書数・dump_date・構築日時・スキーマバージョンなど)の一覧に加えて、未初期化ソース
(`chiezo-trigger` 側の既知ソース一覧に載っているが `/data` にまだ `.db` が無いもの)向けの
「初期化」ボタンが見られます。一覧には最新のスキーマバージョン(いま取り込みを実行すると
焼かれる版。`chiezo-trigger` から取得)も表示され、それより古い DB の行には注意書きが付きます。

登録済みソースには「再構築」ボタンがあり、ダンプの取得から取り込みをやり直せます
(古いスキーマの DB を最新にする、ダンプの新しい版を取り込み直す、が主な用途。
ブルーグリーンなので構築中も現行 DB での配信は続きます)。
初期化・再構築のボタンを押すと [`chiezo-trigger`](operations.md#chiezo-trigger管理画面からの初期化再構築)に
ジョブが積まれ、進行状況(ログ tail 込み)が管理画面に表示されます(実行中は自動でリロードされます)。
ジョブが完了すると、chiezo-api が `data/` の変化(シンボリックリンクの差し替え)を数秒以内に
検知して自動で新しい DB に切り替わります(再起動は不要。検知間隔は
`CHIEZO_RESCAN_INTERVAL` 秒、既定 5。0 以下で無効化でき、その場合は従来どおり再起動で反映)。

「AI の相手」の下には**使用量**の節(`/admin#ai-usage`)があり、相手ごとに
**枠の残り**(聞ける相手だけ)と**Chiezo が使ったぶん**(全部の相手)が並びます。
枠は開いたときには聞きに行かず、控えてある値と取得時刻を出します(取り直しは行のボタン)。
同じ内容は `GET /v1/ai/usage` でも取れます —— 詳しくは
[AI と話す](ai.md#使用量を見る枠の残りとchiezo-が使ったぶん)。

画面の末尾には**いま動いているビルド**(ビルド日時(JST)とビルド元のコミット)が出ます。
`docker compose pull && docker compose up -d` のあとにここが新しくなっていなければ、
古いイメージのままです([動いているビルドを確かめる](operations.md#動いているビルドを確かめる))。

OSM の国別ソース(`osm_<国>`、195 件)と Wikipedia の言語版(`<lang>wiki`、348 件)は、
そのまま並べると他のソースが埋もれるため、一覧ではそれぞれ `osm` / `wikipedia` の 1 行に
まとめてあります。`osm` 行の「国を選ぶ」から国選択の画面(`/admin/osm`)が開き、
大陸ごとに畳まれた一覧から国を選んで初期化できます。各国の pbf サイズと必要メモリの目安、
構築済みかどうかもそこに出ます(国名・`region` での絞り込み可)。同様に `wikipedia` 行の
「言語を選ぶ」から言語選択の画面(`/admin/wikipedia`)が開き、記事数の階層ごとに畳まれた
一覧から言語を選んで初期化できます(言語名・コードでの絞り込み可)。

### ブラウズ画面(`/search/{source}/`)

各ソース名は `/search/{source}/` にリンクしています。トップは全件一覧(doc_id 昇順。
notes のような小さなソースを頭から確かめる導線)で、検索するとその結果一覧に変わります。
一覧はどの経路(未検索・検索・タグ絞り込み)も同じ表(doc_id / title / tags / snippet)で、
1 ページ 100 件でページングします(`?page=2`)。`/search/{source}/doc/{doc_id}` で文書詳細
(本文・tags・links・extra)をブラウザで閲覧できます(`/v1/...` の JSON API を人間向け HTML で
薄くラップしたものです)。

画面はすべて前置き(`/admin`・`/search/`・`/ai/`)の下に置いてあります。ルート直下を
ソース名に使っていた頃は、`ask` や `admin` という名前のソースを足せませんでした。

## MCP から使う(`/mcp`)

REST と同じ機能を MCP(Model Context Protocol)のツールとしても提供しています。
**chiezo-api 自身が MCP サーバー**なので、クライアント側に何もインストールせずに繋がります。

```bash
claude mcp add --transport http chiezo http://<サーバーIP>:7010/mcp
# 次節の設定生成スクリプトを使うなら、この登録も既定で行われます(手動登録は不要)
scripts/gen_claude_config.sh -u http://<サーバーIP>:7010
```

公開しているツールは `sources` / `search` / `doc` / `filter` / `tags` / `titles` / `links` の
7 つ(notes が有効なら `remember` / `recall` / `update` / `forget` を加えて 11)で、引数は REST のクエリ
パラメータと同じです。実体も REST のエンドポイント関数そのものなので、
挙動が二重管理になることはありません。REST と違うのは `doc` / `filter` の `max_chars` が
既定で 4000 字に切られる点だけです(MCP の応答はそのままモデルのコンテキストに載るため。
全文が要るときは `max_chars` を明示的に上げてください)。

トランスポートは Streamable HTTP(ステートレス)です。Streamable HTTP に未対応の
クライアントからは `mcp-remote` 経由で繋いでください。

`/mcp` は既定で Host ヘッダの検証(DNS リバインディング対策)を**無効**にしています。
絞りたい場合の設定は [運用ドキュメントのセキュリティ節](operations.md#セキュリティ)にあります。

Claude Code では**従来の curl + CLAUDE.md 方式も引き続き使えます**(次節)。大量取得では
curl の方がトークン効率が良い場面があるため、どちらかに寄せる必要はありません。

## Claude Code から使う(設定ファイル自動生成)

各アプリの環境で動く Claude に「Chiezo に載っている知識が必要なら Chiezo を使う」よう
促す CLAUDE.md ブロックを、稼働中の Chiezo の設定生成 API
(`GET /admin/claude-config.txt`)に問い合わせて自動生成できます。登録済み
(初期化済み)ソースだけを、実在タイトルを使った具体例つきで列挙します。ただし
**notes(手元で書くメモ)の中身は引き写しません** —— 機密が混じりうるうえ、ブロックは
`--project` でリポジトリ側にも生成できるため、プレースホルダーだけで例示します。
文書数は載せません(取り込みや書き込みのたびに変わるので、正確な値はブロック自身が
案内している `/v1/sources` で引きます)。
ブロック内の curl 例のベース URL は、Chiezo 側が「スクリプトがアクセスしてきた URL の
プロトコル・ホスト名・ポート」から導出するため、`--base-url` に指定した到達可能な URL が
そのまま生成物に載ります(リバースプロキシ越し・非標準ポートでも可)。
`curl` + POSIX シェルだけで動きます(既存の JSON 設定ファイルへマージする場面でのみ
jq か python3 のどちらかを使います。詳細は後述)。

**既定の書き込み先は `~/.claude/CLAUDE.md`**(全プロジェクトの Claude に効く推奨の使い方)。
あわせて既定で、書き込み先に対応する Claude Code 設定(`--user` なら
`~/.claude/settings.json`、`--project`/`--target` なら `.claude/settings.local.json`)に
Chiezo への `curl` を許可するルールを追記するため、生成後は Chiezo への `curl` が
毎回の許可プロンプトなしに実行できます(`--no-permissions` で無効化可)。
さらに既定で、Chiezo を **MCP サーバーとしても登録**します(後述)。

```bash
# ~/.claude/CLAUDE.md を更新・localhost:7010 を参照
/path/to/chiezo/scripts/gen_claude_config.sh

# Chiezo が LAN 上の別ホストにある場合は場所を指定(環境変数 CHIEZO_URL でも可)
scripts/gen_claude_config.sh --base-url http://<サーバーIP>:7010

scripts/gen_claude_config.sh --project     # ~/.claude ではなく ./CLAUDE.md にする
scripts/gen_claude_config.sh --print       # 書き込まず内容だけ確認
```

既存 CLAUDE.md との共存:

- 既定(`--merge markers`)は `<!-- BEGIN chiezo -->`〜`<!-- END chiezo -->` で囲んだ
  ブロックだけを冪等に差し替えます。既存の記述は壊さず、再実行でソース一覧が最新化されます。
- 既存内容との統合に人間的な判断が要る場合は `--merge headless` で Claude Code の
  ヘッドレスモード(`claude -p`)にマージを任せられます(`claude` CLI が必要)。

主なオプション: `--base-url/-u`(Chiezo の場所)、`--user`(既定・`~/.claude/CLAUDE.md`)、
`--project`(`./CLAUDE.md`)、`--target/-o`(書き込み先をパス指定)、`--merge {markers,headless}`、
`--print`、`--no-permissions`(既定で行う上記の権限追記を無効化)、
`--with-hook`(下記の自動許可フックを設置。既定では設置しない)、
`--no-mcp`(既定で行う下記の MCP 登録を無効化)。
生成は Chiezo 本体が行うため、稼働中の Chiezo が必要です(旧 `--offline --sources` は廃止)。
生成される文面の要点は「まず `search` で当たりを付け、必要な文書だけ `doc` を取る(コンテキスト節約)」です。

### MCP サーバーの登録(既定・`--no-mcp` で無効化)

Chiezo は [MCP サーバーでもある](#mcp-から使うmcp)ため、生成時に Claude Code へ登録も行います。
書き込み先は `--user` ならユーザースコープ(`~/.claude.json`)、`--project`/`--target` なら
対象ディレクトリの `.mcp.json` です。`claude` CLI があればどちらも
`claude mcp add --scope {user,project}` に任せ、無ければ設定ファイルの `mcpServers` へ
直接マージします。どちらも再実行で重複しません。あわせて CLAUDE.md
ブロックに「**単発の参照は MCP ツール・大量取得は `curl`**」の使い分けが書かれます
(MCP の応答は必ずモデルのコンテキストを通るため、ページングや突合はファイルに落とせる
`curl` の方が向くという理由です)。反映には Claude Code の再起動(新しいセッション)が必要です。

### 必要なもの(jq / python3 / `claude` CLI)

`curl` 以外は、**既存の JSON 設定ファイルへマージするときだけ**必要です
(書き込み先がまだ無ければ API の応答をそのまま置くので何も要りません)。

| 場面 | 必要なもの |
|---|---|
| CLAUDE.md ブロックの生成 | `curl` のみ |
| 既存 `settings.json` への権限追記 | jq か python3(どちらでも同じ結果。jq を優先) |
| MCP の登録 | `claude` CLI。無ければ jq か python3 |
| `--with-hook` | python3(フック本体が Python スクリプトなので実行に必須) |

権限と MCP は**既定で入れる設定なので、入れられない環境では黙って飛ばさずエラーで停止します**
(「設定が入ったつもり」で使い始めるほうが困るため)。意図的に外すときは
`--no-permissions` / `--no-mcp` を明示してください。`--print` は何も書き込まないので
この検査を行いません。

### 大量取得でプロンプトが出てしまう場合(`--with-hook`)

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

フックが自動許可するのは「**Chiezo だけを読む、読み取り専用のコマンド**」だけです
(登場する URL が全て Chiezo / 実行されるコマンドが `curl`・`jq` 等の許可リスト内 /
`$(...)`・`eval` 等でコマンド位置を隠していない / ディスクへ書かない)。条件を外れたときは
何も出力せず通常の許可フローに戻る、判定に迷ったら黙る設計です。

ただしこれは **Claude が打つ Bash を毎回検査して自動承認しうる**仕掛けで、
権限ルールより影響範囲が広くなります。中身を読んで納得してから入れられるよう
**既定では設置せず**、`--with-hook` を明示したときだけ設置します。
判定ロジックの全文は設置前に確認できます:

```bash
curl -s http://<サーバーIP>:7010/admin/claude-config.hook.py   # フック本体
# 管理画面 /admin/claude-config でもプレビューできます
```

設置には `python3` が必要です(フック本体が Python スクリプトなので実行に必須。
`settings` のマージにも流用します)。
何度実行しても `hooks.PreToolUse` は重複せず、設置先を変えた場合も古いエントリは掃除されます。
反映には Claude Code の再起動(または一度 `/hooks` を開く)が必要な場合があります。
