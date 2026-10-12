---
paths:
  - "docker-compose*.yml"
  - "**/Dockerfile"
  - ".github/**"
  - ".env.example"
  - "tests/test_compose_files.py"
  - "tests/test_dockerfiles.py"
---

# compose・CI・依存更新

compose ファイルの分け方、CI、Dependabot。

- `.github/workflows/ci.yml` — push / PR で `ruff check` と pytest を実行し、main への
  push で `chiezo-app` / `chiezo-ingest` / `chiezo-bridge` の 3 イメージを
  マルチアーキ(amd64 / arm64。bridge だけ amd64)で
  GHCR へ公開(ダイジェストをマージする方式。
  arm64 の無料ランナーが public 限定のため、リポジトリが private の間は公開ジョブをスキップ)。
  ジョブは 4 つ:
  - `lint` / `test` — 固定した版(`requirements-dev.txt`)で回す。`build` はこの 2 つを待つ
  - `test-latest` — **ロックを無視して `requirements-dev.in` の範囲で最新を入れて回す**
    canary。週 1 の `schedule` と手動実行のときだけ動き、`continue-on-error` で公開は
    止めない。ロックを入れると通常のテストが固定版になるので、上流の破壊的変更
    (実例: mcp 2.0 が `mcp.server.fastmcp` を削除)に気づく役目をここへ移した
  - `build` / `merge` — 定期実行では走らない(`build` の `if` が push / 手動実行だけに
    絞ってある)。`permissions` はトップレベルを読み取りのみとし、`packages: write` は
    build / merge ジョブだけに与える(public 化に伴う絞り込み)
- `.github/dependabot.yml` — 依存更新の週次 PR(pip×4 / docker×3 / docker-compose / github-actions)。
  ブリッジ(`bridge/requirements.txt`・Dockerfile)と、compose が pull するイメージ
  (searxng・llama.cpp・alpine・ComfyUI)も見る。ブリッジの CLI の版(npm install の `@版`)は
  追えないので人が上げる。
  拾いたいのは `.in` の範囲で捕まらない「範囲外の新メジャー」と上限ピンの解除、
  Actions・ベースイメージの更新。**ロック(`requirements.txt`)の更新は任せない** ——
  uv がコンパイルした形式は Dependabot が書き戻せないので、範囲が動いたら手元で
  `scripts/lock_requirements.sh` を回す

## compose の約束事

- **compose は「本体 + 上書き」に保つ**。`docker-compose.yml` は検索 API・MCP としての
  Chiezo(chiezo-app + chiezo-trigger + chiezo-ingest)だけを持ち、足すものは上書きを
  重ねる:`build`(手元ビルド)→ `llm`(推論サーバ)→ `llm.cuda`(その推論を GPU で)→
  `comfyui`(絵と音の生成)→ `lan`(GPU の要るものを別ホストへ公開)。重ねる順はこの並び。
  **ファイル名は中身のコンテナに合わせる**(profile も同じ名前)——
  `answer` / `image` と呼んでいた頃は、前者が層の名前でファイルの中身とずれ、
  後者は「画像」とも「Docker イメージ」とも読めた。
  **上書きに本体の設定を写さないこと** —— 以前 `build` が本体の完全なコピーで、
  web 検索と回答パイプラインの設定が抜けたまま取り残された。`tests/test_compose_files.py`
  が行数で見張っている。
- **「答える」層は、コンテナだけを `docker-compose.llm.yml` に置く**。chiezo-app に渡す
  設定(`CHIEZO_LLM_URL` 以下)は本体側に残す —— 推論を LAN の別マシンに任せる使い方では、
  コンテナは要らず設定だけが要るため。
- **`searxng`(web 検索の道具が引く検索エンジン)は本体の compose に置く。** 推論とは
  独立しているため —— ローカルの推論サーバでも Gemini でも、**chiezo が agent ループを
  回す相手**なら web 検索は要るのに、「答える」層の上書きに置いていた頃は、検索を
  使いたいだけで数 GB の推論サーバまで立ち上がっていた。**profile は付けない**
  (本体を上げれば立つ)。使うかは `CHIEZO_WEB_SEARCH_URL`(既定は空)が決めるので、
  立っているだけでは外へ検索を投げない。
  **使うのは chiezo が agent ループを回す相手だけ** —— CLI ブリッジ(claude / codex /
  antigravity)は CLI 自身の web 検索を引くので、ここは通らない(`agent.bridge_web_allowed`)。
  **使えない相手は設定から外す**(`use_default_settings.engines.remove`)——
  残すと起動のたびに登録に失敗して ERROR が並び、本当に見たい行がそこに埋もれる。
  `wikidata` は公開の SPARQL に 403 で断られる(**こちらは Wikipedia も Wikidata も
  手元に持っている**ので、外の口に頼る理由が無い)、`ahmia` / `torch` は Tor 越しなので
  必ず失敗する。**`X-Forwarded-For nor X-Real-IP header is set!` は消せない** ——
  `limiter: false` でも 1 リクエストにつき 1 行出るが、SearXNG はソケットの相手先に
  倒して処理を続けるので実害は無い(偽の `X-Forwarded-For` を送るほうが筋が悪い)。
  **設定は compose の `configs` で流し込む**(素の `searxng/searxng` を使い、
  独自イメージもマウントも作らない)。**焼き込みでもマウントでもいけない**:
  上流のイメージは `/etc/searxng` を **VOLUME 宣言している**ので、焼き込んだ設定は
  匿名ボリュームに隠れる(実際に 0 バイトの settings.yml に隠されて
  `KeyError: 'default_doi_resolver'` で起動しなくなっていた —— 既定の継承ごと
  失われるため)。マウントはリポジトリを置けない環境(単体定義)で使えない。
  `configs` はボリュームより深いパスへ置かれるので、どちらの問題も起きない。
  **VOLUME 宣言されたパス(`/etc/searxng`・`/var/cache/searxng`)には名前付きボリュームを当てる**
  (`chiezo-searxng-etc` / `chiezo-searxng-cache`)。名前が無いとコンテナを作り直すたびに
  名前の無いボリュームが 2 つずつ作られ、古いものが使われないまま溜まり続けていた。
  設定の中身は既定値 + Chiezo から API として引くための 3 点で、
  **SearXNG の既定は HTML しか返さない**ので `search.formats` に `json` を足してある。
  **`SEARXNG_SECRET` は渡さない** —— 上流の entrypoint はそれを読まず、
  settings.yml が既にあれば触らない(秘密の値は `configs` の中で決める)。
