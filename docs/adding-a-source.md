# 新ソース追加手順書

Chiezo に新しいデータソースを追加するのに必要な作業は
**アダプタ 1 モジュール + レジストリ 1 行 + `SOURCE` 指定** だけです。
API・DB スキーマ・共通フレームの変更は不要です。

## ケース 1: 他言語 Wikipedia(例: enwiki)

**作業は不要です。** Wikipedia の 348 言語版は `ingest/sources/wikipedia_editions.py`
(自動生成カタログ)から `<lang>wiki` として登録済みで、そのまま取り込めます:

```bash
docker compose --profile ingest run --rm -e SOURCE=enwiki chiezo-ingest
# 取り込みが終われば chiezo-api が数秒以内に自動で新しい DB を読み込む(再起動は不要)
```

管理画面の `/admin` → `wikipedia` 行の「言語を選ぶ」(`/admin/wikipedia`)からも初期化できます。
カタログの再生成(言語版の追加・記事数の更新)は `python3 scripts/gen_wikipedia_editions.py`。

容量目安: jawiki(DB 30〜50GB)と同規模以上を別途見込むこと(enwiki はその数倍)。

## ケース 1': 他の国の OpenStreetMap(例: フランス)

**作業は不要です。** Geofabrik にある 195 の国・地域は `ingest/sources/osm_regions.py`
(自動生成カタログ)から `osm_<国>` として登録済みで、そのまま取り込めます:

```bash
docker compose --profile ingest run --rm -e SOURCE=osm_france chiezo-ingest
```

管理画面(`/admin`)の `osm` 行 →「国を選ぶ」(`/admin/osm`)からでも初期化できます。
国ごとの pbf サイズ・必要メモリの目安もそこに出ます。

Geofabrik 側に新しい抽出が増えた、pbf サイズが伸びて必要メモリの目安がずれた、という場合は
カタログを作り直します(生成器がネットワークに出るのはこのときだけです):

```bash
python3 scripts/gen_osm_regions.py     # ingest/sources/osm_regions.py を書き換える
```

ソース名の区切りはアンダースコアです(`osm_south_korea`。ハイフンは世代ファイル名
`<source>-<date>.db` の区切りと衝突するため、カタログ生成時に変換しています)。

国より小さい単位(米国の州など)や、複数国をまとめた独自の抽出を足したいときだけ、
`ADAPTERS` に手で 1 行書きます:

```python
"osm_hokkaido": lambda: OsmAdapter("osm_hokkaido", region="asia/japan/hokkaido", lang="ja",
                                   min_docs=10_000, sample_titles=["札幌市"]),
```

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
| `tags` | 分類(Wikipedia ならカテゴリ)。`doc_tags` へ自動展開され `filter?tag=` / `tags` で引ける |
| `links` | 関連文書タイトルの配列 |
| `aliases` | この文書を指す別名(リダイレクト等)→ aliases テーブルへ展開 |
| `updated_at` | 更新日時(ISO 8601) |
| `rank_score` | 同点時ランキング補助(人気度等。無ければ 0) |
| `extra` | ソース固有情報の dict(コアに無いものは全部ここ) |

> **`sample_titles` に「消えうる固有名」を書かないこと。** 検証は「そのタイトルの文書が
> 実在し(完全一致 or alias)、FTS でも引けること」を見るので、書いた 1 件が次のダンプで
> 無くなると**正しく取り込めているのに検証が落ちます**(しかも原因が分かりにくい)。
> 百科事典の記事や著名な山・駅のように消えないものならそのままでよいですが、店舗・施設・
> 事業所のように統廃合されるソースでは、`iter_docs` が取り込んだ中から代表を 1 件選んで
> `self.sample_titles` に入れる形にします(`__init__` では空にしておき、`SAMPLE_TITLES`
> での外からの上書きがあればそちらを優先する)。確かめたいのは「索引が壊れていないか」で
> あって「あの 1 件が実在するか」ではありません。

### 2. レジストリに登録する

`ingest/sources/__init__.py`:

```python
from sources.aozora import AozoraAdapter

ADAPTERS = {
    "jawiki": lambda: WikipediaAdapter("jawiki", lang="ja"),
    "aozora": lambda: AozoraAdapter(),
}
```

### 3. 管理画面の初期化ボタン

`ADAPTERS` に追加すれば自動的に出ます(chiezo-api は ingest のコードを import しませんが、
`chiezo-trigger` の `GET /sources` からソース名・kind・lang を受け取るため)。

`api/app/known_sources.py` の `KNOWN_SOURCES` は、`chiezo-trigger` が未設定・到達不能なときに
管理画面を空にしないための控えです。追記は必須ではありません。

### 4. 取り込む

```bash
docker compose --profile ingest run --rm -e SOURCE=aozora chiezo-ingest
# 取り込みが終われば chiezo-api が数秒以内に自動で新しい DB を読み込む(再起動は不要)
```

これで `/v1/aozora/search` などの全エンドポイントが自動的に使えるようになります
(API は起動時に `/data/aozora.db` を検出して登録するだけで、ソース種別を意識しません)。

