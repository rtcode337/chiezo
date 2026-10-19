---
paths:
  - "app/main.py"
  - "app/registry.py"
  - "app/db.py"
  - "app/fts.py"
  - "app/deps.py"
  - "app/known_sources.py"
  - "app/logs.py"
  - "app/web_guard.py"
  - "app/views/browse.py"
  - "tests/test_api.py"
  - "tests/test_logs.py"
  - "tests/test_web_guard.py"
  - "tests/test_untrusted_input.py"
---

# 知識の引き口(REST・登録・検索)

chiezo-app の組み立てと、溜めた知識を引く REST(search / doc / filter / tags / recent)、ソースの登録・接続・FTS、ブラウズ画面。

## app/ と app/main.py

- `app/` — **chiezo-app**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — **機械向けの口とアプリの組み立て**(/, /healthz, /apple-touch-icon.png,
    /v1/sources, /v1/{source}/search|doc|filter|tags|titles|links|random, /v1/ask, /v1/chat,
    /v1/ai/backends, /v1/ai/complete, /v1/ai/failures, /v1/ai/inflight, /v1/ai/usage,
    外の取り次ぎの /v1/thumbs(POST)・/v1/bookmarks(`collect-tools.md`)、
    lifespan・例外ハンドラ・画面 router の登録・MCP の /mcp のマウント。
    MCP の実体は `app/mcp_server.py`。`.claude/rules/mcp-claude-config.md`)。
    **人間向けの HTML はここに置かない**(`app/views/`)。以前は 2,473 行の 1 ファイルに
    REST と管理画面 HTML が同居していて、変更の理由(API の契約 / 画面の見た目)が
    まったく別のものが混ざっていた
    - **ブラウザから読ませる口は `CHIEZO_CORS_ORIGINS` で開ける**(カンマ区切り。既定は空)。
      印(`Access-Control-Allow-Origin`)が無いと、ブラウザは届いた応答を JS へ渡さない ——
      サーバー側には 200 が並ぶので、開けたつもりで読めていないことに気づきにくい。
      **`CORSMiddleware` は使わず自前で書いている**(`allow_the_browser_to_read`)——
      あちらは組み立て時の設定で固まるので、許可の入れ替えにプロセスの作り直しが要る。
      やることは「Origin が完全一致したら印を返す」だけで、開けるのは GET と
      事前確認(preflight)、資格情報は許さない。**https のページから読ませるなら
      chiezo 側も https で出すこと**(mixed content はこの印では通らない)
    - **いちばん外側の中間層が、許していない名前の Host と別のサイトからの書き込みを断る**
      (`refuse_requests_from_other_sites` / `app/web_guard.py`)。認証が無いので、LAN の端末で
      開いている任意のページがブラウザを踏み台にして叩ける(CSRF・DNS リバインディング)。
      **Host の検査は `CHIEZO_ALLOWED_HOSTS`(か `CHIEZO_MCP_ALLOWED_HOSTS`)を書いたときだけ** ——
      既定で絞ると、VPN 越しの名前などで開いたときに気づかないうちに締め出される。書いたら
      IP・`localhost`・ドットの無い名前(サービス名)と、書いた名前だけを通す(ほかは 421)。**書き込み(POST/PUT/PATCH/DELETE)は `Sec-Fetch-Site: cross-site` か、
      自分でも `CHIEZO_ALLOWED_ORIGINS` でもない `Origin` を 403 で断る**。比べるのは名前とポートで、
      スキームは見ない(TLS を終端する前段の裏では食い違う)。**どちらのヘッダも付けない相手
      (curl・サーバー側のクライアント・MCP)は通す** —— 止めたいのはブラウザだけ
    - `/v1/{source}/filter` — 全文検索ではなく属性(`feature` / `area` / `bbox` / `wikidata` /
      `tag`)の AND での一括抽出(Overpass 相当)。**消えたもの・まだ AI が目を通していない
      ものは `search` / `doc` と同じく既定で外す**(`notes.HIDDEN_TAGS`。`include_removed`
      で含められる)。ここだけ消えたものしか外しておらず、他の口では出ないものが
      一括抽出にだけ並んでいた。`docs` の生成列への索引付き検索。
      条件は `build_attribute_filters()` が
      SQL 断片に変換し、`search` / `doc` からも同じ関数で併用できる
    - `tag`(= Wikipedia のカテゴリ等)だけは生成列ではなく転置表 `doc_tags` を引く。
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
    - **`filter` も新しい順に並べられる**(`order=newest`。既定は `rank` = 人気度の順)。
      人気度の順で件数を絞ると「このタグの最新 N 件」が引けず、呼ぶ側が全件を持ち帰って
      並べ直すことになる —— 上限を超えた日に新しいほうが黙って落ちる(外のアプリで、要約が
      20 件を超えた収集の最新が出なくなった)。**並びと断り方は `recent` と同じ**
      (`updated_at` の降順・`doc_id` の降順。索引を持たないソースは `require_recency_index` で 409)
    - `/v1/{source}/recent` — **新しい順**に並べる。`search`(語が要る)でも
      `filter`(タグや属性)でも取れない読み方で、「この 1 日で何が入ったか」を引くための口。
      集めたものを読む側(重要なものを選ばせる・通知の候補にする)がまず訊くのはこれ。
      **`updated_at` の索引はコアスキーマに無い**ので、持たないソースは 409 で断る
      (`require_recency_index`)—— 黙って通すと `docs` の全走査になり、しかも
      ダンプ由来のソースでは並べても意味を成さない(記事の版や取り込み時刻なので)。
      索引を持つのは**溜まっていくソースだけ**で、アダプタが `extra_index_ddl` で名乗る
      (`ingest/core.py` の `RECENCY_INDEX_DDL`。コアに入れると `SCHEMA_VERSION` が上がり、
      jawiki の焼き直しまで要求してしまう)。
      **`since` はその時刻を含む** —— 時刻は秒までしか持たないので「より後」にすると
      同じ秒に入ったものを黙って落とす。取りこぼすより、同じものが 1 件返るほうが後から直せる
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

