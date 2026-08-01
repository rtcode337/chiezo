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

プライベートな情報のように、**取得コードごと公開リポジトリに置きたくないソース**は、
別リポジトリのモジュールとして書き、`CHIEZO_SOURCE_PLUGINS` で差し込みます。
Chiezo 側には何も入りません(コードもデータも非公開のリポジトリのまま)。

### 前提: 配信側は最初から何も要らない

chiezo-api はソース種別を知りません。`data/<名前>.db` にコアスキーマの SQLite が
置かれていれば、それだけで登録されて `search` / `doc` / `filter` / `tags` / MCP /
ブラウズ画面が全部使えます。**「別リポジトリで `.db` を焼いて `data/` に置く」だけなら、
以下の設定すら要りません**(chiezo-api が数秒以内に検知します)。

以下が要るのは、**管理画面の初期化・再構築ボタンからも回したい**場合です。

### 1. 非公開のリポジトリにアダプタを書く

ケース 2 と同じ `SourceAdapter` を書き、モジュールの直下に `ADAPTERS` を置きます。

```python
# private_sources/__init__.py
from core import Doc          # chiezo-ingest イメージの core.py を使う


class PrivateDocsAdapter:
    source = "private_docs"
    source_kind = "private_docs"
    lang = "ja"
    min_docs = 100
    sample_titles = ["運用手順書"]
    min_build_memory_gb = 0.5

    def fetch(self, workdir):
        # 非公開の API を叩く、別のサーバーからファイルを集める、など
        return collected_path, "20260731"

    def iter_docs(self, path):
        yield Doc(doc_id=1, title="運用手順書", opening="…", body="…")


ADAPTERS = {"private_docs": lambda: PrivateDocsAdapter()}
```

ソース名は `[A-Za-z0-9_]` のみ(ハイフンは世代ファイル名 `<source>-<date>.db` の区切りと
衝突するため弾かれます)。既存ソースと同名も弾かれます(`jawiki` を影で差し替えると
間違ったダンプが `jawiki.db` に焼かれるため)。

**`__init__` は軽くしてください。** 管理画面のカタログ(`GET /sources`)を組み立てる際に
アダプタを実体化するので、コンストラクタで通信やファイル読み込みをしないこと。

### 2. 差し込み方を選ぶ

差し込み方は 2 つあります。**まずマウント方式を検討してください** — イメージを焼かずに済み、
プラグインを直したら再起動だけで反映されます。

| | マウント(推奨) | イメージに焼く |
|---|---|---|
| やること | ボリューム 1 本 + 環境変数 2 つ | `FROM` で継承して `COPY` |
| イメージ | 本体の `chiezo-ingest` のまま | プラグインごとに 1 つ増える |
| 直したとき | `docker compose up -d` | 焼き直す |
| 追加の pip 依存 | 入れられない | 入れられる |
| ソースツリー | 配信先に要る | 要らない |

### 2a. マウントして差し込む(推奨)

`chiezo-ingest`(one-shot)と `chiezo-trigger`(管理画面のボタン)の両方に、コードを
読み取り専用でマウントし、環境変数を 2 つ足します。プラグイン側に置いたオーバーレイを
Chiezo の compose に**重ねる**形にすると、Chiezo 側は `.env` の 2 行だけで済みます。

```yaml
# 別リポジトリ側の docker-compose.plugin.yml
# image: は書かない —— 重ねた先の定義(pull 版 / :local)をそのまま引き継ぐため
services:
  chiezo-trigger:
    volumes:
      - ${CHIEZO_PLUGIN_DIR:-../my-plugin}/private_sources:/plugins/private_sources:ro
    environment:
      - PYTHONPATH=/plugins
      - CHIEZO_SOURCE_PLUGINS=private_sources
  chiezo-ingest:
    volumes:
      - ${CHIEZO_PLUGIN_DIR:-../my-plugin}/private_sources:/plugins/private_sources:ro
    environment:
      - PYTHONPATH=/plugins
      - CHIEZO_SOURCE_PLUGINS=private_sources
```

```bash
# chiezo/.env
COMPOSE_FILE=docker-compose.yml:../my-plugin/docker-compose.plugin.yml
CHIEZO_PLUGIN_DIR=../my-plugin
```

```bash
docker compose up -d                    # trigger にプラグインが載る
docker compose --profile ingest run --rm -e SOURCE=private_docs chiezo-ingest
```

`PYTHONPATH=/plugins` はマウント先を import 対象に加えるためで、`CHIEZO_SOURCE_PLUGINS` が
そのモジュールの `ADAPTERS` を取り込みます。相対パスは**重ねた先のプロジェクトディレクトリ**
(= Chiezo のチェックアウト)から解決されるので、別の場所に置くなら `CHIEZO_PLUGIN_DIR` を
絶対パスにしてください。

> **プラグインを新しく足したときだけ、管理画面への反映に最大 `CHIEZO_CATALOG_TTL` 秒
> (既定 300)かかります。** 「取り込めるソースの一覧」は chiezo-trigger から取って
> api がキャッシュするためです。待てないなら `docker compose restart chiezo-api`。
> 取り込み(上の `docker compose run`)は待たずに通ります。

**追加の pip 依存が要るプラグインではこの方式は使えません**(マウントするのはコードだけで、
本体のイメージに依存は入っていないため)。その場合は次のイメージを焼く方式にしてください。

### 2b. イメージに焼いて差し込む

```dockerfile
# 非公開リポジトリ側の Dockerfile
FROM ghcr.io/rtcode337/chiezo-ingest:latest
COPY private_sources /srv/chiezo-ingest/private_sources
ENV CHIEZO_SOURCE_PLUGINS=private_sources
```

複数のモジュールを入れるならカンマ区切りで並べます。

#### compose のイメージを差し替える

`chiezo-ingest` と `chiezo-trigger` はどちらも `CHIEZO_INGEST_IMAGE` で差し替えられます。

```bash
# .env
CHIEZO_INGEST_IMAGE=ghcr.io/<自分のアカウント>/chiezo-ingest-private:latest
```

```bash
docker compose up -d           # trigger が差し替わり、管理画面に private_docs が出る
```

これで組み込みソースと同じように、管理画面の「初期化」「再構築」ボタンから回せます。
`SOURCE=private_docs` での one-shot 実行も同様です。

**指定したモジュールが読めない・`ADAPTERS` が無い場合は起動時に落とします**
(黙って無視すると「管理画面に出ない」「unknown SOURCE」として後から現れ、原因が
分からなくなるため)。`docker logs` にモジュール名つきの理由が出ます。

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