## ケース 3: このリポジトリに入れられないソース(別リポジトリのプラグイン)

プライベートな情報やライセンスの都合で、**取得コードごと公開リポジトリに置きたくない
ソース**は、別リポジトリのプラグインとして書きます。Chiezo 側には何も入りません
(コードもデータも別リポジトリのまま)。

### 前提: 配信側は最初から何も要らない

chiezo-api はソース種別を知りません。`data/<名前>.db` にコアスキーマの SQLite が
置かれていれば、それだけで登録されて `search` / `doc` / `filter` / `tags` / MCP /
ブラウズ画面が全部使えます。**「別リポジトリで `.db` を焼いて `data/` に置く」だけなら、
以下の設定すら要りません**(chiezo-api が数秒以内に検知します)。

以下が要るのは、**取り込み自体を Chiezo に任せたい**場合です(管理画面の初期化・再構築
ボタンから回せるようになります)。

### プラグインは「文書を配るサービス」

プラグインは HTTP で 2 つの口を持つだけのサービスです。**役割は「取得と整形」まで**で、
DB の構築(FTS・タグ転置表・世代切り替え・検証)は Chiezo 本体が行います。

```
[chiezo-trigger / chiezo-ingest]  --HTTP-->  [あなたのプラグイン]
        DB を構築する                          元データを取得して NDJSON で配る
```

#### 契約

```
GET /sources            → {"sources": [{"name","kind","lang","label","min_docs","memory_gb"}]}
GET /fetch?source=NAME  → NDJSON(1 行目に meta、以降は 1 行 1 文書)
```

`/fetch` の 1 行目は省略できます(その場合は取り込んだ日が世代の日付になります)。

```json
{"meta": {"dump_date": "20260805", "min_docs": 20000, "sample_titles": ["運用手順書"]}}
{"doc_id": 1, "title": "運用手順書", "opening": "…", "body": "…", "tags": ["社内"], "rank_score": 0.5}
{"title": "障害対応メモ", "body": "…", "extra": {"lat": 35.68, "lon": 139.76}}
```

- 文書の項目はコアスキーマの `Doc` と同じ(`title` だけ必須)。`doc_id` を省くと行番号を振ります
- **`meta` をヘッダではなく本文の 1 行目に置く**のは、HTTP ヘッダが latin-1 しか運べず
  日本語のタイトルを載せられないため。取り込んだ中身を見てから代表を選ぶソースは、
  全部数え終えてから 1 行目を書き出せます
- ソース名は `[A-Za-z0-9_]` のみ(ハイフンは世代ファイル名 `<source>-<date>.db` の区切りと
  衝突します)。組み込みソースや他のプラグインと同名も弾かれます

実装の受け口は本体側の [`ingest/sources/remote.py`](../ingest/sources/remote.py) が正です。

#### 繋ぎ方

プラグイン側にオーバーレイを置き、Chiezo の compose に**重ねます**。

```yaml
# 別リポジトリ側の docker-compose.plugin.yml
services:
  my-plugin:
    image: ghcr.io/<自分のアカウント>/my-plugin:latest   # 本体とは別に pull する
    restart: unless-stopped
  chiezo-trigger:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080
    depends_on: [my-plugin]
  chiezo-ingest:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080
```

```bash
# chiezo/.env
COMPOSE_FILE=docker-compose.yml:../my-plugin/docker-compose.plugin.yml
```

```bash
docker compose up -d                    # プラグインも一緒に立ち、trigger が見つける
docker compose --profile ingest run --rm -e SOURCE=private_docs chiezo-ingest
```

プラグインを増やすときは、サービスを 1 ブロック足して URL を `,` で並べます
(compose は環境変数のリストからサービスを生やせないので、両方に書きます)。

> **落ちていても本体は動きます。** カタログが引けないプラグインは警告を出して飛ばすだけです
> (別コンテナである以上、再起動中に繋がらないのは正常な状態のため)。ただし**繋がったのに
> 応答の形が違う場合は落とします** —— 直すべき不具合を黙って無視しないためです。

実例が [chiezo-plugin](https://github.com/rtcode337/chiezo-plugin)(郵便局データ)にあります。

### スキーマバージョンについて

`schema_version` は「api がどの機能を出せるか」の指標です(2 で `filter`、3 で `tag`、
4 で `bbox` と大きな並べ替え)。DDL は `core.py` にあるので、上のようにイメージを
継承していれば最新版が自動的に焼かれます。**独自に `.db` を組み立てる場合は、
`core.CORE_SCHEMA_DDL` 一式をそのまま使ってください** — 索引が欠けると
`filter` / `tags` が 409 になります。

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

## 守るべき原則

1. ソースごとに独立した SQLite ファイル 1 つ。ソース間で JOIN しない。
2. コアスキーマ(meta / docs / aliases / docs_fts / doc_tags / tag_counts / doc_coords)は
   全ソース共通。
   足りないフィールドは `extra` に逃がし、コアスキーマ変更は最終手段
   (変更時は `schema_version` を上げ、api 側で複数バージョン対応)。
3. `fetch()` は再開可能に、`iter_docs()` はストリーミングで。
