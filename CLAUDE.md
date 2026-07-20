# chiezo — ローカル知識サーバー

LAN 内で動く読み取り専用の知識検索 REST API。複数のデータソース(日本語 Wikipedia = `jawiki`、
OpenStreetMap 日本抽出 = `osm_japan`)をソースごとに独立した SQLite ファイル
(`/data/<source>.db`)として収容する。設計書は v0.2。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — ルーティング(/, /healthz, /v1/sources, /v1/{source}/search|doc|titles|links|random,
    /admin, /admin/init/{source}, /{source}/, /{source}/doc/{doc_id})
  - `app/registry.py` — /data 走査・ソース登録
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
  - `app/known_sources.py` — 管理画面の初期化ボタンに出す既知ソース一覧(`ingest/sources/__init__.py`
    の `ADAPTERS` と手動同期。chiezo-api は ingest のコードを import しないため名前・kind・lang だけ複製する)
  - `app/pages.py` — 管理画面・ブラウズ画面共通の HTML 組み立てヘルパー(`page_shell`, `esc`)
  - `/`(GET) — `/admin` へ 302 リダイレクト
  - `/admin`(GET) — 登録ソース(name/kind/lang/文書数/dump_date/built_at/schema_version)の一覧、
    未初期化ソース(`known_sources.KNOWN_SOURCES` にあるが未登録)向けの初期化ボタン、
    `chiezo-trigger` のジョブ状況(state/source/log tail)を表示する簡易 HTML 管理画面
    (実行中は 5 秒ごとに自動リロード)
  - `/admin/init/{source}`(POST) — `chiezo-trigger` の `POST /run/{source}` へプロキシし、
    `/admin` へ 303 リダイレクト。`CHIEZO_TRIGGER_URL` 未設定なら 503、未知ソースなら 404、
    登録済みソースなら 409
  - `/{source}/`(GET) — 検索フォーム(HTML)。`?q=` 未指定時は一覧を出さずフォームのみ表示
    (jawiki 等の大規模ソースで rank_score 順の全件一覧がフルスキャンとなりタイムアウトするため)。
    `?q=` 指定時は結果一覧を表示し、`/v1/{source}/search` と同じロジック
    (FTS または短語のタイトル前方一致フォールバック)
  - `/{source}/doc/{doc_id}`(GET) — 文書詳細(title/tags/opening/body/links/extra)の HTML 表示
- `ingest/` — **chiezo-ingest**: ワンショット構築バッチ。
  - `main.py` — 共通フレーム: 取得 → `.building` へ構築 → FTS → 検証 → ブルーグリーン切り替え(シンボリックリンク差し替え、旧世代 1 つ保持)。
    アダプタが `fetch_pageviews` を持つ場合、`fetch()` の後にそれも呼ぶ(docs.extra 補強用の追加データ取得フック)
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)
  - `sources/wikipedia.py` — Wikipedia 標準 XML ダンプアダプタ(`wiki_id` パラメータ化、enwiki
    流用可)。`https://dumps.wikimedia.org/<wiki_id>/<date>/<wiki_id>-<date>-pages-articles.xml.bz2`
    (MediaWiki エクスポート形式、単一ファイル)を取得する。旧実装は CirrusSearch ダンプの `text`
    フィールドを docs.body にそのまま使っていたが、この `text` フィールドは Wikipedia の
    折りたたみ(collapsible)セクション(`{{hidden begin}}`〜`{{hidden end}}` 等)を検索
    インデックスから除外しており、例えば「ブラタモリ」の放送回一覧表のような内容が本文に
    一切含まれない欠落があったため、標準 XML ダンプ + wikitext 解析(`mwparserfromhell`。
    wikipedia 系ソースのみの例外的依存、下記参照)に切り替えた。
    `xml.etree.ElementTree`(標準ライブラリ)でストリーミング解析し、`<redirect>` を持つ
    ページは 2 パス走査(パス1: リダイレクト元→対象タイトルの収集、パス2: 本体の Doc 生成)
    で aliases に変換する(`sources/osm.py` の relation 2 パスと同じ精神)。wikitext →
    プレーンテキスト変換は、最初の見出しより前のノード列(lead section)を `opening`、
    記事全体を `strip_code(keep_template_params=True)` した結果を `body` とする。
    折りたたみテンプレートは通常のテンプレート呼び出しとして wikicode 木に残るため、
    中身(表を含む)が `body` へ自然に含まれる。`{{Dts|年|月|日}}` 等のテンプレートは
    完全展開せずパラメータ値の連結として残す(例:「2015 4 11」。検索対象としては機能する
    が整形はされない、という現実的な妥協)。XML ダンプには CirrusSearch の
    `popularity_score` 相当が無いため `rank_score` は `0.0` 固定。あわせて
    `other/pageview_complete/monthly/` の月次ページビュー(bot 除外・全プロジェクト合算、
    圧縮 5〜6GB)を `page_id`(`<page><id>`)で突合し、`docs.extra` に
    `{"pageviews_month": ..., "pageviews_period": "YYYY-MM"}` として格納する
    (`WIKI_DOMAIN` 未登録の wiki_id では突合をスキップ)。
  - `sources/osm.py` — OpenStreetMap アダプタ(`region` パラメータ化、Geofabrik の
    `<region>-latest.osm.pbf` を pyosmium(libosmium バインディング)で解析)。
    Geofabrik が 2026 年に `.osm.bz2` 配布を終了し `.osm.pbf` のみになったため、標準ライブラリの
    `xml.etree` では読めなくなった。osm 系ソースに限り pyosmium への依存を許容している
    (それ以外は標準ライブラリのみの方針を維持。wikipedia 系ソースの mwparserfromhell が
    もう1つの例外、上記参照)。
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
  - `sources/__init__.py` — アダプタレジストリ(新ソースはここに 1 行追加。あわせて
    `api/app/known_sources.py` の `KNOWN_SOURCES` にも表示用の 1 行を追加すること)
  - `server.py` — **chiezo-trigger**: 管理画面の初期化ボタンから叩かれる内部専用トリガー。
    ingest イメージを流用し、docker-compose.yml で CMD だけ `uvicorn server:app` に上書きする
    常駐コンテナ(`/data` に書き込み権限を持つ点だけ chiezo-ingest の one-shot 実行と異なる)。
    `POST /run/{source}` で `main.run(source, data_dir)` をバックグラウンドスレッドで実行し
    (同時実行は 1 ジョブまで、429/409 で拒否)、`GET /status` で state(idle/running/done/error)・
    source・started_at/finished_at・error・ログ tail(`chiezo.ingest` logger に登録した
    `_TailHandler` 経由)を返す。状態はプロセス内メモリのみ(永続化なし)。
    ホストへポート公開せず、`chiezo-api` からのみ docker 内部ネットワーク経由で到達可能
    (`chiezo-api` 側は環境変数 `CHIEZO_TRIGGER_URL` で URL を知る。未設定なら管理画面の
    初期化機能は無効)
