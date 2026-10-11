# <img src="assets/icon.svg" width="40" alt="Chiezo icon"> Chiezo — AI を使うのに要るものを貸し出すサーバー

- AI とアプリが共通で要るもの —— 知識・記憶・話せる AI・鍵・計算資源・時計 —— を 1 か所で持って貸し出す
- 知識は公開ダンプ(Wikipedia・OpenStreetMap・GeoNames・Overture)を手元の SQLite に取り込み、オフラインで引ける
- 推論は同居させない。考えるのは呼ぶ側で、Chiezo はそれが持てないものを預かる

| 借りる側 | 口 | 主に借りるもの |
|---|---|---|
| AI(Claude Code など) | MCP(`/mcp`) | 溜めた知識、覚えたことの置き場、絵や音を作る道具 |
| アプリ | REST(`/v1/...`) | 話せる AI そのもの(鍵は Chiezo が握る)、知識、生成 |
| 人 | 画面(`/admin`) | 設定と、動いているかの確認 |

できることの一覧と各面の使い方は [全体像](docs/overview.md) にある。

## 起動する

```bash
docker compose up -d                       # イメージは GHCR から pull される。更新は docker compose pull
curl -s http://localhost:7010/healthz
```

ブラウザで `http://<サーバー>:7010/` を開くと管理画面に入る。知識を取り込むソースは次のように足す
(取り込みが終われば、再起動なしで数秒以内に読み込まれる)。

```bash
docker compose --profile ingest run --rm chiezo-ingest                      # jawiki(構築 2〜6 時間)
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest  # 国別の地名と POI
docker compose --profile ingest run --rm -e SOURCE=geonames chiezo-ingest   # 全世界の地名
```

どのソースもメモリ 2 GiB で構築できる。足りなければダウンロードの前に中止する
([メモリについて](docs/operations.md#メモリについて))。ソースの一覧は [全体像](docs/overview.md#ためられるソース)。

## 引く

```bash
BASE=http://<サーバー>:7010
curl -s  "$BASE/v1/sources"                                                              # ソース一覧
curl -sG "$BASE/v1/jawiki/search?limit=5" --data-urlencode "q=浅草寺"                    # 全文検索
curl -sG "$BASE/v1/jawiki/doc?fields=title,opening,tags" --data-urlencode "title=浅草寺" # 文書
curl -sG "$BASE/v1/jawiki/filter?limit=200" --data-urlencode "tag=ラーメン店"            # カテゴリ全件
```

- 日本語・スペースを含む値は `-G --data-urlencode` で渡す
- 3 文字未満の語は全文検索できず、タイトルの前方一致になる(応答の `mode` が `title_prefix`)
- カテゴリの列挙は `filter?tag=` を使う。本文の全文検索で代用すると、ソートキー付きの記事を取りこぼす

Claude Code からは MCP で繋ぐ。設定一式(CLAUDE.md の節・権限・MCP の登録)は稼働中の Chiezo から作れる。

```bash
scripts/gen_claude_config.sh -u http://<サーバー>:7010
```

## 運用

```bash
docker compose pull && docker compose up -d                 # アプリの更新
docker compose --profile ingest run --rm chiezo-ingest      # ダンプの更新(別ファイルに作って切り替える)
```

| したいこと | 参照 |
|---|---|
| AI と話す相手・鍵・web 検索・GPU を設定する | [AI と話す](docs/ai.md) |
| `.env` を置けない環境(NAS など)で起動する | [単体定義](docs/operations.md#env-を置けない環境で起動する単体定義) |
| IP 以外の名前で開く・外に開けてよいか確かめる | [セキュリティ](docs/operations.md#セキュリティ)(名前で開くなら `CHIEZO_ALLOWED_HOSTS` が要る) |
| どのコミットが動いているか確かめる | [動いているビルドを確かめる](docs/operations.md#動いているビルドを確かめる) |
| メモリの多い別マシンで DB を焼いて配る | [別マシンで DB を焼く](docs/build-on-another-machine.md) |

## ドキュメント

| | 中身 |
|---|---|
| [全体像](docs/overview.md) | 持っているもの・ためられるソース・覚える・ToDo・AI に話させる/作らせる |
| [API リファレンス](docs/api-reference.md) | REST の全パラメータ、`extra` の中身、MCP、Claude Code 連携、画面 |
| [AI と話す](docs/ai.md) | 話す相手の増やし方・CLI ブリッジ・agent モード・web 検索・生成・環境変数 |
| [運用](docs/operations.md) | 取り込みの環境変数、古いスキーマの扱い、メモリ、配布、セキュリティ |
| [ToDo](docs/tasks.md) | タスク・ルールの画面と、notes のタグでの表し方 |
| [設計メモ](docs/design-notes.md) | なぜこの形なのか。実測して方針が変わったものは数字と一緒にここへ残す |
| [開発](docs/development.md) | テスト・依存の固定・CI とイメージ・compose の重ね方 |
| [ソースの追加手順](docs/adding-a-source.md) | 新しい種類のデータを足すとき |
| [FTS トークナイザの評価](docs/fts-tokenizer-evaluation.md) | 形態素解析への差し替えを見送った経緯と実測値 |

## ライセンス

このリポジトリのコードは [MIT License](LICENSE)。

リポジトリ自体はデータを含まない。取り込んで構築した DB の中身は各ソースのライセンスに従い、
DB や検索結果を配布・公開するときは帰属表示やライセンスの継承が要る。

| ソース | データ提供元 | ライセンス |
|---|---|---|
| `<lang>wiki` | [Wikimedia ダンプ](https://dumps.wikimedia.org/) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.ja)(© Wikipedia 各記事の執筆者) |
| `osm_<国>` | [Geofabrik](https://download.geofabrik.de/)(OpenStreetMap 抽出) | [ODbL 1.0](https://opendatacommons.org/licenses/odbl/)(© OpenStreetMap contributors) |
| `geonames` | [GeoNames](https://www.geonames.org/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.ja) |
| `overture_japan` | [Overture Maps Foundation](https://overturemaps.org/) | CDLA Permissive 2.0 / Apache 2.0(元データによる) |
| `jquants_*` | [J-Quants](https://jpx-jquants.com/) | 利用規約に従う。個人の私的利用に限り、第三者への配信・公開はできない([運用](docs/operations.md#j-quants)) |
