---
paths:
  - "scripts/**"
  - "tests/conftest.py"
  - "tests/fixtures/**"
  - "pyproject.toml"
  - "requirements-dev.*"
  - "*/requirements.*"
  - "assets/**"
---

# 補助スクリプト・テスト・開発ツールの設定

`scripts/`・`tests/`・`pyproject.toml`・依存の書き方。

- `scripts/` — 補助スクリプト(app/ingest 本体ではない運用ツール)

  - `gen_wikipedia_editions.py` — `ingest/sources/wikipedia_editions.py`(Wikipedia 言語版
    カタログ)の生成器。Wikimedia の sitematrix(言語版一覧。closed/private/fishbowl は除外)、
    wikistats(記事数)、CLDR の言語名日本語表記を引いて 348 件の表を書き出す。
    ネットワークに出るのは生成時だけで、生成物はコミットする
  - `gen_osm_regions.py` — `ingest/sources/osm_regions.py`(OSM 国別抽出カタログ)の生成器。
    Geofabrik の `index-v1.json` と大陸別 HTML(pbf サイズ)、CLDR の国名・主要言語を引いて
    195 件の表を書き出す。ネットワークに出るのは生成時だけで、生成物はコミットする。
    CLDR の国名は Geofabrik の英語名と一致したときだけ採用し、一致しないもの(「Ivory Coast」
    「Ukraine (with Crimea)」や複数国をまとめた抽出)は `OVERRIDES` に人手で書く
    — Geofabrik の ISO コードには取り違え(トケラウに `VU` 等)があり、そのまま引くと
    「トケラウ = バヌアツ」のような誤表示になるため
  - **テストで待つときは、確かめたいことに要る長さだけ待つ。** 1 本ごとの待ちは
    小さく見えても、本数を掛けると走らせる気が失せる長さになる。実測で直した 4 か所:
    後片付けの見回り(`HTTPServer.serve_forever` の既定は 0.5 秒で、**テスト 1 本ごと**に
    払っていた)、繋がらない相手を待つ既定(5 秒)、1 行ずつ眠らせる読み手(2 秒)、
    締め切りより長く眠る子プロセス。**確かめたいのは「その扱いになること」で、
    何秒かかるかではない** —— 締め切りを超えたことが分かる長さがあれば足りる
    (43 秒 → 27 秒。テストは 1 本も減らしていない)
  - `run_tests.sh` — テスト実行のラッパー(引数はそのまま pytest へ)。手元に依存が
    揃っていればそれで、無ければ **CI と同じ Python 3.12 のイメージを組み立てて** Docker で
    回す。ホストの python が 3.12 でない環境(依存に C 拡張があるので import から落ちる)で
    準備なしにテストを通せるようにするためのもの。入れるのは `requirements-dev.txt`
    (app + ingest + pytest + ruff を 1 本にまとめたロック)—— **2 つのロックを同時に
    渡してはいけない**。共通の依存(fastapi 等)が二重指定になり pip が断る。
    **Docker のビルドコンテキストはロック 1 つだけの一時ディレクトリにすること** —
    リポジトリのルートを渡すと
    `data/` の `.db`(数十 GB)まで docker daemon へ送られる。リポジトリ側は
    バインドマウントなので大きさは関係ない。実行ユーザーを呼び出し元に合わせるのは、
    `__pycache__` が root 所有で残るとホスト側の実行が書き込めなくなるため
  - `refresh_rank_score.py` — 既存 DB の `rank_score` を `extra` から計算し直す
    (wikipedia は `pageviews_month`、geonames は `population`。osm は元から 0〜1 なので
    対象外)。スキーマは変わらないので `schema_version` は上げない。ダンプの取り直しも不要
  - `fts_lab.py` — trigram と形態素トークナイザの FTS を同じ文書集合の上で作って比べる
    実験台。索引サイズ・構築時間・ヒット数・順位を出す。人気度の重みを振るのにも使った。
    拡張(`sqlite-vaporetto` 等)は同梱していないので別途取ってくる
- `assets/` — プロジェクトアイコン(`icon.svg`。README ヘッダー・管理画面ファビコン・
  apple-touch-icon の原本)
- `tests/` — フィクスチャ(`fixtures/mini_jawiki.xml.gz` 12 文書、`fixtures/mini_osm.osm.pbf`、
  `fixtures/mini_geonames.zip` ほか geonames 一式)での API / ingest テスト。
  `test_mcp.py` は /mcp に生の JSON-RPC を 1 発 POST して確かめる(ステートレスな
  トランスポートなので initialize のハンドシェイクが要らず、応答は SSE フレームの data: 行)。
  `test_answer.py` は `answer._llm_client` を `httpx.MockTransport` 入りのクライアントに
  差し替えて偽の OpenAI 互換サーバを演じさせる(推論サーバもネットワークも無しで、
  クエリ生成 → 検索 → 回答の全経路を通せる)。`test_agent.py` は同じ仕掛けの偽サーバに
  **`tool_calls` を返させて** agent ループ(道具を呼ぶ → 実行して返す → 答える)を通す。
  GPU もモデルも要らないので CI で回る
- `pyproject.toml` — **開発ツールの設定だけ**(ruff / pytest)。`[project]` は持たない ——
  pip で配るライブラリではなく、依存の違う 2 つのイメージとして動くアプリだから。
  - `pythonpath = ["app", "ingest"]` で両方を import 可能にする。以前は
    `tests/conftest.py` が `sys.path` を書き換えていたが、それは設定の置き場が無いことの
    回避策だった
  - ruff は `line-length = 120`(既定の 88 は日本語コメントに短すぎる)、
    `RUF001/002/003`(全角記号を「紛らわしい Unicode」と見なす)は落とす。
    自動生成カタログ 2 つは `E501` を per-file-ignore
- `*/requirements.in` / `requirements.txt` — **直接の依存は `.in` に範囲(>=)で書き、
  実際に入る版は `.txt`(全依存 + ハッシュ)で固定する**。作り直しは
  `scripts/lock_requirements.sh`(uv pip compile)。`.txt` を手で編集しない。
  ルートの `requirements-dev.in` は app + ingest + pytest + ruff をまとめたもので、
  CI と `run_tests.sh` の Docker 経路が使う