- `scripts/` — 補助スクリプト(api/ingest 本体ではない運用ツール)
  - `gen_claude_config.sh` — chiezo 連携用の Claude 設定生成器。`curl` + POSIX ツールのみで
    動く(jq も Python も不要)。稼働中の chiezo(`--base-url`、既定 `http://localhost:9000`。
    環境変数 `CHIEZO_URL` でも指定可)の `/v1/sources` を引いて登録済みソースを列挙し
    (`kind` が `wikipedia`/`osm`/その他で文面を出し分け、`/v1/<src>/random` で実在タイトルを
    1 件拾って具体例に使う)、対象 CLAUDE.md へ `<!-- BEGIN chiezo (auto-generated) -->`〜
    `<!-- END chiezo -->` のマーカーブロックを書き込む。書き込み先は既定 `--user`
    (`~/.claude/CLAUDE.md`。推奨)、`--project`(`./CLAUDE.md`)、`--target/-o <path>`。
    共存は 2 方式: `--merge markers`(既定・冪等にブロックだけ差し替え)と `--merge headless`
    (`claude -p` に既存との統合を任せる)。`--offline --sources name[:kind],...` で未起動でも
    雛形生成、`--with-permissions` で対象の `.claude/settings.local.json` に curl 許可を追記。
    README の「Claude Code から使う」節と対応
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

# 管理画面(http://localhost:9000/、/admin へリダイレクト)から未初期化ソースの初期化も可能
# (chiezo-trigger 経由。完了後は上と同様に chiezo-api の再起動が必要)

# Claude 連携設定(CLAUDE.md ブロック)を稼働中の chiezo から生成(既定は ~/.claude/CLAUDE.md)
scripts/gen_claude_config.sh --base-url http://localhost:9000       # curl のみ・追加インストール不要
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
  `/data` への書き込み権限を持つのは chiezo-ingest(one-shot)と chiezo-trigger(常駐)だけで、
  chiezo-api は引き続き read-only マウント。
- エラーレスポンスは `{"error": "..."}` 形式。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。chiezo-trigger はホストへポート公開せず、
  chiezo-api からのみ内部ネットワーク経由で到達可能にすること。
- コード(api/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(セットアップ・API 仕様・運用手順)と本ファイル(CLAUDE.md、アーキテクチャ記述)も
  あわせて更新すること。ドキュメントだけを別コミット・別対応に先送りしない。
