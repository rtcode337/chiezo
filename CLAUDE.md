# chiezo — ローカル知識サーバー

**AI のための知識ベース**。公開ダンプ(Wikipedia / OpenStreetMap / GeoNames)をローカルの
SQLite (FTS5) に取り込んで索引し、AI が引ける形で出す。完全ローカルで、レート制限も
問い合わせ内容の外部送信も無いことが存在理由。3 層で捉えるとよい:

- **ためる** — `ingest/` が公式ダンプを取り込み、ソースごとに独立した SQLite ファイル
  (`/data/<source>.db`)を作る。更新はブルーグリーン
- **取り出す** — `api/` が **MCP**(`/mcp`)と **REST**(`/v1/...`)の 2 経路で出す。
  Claude Code 向けには「いつ chiezo を使うか」を書いた CLAUDE.md ブロックも生成する
- **覚える** — `api/app/notes.py` が **chiezo で唯一書き込めるソース** `notes` を持つ。
  「覚えておいて」と言われたことを溜め、`recall` で新しい順に引く。CLAUDE.md や記憶ファイルと
  違い**常駐するのはツール定義だけ**なので、件数が増えてもコンテキストを食わない
- **答える(任意)** — `api/app/answer.py` が、ためた知識だけを根拠に回答を返す
  (`/v1/ask` と ブラウザの `/ask`)。推論は同居させず OpenAI 互換 API を叩くだけで、
  **`CHIEZO_LLM_URL` 未設定が既定 = 丸ごと無効**(配信側が数百 MB で動く前提を壊さないため)。
  `?mode=agent` では `api/app/agent.py` が MCP と同じ道具を LLM 自身に引かせる
  (GPU + 8B 級が前提なので**既定は 1 回検索する `rag`**)。会話として続けるのが
  `/v1/chat`(履歴はクライアントが持つ)、足りないぶんを外から補うのが
  `api/app/websearch.py`(既定では無効)。**この層は「chiezo を使う側」の実装**で、
  知識ベース本体は今までどおり外を叩かない

