# chiezo — ローカル知識サーバー

LAN 内の開発マシン(主に Claude Code)から使う、完全ローカルの知識検索 REST API です。
公式ダンプを SQLite (FTS5 trigram) に取り込み、外部 API のレート制限や負荷を気にせず参照できます。

- マルチソース設計: ソースごとに独立した SQLite ファイル 1 つ(`data/<source>.db`)
- v0.2 収録ソース:
  - `jawiki` — 日本語 Wikipedia(CirrusSearch ダンプ由来)
  - `osm_japan` — OpenStreetMap 日本抽出(Geofabrik 由来の地名辞典 + POI 辞典。
    地名・行政区・自然地物に加え、病院・学校・店舗・観光地等の主要 POI と座標)
- API: FastAPI + uvicorn(ポート 9000)、認証なし・LAN 内前提

## セットアップ

```bash
# 1. API を起動(DB が無い間もソース 0 件で起動する)
docker compose up -d
curl -s http://localhost:9000/healthz

# 2. jawiki を取り込む(ダンプ 5〜6GB DL + 構築 2〜6 時間、ディスク空き 80GB 以上推奨)
docker compose --profile ingest run --rm chiezo-ingest

# 2'. osm_japan を取り込む(ダンプ 2〜3GB DL [.osm.pbf] + 構築 1〜4 時間、
#     POI を含むため DB は数 GB 規模)
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 3. 新しい DB を読み込ませる
docker compose restart chiezo-api
```

## API の使い方

```bash
BASE=http://<サーバーIP>:9000

curl -s "$BASE/v1/sources"                                        # ソース一覧
curl -s "$BASE/v1/jawiki/search?q=浅草寺&limit=5"                  # 全文検索
curl -s "$BASE/v1/jawiki/doc?title=浅草寺&fields=title,opening,tags" # 文書概要
curl -s "$BASE/v1/jawiki/doc?title=浅草寺&max_chars=8000"           # 文書全文(切り詰め)
curl -s "$BASE/v1/jawiki/doc?title=浅草寺&fields=title,extra"       # ページビュー等の付加情報
curl -s "$BASE/v1/jawiki/titles?prefix=浅草"                        # タイトル前方一致
curl -s "$BASE/v1/jawiki/links?title=浅草寺"                        # リンク先一覧
curl -s "$BASE/v1/jawiki/random?limit=3"                           # ランダム文書

curl -s "$BASE/v1/osm_japan/search?q=富士山&limit=5"                # 地名・POI検索(同一エンドポイント)
curl -s "$BASE/v1/osm_japan/doc?title=京都市&fields=title,extra"    # 座標・OSMタグ等
```

ブラウザで `http://<サーバーIP>:9000/admin` を開くと、登録済みソース(文書数・dump_date・構築日時など)
を一覧できる簡易管理画面が見られます。

主な仕様:

- `search` — `limit` 既定 10・最大 50。3 文字以上の語が無いクエリは自動的にタイトル前方一致へ
  フォールバックし、レスポンスの `"mode"` が `"title_prefix"` になります(通常は `"fts"`)。
- `doc` — `title` 完全一致 → リダイレクト(alias)解決 → 見つからなければ 404 と近似候補 5 件。
  `fields`(既定 `title,opening,body,tags,updated_at`)と `max_chars` で応答サイズを制御できます。
- `extra` フィールド(jawiki) — ページビューを突合できた記事には
  `{"pageviews_month": <月間閲覧数>, "pageviews_period": "YYYY-MM"}` が入ります(Wikimedia の
  `pageview_complete` 月次ダンプ由来、bot 除外・全アクセス種別合算)。突合できなかった記事は `null`。
- `extra` フィールド(osm_japan) — `{"osm_type": "node|way|relation", "osm_id": ..., "lat": ...,
  "lon": ..., "feature": "place=city", "tags": {<OSM 生タグ>}, ...}`。way / relation の座標は
  構成ノードの平均(近似重心)で、行政境界は admin_centre / label ノードを優先します。
  同名地物はタイトルを「名前 (node:123)」形式で弁別し、元の名前は alias として引けます。
  POI(`feature` が `amenity=*` / `shop=*` / `tourism=*` / `leisure=*` / `historic=*` /
  `craft=*` / `office=*` / `healthcare=*`)では、住所(`addr:*` 由来)が取れれば `address`、
  電話・サイト・営業時間が取れれば `phone` / `website` / `opening_hours` も入ります。
  地名(place/boundary/natural)と POI は同一ソース内に混在し、`search` は両方をヒットさせます。
- 全クエリ 5 秒タイムアウト(超過は 504)。エラーは `{"error": "..."}` 形式。

