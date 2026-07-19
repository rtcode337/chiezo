# chiezo — ローカル知識サーバー

LAN 内で動く読み取り専用の知識検索 REST API。複数のデータソース(日本語 Wikipedia = `jawiki`、
OpenStreetMap 日本抽出 = `osm_japan`)をソースごとに独立した SQLite ファイル
(`/data/<source>.db`)として収容する。設計書は v0.2。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — ルーティング(/healthz, /v1/sources, /v1/{source}/search|doc|titles|links|random, /admin)
  - `app/registry.py` — /data 走査・ソース登録
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
  - `/admin` — 登録ソース(name/kind/lang/文書数/dump_date/built_at/schema_version)を一覧表示する簡易 HTML 管理画面
- `ingest/` — **chiezo-ingest**: ワンショット構築バッチ。
  - `main.py` — 共通フレーム: 取得 → `.building` へ構築 → FTS → 検証 → ブルーグリーン切り替え(シンボリックリンク差し替え、旧世代 1 つ保持)。
    アダプタが `fetch_pageviews` を持つ場合、`fetch()` の後にそれも呼ぶ(docs.extra 補強用の追加データ取得フック)
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)
  - `sources/wikipedia.py` — CirrusSearch ダンプアダプタ(`wiki_id` パラメータ化、enwiki 流用可)。
    `other/cirrus_search_index/<date>/index_name=<wiki_id>_content/` 配下の複数 `.json.bz2` シャードを取得する
    (旧 `other/cirrussearch/current/` は 2026 年に廃止)。あわせて `other/pageview_complete/monthly/` の
    月次ページビュー(bot 除外・全プロジェクト合算、圧縮 5〜6GB)を `page_id` で突合し、
    `docs.extra` に `{"pageviews_month": ..., "pageviews_period": "YYYY-MM"}` として格納する
    (`WIKI_DOMAIN` 未登録の wiki_id では突合をスキップ)。
  - `sources/osm.py` — OpenStreetMap アダプタ(`region` パラメータ化、Geofabrik の
    `<region>-latest.osm.pbf` を pyosmium(libosmium バインディング)で解析)。
    Geofabrik が 2026 年に `.osm.bz2` 配布を終了し `.osm.pbf` のみになったため、標準ライブラリの
    `xml.etree` では読めなくなった。osm 系ソースに限り pyosmium への依存を許容している
    (他ソースは標準ライブラリのみの方針を維持)。
    名前付き地物(`place=*` / `boundary=administrative` / 主要 `natural=*`)を地名辞典として、
    加えて主要 POI(`amenity=*` / `shop=*` / `tourism=*` / `leisure=*` / `historic=*` /
    `craft=*` / `office=*` / `healthcare=*`。`name` タグ必須)を POI 辞典として同一ソースに
    取り込む(地名と POI は同じ docs/docs_fts に混在し、`search` は両方をヒットさせる)。
    OSM は node → way → relation 順で並ぶため 2 パスで読む: パス1
    (`_RelationScanHandler`)で対象 relation が参照する way ID を集め、パス2
    (`_MainHandler`)で pyosmium の `NodeLocationsForWays` によるノード座標自動解決を
    使いながら node/way/relation を走査して Doc を生成する。relation の label /
    admin_centre ノード座標は位置インデックス (`idx.get(node_id)`) に直接問い合わせて解決し、
    それ以外は構成要素の平均(近似重心)。pyosmium はコールバック駆動のため、別スレッドで
    `osmium.apply()` を回し `queue.Queue` 経由で Doc をジェネレータへ橋渡しする(メモリ抑制)。
    docs.title の UNIQUE 制約に合わせ、同名地物は先勝ちで「名前 (node:123)」形式に弁別し
    元の名前を alias に残す。POI は `addr:*` タグから `docs.extra.address`、
    `phone` / `website` / `opening_hours` 系タグから同名の extra フィールドも拾う。
    ソース名の区切りはアンダースコア
    (`osm_japan`。ハイフンは世代ファイル名 `<source>-<date>.db` と衝突するため不可)
  - `sources/__init__.py` — アダプタレジストリ(新ソースはここに 1 行追加)
- `tests/` — フィクスチャ(`fixtures/mini_jawiki.json.gz` 12 文書、`fixtures/mini_osm.osm.pbf`)
  での API / ingest テスト

## コマンド

```bash
# テスト(fastapi, httpx, pytest が必要)
python -m pytest tests/ -v

# フィクスチャ再生成
python tests/fixtures/make_fixture.py
python tests/fixtures/make_osm_fixture.py

# API 起動(Docker)
docker compose up -d

# 取り込み(本番: jawiki は 5〜6GB ダウンロード、構築 2〜6 時間、ディスク空き 80GB 推奨)
docker compose --profile ingest run --rm chiezo-ingest

# OSM 日本抽出の取り込み(.osm.pbf 2〜3GB ダウンロード、POI 込みで構築 1〜4 時間)
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 取り込み後の反映
docker compose restart chiezo-api
```

ingest の主な環境変数: `SOURCE`(必須)、`DUMP_DATE`(日付固定)、`DUMP_FILE`(ダウンロードスキップ)、
`MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータ上書き。小規模データでの動作確認用)、
`PAGEVIEW_PERIOD`(ページビュー突合対象の年月 `YYYY-MM` を固定。省略時は最新月を自動検出)。
OSM 系ソースでは Geofabrik が latest 1 世代のみ配布のため、`DUMP_DATE` は取得対象の固定ではなく
世代ファイル名ラベルの上書きとしてのみ機能する。

## 実装上の約束事

- コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。ソース固有情報は `docs.extra`(JSON)へ。
  変更は最終手段で、`schema_version` を上げ api 側で複数バージョン対応する。
- ソース間で JOIN しない。API はソース種別を意識せず docs/aliases/docs_fts のみ参照する。
- FTS5 は trigram。ユーザー入力は必ずフレーズエスケープしてから MATCH に渡す(`app/fts.py` 経由)。
- 運用 DB は読み取り専用(`immutable=1`)。更新はブルーグリーン(別ファイル構築 → シンボリックリンク差し替え → api 再起動)のみ。
- エラーレスポンスは `{"error": "..."}` 形式。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。
- コード(api/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(セットアップ・API 仕様・運用手順)と本ファイル(CLAUDE.md、アーキテクチャ記述)も
  あわせて更新すること。ドキュメントだけを別コミット・別対応に先送りしない。