現在の収録ソースは日本語 Wikipedia = `jawiki`、OpenStreetMap 日本抽出 = `osm_japan`、
GeoNames 全世界地名辞典 = `geonames`(いずれも 348 言語版・195 か国から選んで増やせる)。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — ルーティング(/, /healthz, /apple-touch-icon.png, /v1/sources,
    /v1/{source}/search|doc|filter|tags|titles|links|random, /v1/ask, /v1/chat,
    /admin, /admin/init/{source}, /admin/rebuild/{source},
    /search/{source}/, /search/{source}/doc/{doc_id}, /localllm/chat、
    および MCP の /mcp(実体は下の `app/mcp_server.py`))
    - `/v1/{source}/filter` — 全文検索ではなく属性(`feature` / `area` / `bbox` / `wikidata` /
      `tag`)の AND での一括抽出(Overpass 相当)。`docs` の生成列への索引付き検索なので
      `schema_version` 2 以上が必要(1 の DB には 409)。条件は `build_attribute_filters()` が
      SQL 断片に変換し、`search` / `doc` からも同じ関数で併用できる
    - `tag`(= Wikipedia のカテゴリ等)だけは生成列ではなく転置表 `doc_tags` を引くので
      `schema_version` 3 以上が必要(2 の DB には 409 + `scripts/add_tag_index.py` を案内)。
      SQL は `docs.doc_id IN (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag IN (…))` の形で
      **書き方が性能に直結する**: `EXISTS (…)` にすると SQLite は docs 側を全走査して 1 行ずつ
      確認する計画を選び、jawiki 規模で数百倍遅くなる(実測 0.3ms → 100ms/30万件)。
      `IN (サブクエリ)` なら LIST SUBQUERY → rowid 検索に落ちる
    - **条件はまず「doc_id の集合」に落とす**(`build_doc_id_set()`)。`tag` → `doc_tags`、
      `bbox` → `doc_coords`、`feature`/`area` → `idx_docs_feature_area`、`wikidata` →
      `idx_docs_wikidata` と、どれも索引だけで doc_id を出せる(生成列でも**索引には
      計算済みの値が入っている**ので、doc_id を取り出す分には行本体が要らない)。
      複数条件は **`INTERSECT` で交差させる**: `doc_id IN (座標の集合) AND feature IN (…)`
      と書くと SQLite は片側の索引だけで駆動して**もう片方の判定に行本体を読む**
      (全国の `amenity=restaurant` 10 万行を読んで配信機で 5 秒 = 504 だった。
      `INTERSECT` なら 1/16 の時間で、行を読むのは交差した分だけ)
    - **`total` はその集合を数えるだけ**(`docs` を 1 行も読まない)。`docs` 側で
      `COUNT(*) … WHERE doc_id IN (…)` にすると該当 doc_id ごとに 41GB の `docs` へ
      rowid 検索が飛び、jawiki の「存命人物」25 万件で 33 秒。索引だけなら 0.01 秒
    - **並び替えは 2 経路あり、費用の形が違う**。`rank_index_hint()` が安い方を選ぶ:
      既定(名指し無し)は該当を全部 `docs` から読むので **`total` 件の行読み**、
      `INDEXED BY idx_docs_rank` は並び順の索引を上から走って `offset+limit` 件見つけた
      時点で打ち切るので **`doc_count * (offset+limit) / total` 件の索引走査**。
      **判定に `offset` を入れること**: 後者は `total` に依らず深い頁ほど伸び、末尾では
      索引の全走査に落ちる。総件数だけで切り替えていたときは、336 件のタグの
      `offset=300` や 131 件のタグの `limit=131` が 150 万件の全走査になって 504 だった
      (`offset=250` までは 0.665 秒で返っていたので、浅い頁だけ見ても気づけない)
    - **`bbox` は生成列ではなく `doc_coords`(4 以降)を引く**。lat/lon は VIRTUAL
      なので、`idx_docs_lat_lon` では緯度の範囲までしか絞れず、経度の判定に行本体を
      読み直す(被覆索引にならない)。費用が該当件数ではなく**その緯度帯にある全文書数**に
      比例し、0.05 度四方でも 3.5 万行を読んで配信機で 13 秒 = 504 だった。
      `doc_coords` は実体の値を持つので索引の中だけで完結する(実測 2.8 秒 → 0.04 秒)
    - `/v1/{source}/tags` — タグ名を文書数つきで列挙(`prefix` = 索引の範囲検索 /
      `contains` = 索引が効かない部分一致)。`filter?tag=` が完全一致なので、
      当てずっぽうで 0 件を掴む前に実在する名前を確かめるための窓口
    - `exact_title_first()` — 並びの第 1 段は**タイトルの完全一致**。bm25 は「その語を
      よく含む文書」を上げるが「その語そのものを説明している文書」を特別扱いしないため、
      `京都` の検索で記事「京都」が 5 位以内に入らなかった(長い記事ほど長さ正規化で不利)。
      人気度や関連度と混ぜず独立した段にしてある(完全一致が無いクエリでは何も起きない)。
      ORDER BY にパラメータを持つので、**呼び出し側は WHERE の後・LIMIT の前に検索語を
      渡す**必要がある(位置を間違えると静かに並びだけが狂う)
    - `relevance_order()` — 第 2 段は **bm25 に人気度(`rank_score`)を掛け合わせる**。
      bm25 は「良い一致ほど小さい負値」なので、係数を大きくするほど上位へ動く。
      重み `POPULARITY_WEIGHT`(0.4)は `scripts/fts_lab.py` で本番 jawiki 3 万件に対し
      0〜2 を振って決めた実測値で、2.0 まで上げると語の関連が薄い人気記事を拾い始める。
      `rank_score` を 0〜1 に丸めてから使うのは、入れ直していない geonames が人口の
      生値(最大 14 億)を持っているため。丸めれば全件 1.0 に張り付いて実質 bm25 のみに
      戻るので、古い DB でも壊れない(`scripts/refresh_rank_score.py` で入れ直せる)
    - `fts_search()` — **並べ替えの前に `docs` を読む件数を候補数で抑える**。素直に
      `docs_fts MATCH … JOIN docs ORDER BY <bm25 と人気度>` と書くと、並び替えに
      `rank_score`/`title` が要るせいで**該当した全文書**を読む。osm_japan の「東京都」は
      17 万件が該当し(施設の本文に「所在: 東京都…」が入る)、5 件返すのに 17 万行を
      読んで 504 だった = **都道府県名が軒並み引けなかった原因**。①bm25 だけで上位 N 件の
      doc_id を取り(FTS 索引の中で完結)②その N 件だけ `docs` と突き合わせる、の 2 段にする。
      ここで **`+docs_fts.rowid IN (…)` の単項 `+` は必須**(「この条件を索引に使うな」の指示)。
      付けないと SQLite は候補 1 件ごとに FTS の rowid 検索を選び、そのたびに doclist を
      たどり直して候補 300 件で 0.87 秒 → `+` 付きで 0.013 秒。
      候補の外は順位付けから漏れるので、タイトル完全一致だけは `idx_docs_title` から
      直接拾って候補に足す(関連度の運任せにしない)。絞り込み(`area`/`tag` 等)が
      付くときは候補を先に選べない(候補の中に条件を満たす文書が無いかもしれない)ため、
      従来どおり全件見る経路のままにしてある
    - `doc` は同名の別地物がある場合 `alternatives` を併記する(`fetch_doc_candidates()`)。
      OSM は「博多駅」のような名前が駅とラーメン店で衝突するため、黙って 1 件返すと
      取り違えに気づけない
  - `app/mcp_server.py` — **MCP サーバー(`/mcp`、Streamable HTTP・ステートレス)**。
    使い方は README「MCP から使う」節。ツールの実体は `app/main.py` のエンドポイント関数
    そのもので、MCP 用に処理を書き直していない(別実装にすると片方だけ直されて必ずずれる)。
    そのぶん**踏み抜きやすい罠が 3 つ**あるので、触るときは順に確認すること:
    - FastAPI のエンドポイントは既定値が `Query(...)` オブジェクトなので、Python から
      直接呼ぶときに**全パラメータを明示的に渡す**必要がある。渡し忘れると Query
      インスタンスが値として入り、`if tag:` が常に真になる等、例外にならず静かに壊れる。
      `tests/test_mcp.py::TestStaysInSyncWithRest` がシグネチャを突き合わせて落とす
    - FastMCP は**同期のツール関数をイベントループ上で直接呼ぶ**(await するのは async
      関数だけ)。chiezo のクエリは最大 5 秒ブロックしうるので、必ず `run_in_threadpool`
      に逃がす。でないと重いクエリ 1 本で API 全体が止まる
    - `TransportSecuritySettings` の既定は「localhost 系の Host しか受け付けない」。
      そのままだと LAN の別マシンから叩いた時点で 421 になるので、既定では検証を外し
      (REST 側も認証なし・LAN 内前提)、`CHIEZO_MCP_ALLOWED_HOSTS` で絞れるようにしている
    セッションマネージャは lifespan の中で `run()` する必要があり(python-sdk#1367)、かつ
    1 インスタンス 1 回しか呼べないため、**起動ごとに `build_mcp()` で作り直して**
    `app.state.mcp_asgi` に置き、マウント先(`main._mcp_asgi`)がそれを見る形にしている
    (モジュール読み込み時に作り置きすると、同一プロセスで二度起動するテストが落ちる)
  - `app/notes.py` — **「覚える」層(`/v1/notes`)の本体。chiezo で唯一書き込む場所**。
    使い方は README「覚える(notes)」節、なぜこの形かは `docs/design-notes.md`
    「「覚える」(notes)はなぜ chiezo に置くのか」が正。実装側の要点:
    - **`CHIEZO_NOTES_DIR` が機能フラグを兼ねる**(未設定 = 503、MCP の道具も出さない)。
      ツール定義は常時コンテキストに載るので、使えないものを並べない
    - **置き場を `/data` と分けるのは性能上の理由**。`registry.data_dir_fingerprint()` が
      `/data/*.db` の mtime/size を 5 秒ごとに見て、変われば**全ソース再走査(`COUNT(*)` 込み)**
      する。同じ場所に置くとメモ 1 件ごとに jawiki 150 万件の COUNT が走る
    - **読み手は `mode=ro`**(`db.set_mutable_paths` / `registry.Source.mutable`)。
      `immutable=1` は「開いている間このファイルは変わらない」という宣言で、SQLite は
      ロックも WAL 確認もしない。追記される DB をこれで開くと壊れたページを掴む
    - **`docs_fts` は external content 方式なので自動同期しない**。ingest が全件投入後に
      `INSERT INTO docs_fts(rowid, title, body) SELECT …` しているのと同じことを 1 件ずつやる。
      削除は `INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', …)` が要る
      (入れたときと同じ値を渡さないと索引に残る)
    - **DDL は `ingest/core.py` の写し**(api は ingest を import しない)。ずれると
      「notes だけ filter が 409」のように静かに壊れるので、`tests/test_notes.py` が
      ingest 側から作った DB と `sqlite_master` を突き合わせて落とす
    - `doc_count` は走査時の値だが notes は指紋に入らないので、書いた側
      (`main._refresh_notes_count`)で数え直す
    - **想起の主役は全文検索ではなく時系列の見込み**。「さっき話したあの件」は語が
      一致しないため、`recall` は `q` を省いて `updated_at` 順に引ける形にしてある
      (`idx_docs_updated` は notes だけが持つ。コアスキーマの追加ではないので
      `schema_version` は上げない)
  - `app/answer.py` — **「答える」層(`/v1/ask`・`/ask`)の本体**。使い方・環境変数は
    README「答える(ローカル LLM。既定では無効)」節が、なぜこの形かは
    `docs/design-notes.md`「「答える」層はなぜ 2 段の RAG か」が正。実装側の要点:
    - **`CHIEZO_LLM_URL` が機能フラグを兼ねる**(未設定 = 丸ごと無効、`/v1/ask` は 503)。
      推論はこのプロセスに入れない。ここがするのは OpenAI 互換の `/chat/completions` を
      叩くことだけで、モデルは別コンテナ(compose の profile `answer` の `chiezo-llm`
      = llama.cpp の `llama-server`)か LAN 上の別マシンにいる
    - **検索は `app/main.py` のエンドポイント関数をそのまま呼ぶ**(`app/mcp_server.py` と
      同じ方針。取り出し方を二重に持つと片方だけ直されて必ずずれる)。FastAPI の
      エンドポイントは既定値が `Query(...)` オブジェクトなので**全パラメータを明示的に渡す**。
      同期関数なので `run_in_threadpool` に逃がす(重いクエリ 1 本で API 全体を止めない)
    - **クエリ生成の段(LLM 1 回目)は `?source=` 指定時も省かない**。ソースを絞っても
      「質問文 → 検索語」の変換は残るからで、飛ばすと `app/fts.py` が質問文全体を
      1 フレーズにして 0 件になる(日本語は空白で切れない)
    - **`parse_plan()` は諦めながら落ちる**(素直な JSON → `{…}` 抜き出し → `"q"` の
      拾い出し → 質問文の最長断片)。小型モデルの JSON 出力は当てにならないので、
      1 段目の失敗で 500 にせず劣化経路で回答まで到達させる。何で引いたかは応答の
      `queries` に必ず載るので、劣化は呼び出し側から見える
    - **回答方針は `grounded` で切り替える**(既定 1 = 抜粋のみ)。「抜粋だけ」は chiezo の
      設計思想ではなく**モデルの幻覚への対処**なので固定しない — chiezo は AI 用の知識ベースで、
      ローカル LLM はそれを使う側。ただし `grounded=1` で抜粋 0 件のときは `has_no_basis()` が
      **推論を走らせず定型文を返す**。実測で gemma-3-1b が「抜粋が空でも自分の知識で答える」
      ことを確かめたため、プロンプトに委ねず経路として断ってある
    - 数値の環境変数は `_env_num()` で読む。compose は未設定の変数を `VAR=`(空文字)で
      渡すため、素直に `float()` すると「.env に書いていない」だけで 500 になる
    - ストリーミング(`?stream=1`)は**クエリ生成・検索を流し始める前に済ませる**
      (`prepare()` → `stream_answer()`)。SSE はヘッダ送出後にステータスを変えられない
    - `content_of()` が**思考タグの残骸を落とす**。thinking 系モデルは推論サーバの設定次第で
      `<think>…</think>` や閉じタグだけが `content` に残る(実測: Qwen3 + 思考オフで先頭に
      `</think>`)。相手の設定は chiezo が握っていないので受け側で落とす
  - `app/agent.py` — **agent モード(`/v1/ask?mode=agent`)の本体**。LLM 自身に道具を
    引かせるループ。使い方・環境変数は README「agent モード(モデルに道具を引かせる)」節が、
    なぜこの形かは `docs/design-notes.md`「agent モード: 道具をモデルに引かせる」が正。
    実装側の要点:
    - **道具の定義も実行も `app/mcp_server.py` から借りる**(`list_tools()` → OpenAI の
      function 形式、実行は `call_tool()`)。書き写すと REST・MCP・agent の三重管理になる。
      システムプロンプト前半の使い方も MCP の `INSTRUCTIONS` をそのまま使う。
      `tests/test_agent.py` が `AGENT_TOOLS` と MCP のツール名を突き合わせて落とす
    - 道具は 2 群に分かれる。`KNOWLEDGE_TOOLS`(読み取り専用。常に渡す)と
      `NOTE_TOOLS`(`remember` / `recall`)。**後者は chiezo で唯一の書き込みを含む**ので、
      `notes_allowed()` が「notes が有効 かつ リクエストで切られていない」ときだけ渡す。
      当初は書き込みを一切渡していなかったが、会話で「覚えておいて」と明示的に頼まれるなら
      副作用ではないので渡す。代わりに**やり取りごとに切れる**(画面のトグル・`notes=0`)、
      **何を書いたかは step に出る**、の 2 つで見えるようにしてある
    - 上限は 3 つ(`CHIEZO_AGENT_MAX_STEPS` / `_TOOL_CHARS` / `_TIMEOUT`)。**予算を
      使い切っても打ち切らず**、道具を渡さずにもう 1 回だけ聞いて答えさせる(調べただけで
      終わらせない)。**同じ引数の呼び出しは実行せず突き返す**(小型モデルは 0 件のクエリを
      投げ直してステップを空回りさせる)
    - **道具の失敗はモデルに返す**(`execute()` は例外にしない)。404 の candidates は
      次の手の材料になる。ToolError の文言には FastMCP の前置きが付くので
      `_tool_error_payload()` で剥がしてから渡す
    - **最終回答はストリーミングしない**。ツール呼び出しかどうかは応答を途中まで読まないと
      分からず、断片から復元すると壊れやすい。代わりに `step` イベントで進捗を流す
    - 出典は道具の応答に出てきた文書を出現順に集めたもの。**本文の番号とは対応しない**
      (生の応答に番号を振る先が無いため)。web の結果は `source: "web"` として同じ一覧に
      混ぜる(どれが外から来たか出典を見た人に分かる必要がある)
    - **web 検索の道具だけは MCP から借りない**(`app/websearch.py` で定義)。chiezo の MCP は
      「ためた知識の引き口」であって web はその外側だから — MCP の利用者(Claude Code)は
      自前の web 検索を持っている。有効なときだけ道具一覧に足す
  - `app/websearch.py` — **web 検索の道具(既定では無効)**。`CHIEZO_WEB_SEARCH_URL` が
    機能フラグを兼ねる(未設定 = 道具ごと出さない)。**使うかどうかはやり取りごとに選べる**
    (`agent.web_allowed()`: サーバー設定 AND リクエストの `web` が false でない)。
    画面のトグルは毎回これを送る。使い方は README「web 検索で足りないぶんを
    補う」節が正。実装側の要点:
    - **これは「答える」層(= chiezo を使う側)の機能で、chiezo 本体の機能ではない**。
      知識ベースそのものは引き続き外を叩かない。この整理を崩さないこと
    - **本文は取りに行かない**(タイトル・要約・URL だけ)。ページ取得はスクレイピングに
      踏み込む話で、相手への負担も壊れやすさも別次元になる
    - `MIN_INTERVAL` で**自分でレート制限をかける**。ツールループはモデルの気分で何度でも
      呼ぶので、呼ばれた回数ぶん素直に外へ出さない
    - `USER_AGENT` に**個人情報を入れない**(名乗るのはプロジェクト名だけ)。
      `tests/test_chat.py` が固定している
  - `app/registry.py` — /data 走査・ソース登録、`SUPPORTED_SCHEMA_VERSIONS` /
    `FILTER_MIN_SCHEMA_VERSION` / `TAG_MIN_SCHEMA_VERSION`
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
  - `app/known_sources.py` — `chiezo-trigger` が未設定・到達不能なときの控えの既知ソース一覧と、
    国選択画面の大陸表示名・言語選択画面の記事数階層(`WIKIPEDIA_TIERS`)。
    初期化できるソースの正は ingest 側の `ADAPTERS` で、通常は
    `chiezo-trigger` の `GET /sources` から受け取る(`main.initializable_sources()`。
    osm 国別 195 件 + wikipedia 言語版 348 件あり、api 側に複製すると必ず腐るため)
  - `app/pages.py` — 管理画面・ブラウズ画面共通の HTML 組み立てヘルパー(`page_shell`, `esc`)と、
    画面の URL(`browse_url` / `doc_url`。出典のリンクもここを通すので、移すときに漏れない)。
    ファビコンは `assets/icon.svg` を最小化した data URI(`FAVICON_DATA_URI`)として埋め込む
    (api イメージのビルドコンテキストは `api/` のみで `assets/` を含まないため。原本を変えたら更新)。
    iPhone の「ホーム画面に追加」用に 180×180 の PNG(`APPLE_TOUCH_ICON_PNG`)も持ち、
    `page_shell` が `<link rel="apple-touch-icon">` を出す + `main.py` が
    `/apple-touch-icon.png` で配信する — iOS は SVG や data URI のファビコンをホームアイコンに
    使わないため。角丸マスクは iOS が自前で掛けるので角丸なし・全面塗りで描いてある
    (再生成手順は README「開発 > アイコンを変えたとき」節が正)
  - `/`(GET) — `/admin` へ 302 リダイレクト
  - `/admin`・`/admin/osm`・`/admin/wikipedia`(GET) — 簡易 HTML 管理画面(画面に何が出るかは
    README「API の使い方」節の後半、管理画面の説明が正)。実装側の要点は 3 つ: ジョブ実行中は `page_shell` の
    `refresh` で 5 秒ごとに自動リロードする / `osm_<国>` 195 件と `<lang>wiki` 348 件は
    そのまま並べると他のソースが埋もれるので `group` で 1 行に畳み、国・言語の選択だけを
    `/admin/osm`(大陸ごとの `<details>`)・`/admin/wikipedia`(`WIKIPEDIA_TIERS` の記事数階層ごと)
    へ切り出す / `?q=` の絞り込みは JS なしのサーバ側フィルタ
  - `/admin/init/{source}`(POST) — `chiezo-trigger` の `POST /run/{source}` へプロキシし、
    `/admin` へ 303 リダイレクト。`CHIEZO_TRIGGER_URL` 未設定なら 503、未知ソースなら 404、
    登録済みソースなら 409
  - `/admin/rebuild/{source}`(POST) — 登録済みソースの再構築(init と同じプロキシで、
    要求条件だけ逆: **登録済みであることを要求**し、未登録は 404。未登録の取り込みは init 側)。
    ソースの正は trigger 側の `ADAPTERS` なので、カタログに無い登録済みソースでも trigger に
    判断を委ねる。管理画面は「最新のスキーマバージョン」(`latest_schema_version()` —
    trigger の `GET /sources` が返す `schema_version` = ingest の `core.SCHEMA_VERSION`。
    取れなければ api の `max(SUPPORTED_SCHEMA_VERSIONS)` で代替)を表示し、
    それより古い DB の行に注意書きを付けて再構築を促す
  - `/admin/claude-config`(GET/HTML)・`/admin/claude-config.txt`(GET/text/plain)・
    `/admin/claude-config.permissions.json`(GET/application/json)・
    `/admin/claude-config.mcp.json`(GET/application/json)・
    `/admin/claude-config.hook.py`(GET/text/x-python)・
    `/admin/claude-config.hook.json`(GET/application/json) —
    Claude 連携設定の生成 API。現在の登録ソースから CLAUDE.md ブロックと
    **権限ファイル(`settings.json`/`settings.local.json` の `permissions.allow`)**、
    および任意で入れる**自動許可フック**を生成して配信する(実ファイルは書き換えない。
    ホームの `~/.claude/CLAUDE.md` 等はクライアント側にあり api からは見えないため)。
    `gen_claude_config.sh` はこれらのエンドポイントを取得して書き込む。
    HTML はプレビュー + コピーボタン付き。
    curl 例・許可ルールのベース URL はアクセス元 URL のプロトコル・ホスト名・ポート
    (`request_origin`: スキーム=`X-Forwarded-Proto`(あれば)、ホスト=`X-Forwarded-Host`
    (あれば)→無ければ `Host` ヘッダ。`Host` はポートを保持する)から導出するので、
    リバースプロキシ越し・非標準ポートでもそのまま到達可能な URL になる
    - `.txt` の `?hook=1` は「自動許可フックを入れる前提の書き方の指示」を本文に足す。
      フックはクライアント側で `--with-hook` を指定したときしか入らないので、
      既定で書くと入れていない環境には嘘になる。設置するときだけ付けて取りに来る。
      `?mcp=1` は MCP 登録済み環境向けの使い分けの指示(単発の参照は MCP・大量取得は
      curl)を足す。**MCP 登録はスクリプトの既定**なのでこちらは通常付いてくる
      (`--no-mcp` のときだけ落ちる。HTML プレビューも既定に合わせて `mcp=True` で出す)
    - `.mcp.json` は MCP サーバー登録の断片(`mcpServers.chiezo` → `<base>/mcp`)。
      プロジェクト用 `.mcp.json` へのマージやユーザースコープの `claude mcp add` は
      スクリプト側の仕事(登録名の正は `claude_config.MCP_SERVER_NAME`)
  - `app/hooks/chiezo_autoallow.py` — 上記フックの実体(`PreToolUse` フック)。
    Claude Code の `permissions.allow` は**コマンド文字列の前方一致**でしか判定できず、
    `for … do curl … done` やパイプに包まれた curl には 1 本もマッチしない。
    大量取得は必ずその形になるので、いちばん許可したい場面でルールが効かない。
    このフックは前方一致ではなく**構造**(登場する URL が全て chiezo か・コマンド位置に
    来る語が読み取り専用の許可リスト内か・`$(…)`/`eval` 等でコマンド位置を隠していないか)
    で判定し、条件を満たすときだけ `permissionDecision: "allow"` を返す。
    外れたら**何も出力しない**(= 通常の許可プロンプトに戻る)ので、判定に迷ったら
    黙るのが正。api プロセスからは import も実行もされず、`claude_config.hook_script()`
    がソースを読んで `CHIEZO_ORIGIN` の行だけ差し替えて配信する。文字列テンプレートに
    せず実ファイルで持つのは、通常の Python として lint・テストできるようにするため
    (`tests/test_claude_hook.py` が判定の許可/拒否を両側から固定している)
  - `app/claude_config.py` — 上記ブロックの生成ロジック。**生成の正はここ(api 側)に置く**。
    同一プロセスの DB を schema_version と索引付きの `WHERE lat IS NOT NULL LIMIT 1` 等で
    直接引くので、HTTP プローブと違い巨大ソース(jawiki)でも timeout の false-negative が出ない。
    ただし**索引の無い列を同じ形で探ってはいけない**: `links` は生成列でも索引付きでもないので
    `WHERE links IS NOT NULL LIMIT 1` は 1 件も無いソースほど全表を舐める(geonames 1300 万件で
    3.2 秒)。`_has_links()` は先頭 `_LINKS_SAMPLE_ROWS` 行だけ見て判定する
  - `/search/{source}/`(GET) — 検索フォーム(HTML)。**画面はすべて前置きの下に置く**
    (`/admin`・`/search/…`・`/localllm/…`)。以前はソース名をそのままルート直下に置いていて、
    ルートがキャッチオールになるため `ask` や `admin` という名前のソースを足せなかった。
    URL の組み立ては `app/pages.py` の `browse_url()` / `doc_url()` に閉じてある。
    `?q=` 未指定時は一覧を出さずフォームのみ表示
    (jawiki 等の大規模ソースで rank_score 順の全件一覧がフルスキャンとなりタイムアウトするため)。
    `?q=` 指定時は結果一覧を表示し、`/v1/{source}/search` と同じロジック
    (FTS または短語のタイトル前方一致フォールバック)
  - `/search/{source}/doc/{doc_id}`(GET) — 文書詳細(title/tags/opening/body/links/extra)の HTML 表示
  - `/v1/notes`(POST)・`/v1/notes/recall`(GET)・`/v1/notes/{doc_id}`(DELETE) —
    「覚える」層の REST。読み出しはコアスキーマなので `/v1/notes/search|doc|filter|tags` と
    `/search/notes/` のブラウズ画面もそのまま効く(専用の口は追記・削除・時系列の想起だけ)
  - `/v1/ask`(GET) — 「答える」層の REST。`stream=0`(既定)は JSON 一括、`stream=1` は
    SSE(`references` → `delta` × n → `done`、失敗時は `error` を挟む)。
    無効なら 503、推論サーバに繋がらなければ 502、タイムアウトは 504。
    `mode=agent` は `app/agent.py` のループへ回す(SSE は `meta` → `step` × n →
    `references` → `delta` → `done`。**流し始める前に済ませられる検査はソースだけ**なので、
    それだけ `prepare_catalog()` で先に通し、残りの失敗は `error` イベントになる)
  - `/v1/chat`(POST) — 会話の口。`messages` の**末尾が今回の発言、それより前が履歴**
    (末尾が user でなければ 400)。**サーバーは会話の状態を持たない** — 履歴はクライアントが
    持って毎回送る(読み取り専用・LAN 内・複数ワーカーの前提を崩さないため。MCP を
    ステートレスにしたのと同じ判断)。rag / agent とも `/v1/ask` と同じ実装に流す
  - `/localllm/chat`(GET) — 会話画面と、JS なし用の 1 問 1 答の HTML。**ローカル LLM を使う側の
    機能なので `/localllm/` の下**(chiezo 本体の画面と並びで区別する)。見た目は
    **この画面だけ作り込んである**(`app/pages.py` の `CHAT_STYLE` を `page_shell(style=…)` で
    上乗せ。管理画面・ブラウズ画面は素っ気ないままでよいので、CSS を混ぜない)。
    入力欄は数行ぶんの高さを持ち、設定(ソース・引き方・根拠・web 検索)はその下に並ぶ。見出しは
    **`AI(<モデル名>)と話す`**(`answer.model_label()`。`CHIEZO_LLM_MODEL` が無ければ
    推論サーバの `/models` に聞き、5 分覚える。取れなければ「AI と話す」)。
    **chiezo は AI が引く知識であって AI 自身ではない**という関係を画面にもプロンプトにも
    出すため。既定はサーバ側で推論を回さず、inline JS(`CHAT_JS`)が `/v1/chat?stream=1`
    を叩いて埋める — ここでサーバ側でも回答を作ると推論が二重に走る(数十秒 × 2)。
    **`EventSource` ではなく `fetch` で SSE を読む**のは、履歴を送るのに POST が要るため
    (EventSource は GET しか張れない)。会話の履歴を持つのもこの JS。JS が無い環境向けに
    `?nojs=1`(1 問 1 答・出揃ってから表示)への導線を `<noscript>` で出す。
    **この画面だけ JS を使う**のは、数十秒無反応で待たせる体験を避けるためと、
    会話の主体がクライアント側だから(他の画面は従来どおり JS なし)
- `ingest/` — **chiezo-ingest**: ワンショット構築バッチ。
  - `main.py` — 共通フレーム: 取得 → `.building` へ構築 → FTS → 検証 → ブルーグリーン切り替え(シンボリックリンク差し替え、旧世代 1 つ保持)。
    アダプタが `EXTRA_FETCH_HOOKS`(`fetch_pageviews` / `fetch_page_props`)を持つ場合、
    `fetch()` の後にそれも呼ぶ(docs.extra 補強用の追加データ取得フック)。
    構築用 SQLite のページキャッシュは `BUILD_CACHE_KIB`(`build_pragmas()`)で
    プロファイル別: low_memory(既定)64MiB / fast 512MiB(下記「メモリ方針」参照)
  - `lookup.py` — 取り込み中だけ参照する巨大な「ID → 値」対応表(リダイレクト・ページビュー・
    wikidata の Q 番号)を、メモリではなくディスク上の一時 SQLite に持つための小道具
    (`DiskLookup` / `DiskMultiMap` / ヌルオブジェクト `EMPTY`)。以前これらを dict で抱えて
    ja Wikipedia 規模(各 160〜190 万件、合計 GB 級)になり、XML 本体解析と同時に生きるため
    ホストごと OOM killer に巻き込まれて落ちた。常駐メモリは SQLite のページキャッシュ
    上限(既定 32MiB)に固定される。中断時にも消えるよう `iter_docs` の finally で必ず close する。
    これにより wikipedia 系の取り込みは軽くなり、必要メモリは 3GiB 見当に収まる
    (下記「メモリ方針」を参照)
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)。`SCHEMA_VERSION` は 3。
    構築プロファイル(`build_profile()` / `is_low_memory_build()`、環境変数
    `BUILD_PROFILE`)もここに置く — main(PRAGMA)と各アダプタ(必要メモリ宣言・
    osm の索引方式)の両方が参照するため。未知の値は fast に黙って倒さず `SystemExit`
    (綴り間違いを「指定したのに 12GiB 要求される」で気づかせないため)。
    `rank_score` は **全ソース共通で 0.0〜1.0** という約束(`normalized_popularity()` で
    対数正規化する)。API が bm25 に掛け合わせて並べるため、ソースごとに桁が違うと
    混ぜられないから。人口の対数上限だけは都市規模(osm、1 億)と国家規模(geonames、
    100 億)で分けてある — geonames は国そのものを 1 文書として持ち、同じ係数だと
    1 億超の国が全部 1.0 に張り付いて上位の区別が消えるため。
    `docs` に生成列(VIRTUAL)`feature` / `area` / `lat` / `lon` / `wikidata` を持ち、
    実体は従来どおり `extra`(JSON)から `json_extract` する射影+索引でしかない。
    アダプタ側は `extra` に詰めるだけでよく、`Doc` の形は変わらない。
    3 で足した `doc_tags`(タグ → 文書の転置表)も同じ精神で `docs.tags`(JSON 配列)の
    射影でしかなく、`DOC_TAGS_POPULATE_SQL` が docs 投入後に `json_each` で一括展開する
    (生成列にできないのは 1 文書が複数タグを持ち 1 行に畳めないため)。
    行ごとに append せず docs から作り直すのは、`docs` 側が `INSERT OR REPLACE` を使う
    (同じ doc_id が二度来たら最後の 1 件が残る)ため。同じ SQL を
    `scripts/add_tag_index.py`(既存 DB の 2 → 3 移行)も使う。
    4 で足した `tag_counts`(タグ → 文書数)は `doc_tags` をさらに畳んだ要約で、
    新しい情報は持たない。**分けてある理由は配信側の読み取り量**: 「どんなタグがあるか」
    を探す `/v1/{source}/tags` は転置表側だと jawiki で 764 万行・索引 300MB を読むが、
    こちらは 29 万行・12MB で済む。配信機は数百 MB メモリの小型機で毎回ディスクから
    読むため、この差がそのまま応答時間になる(部分一致が 5 秒のタイムアウトを超えて
    504 になっていた)。
    同じく 4 の `doc_coords`(座標 → 文書)は `docs` の生成列 lat/lon と同じ値の写し。
    **生成列の索引では bbox が引けない**のが理由で、VIRTUAL(値を保存しない)ぶん
    SQLite が索引の値を使えず、経度の判定に行本体を読み直してしまう。実体の値を持つ表に
    写して初めて、緯度帯の走査も経度の判定も索引の中で終わる
  - `sources/wikipedia.py` — Wikipedia 標準 XML ダンプアダプタ(`wiki_id` パラメータ化。
    全言語版は下の `wikipedia_editions.py` から機械的に登録される。pageview ドメインは
    URL 言語コードから `<lang>.wikipedia` を導出し、不規則な wiki だけ `WIKI_DOMAIN` で上書き)。`https://dumps.wikimedia.org/<wiki_id>/<date>/<wiki_id>-<date>-pages-articles.xml.bz2`
    (MediaWiki エクスポート形式、単一ファイル)を取得する。CirrusSearch ダンプの `text` を
    使っていた旧実装から標準 XML ダンプ + wikitext 解析(`mwparserfromhell`。wikipedia 系
    ソースのみの例外的依存)へ切り替えた経緯は README「ダンプ更新」節を参照(要点: `text` は
    折りたたみセクションを検索インデックスから除外しており本文が欠落していた)。
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
    (`WIKI_DOMAIN` 未登録の wiki_id では突合をスキップ)。あわせて `page_props` SQL ダンプから
    `wikibase_item`(wikidata の Q 番号)を正規表現で拾い `extra.wikidata` に入れる
    (`filter?wikidata=` での逆引き用。OSM 側の `wikidata` タグと突き合わせられる)。
    記事の代表座標も wikitext から取り、`extra.lat` / `extra.lon` に入れる
    (`{{Coord}}` 系テンプレートの位置引数・度分秒に加え、駅や空港の Infobox が持つ
    `緯度度`/`経度度`・`latd`/`longd` のような名前付き引数にも対応。Wikidata の P625 を
    引くには別途巨大なダンプが要るため、本文だけで完結させている)。
    カテゴリ(`[[Category:X]]`)は `Doc.tags` に入れる(wikilink のリンク先から取るので、
    `[[Category:ラーメン店|らあめんしろう]]` のようにソートキーが付いていてもカテゴリ名が
    取れる)。**本文(`body`)側はソートキー付きだと表示テキスト側しか残らずカテゴリ名が
    消える**ため、カテゴリの全記事列挙を全文検索(`search?q=Category:…`)で代用すると
    静かに取りこぼす(実例: ラーメン二郎が漏れた)。列挙は `filter?tag=` を使う
  - `sources/osm.py` — OpenStreetMap アダプタ(`region` パラメータ化、Geofabrik の
    `<region>-latest.osm.pbf` を pyosmium(libosmium バインディング)で解析)。
    Geofabrik が 2026 年に `.osm.bz2` 配布を終了し `.osm.pbf` のみになったため、標準ライブラリの
    `xml.etree` では読めなくなった。osm 系ソースに限り pyosmium への依存を許容している
    (それ以外は標準ライブラリのみの方針を維持。wikipedia 系ソースの mwparserfromhell が
    もう1つの例外、上記参照)。
    取り込む地物の種類(地名 + POI + 交通インフラ。いずれも `name` タグ必須)は README
    「地理データの守備範囲」節が正。地名と POI は同じ docs/docs_fts に混在し `search` は
    両方をヒットさせる。交通インフラだけ `VALUE_LIMITED_KEYS`(`railway` / `aeroway` /
    `aerialway` / `public_transport` / `highway` / `man_made`)で値を列挙するのは、
    `railway=rail` や `highway=residential` のような、名前付きでも地点辞典として意味を
    成さない線形地物を除くため。`POI_KEY_SCORE` では交通インフラを POI より高く置く
    (「博多駅」で同名の飲食店を先に返す取り違えの防止)。
    OSM は node → way → relation 順で並ぶため 3 パスで読む: パス1
    (`_RelationScanHandler`)で対象 relation が参照する way ID と、`extra.area` 用の
    行政境界(既定 `admin_level=4`)relation のメンバー way を集め、パス2
    (`_AreaWayHandler`)で境界 way のノード座標から点内包判定器(`_AreaIndex`。緯度バンド
    索引付きレイキャスティング)を組み立て、パス3
    (`_MainHandler`)で pyosmium の `NodeLocationsForWays` によるノード座標自動解決を
    使いながら node/way/relation を走査して Doc を生成する。この位置インデックスの置き場は
    `node_index_kind`(環境変数 `OSM_NODE_INDEX` > `BUILD_PROFILE` > ソースごとの既定)で
    決まる。既定プロファイル(low_memory)ではディスクの `sparse_file_array`
    (`<dumps>/<source>.nodeloc.idx` へ退避。必要メモリは 2GiB まで下がる代わりに
    ノード解決がランダム読みになり数倍〜10倍遅い)。`BUILD_PROFILE=fast` では RAM 上の
    `sparse_mmap_array` で、これが最速(参照ノードぶんの座標=1件16B程度を抱えるため
    日本抽出で5〜10GB。足りるかは `require_build_memory()` が事前検査する)。
    ファイル索引の一時ファイルは `iter_docs` の finally で
    必ず unlink する(中断時も残さない。`ingest/lookup.py` と同じ精神)。座標を持つ Doc には所属行政区を
    `extra.area` として付ける(bbox 近似ではないので県境のはみ出しが無い)。境界ポリゴンは
    relation より前に必要なため専用パスを挟んでいる(`OSM_AREA_ADMIN_LEVEL=0` でパスごと省略可)。
    relation の label / admin_centre ノード座標は位置インデックス (`idx.get(node_id)`) に直接問い合わせて解決し、
    それ以外は構成要素の平均(近似重心)。pyosmium はコールバック駆動のため、別スレッドで
    `osmium.apply()` を回し `queue.Queue` 経由で Doc をジェネレータへ橋渡しする(メモリ抑制)。
    docs.title の UNIQUE 制約に合わせ、同名地物は先勝ちで「名前 (node:123)」形式に弁別し
    元の名前を alias に残す。既出判定は `set[str]` ではなく固定 256MiB のビットフィルタ
    (`_TitleBloomFilter`)で行う。かつて大陸単位の抽出(名前付き地物が数千万〜億件規模)を
    試した際、全タイトル文字列を素朴に `set[str]` へ貯めるとメモリを食い尽くして
    スワップで暴走したため、コーパス規模によらず固定メモリで済む方式に切り替えた。
    ビットフィルタは誤検出(未出現なのに「出現済み」と判定)のみで見逃しは原理上
    起きないため、UNIQUE 制約違反にはならず、稀に不要な弁別が付くだけに留まる。
    POI は `addr:*` タグから `docs.extra.address`、
    `phone` / `website` / `opening_hours` 系タグから同名の extra フィールドも拾う。
    ソース名の区切りはアンダースコア
    (`osm_japan`。ハイフンは世代ファイル名 `<source>-<date>.db` と衝突するため不可)
  - `sources/geonames.py` — GeoNames アダプタ(全世界の地名辞典)。
    `allCountries.zip`(約400MB・約1,200万件のタブ区切り19列)を zip のままストリーム読みする
    (イメージに unzip を入れないため `zipfile` + `io.TextIOWrapper`)。あわせて
    `alternateNamesV2.zip`(多言語別名)・`countryInfo.txt`(国コード→国名)・
    `admin1CodesASCII.txt`(1次行政区)を `fetch_extra()` で取得する。別名は 2,000 万行規模に
    なるため `lookup.py` の `DiskMultiMap` でディスクへ逃がし、`iter_docs` の finally で必ず消す。
    `isolanguage` が `wkdt` の行は wikidata の Q 番号なので `extra.wikidata` に入れ、jawiki 側と
    突き合わせられるようにする。GeoNames は本文を持たないため、FTS が効くように
    「名前(行政区, 国)— コード / 分類、人口 N」という 1 行を組み立てて `opening`/`body` に入れる。
    `extra.feature` は OSM と同じ key=value 形式に揃える(`P=PPLC` 等)。`rank_score` は人口。
    **同名地名の解決が必須**(`_TitleOwners`)。`docs.title` は UNIQUE で、しかもその索引は
    全行 INSERT 後に張られる(`CORE_INDEX_DDL`)ため、重複を放置すると 1,100 万行入れた後に
    `UNIQUE constraint failed: docs.title` で落ちる。GeoNames は同名が大量にある
    (Paris は仏/テキサス/オンタリオ、Springfield は数百件)。そこで **2 パス**にし、
    パス1 で「名前 → 人口最大の geonameid」を決め(同数なら geonameid が小さいほう。
    実行ごとにぶれないため)、パス2 で代表だけが素の名前を名乗り、それ以外は
    `名前 (国コード:geonameid)` に弁別して元の名前を alias に残す(osm の
    `名前 (node:123)` と同じ方針だが、あちらの「先勝ち」と違い**人口で代表を選ぶ** —
    ファイル順は geonameid 順でしかなく「Paris と言えばフランス」を選べないため)。
    この対応表も 12M 件規模になるのでディスクの一時 SQLite に置く。
    既定では道路(feature class `R`)を除外し、別名は `ja,en` のみ拾う
    (`GEONAMES_FEATURE_CLASSES` / `GEONAMES_ALT_LANGS` で変更可)。
    **大陸単位の OSM 抽出(旧 `osm_europe`)はこれに置き換えて廃止した**。理由は
    「地理データの守備範囲」(README)参照 — 実測で osm_japan の 73% は店舗・施設の裾で、
    全世界の地名を得る手段としては桁違いに非効率だった。
  - `sources/__init__.py` — アダプタレジストリ(新ソースはここに 1 行追加するだけ。
    管理画面には `chiezo-trigger` の `GET /sources` 経由で自動的に出るので、
    `api/app/known_sources.py` への複製は不要)。
    `osm_<国>`(下の `osm_regions.py` から 195 件)と `<lang>wiki`(下の
    `wikipedia_editions.py` から 348 件)だけは例外で、自動生成カタログから機械的に登録している。
    `load_plugin_adapters()` は **このリポジトリに入れられないソース(社内 wiki 等)を
    別リポジトリのモジュールから差し込む唯一の口**(環境変数 `CHIEZO_SOURCE_PLUGINS` に
    モジュール名をカンマ区切り。そのモジュールの `ADAPTERS` を取り込む)。使い方は
    README「ソースの追加・削除」と `docs/adding-a-source.md` のケース 3 が正。実装側の要点:
    - **壊れた指定は握り潰さず `SystemExit`**(import できない / `ADAPTERS` が無い /
      生成関数でない)。黙って無視すると「管理画面に出ない」「unknown SOURCE」として
      後から現れて原因が分からない。opt-in の設定なので、起動時に落ちるほうが分かりやすい
      (chiezo-trigger が起動しないのも、カタログが静かに欠けているより気づける)
    - **既存ソースと同名は拒否する**。上書きを許すと `jawiki` を影で差し替える取り違えに
      気づけない(名前を変えれば済むので安全側に倒す)
    - **ソース名は `[A-Za-z0-9_]` のみ**。ハイフンは世代ファイル名 `<source>-<date>.db` の
      区切りと衝突し、取り込みは通るのにブルーグリーン切り替えの段で壊れるため先に弾く
    - 差し込みは ingest 側だけで完結する。**api は変更不要**(ソース種別を意識しない設計なので、
      コアスキーマの `.db` が `/data` にあれば全エンドポイントがそのまま効く)。
      compose は `chiezo-ingest` と `chiezo-trigger` の両方が `CHIEZO_INGEST_IMAGE` を
      見るので、継承イメージを指すだけで管理画面の初期化・再構築も効く
  - `sources/osm_regions.py` — **自動生成物**。Geofabrik の国別抽出カタログ
    (`scripts/gen_osm_regions.py` で再生成。Geofabrik の index-v1.json + 大陸別 HTML の
    pbf サイズ + CLDR の国名/主要言語から起こす)。1 件あたり region パス・日本語表示名・
    lang・pbf サイズ・必要メモリの目安・既定のノード座標索引・検証の最低文書数を持つ。
    手で 195 行書くと region パスの綴り間違いにダウンロード時まで気づけないため機械生成にした。
    `memory_gb` は pbf 1GB あたり 5GiB(osm_japan の実績: pbf 2.3GB → 12GiB)、
    それが 12GiB を超える国は `node_index` をディスク索引にして「どのソースも 12GiB のマシンで
    構築できる」方針を保つ(fast プロファイル時の話。既定の low_memory では全ソースが
    ディスク索引・2GiB に収まる)
  - `sources/wikipedia_editions.py` — **自動生成物**。Wikipedia 言語版カタログ
    (`scripts/gen_wikipedia_editions.py` で再生成。Wikimedia sitematrix + wikistats の記事数 +
    CLDR の言語名日本語表記から起こす)。1 件あたり URL 言語コード(`lang`。pageview ドメインの素)・
    dbname(`wiki_id` = ソース名。ダンプ URL の素)・日本語/英語の言語名・自称・記事数・
    検証の最低文書数(記事数の 50%)を持つ。言語コードは 2 系統ある点に注意
    (sitematrix は現行コード yue、URL・pageview・wikistats は歴史的コード zh-yue。
    `lang` には URL コードを入れてある。dbname はハイフンを含まない zh_yuewiki 形式なので
    世代ファイル名の区切りと衝突しない)
  - `server.py` — **chiezo-trigger**: 管理画面の初期化・再構築ボタンから叩かれる内部専用トリガー。
    ingest イメージを流用し、docker-compose.yml で CMD だけ `uvicorn server:app` に上書きする
    常駐コンテナ(`/data` に書き込み権限を持つ点だけ chiezo-ingest の one-shot 実行と異なる)。
    `POST /run/{source}` で `main.run(source, data_dir)` をバックグラウンドスレッドで実行し
    (同時実行は 1 ジョブまで、429/409 で拒否)、`GET /sources` で取り込めるソースのカタログ
    (名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
    wikipedia 言語版の表示名・自称・記事数)と、このイメージが焼くスキーマバージョン
    (`schema_version` = `core.SCHEMA_VERSION`。管理画面の「最新のスキーマバージョン」表示の正)を返す
    (管理画面の初期化一覧と国・言語選択画面はこれを読む。アダプタは実体化せずに答える。
    osm の `node_index` はカタログの既定を実行時設定〔`OSM_NODE_INDEX` >
    `BUILD_PROFILE=low_memory`〕で解決してから返す — 管理画面の必要メモリ表示を
    実際の実行条件と一致させるため)。
    `GET /status` で state(idle/running/done/error)・
    source・started_at/finished_at・error・ログ tail(`chiezo.ingest` logger に登録した
    `_TailHandler` 経由)を返す。状態はプロセス内メモリのみ(永続化なし)。
    ホストへポート公開せず、`chiezo-api` からのみ docker 内部ネットワーク経由で到達可能
    (`chiezo-api` 側は環境変数 `CHIEZO_TRIGGER_URL` で URL を知る。未設定なら管理画面の
    初期化機能は無効)
