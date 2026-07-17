# 新ソース追加手順書

chiezo に新しいデータソースを追加するのに必要な作業は
**アダプタ 1 モジュール + レジストリ 1 行 + `SOURCE` 指定** だけです。
API・DB スキーマ・共通フレームの変更は不要です。

## ケース 1: 他言語 Wikipedia(例: enwiki)

wikipedia アダプタは `wiki_id` をパラメータ化しているため、コードは 1 行で済みます。

`ingest/sources/__init__.py` の `ADAPTERS` に追加:

```python
"enwiki": lambda: WikipediaAdapter("enwiki", lang="en"),
```

取り込み:

```bash
docker compose --profile ingest run --rm -e SOURCE=enwiki chiezo-ingest
docker compose restart chiezo-api
```

容量目安: jawiki(DB 30〜50GB)と同規模以上を別途見込むこと。

## ケース 2: 新しい種類のソース(例: 青空文庫)

### 1. アダプタモジュールを書く

`ingest/sources/aozora.py` を作成し、`core.SourceAdapter` プロトコルを満たすクラスを実装します:

```python
from pathlib import Path
from typing import Iterator

from core import Doc


class AozoraAdapter:
    source = "aozora"
    source_kind = "aozora"
    lang = "ja"
    min_docs = 10_000            # 検証: 構築後の最低文書数
    sample_titles = ["吾輩は猫である", "走れメロス"]  # 検証: 検索が通るべきタイトル

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """元データを workdir に取得し (パス, 日付YYYYMMDD) を返す。

        - 再開可能にする(curl -C - / 既存ファイルはスキップ)
        - 日付は世代ファイル名 aozora-<date>.db に使われる
        """
        ...

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """元データをストリーミングで読み、Doc を yield する。

        - 全体をメモリに載せないこと(共通フレームがバッチ INSERT する)
        - コアスキーマに無いソース固有情報は extra (dict) に入れる
        - 旧字題名などの別名は aliases (list[str]) に入れる
        """
        ...
```

`Doc` のフィールド対応:

| Doc フィールド | 意味 |
|---|---|
| `doc_id` | ソース側の一意な整数 ID |
| `title` | 一意なタイトル(UNIQUE 制約あり) |
| `opening` | 冒頭要約(無ければ None) |
| `body` | 本文プレーンテキスト |
| `tags` | 分類(Wikipedia ならカテゴリ) |
| `links` | 関連文書タイトルの配列 |
| `aliases` | この文書を指す別名(リダイレクト等)→ aliases テーブルへ展開 |
| `updated_at` | 更新日時(ISO 8601) |
| `rank_score` | 同点時ランキング補助(人気度等。無ければ 0) |
| `extra` | ソース固有情報の dict(コアに無いものは全部ここ) |

### 2. レジストリに登録する

`ingest/sources/__init__.py`:

```python
from sources.aozora import AozoraAdapter

ADAPTERS = {
    "jawiki": lambda: WikipediaAdapter("jawiki", lang="ja"),
    "aozora": lambda: AozoraAdapter(),
}
```

### 3. 取り込む

```bash
docker compose --profile ingest run --rm -e SOURCE=aozora chiezo-ingest
docker compose restart chiezo-api
```

これで `/v1/aozora/search` などの全エンドポイントが自動的に使えるようになります
(API は起動時に `/data/aozora.db` を検出して登録するだけで、ソース種別を意識しません)。

## 動作確認のコツ

小さなサンプルデータで先に流れを確認できます:

```bash
docker compose --profile ingest run --rm \
  -e SOURCE=aozora -e DUMP_FILE=/data/dumps/sample.json.gz -e DUMP_DATE=20260101 \
  -e MIN_DOCS=5 -e SAMPLE_TITLES=吾輩は猫である \
  chiezo-ingest
```

- `DUMP_FILE`: `fetch()` をスキップして既存ファイルを使う
- `MIN_DOCS` / `SAMPLE_TITLES`: アダプタの検証パラメータを一時的に上書き

## 守るべき原則(設計書 §1)

1. ソースごとに独立した SQLite ファイル 1 つ。ソース間で JOIN しない。
2. コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。
   足りないフィールドは `extra` に逃がし、コアスキーマ変更は最終手段
   (変更時は `schema_version` を上げ、api 側で複数バージョン対応)。
3. `fetch()` は再開可能に、`iter_docs()` はストリーミングで。
