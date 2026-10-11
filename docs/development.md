# 開発

テストの回し方、手元での環境の作り方、依存の固定、CI とイメージ、compose の重ね方、アイコンの作り直し。

## テスト

```bash
scripts/run_tests.sh                          # 全テスト
scripts/run_tests.sh tests/test_notes.py -v   # 引数はそのまま pytest へ渡る
ruff check .                                  # lint(設定は pyproject.toml)
```

依存が揃っていればそのまま、無ければ CI と同じ Python 3.12 のイメージを組み立てて
Docker で実行する(リポジトリはバインドマウントするだけなので `data/` の大きさは
関係しない)。手元に環境を作るなら Python 3.12 を使う(`app/` と `ingest/` のイメージ・
CI と同じ系列。依存に C 拡張が含まれるため、別のバージョンでは import から落ちる)。

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

`.venv` があれば `scripts/run_tests.sh` はそちらを優先する。

テストは同梱の小型フィクスチャ(`tests/fixtures/mini_jawiki.xml.gz` 12 文書、
`tests/fixtures/mini_osm.osm.pbf` 12 ノード + 2 way + 2 relation、
`tests/fixtures/mini_geonames.zip` ほか geonames 一式)から実際に
DB を構築して全エンドポイントを検証する。ネットワーク・実データは要らない。

## 依存の固定

依存は `requirements.in`(直接の依存を範囲で指定)から `requirements.txt`(版とハッシュを
固定したロック)を作る形にしてある。`.in` を編集したら `scripts/lock_requirements.sh` で
ロックを作り直す。イメージが再現する代わりに上流の新版は自動では入らないので、
破壊的変更への追従は CI の週次ジョブ(最新の依存でテストを回す)が受け持つ。

## データの置き場

**バインドするホストのディレクトリは `data/` 1 つだけ。** 下の `corpus/`(取り込んだ本体)・
`notes/`(覚える層)・`state/`(管理画面の設定。**API キーが入る**)は `chiezo-init` が
起動前に作り、所有者を実行ユーザー(既定 uid 10001。ホストに実在しない番号)に合わせる。
`data/` をホストから直接編集・書き戻ししたいときだけ `.env` に `CHIEZO_UID` / `CHIEZO_GID` を書く。

## CI とイメージ

CI(`.github/workflows/ci.yml`)は push / PR でこのテストを実行し、main への push で
`ghcr.io/rtcode337/chiezo-app` / `ghcr.io/rtcode337/chiezo-ingest` のマルチアーキ
(amd64 / arm64)イメージを GHCR へ公開する。`docker-compose.yml` はこのイメージを
pull して使うので、`app/` や `ingest/` を変更してローカルで動作確認するときは
ビルド版を使う。

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

## compose の重ね方

compose は「本体 + 上書き」の形にしてある。本体(`docker-compose.yml`)が検索 API・MCP で、
上書きは足したいものだけを数行ずつ:`build`(手元ビルド)・`llm`(推論サーバ)・
`llm.cuda`(その推論を GPU で)・`comfyui`(絵と音の生成)・`lan`(GPU の要るものを別ホストへ公開)。
重ねる順はこの並びどおり。

## アイコンを変えたとき

原本は `assets/icon.svg` で、`app/pages.py` に 2 つの派生物を埋め込んである
(app イメージのビルドコンテキストは `app/` のみで `assets/` を含まないため)。
原本を変えたら両方を作り直す。

- `FAVICON_DATA_URI` — SVG を最小化した data URI(ブラウザタブ用)
- `APPLE_TOUCH_ICON_PNG` — iPhone「ホーム画面に追加」用の 180×180 PNG
  (`/apple-touch-icon.png` で配信)。iOS は SVG や data URI のファビコンを
  ホームアイコンに使わないため PNG が別に要る。角丸マスクは iOS が自前で掛ける
  (透過部分は黒く塗られる)ので、角丸なし・全面塗りでラスタライズする。

```bash
pip install cairosvg
python - <<'EOF'
import base64, cairosvg, pathlib, textwrap
svg = pathlib.Path("assets/icon.svg").read_text().replace('rx="56"', 'rx="0"', 1)
png = cairosvg.svg2png(bytestring=svg.encode(), output_width=180, output_height=180)
print("\n".join(textwrap.wrap(base64.b64encode(png).decode(), 96)))
EOF
```