- `scripts/` — 補助スクリプト(api/ingest 本体ではない運用ツール)
  - `gen_claude_config.sh` — chiezo 連携用の Claude 設定生成器。**使い方・オプション一覧は
    README「Claude Code から使う(設定ファイル自動生成)」節が正**で、ここには実装側の要点だけ置く:
    - `curl` + POSIX ツールのみで動く(Python 不要。既存 settings のマージにだけ jq を使う)。
      稼働中の chiezo の `/admin/claude-config.*` を取得して書き込むだけの薄いクライアントで、
      **生成の正は api 側 `app/claude_config.py`**。ベース URL はサーバーがアクセス元 URL から
      導出するので、接続に使った URL がそのまま生成物の curl 例・許可ルールになる
    - `--with-hook` を付けたときだけ自動許可フックも設置する。**既定では設置しない** —
      Claude が打つ Bash を毎回検査して自動承認しうる仕掛けで権限ルールより影響が広く、
      中身を読んで納得してから入れられるようにするため。設置に要る python3 / jq が欠けていれば
      黙って諦めず落とす(明示的に頼まれた設置なので)
    - settings のマージは「コマンドが `chiezo-autoallow.py` を含む既存エントリ」を落としてから
      足すので、再実行しても重複せず、設置先を変えたときも古いパスのエントリが残らない
    - MCP サーバーの登録も**既定で行う**(`--no-mcp` で無効)。`--user` は claude CLI
      (`claude mcp add --scope user`。remove → add で冪等)、CLI の無い環境では jq で
      `~/.claude.json` の `mcpServers` へ直接マージ、`--project`/`--target` は
      `.mcp.json` へのマージ(新規なら API 応答をそのまま置く)。
      前提(claude CLI か jq)が無ければ警告して登録だけ飛ばす — 既定の動作なので、
      明示的に `--with-mcp` されたときだけ落とす。
      **フックと違って既定で入れる**理由: フックが opt-in なのは Bash を自動承認しうる
      security 上の判断で、MCP 登録にはその性質が無い。ツール定義は常時コンテキストに
      載るが(7 ツールで約 4.4k 字)、既定で入れている CLAUDE.md ブロック(約 4.3k 字)と
      同程度で、chiezo を設定する時点で使う前提なのだから片方だけ渋る理由が無い
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
  - `refresh_rank_score.py` — 既存 DB の `rank_score` を `extra` から計算し直す
    (wikipedia は `pageviews_month`、geonames は `population`。osm は元から 0〜1 なので
    対象外)。スキーマは変わらないので `schema_version` は上げない。ダンプの取り直しも不要
  - `fts_lab.py` — trigram と形態素トークナイザの FTS を同じ文書集合の上で作って比べる
    実験台。索引サイズ・構築時間・ヒット数・順位を出す。人気度の重みを振るのにも使った。
    拡張(`sqlite-vaporetto` 等)は同梱していないので別途取ってくる
  - `add_tag_index.py` — 既存 DB のタグまわりの移行(2 → 3: 転置表 `doc_tags` の追加、
    3 → 4: 集計表 `tag_counts` の追加)。元の情報は 2 の DB にも `docs.tags` として
    入っているので、ダンプを取り直さずその場で作れる(jawiki の再取り込み 2〜6 時間に対し
    数分〜十数分。3 → 4 だけなら jawiki で 1 分弱)。足りないステップだけ流す。
    `meta` の更新を最後の 1 ステップにしてあるので、中断しても中途半端に新しい版を名乗る
    DB は残らない(もう一度実行すればやり直す)。書き込むので API は止めてから実行する。
    **取り込み側の PRAGMA をそのまま持ち込まないこと**: ここが触るのは使い捨ての
    `.building` ではなく運用 DB 本体なので、`journal_mode=OFF` にすると kill 一発で
    42GB が壊れる。速度よりロールバックできることを優先する。
    `docs` の全走査を 20 万件ずつに分けるのは、`DISTINCT` の並べ替えが一時ファイルに
    落ちるため(メモリは文書数によらず一定で、実測 100 万文書でピーク RSS 24MiB)。
    doc_id の値で等分割できない(osm は `osm_id*4` で 10 桁超)ので件数で刻んでいる
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
- `.github/workflows/ci.yml` — push / PR で pytest を実行し、main への push で
  `chiezo-api` / `chiezo-ingest` の 2 イメージをマルチアーキ(amd64 / arm64)で GHCR へ公開
  (cc-tasks / travel-log の docker-publish と同じダイジェストマージ方式。
  arm64 の無料ランナーが public 限定のため、リポジトリが private の間は公開ジョブをスキップ)。
  加えて週 1 の `schedule` でテストのみ実行する — requirements が範囲指定(>=)なので、
  コミットが無くても上流の破壊的変更(実例: mcp 2.0)に気づくため。定期実行では
  イメージ公開は走らない(`build` の `if` が push / 手動実行だけに絞ってある)。
  `permissions` はトップレベルを読み取りのみとし、`packages: write` は build / merge
  ジョブだけに与える(public 化に伴う絞り込み)