## 控えの出し方・下ごしらえ・登録・接続

- `app/logs.py` — **chiezo-app の控えの出し方**。**既定では 1 行も出ていなかった** ——
  uvicorn は自分の 3 つのロガーしか設定せず、`chiezo.*` の実効レベルは WARNING の
  まま(`log.info` は全部捨てられていた)。しかも WARNING 以上は Python の最後の
  受け皿が拾うので、**時刻もロガー名も付かない裸の行**になり、`docker logs` を
  時刻で絞っても 1 件も当たらない(実際にそうなった)。
  **時刻は日本時間**(取り込み側の控えと揃える。固定の +09:00)、
  **`chiezo` にだけ handler を付ける**(root だと uvicorn の行まで二重に出る)、
  **`propagate` は切らない**(切るとテストの `caplog` が拾えない。root に
  handler が無いので二重にはならない)。`CHIEZO_LOG_LEVEL` で変えられ、
  読めない値は既定へ倒す —— 綴りを間違えて控えが丸ごと消えるのがいちばん困る
- `app/deps.py` — **REST と画面が共有する下ごしらえ**(`get_source`、ORDER BY 断片の
  `exact_title_first` / `relevance_order`、持っていないものを断る `require_*`)。
  **ここは app の他モジュールを import しない** —— views が main を import すると
  main → views(router 登録)との間で循環参照になるため、共有物はここへ降ろす
- `app/registry.py` — /data 走査・ソース登録、`SUPPORTED_SCHEMA_VERSIONS`(読めるのは 4 だけ)。
  **3 以前の DB は登録しない** —— 足りない表や索引を前提に組んだ問い合わせが通らないため。
  警告(`unsupported schema_version=… re-run ingest`)を出して飛ばし、記憶の面にも
  「登録できなかったソース」として取り込み直しを促す(`unsupported_sources`。
  登録されなかった名前だけを開くので、ふだんは何も読まない)。
  1 つ前の世代の素性(`generation_info`)もここで読む
- `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
- `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
- `app/known_sources.py` — `chiezo-trigger` が未設定・到達不能なときの控えの既知ソース一覧と、
  国選択画面の大陸表示名・言語選択画面の記事数階層(`WIKIPEDIA_TIERS`)。
  初期化できるソースの正は ingest 側の `ADAPTERS` で、通常は
  `chiezo-trigger` の `GET /sources` から受け取る(`views/admin.py` の `initializable_sources()`。
  osm 国別 195 件 + wikipedia 言語版 348 件あり、app 側に複製すると必ず腐るため)

## ブラウズ画面

- `/search/{source}/`(GET) — 検索フォーム(HTML)。**画面はすべて前置きの下に置く**
  (`/admin`・`/search/…`・`/ai/…`)。以前はソース名をそのままルート直下に置いていて、
  ルートがキャッチオールになるため `ask` や `admin` という名前のソースを足せなかった。
  URL の組み立ては `app/pages.py` の `browse_url()` / `doc_url()` に閉じてある。
  `?q=` 未指定時は一覧を出さずフォームのみ表示
  (jawiki 等の大規模ソースで rank_score 順の全件一覧がフルスキャンとなりタイムアウトするため)。
  `?q=` 指定時は結果一覧を表示し、`/v1/{source}/search` と同じロジック
  (FTS または短語のタイトル前方一致フォールバック)
- `/search/{source}/doc/{doc_id}`(GET) — 文書詳細(title/tags/opening/body/links/extra)の HTML 表示
