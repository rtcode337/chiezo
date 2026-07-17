# chiezo — ローカル知識サーバー

LAN 内で動く読み取り専用の知識検索 REST API。複数のデータソース(現状は日本語 Wikipedia = `jawiki`)を
ソースごとに独立した SQLite ファイル(`/data/<source>.db`)として収容する。設計書は v0.2。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — ルーティング(/healthz, /v1/sources, /v1/{source}/search|doc|titles|links|random)
  - `app/registry.py` — /data 走査・ソース登録
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
- `ingest/` — **chiezo-ingest**: ワンショット構築バッチ。
  - `main.py` — 共通フレーム: 取得 → `.building` へ構築 → FTS → 検証 → ブルーグリーン切り替え(シンボリックリンク差し替え、旧世代 1 つ保持)
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)
  - `sources/wikipedia.py` — CirrusSearch ダンプアダプタ(`wiki_id` パラメータ化、enwiki 流用可)
  - `sources/__init__.py` — アダプタレジストリ(新ソースはここに 1 行追加)
- `tests/` — フィクスチャ(`fixtures/mini_jawiki.json.gz`、12 文書)での API / ingest テスト

## コマンド

```bash
# テスト(fastapi, httpx, pytest が必要)
python -m pytest tests/ -v

# フィクスチャ再生成
python tests/fixtures/make_fixture.py

# API 起動(Docker)
docker compose up -d

# 取り込み(本番: 5〜6GB ダウンロード、構築 2〜6 時間、ディスク空き 80GB 推奨)
docker compose --profile ingest run --rm chiezo-ingest

# 取り込み後の反映
docker compose restart chiezo-api
```

ingest の主な環境変数: `SOURCE`(必須)、`DUMP_DATE`(日付固定)、`DUMP_FILE`(ダウンロードスキップ)、
`MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータ上書き。小規模データでの動作確認用)。

## 実装上の約束事

- コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。ソース固有情報は `docs.extra`(JSON)へ。
  変更は最終手段で、`schema_version` を上げ api 側で複数バージョン対応する。
- ソース間で JOIN しない。API はソース種別を意識せず docs/aliases/docs_fts のみ参照する。
- FTS5 は trigram。ユーザー入力は必ずフレーズエスケープしてから MATCH に渡す(`app/fts.py` 経由)。
- 運用 DB は読み取り専用(`immutable=1`)。更新はブルーグリーン(別ファイル構築 → シンボリックリンク差し替え → api 再起動)のみ。
- エラーレスポンスは `{"error": "..."}` 形式。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。