- `.github/dependabot.yml` — 依存更新の週次 PR(pip×2 / docker×2 / github-actions)。
  範囲指定の requirements で拾えない「範囲外の新メジャー」と上限ピンの解除、
  Actions・ベースイメージの更新を PR + CI で受ける

## コマンド

セットアップ・取り込み・運用(ダンプ更新、別マシンでのビルド、既存 DB の移行)の手順と
ingest の環境変数一覧は **README が正**。ここには開発時にしか使わないものだけ置く。

```bash
# テスト(api/requirements.txt + ingest/requirements.txt + pytest が必要)
python -m pytest tests/ -v

# フィクスチャ再生成
python tests/fixtures/make_fixture.py
python tests/fixtures/make_osm_fixture.py
python tests/fixtures/make_geonames_fixture.py

# api/ ingest/ を変更したときのローカルビルドでの動作確認
# (docker-compose.yml は GHCR の公開イメージを pull するので、こちらを使わないと反映されない)
docker compose -f docker-compose.build.yml up -d --build
```

実データを落とさずに取り込みを試すなら、`DUMP_FILE`(ダウンロードを飛ばして手元のファイルを使う)
と `MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータを緩める)を使う。`tests/` はこの経路を
フィクスチャで自動化したもの。

`SOURCE` に渡せる名前や、そのイメージが焼く `schema_version` はイメージ単体に聞ける
(README「別マシンでビルドして .db を配布する」節。ローカルビルド版なら
`chiezo-chiezo-ingest:latest` に置き換える)。

### メモリ方針: 「足りると確認できたときだけ取り込む」

**取り込み系コンテナに `mem_limit` は課さない。** 上限で締めても、足りないときは OOM killer が
数時間かけた構築を最後に殺すだけで得がない(実際に 1GiB で締めて osm のノード座標索引が
入りきらず落ちた)。代わりに `main.require_build_memory()` が**構築開始前**にメモリを検査し、
足りなければダウンロードもせず `SystemExit` する。判断材料は `/proc/meminfo` の `MemAvailable` と
cgroup 上限(`memory.max`)の小さいほう(`available_memory_bytes()`)。

構築プロファイル(環境変数 `BUILD_PROFILE`、`core.build_profile()`)で速度とメモリの
どちらを優先するかを切り替えられる: `low_memory`(既定)は**どのソースも 2GiB で
構築できる**メモリ優先(構築用 SQLite キャッシュを 64MiB に絞り、osm のノード座標索引を
ディスクに置く。osm は数倍〜10 倍遅くなる)、`fast` は速度優先(キャッシュ 512MiB、
osm はソースごとの既定索引)。既定を low_memory にしてあるのは、本番(配信)サーバも
開発機もメモリ 2GiB 級という運用のため。fast はメモリの潤沢なビルド機で
`docker run -e BUILD_PROFILE=fast …` と**実行時の引数として明示したときだけ**使い、
compose には常設しない(小さいマシンに設定が持ち込まれて OOM の芽になるため)。

必要量は各アダプタの `min_build_memory_gb`(`core.SourceAdapter` の一部)で宣言する
(ソースごとの実際の数値は README「メモリについて」の表が正)。wikipedia / geonames は
巨大な対応表を `lookup.py` でディスクへ逃がしてあるため fast 3GiB / low_memory 2GiB
(実測ピークは 1GiB 未満で、3GiB との差はキャッシュ分の余裕)。`OsmAdapter` は
使う索引方式(`node_index_kind` = 環境変数 `OSM_NODE_INDEX` > `BUILD_PROFILE=low_memory` >
ソースごとの `default_node_index`。明示指定がプロファイルより優先)がファイル索引なら
2GiB、RAM 索引なら `ram_index_memory_gb`。国別ソースのこの 2 つは
`sources/osm_regions.py`(自動生成カタログ)が pbf サイズから決める。
**既定(low_memory)でどのソースも 2GiB 以内・fast でどのソースも 12GiB 以内に収まること**を
テストで担保している(`test_low_memory_profile_fits_every_source_in_2gb` /
`test_no_source_requires_more_than_12gb_in_fast_profile`)ので、この不変条件を壊さないこと。
osm ソースは**国スケールに留める**(大陸スケールはディスク索引にしてもディスク 300GB・
構築 1 日以上で非現実的。全世界カバーは `geonames` の担当)。

**ビルド機と配信機を分けられる**のが設計の要なので壊さないこと: `.db` は自己完結した単一
SQLite ファイルで、配信側 chiezo-api は read-only immutable で開くだけの常駐 80〜150MB。
効いてくるのはメモリでなくディスク(jawiki.db 約 42GB)。手順は README「別マシンでビルドして
.db を配布する」と `handoff/BUILD-ON-ANOTHER-MACHINE.md`。

## 実装上の約束事

- コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。ソース固有情報は `docs.extra`(JSON)へ。
  変更は最終手段で、`schema_version` を上げ api 側で複数バージョン対応する。
- ソース間で JOIN しない。API はソース種別を意識せず docs/aliases/docs_fts のみ参照する。
- FTS5 は trigram。ユーザー入力は必ずフレーズエスケープしてから MATCH に渡す(`app/fts.py` 経由)。
  **形態素トークナイザへの差し替えは 2026-07 に候補 2 つを実測して見送っている**
  (`docs/fts-tokenizer-evaluation.md` に数字と再現手順)。索引 −72%・2 文字クエリが
  引けるという利得は本物だが、**軽いほうは遅すぎ、速いほうは重すぎる**という状態:
  - `sqlite-vaporetto` — 速い(索引化は trigram より速い)が、モデルを SQLite の
    **接続ごと**に持つ。接続 1 本で +395MiB・8 本で +926MiB になり、`app/db.py` が
    スレッドごとに接続を張る以上「配信機は数百 MB」の前提を壊す
  - `lindera-sqlite` — 辞書を静的リンクで共有するのでメモリは軽い(1 本 +95MiB)が、
    **索引化が文書長に対してほぼ O(n²)**。1 万字級の Wikipedia 記事で 1 件 195ms、
    jawiki 全体では数十時間規模になり取り込みが成立しない
    (おまけに現行 SQLite ではロード自体が失敗する。上流のバグ 2 つを直して測った)
  再挑戦の条件は評価ドキュメントの「採用できる条件」に 3 つ整理してある。
- 運用 DB は読み取り専用(`immutable=1`)。更新はブルーグリーン(別ファイル構築 → シンボリックリンク差し替え)のみ。
  **例外は `notes` の 1 ソースだけ**(`app/notes.py`)。書き込みは `CHIEZO_NOTES_DIR` 配下に
  閉じ、`/data` は read-only マウントのまま保つこと。そのソースだけ読み手も `mode=ro` に落とす。
  差し替えは api が自動検知する: lifespan の常駐タスクが `CHIEZO_RESCAN_INTERVAL` 秒(既定 5)ごとに
  `/data` の指紋(`registry.data_dir_fingerprint`)を見て、変わっていれば再走査(`main.refresh_sources`)。
  スレッドローカルの接続キャッシュも `db.get_connection` がリンク先の inode を見て開き直すので再起動不要。
  `/data` への書き込み権限を持つのは chiezo-ingest(one-shot)と chiezo-trigger(常駐)だけで、
  chiezo-api は引き続き read-only マウント。
- エラーレスポンスは `{"error": "..."}` 形式。
- **ドキュメントの強調は句読点を `**` の外に出す。**`**…です。**次の文` のように閉じる `**` の
  直前が句読点・閉じ括弧だと、CommonMark の flanking rule で閉じ側と認識されず、太字にならずに
  `*` がそのまま表示される(日本語では句点で終える書き方が自然なので踏みやすい)。
  `**…です**。次の文` と書く。検出は markdown をレンダリングして本文に `*` が残るかを見る。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。chiezo-trigger と chiezo-llm は
  ホストへポート公開せず、chiezo-api からのみ内部ネットワーク経由で到達可能にすること。
- **「答える」層は既定で無効のまま保つ**。推論を chiezo-api の中で動かさない(配信側が
  数百 MB で動く前提)。LLM を呼ぶコードは `app/answer.py` と `app/agent.py` に閉じ、
  検索・文書取得は `app/main.py` の関数(agent は MCP の道具)を再利用する。compose では
  profile `answer` を付けたときだけ `chiezo-llm` が起動する状態を崩さない。
  **素の既定は `rag` + `grounded=1`**(小さな機械でも安全に動く側)。潤沢な環境では
  `CHIEZO_ASK_DEFAULT_MODE` / `_GROUNDED` で倒せるが、**素の既定は変えない**。
- **外へ出るのは「答える」層だけ**。chiezo 本体(知識ベース)は ingest がダンプを取る以外
  外を叩かない。web 検索は使う側の機能として `app/websearch.py` に閉じ、既定は無効のまま保つ。
  有効にしたときも「どれが web 由来か分かる」ことを崩さない(出典の `source` が `web`)。
- **会話の状態をサーバーに持たせない**。`/v1/chat` は履歴を毎回まるごと受け取る。
  セッションを持つと read-only・複数ワーカーの前提が崩れる(MCP をステートレスにしたのと同じ)。
- **GPU の設定は `docker-compose.gpu.yml`(上書きファイル)に閉じる**。`gpus: all` は
  GPU の無い環境では起動そのものが失敗するので、本体の compose には書かない。
- コード(api/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(セットアップ・API 仕様・運用手順)と本ファイル(CLAUDE.md、アーキテクチャ記述)も
  あわせて更新すること。ドキュメントだけを別コミット・別対応に先送りしない。