### Claude Code から使う(設定ファイル自動生成)

各アプリの環境で動く Claude に「chiezo に載っている知識が必要なら chiezo を使う」よう
促す CLAUDE.md ブロックを、稼働中の chiezo に問い合わせて自動生成できます。登録済み
(初期化済み)ソースだけを、実データの文書数・実在タイトルを使った具体例つきで列挙します。
`curl` だけで動き追加インストールは不要(jq も Python も要りません)。
**既定の書き込み先は `~/.claude/CLAUDE.md`**(全プロジェクトの Claude に効く推奨の使い方)。

```bash
# ~/.claude/CLAUDE.md を更新・localhost:9000 を参照
/path/to/chiezo/scripts/gen_claude_config.sh

# chiezo が LAN 上の別ホストにある場合は場所を指定(環境変数 CHIEZO_URL でも可)
scripts/gen_claude_config.sh --base-url http://192.168.1.20:9000

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
`--print`、`--with-permissions`(対象の `.claude/settings.local.json` に chiezo への curl 許可を
追記し権限プロンプトを減らす)、`--offline --sources jawiki,osm_japan`(chiezo 未起動でも雛形を生成)。
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

Wikipedia 系ソースは記事本文のダンプ(CirrusSearch)に加えて、Wikimedia の月次ページビューダンプ
(`other/pageview_complete/monthly/`、全プロジェクト合算で圧縮 5〜6GB)もダウンロードし、
`page_id` で突合して `docs.extra` に月間閲覧数を格納します。`WIKI_DOMAIN`
(`ingest/sources/wikipedia.py`)に対応表が無い wiki_id ではこの突合をスキップします。
この分、初回取り込みのダウンロード量・所要時間は README 冒頭の見積もりよりやや増えます。

OSM 系ソース(`osm_japan` 等)は Geofabrik の地域抽出
`https://download.geofabrik.de/<region>-latest.osm.pbf` をダウンロードし、pyosmium
(libosmium バインディング)でストリーミング解析します(Geofabrik が 2026 年に `.osm.bz2` の
配布を終了し `.osm.pbf` のみになったため、標準ライブラリの `xml.etree` では読めなくなった。
osm 系ソースに限り pyosmium への依存を許容している)。取り込むのは「名前付き地物」です:
`place=*`、`boundary=administrative`、主要な `natural=*`(山頂・湖沼・島・湾など)に加えて、
主要 POI(`amenity=*` `shop=*` `tourism=*` `leisure=*` `historic=*` `craft=*` `office=*`
`healthcare=*`。いずれも `name` タグ必須)。住所補間・逆ジオコーディングはできません
(それが必要な場合は公式の [nominatim-docker](https://github.com/mediagis/nominatim-docker)
を別途立ててください)。ファイルは relation メンバーの把握(パス1)→ ノード座標解決込みの
node/way/relation 走査(パス2)の 2 パスで読むため、構築時間はダウンロード後さらに
1〜4 時間程度かかります。

中断しても運用 DB は壊れません(`.building` の一時ファイルに構築するため)。再実行すれば最初からやり直します。

### ソースの追加・削除

`data/` に `<source>.db` を置いて(または消して)`docker compose restart chiezo-api` するだけです。
新しい種類のソースの取り込み方は [docs/adding-a-source.md](docs/adding-a-source.md) を参照してください。

### セキュリティ

認証はありません。LAN 内利用が前提です。ルーターでポート 9000 を外部に開放しないでください。
必要ならホストの LAN インターフェースのみに bind するよう compose の `ports` を
`"192.168.x.x:9000:8000"` の形式に変更してください。

## 開発

```bash
python -m venv .venv && .venv/bin/pip install fastapi 'uvicorn[standard]' httpx pytest
.venv/bin/python -m pytest tests/ -v
```

テストは同梱の小型フィクスチャ(`tests/fixtures/mini_jawiki.json.gz` 12 文書、
`tests/fixtures/mini_osm.osm.pbf` 12 ノード + 2 way + 2 relation)から実際に
DB を構築して全エンドポイントを検証します。ネットワーク・実データは不要です。

## 設計メモ

- SQLite + FTS5 (trigram) 採用。読み取り専用・少数クライアントなら数 ms〜数十 ms で十分。
  「ソース = 1 ファイル」が世代管理・ブルーグリーンとよく噛み合います。
- 割り切り: 3 文字未満の語は FTS 不可(前方一致へ自動フォールバック)、ランキングは簡易(bm25 + 人気度)。
- 移行トリガー: 検索精度に不満 → Meilisearch / 同時接続・書き込み要件 → PostgreSQL + PGroonga。
  API 層があるため DB だけ差し替え可能です。