- **絵と音の生成の ComfyUI は `docker-compose.comfyui.yml`(profile `comfyui`)に置く**。
  GPU が要るので既定では立てない。**GPU が別マシンにあるなら重ねず、`CHIEZO_IMAGE_URL` で
  そちらを指す**(推論サーバと同じ逃げ道)。**モデルは同梱も自動取得もしない** ——
  数 GB あり、ライセンスも配布条件もモデルごとに違う。**絵・音・動画で別のファイルが要る**
  (音は `stable-audio-open`(+ `text_encoders/t5-base`)か `ace_step`。
  **動画は置き場そのものが違う** —— Wan は UNet 単体で配られるので
  `diffusion_models/` に置き、`text_encoders/umt5_xxl_*` と `vae/wan_2.1_vae` を別に読む。
  対応しているのは Wan 系だけで、3 つのうち 1 つでも欠けるとグラフごと通らない)。
  **置き場はホストの `./models/comfyui/ComfyUI/models/` の下**(上書きが `./models/comfyui` を
  コンテナの `/root` に繋ぐので、ComfyUI 本体が展開される 1 段下になる)。
  **イメージは日付つきのタグで固定する**(`cu126-slim-<日付>`。動くタグは毎週差し替わる)。
  **root のまま動かす** —— このイメージは `/root` を置き場の前提に組んであり、非 root に
  切り替える口が無い。代わりに **ComfyUI-Manager の `security_level` を起動のたびに `normal` へ
  戻す**(compose の `command` が `ComfyUI/user/__manager/config.ini` を書き戻してから起動する。
  `weak` だと画面や API から任意の拡張 = 任意のコードを入れられ、lan で 7014 を開けると
  LAN の誰でもそれができる)。設定の環境変数がイメージに無いので、この形になっている
- **GPU の設定は `docker-compose.llm.cuda.yml`(上書きファイル)に閉じる**。`gpus: all` は
  GPU の無い環境では起動そのものが失敗するので、本体の compose には書かない。
  **この上書きは NVIDIA 専用**(イメージが CUDA ビルドで、`gpus: all` も NVIDIA
  Container Toolkit の経路)なのでファイル名も `gpu` ではなく `cuda` にしてある。
  llama.cpp は同じビルド番号で rocm(AMD)・vulkan(ベンダー非依存)・intel などの
  イメージも出しているが、デバイスの渡し方が違う(ROCm は `/dev/kfd` と `/dev/dri`、
  Vulkan は `/dev/dri`)ため、対応するなら別の上書きファイルを起こすこと。
- **`docker-compose.standalone.example.yml` は「`.env` もシェルの環境変数も無い環境」向けの単体定義**。
  管理画面に YAML を貼り付けて起動するタイプの環境では `${...}` を解決できず、profile も
  付けられないため、値を直接書き・使うサービスだけを並べてある(chiezo-init + chiezo-app +
  chiezo-trigger + searxng)。
  **推論サーバは設定だけを載せ、コンテナは載せない** —— 別サーバーのものを指せば済み、
  この環境で同居させる前提が無いため(本体側と同じ「設定は残す・コンテナは外す」の分け方)。
  **SearXNG は載せる**(本体と同じく素のイメージ + `configs` なので、リポジトリが無くても立つ)。
  編集箇所は先頭の置き場アンカー(`x-data-volume` と、同じ親に揃える `x-corpus-volume` ほか)に
  集約し、**ホスト側は絶対パス**にする
  (貼り付けて登録する環境では相対パスの基準が読めない)。
  `docker-compose.yml` を変えたらこちらも追従させること —— 値が直書きなぶん古くなりやすい。
  **追従漏れは `tests/test_compose_files.py` が検知する**(本体の chiezo-app に渡している
  環境変数が、コメントとしてでも単体定義に出てくるかを照合。実際に 2 回取り残された)。
  **実値を書いたコピー(`docker-compose.standalone.yml`)は `.gitignore` 済み**。
  リポジトリに置くのは雛形(`.example`)だけで、置き場の絶対パス・接続先の IP を
  コミットしない(追跡したままだと、手元で書き換えたものが `git add` に巻き込まれる)。
