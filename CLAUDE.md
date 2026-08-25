# Chiezo — ローカル知識サーバー

**AI が使う知識を AI の外に置くための知識ベース**。知識をローカルの SQLite (FTS5) に索引して
置き、AI は必要になった瞬間に必要なぶんだけ引く。存在理由は 2 つで、**常駐コストを
溜めた量から切り離すこと**(載るのは道具の定義だけ)と、**速く確実に引けること**
(完全ローカル。レート制限も問い合わせ内容の外部送信も無い)。知識ベースとしての中身は次の 3 層:

- **ためる** — `ingest/` がソースごとに独立した SQLite ファイル(`/data/<source>.db`)を作る。
  取得元は公開ダンプ(Wikipedia / OpenStreetMap / GeoNames)に限らず、このリポジトリに
  入れられないものは**別コンテナのプラグイン**(`CHIEZO_PLUGIN_SOURCES`)から足せる。
  更新はブルーグリーン
- **取り出す** — `api/` が **MCP**(`/mcp`)と **REST**(`/v1/...`)の 2 経路で出す。
  Claude Code 向けには「いつ Chiezo を使うか」を書いた CLAUDE.md ブロックも生成する
- **覚える** — `api/app/notes.py` が **Chiezo で唯一書き込めるソース** `notes` を持つ。
  **書き手は AI でもよい**(MCP の `remember`)。「覚えておいて」と言われたことを溜め、
  `recall` で新しい順に引く。CLAUDE.md や記憶ファイルと違い**常駐するのはツール定義だけ**
  なので、件数が増えてもコンテキストを食わない

これとは**性質の違う層**がもう 1 つある。**「使う」層(任意・既定では無効)** —
**Chiezo を上手に引ける AI を Chiezo 自身が用意する**もので、知識ベース本体の機能ではない
(`scripts/gen_claude_config.sh` が Claude Code 用の設定を配るのと同じ考え方。どう引けば
当たるかをいちばん知っているここが、道具立てとプロンプトを持つ)。`api/app/answer.py` が
`/v1/ask` と ブラウザの `/ai/chat` を受ける。推論は同居させず OpenAI 互換 API を叩くだけで、
**`CHIEZO_LLM_URL` 未設定が既定 = 丸ごと無効**(配信側が数百 MB で動く前提を壊さないため)。
`?mode=agent` では `api/app/agent.py` が MCP と同じ道具を LLM 自身に引かせる
(GPU + 8B 級が前提なので**既定は 1 回検索する `rag`**)。会話として続けるのが
`/v1/chat`(履歴はクライアントが持つ)、足りないぶんを外から補うのが
`api/app/websearch.py`(既定では無効)。知識ベース本体は今までどおり外を叩かない。

現在の収録ソースは日本語 Wikipedia = `jawiki`、OpenStreetMap 日本抽出 = `osm_japan`、
GeoNames 全世界地名辞典 = `geonames`(いずれも 348 言語版・195 か国から選んで増やせる)。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — **機械向けの口とアプリの組み立て**(/, /healthz, /apple-touch-icon.png,
    /v1/sources, /v1/{source}/search|doc|filter|tags|titles|links|random, /v1/ask, /v1/chat,
    /v1/ai/backends, /v1/ai/complete, /v1/ai/failures, /v1/ai/usage、
    lifespan・例外ハンドラ・画面 router の登録・MCP の /mcp のマウント。
    MCP の実体は下の `app/mcp_server.py`)。
    **人間向けの HTML はここに置かない**(`app/views/`)。以前は 2,473 行の 1 ファイルに
    REST と管理画面 HTML が同居していて、変更の理由(API の契約 / 画面の見た目)が
    まったく別のものが混ざっていた
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
    使い方は `docs/api-reference.md`「MCP から使う」節。ツールの実体は `app/main.py` のエンドポイント関数
    そのもので、MCP 用に処理を書き直していない(別実装にすると片方だけ直されて必ずずれる)。
    そのぶん**踏み抜きやすい罠が 3 つ**あるので、触るときは順に確認すること:
    - FastAPI のエンドポイントは既定値が `Query(...)` オブジェクトなので、Python から
      直接呼ぶときに**全パラメータを明示的に渡す**必要がある。渡し忘れると Query
      インスタンスが値として入り、`if tag:` が常に真になる等、例外にならず静かに壊れる。
      `tests/test_mcp.py::TestStaysInSyncWithRest` がシグネチャを突き合わせて落とす
    - MCPServer は**同期のツール関数をイベントループ上で直接呼ぶ**(await するのは async
      関数だけ)。Chiezo のクエリは最大 5 秒ブロックしうるので、必ず `run_in_threadpool`
      に逃がす。でないと重いクエリ 1 本で API 全体が止まる
    - `TransportSecuritySettings` の既定は「localhost 系の Host しか受け付けない」。
      そのままだと LAN の別マシンから叩いた時点で 421 になるので、既定では検証を外し
      (REST 側も認証なし・LAN 内前提)、`CHIEZO_MCP_ALLOWED_HOSTS` で絞れるようにしている
    セッションマネージャは lifespan の中で `run()` する必要があり(python-sdk#1367)、かつ
    1 インスタンス 1 回しか呼べないため、**起動ごとに `build_mcp()` で作り直して**
    `app.state.mcp_asgi` に置き、マウント先(`main._mcp_asgi`)がそれを見る形にしている
    (モジュール読み込み時に作り置きすると、同一プロセスで二度起動するテストが落ちる)。
    **ステートレス・待ち受けパス・Host 検証は `build_mcp_app()`(= `streamable_http_app()`)
    側の設定**で、mcp 2.x でサーバー本体の引数から移った(1.x の `FastMCP(...)` に
    まとめて渡していた頃の書き方は通らない)
  - `app/notes.py` — **「覚える」層(`/v1/notes`)の本体。Chiezo で唯一書き込む場所**。
    使い方は `docs/api-reference.md`「notes(唯一書き込めるソース)の REST」節、
    なぜこの形かは `docs/design-notes.md`
    「「覚える」(notes)はなぜ Chiezo に置くのか」が正。実装側の要点:
    - **`CHIEZO_NOTES_DIR` が機能フラグを兼ねる**(未設定 = 503、MCP の道具も出さない)。
      ツール定義は常時コンテキストに載るので、使えないものを並べない
    - **タグの定番語彙は `CANONICAL_TAGS` の 1 か所で持ち、MCP の `remember` の
      ツール定義(`tag_guide()`)として配る**。クライアント側の CLAUDE.md に写しを
      持たせると写しごとにずれる(実際に NAS と nas に割れた)。語彙を変えるのは
      この定数だけでよい —— テストが「語彙がツール定義に載ること」を突き合わせる
    - **タスク・ルールはタグで表す**(`todo` / `着手中` / `完了` / `難所` / `rule` /
      `無効` / `project` / `アーカイブ`。所属プロジェクトはリポジトリ名のタグ)。
      **専用のテーブルも列も足さない** —— 種別も状態も絞り込みの軸でしかないので、
      `doc_tags` の索引がそのまま効く。状態のタグが無いものが「未着手」で、
      これなら既存の `todo` メモに手を入れずタスクとして扱える
    - **タグで表せないものだけ `docs.extra`(JSON)に置く**。いまは並び順
      (`sort_order`)だけ。**`recall` の既定の項目には入れない** —— 入れると
      `extra` を持たないほとんどのメモにも `"extra": null` が並び、`recall` を読む
      AI のコンテキストを食う(`RECALL_OPTIONAL_FIELDS`。要る側が `fields` で名指しする)
    - **`Body(...)` を既定値に持つ引数を増やしたら、MCP 側で明示的に渡すこと**。
      `app/mcp_server.py` は FastAPI を通さず `app/main.py` のハンドラを直接呼ぶので、
      省略すると `FieldInfo` オブジェクトがそのまま保存側へ流れる
      (`tests/test_notes.py` の `test_mcp_remember_still_works_without_extra` が押さえる)
    - **置き場を取り込み本体(`/data/corpus`)と分けるのは性能上の理由**。
      `registry.data_dir_fingerprint()` が `CHIEZO_DATA_DIR/*.db` の mtime/size を 5 秒ごとに
      見て、変われば**全ソース再走査(`COUNT(*)` 込み)**する。同じ場所に置くとメモ 1 件ごとに
      jawiki 150 万件の COUNT が走る。**バインドするホストのディレクトリは `data/` 1 本**で、
      その下を `corpus/`(本体)・`notes/`(覚える層)・`state/`(管理画面の設定)に切っている ——
      走査するのは `corpus/` だけなので、1 本にまとめても再走査は起きない
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
    - **`limit`/`offset` の上限は `notes.recall()` の中で担保する**。REST の
      `Query(ge=1, le=…)` は HTTP の口にしか効かず、MCP と agent は api の関数を
      Python から直接呼ぶので通らない。SQLite は **`LIMIT -1` を「無制限」と解釈する**ため、
      素通しすると頁を送る意図の呼び出しが静かに全件取得になる。`max_chars` の負値も
      同じ理由でここで 0 に丸める(Python の負の添字は末尾を削る意味になる)
    - **本文は既定で 400 文字に切る**(`RECALL_MAX_CHARS_DEFAULT`)。絞り込めても
      1 件あたりが重いままだと、20 件当たれば 20 件分の全文がコンテキストに載る。
      他ソースの `search`(冒頭)→ `doc`(全文)と同じ二段構えにするための既定で、
      `max_chars=0` で切らない。`fields` で項目も選べる(`RECALL_FIELDS`)
    - **切ったものには `truncated: true` を立てる**。黙って切ると「これで全部」と
      読まれる —— 504 を 0 件と読むのと同じ取りこぼし方をするため、全文が要ると
      分かる印を必ず返す(取り直し先は `/v1/notes/doc/{doc_id}`)
  - `app/tasks.py` — **「やること」層(タスク・プロジェクト・ルール)**。cc-tasks から
    移してきたもので、**notes の上にタグで載っているだけ。専用のテーブルも列も持たない**。
    種別も状態も所属も絞り込みの軸でしかないので `doc_tags` がそのまま効き、
    おかげで移植にスキーマ変更が 1 つも要らなかった。実装側の要点:
    - **既に `todo` タグで書かれていたメモが、そのままタスクとして並ぶ**。
      これが移す理由そのもの(cc-tasks と notes が同じことを二重に持っていた)
    - **状態のタグが無いものが「未着手」**。一番多い状態にタグを増やさずに済み、
      既存のメモに手を入れなくてよくなる
    - **タスクの所属は「実在する `project` 文書の名前と一致するタグ」**。
      プロジェクトを作る前から付いていたタグが、その名前の `project` を作った瞬間に
      紐づく。名前を変えたら `_rename_project_tag()` が全タスクのタグを付け替える
    - **プロジェクト名に構造タグ・定番タグは使えない**。名前はそのままタスクに付く
      タグになるので、`完了` のような語だと「所属」ではなく「状態」と読まれてしまう
    - **書き込みは必ず `app/notes.py` を通す**。`docs` を直接 UPDATE すると FTS と
      `doc_tags` / `tag_counts` が本体とずれる。例外は `notes.set_extra()` だけで、
      `extra` は索引のどれにも関わらないから直接書いてよい
    - **並び替えで `updated_at` を動かさない**(だから `set_extra()` が要る)。
      カードを 1 枚ドラッグしただけでメモが `recall` の先頭に浮くと時系列が乱れる
    - **ルールだけは本文にタイトル行を混ぜない**。`combined()` が `## <見出し>` を
      自分で付けるので、混ぜると見出しが二重になる(タスクとプロジェクトは逆に、
      notes が空本文を許さないので本文をタイトルから始める)
    - **連結(`combined()`)と取り込み(`parse_rule_markdown()`)は対**。前置きと
      「規約リポジトリの扱い」は連結時に自動で付くので取り込みでは捨てる ——
      捨てないと貼り替えのたびに増える。捨てる見出しは `COMBINED_REPO_RULE` の
      1 行目から取る(2 か所に書くと片方だけ直したときに黙って二重取り込みになる)
    - **見出しの判定はコードフェンスの外だけ**。ルール本文にはシェルの例が入るので、
      フェンス内の `## …` を見出しと読むとそこでルールが分断される
    - **持ち出し(`export_tasks`)と取り込み(`import_tasks`)は対**。書き出したものが
      そのまま取り込みの入力になるので、テキストで手元に置けばバックアップになる。
      運ぶのは未完了タスクと所属プロジェクトの名前・リポジトリだけ(戻したいのは
      待ち行列であって画面の状態ではない)。**同じものを二度読んでも増えない**ように、
      (プロジェクト名, タイトル)で照合して飛ばす
  - `app/tasks_api.py` — **やること層の REST(`/api/**`)**。cc-tasks から移した画面が
    そのまま話せる形にしてある。**本体の `/v1/...` とは別の面**なので、流儀を 2 つ変えてある:
    - **JSON は camelCase**(要求は snake_case でも受ける)。画面側の型定義に合わせる
    - **エラーは `{"error": {"code", "message"}}`**。本体は `{"error": "..."}` だが、
      画面が `error.message` をそのまま出すので、平たくすると
      「未完了のタスクが 3 件あるためアーカイブできません」のような案内が消える
    - **固定のパスを `/{id}` より先に宣言する**。`/api/tasks/{task_id}` が先だと
      `order` / `export` / `import` が id として解釈されて 422 になる(テストが押さえる)
    - **所属は外に出すときだけ doc_id に直す**。中ではタグ(名前)で持っているので、
      `_project_ids()` で引き直す。`projectId: 0` は「紐づけを外す」(`UNLINK_PROJECT_ID`)
  - `app/tasks_app.py` — **やること層のアプリ(`chiezo-tasks`)。外に出すのはこれだけ**。
    知識ベース本体(`app/main.py` / 7010)は LAN 内・認証なしのまま変えない ——
    あちらを公開すると、サーバー側の鍵で AI を叩く `/v1/ai/complete`、課金の走る
    `/v1/media/*`、取り込みを起動できる `/admin`、メモを消せる `DELETE /v1/notes/{doc_id}`
    まで一緒に外へ出る。**面をプロセスごと分ける**ほうが、認証を 1 枚かぶせるより確実に安い。
    - **notes を `db.set_mutable_paths()` に登録すること**。登録しないと
      `immutable=1` で開かれ、書き込み途中のページを掴みうる(本体は `/data` の走査で
      登録しているが、こちらは notes しか読まないので lifespan で明示する)
    - **画面(`tasks-frontend/` のビルド成果物)もここが配る**(`_serve_spa`)。
      総取りのルートなので**全部のルートを登録し終えた後**に足す。
      `/api/` `/oauth2/` `/login/` に前方一致するものは画面に落とさない ——
      落とすと、綴りを間違えた API 呼び出しに殻が 200 で返って原因を追えなくなる。
      ハッシュの付いた `/assets/` だけ長く持たせ、殻と Service Worker は `no-cache`
  - `tasks-frontend/` — **やること画面(Vue 3 + Vite + PWA)**。cc-tasks から移した。
    使い方は `docs/tasks.md`「画面」が正。**サーバーレンダリングの `api/app/views/` とは
    別系統**で、ここだけビルドステップを持つ。
    - **クエリの真偽は空文字を「絞り込まない」として受けること**(`_optional_bool`)。
      画面は「全件」を `?archived=`(値だけ空)で送るので、FastAPI に `bool` として
      宣言すると 422 になり一覧がまるごと出ない。**テストでは `?archived=true` しか
      送っていなくて気づけず、実ブラウザで開いて初めて出た**
    - アイコンは `scripts/gen_tasks_icons.py`(標準ライブラリのみ)。この環境には
      SVG のラスタライザが無いので、距離関数で描いて zlib で PNG を組んでいる
  - `app/tasks_auth.py` — **やること層の認証(Google OAuth)。外に出す面はここだけが守る**。
    要件は `docs/tasks.md`「守り」が正。実装側の要点:
    - **足りないときは閉じる**。`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` /
      `ALLOWED_EMAIL` のどれかが欠けていたら `/api/**` は 401 を返し続ける。
      「設定が無いから素通しする」は外に出す面では致命的
    - **`id_token` を自分で検証しない**。`code` の交換も利用者情報の取得もサーバー間の
      TLS で直接行う(`token` → `userinfo`)ので、署名検証を省いた `jwt.decode` を
      書かずに済む。pyjwt は依存にあるが、ここでは使わないほうが読み手に優しい
    - **突き合わせは `_same()` を通す**。`secrets.compare_digest` は非 ASCII の str で
      TypeError を投げるので、外から来た値(クエリの `state` など)をそのまま渡すと
      1 文字混ぜられただけで 500 になる。UTF-8 に符号化してから比べる
    - **ヘッダを自分で読まない**。レート制限のキーもリダイレクト URI のホストも
      `request` から取る。`X-Forwarded-For` を自分で読むと、値を変えるだけで
      毎リクエスト別バケットになり制限が無効化される
    - **ミドルウェアはルーターより後に仕込む**(Starlette は後から足したものが外側)
  - `app/providers.py` — **話せる相手の定義(URL・表示名・モデル候補はここに決め打ち)**。
    相手ごとに URL は 1 つに決まるので設定にしない —— 書き間違いの余地を増やすだけ。
    **ユーザーが決めるのは on/off・API キー・モデルの 3 つだけ**で、それらは
    `app/settings_store.py`(`/data/state/settings.db`)に入る。例外は `CHIEZO_LLM_URL` で指す相手で、
    LAN の別マシンを指す用途があり URL を決め打ちにできない。
  - `app/settings_store.py` — 管理画面から入れた設定の置き場。**`CHIEZO_STATE_DIR` が
    機能フラグを兼ねる**。「答える」層そのものの元栓(`answer_enabled`)もここに持つ ——
    **既定は有効だが、それで勝手に動き出すことは無い**(相手が全部既定 off なので)。
    元栓は「相手を 1 つずつ切って回らずに機能ごと止める」ためのもの。`/data/corpus`(読み取り専用)にも `/data/notes`(「覚える」層の中身)にも
    混ぜない。**API キーは平文**(認証なし・LAN 内前提のサービスで暗号化しても守れるものが
    増えない)だが、**画面には二度と出さない**。
  - `app/views/ai_settings.py` — 管理画面の「AI の相手」節と、on/off・API キーの受け口。
    **on にできる条件を画面側でも守る**: 鍵の要る相手は鍵が要る / 同居のコンテナ
    (推論サーバ・CLI ブリッジ)は**立っていなければ on にできない**(到達確認は並行に
    行う。直列だと立っていない相手の数だけ画面が遅れる)。`app/answer.py` 側でも弾く。
    **CLI の認証情報も管理画面から入れる** —— ブリッジが設定 DB(`/data/state`)を読み取り専用で
    マウントして要求のたびに読むため(再起動は要らない)。chiezo-api に「トークンを返す口」を
    開けずに済むのが要点。**そのため settings.db は WAL にしない** —— WAL の読み手は -shm への
    書き込みを要求し、read-only のマウントでは `unable to open database file` になる。
    **journal_mode はファイルに焼き付く属性**なので `PRAGMA` を書かないだけでは既存の
    ファイルが戻らない。接続のたびに `DELETE` を明示している(本番で 502 の原因になった)。
    **「接続を試す」が一度でも通るまで on にできない**(`provider_settings.verified_at`)。
    登録の有無でも到達確認でもなく「いま話せるか」を条件にする —— 認証情報が間違っていても
    到達はするし、会話して初めて失敗すると原因を追いにくい(本番で 502 になった)。
    確かめ方は `/models` を引くだけで、会話は 1 往復もしない(サブスクの枠を食わないため)。
    ブリッジだけは `/health?check=1` で CLI に直接聞く(`claude auth status` 等)。
    **認証情報を入れ替えたら印を消す**(まだ確かめていないため)。**管理画面は描画のときに
    相手へ問い合わせない** —— 記録された結果だけを見るので、落ちている相手があっても遅くならない。
  - `app/usage.py` / `app/usage_store.py` — **各 AI の使用量**(`/v1/ai/usage`・管理画面の
    「使用量」節)。**数を 2 つ持ち、混ぜない**:
    - **相手が言う枠**(`quota`)…… 相手の勘定なので**残りが分かる**が、**聞ける相手が限られる**。
      聞き方は `Provider.usage`(`app/providers.py`)で相手ごとに決まる ——
      claude は `api.anthropic.com/api/oauth/usage`(**CLI の `/usage` と同じ口**。
      CLI 側に出口が無く、ブリッジが立っていなくても引けるので直に引く)——
      ただし**`claude setup-token` の長期トークンでは通らない**(実測: HTTP 403
      `OAuth token does not meet scope requirement user:profile`。長期トークンは
      推論だけに絞られていて、完全スコープは `claude auth login` にしか無い)。
      **会話・要約には影響しない**ので経路は残し、**そうと分かる文言を返す**
      (`SCOPE_HINT`)—— 生の英文だけだと「鍵が違う」と読んでトークンを入れ直すことになる。
      相手が 400 番台を返したら**本文まで出す**のもこれが理由で、「HTTP 403」だけでは
      打つ手が分からなかった、
      codex / antigravity は**ブリッジの `/usage`**(CLI に聞かせる。
      **手元に控えた認証情報は期限切れになる**ので、更新は CLI に任せる)、
      openrouter は `/api/v1/key`。**gemini・openai・推論サーバには口が無い**
      (前者は Google Cloud の Quotas API 側、後者は Admin キーが要る)ので
      画面には「この相手は枠を出さない」と書く —— **空欄にすると「使っていない」と読める**
    - **Chiezo が使ったぶん**(`spent`)…… `data/state/usage.db` に 1 呼び出し 1 行で残し、
      5 時間 / 24 時間 / 7 日で集計する。**全部の相手で同じ物差し**だが、
      **Chiezo を通していない利用は入らない**(手元の端末で回した CLI など)。
      記録するのは `answer.complete_message`(会話)と `media._run` / `media.transcribe`
      (絵・音・動画・声)—— **絵と音も同じサブスクの枠を食う**ので同じ表に入れる
    決めごと:
    - **どの聞き方もモデルを呼ばない**(「接続を試す」と同じ方針。確かめるたびに枠を食わない)
    - **画面は描画のときに聞きに行かない。** 控えと取得時刻を出し、取り直しは行のボタン。
      API も既定は控えを返し、`?refresh=1` のときだけ外へ出る ——
      **見ているだけで相手のレート制限に当たらない**ため
    - **取れなかったときに前の値を消さない**(一時的に繋がらないだけのことがある)。
      値と失敗の理由を並べて出す
    - **トークン数の `NULL` は「相手が言わなかった」、`0` は「使わなかった」。**
      混ぜると、数を返さない相手(CLI ブリッジ)が「0 トークンで動く相手」に見える
    - **`settings.db` に相乗りしない** —— あちらは CLI ブリッジが読み取り専用でマウントする
      ファイルで、呼ぶたびに書く表を同居させたくない(ジョブ DB と同じ判断)
    - **推測で数字を作らない。** ブリッジは読めた形だけを窓にして返し、読めなければ
      CLI の返事をそのまま渡す(画面はそれを理由として出す)
  - `app/views/ai_usage.py` — 管理画面の「使用量」節と「取り直す」の受け口。
    **「AI の相手」の表とは分けてある** —— あちらは設定を一度入れたら開かない場所、
    こちらは何度も見に来る場所(重い仕事を頼む前に枠を見る)。
  - `app/jst.py` — **人に見せる日時の書式(JST 固定)**。保存と比較は UTC のまま、
    表示の直前だけここを通す。**変換と書式を 1 か所に集める** —— 画面ごとに書くと
    同じサーバーの中で表記も時差もばらつく。`astimezone()` に任せないのは、
    api コンテナの `TZ` 次第で表示が変わるため(`TZ` はログを読みやすくするためのもので、
    画面の正しさをそこに依存させない)。
  - `app/answer.py` — **「使う」層(`/v1/ask`・`/ai/chat`)の本体**。要求するのは
    OpenAI 互換の `/chat/completions` だけなので、ローカルの推論サーバでも Gemini・OpenRouter でも、
    CLI を包んだブリッジでも同じ 1 本の口で扱う。**`_normalize_base_url` が `/v1` を補うのは
    パスを持たない相手にだけ** —— Gemini の互換の口(`…/v1beta/openai`)は直下が
    `chat/completions` で、足すと 404 になる。**モデルは会話のたびに選べる**
    (`available_models` が相手に聞き、聞けなければ `app/providers.py` の控えに落ちる)。
    使い方・環境変数は
    `docs/ai.md` が、なぜこの形かは
    `docs/design-notes.md`「「使う」層はなぜ 2 段の RAG か」が正。実装側の要点:
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
    - **回答方針は `grounded` で切り替える**(既定 1 = 抜粋のみ)。「抜粋だけ」は Chiezo の
      設計思想ではなく**モデルの幻覚への対処**なので固定しない — Chiezo は AI 用の知識ベースで、
      ローカル LLM はそれを使う側。ただし `grounded=1` で抜粋 0 件のときは `has_no_basis()` が
      **推論を走らせず定型文を返す**。実測で gemma-3-1b が「抜粋が空でも自分の知識で答える」
      ことを確かめたため、プロンプトに委ねず経路として断ってある
    - 数値の環境変数は `_env_num()` で読む。compose は未設定の変数を `VAR=`(空文字)で
      渡すため、素直に `float()` すると「.env に書いていない」だけで 500 になる
    - ストリーミング(`?stream=1`)は**クエリ生成・検索を流し始める前に済ませる**
      (`prepare()` → `stream_answer()`)。SSE はヘッダ送出後にステータスを変えられない
    - `content_of()` が**思考タグの残骸を落とす**。thinking 系モデルは推論サーバの設定次第で
      `<think>…</think>` や閉じタグだけが `content` に残る(実測: Qwen3 + 思考オフで先頭に
      `</think>`)。相手の設定は Chiezo が握っていないので受け側で落とす
- **画像は CLI の内蔵ツールでも作れる**(`/v1/images/generations`。**持っているのは
  Codex と Antigravity だけ**で、claude は持たない。どちらも「エージェントにファイルを
  書かせる」形なので、ブリッジの段取りは共通で違うのは起動コマンドだけ)。**課金は ChatGPT の
  サブスク枠**で、API キーの `openai` とは別勘定(同じ gpt-image-2 でも出どころが違う)。
  会話の口は `-s read-only` だが、**画像はファイルに書き出されるので
  `-s workspace-write` にして、この 1 回のための作業ディレクトリにだけ許す**。
  拾うのは**その実行で増えた画像だけ**(保存先は使い回されるので、前回のぶんを混ぜない)。
  **作業ディレクトリを先に見て、共有の保存先(`$CODEX_HOME/generated_images`)は
  走らせる前にあったものを除く。さらに直列化する**(`_IMAGE_LOCK`)—— 共有の保存先を
  時刻だけで選ぶと、同時に走った別の実行の絵を返す(4 件同時に頼んで 4 件とも同じ絵が返った)。
  相手はエージェントなので、**保存先と枚数を言い切る**(曖昧だと説明だけ返してファイルを書かない)。
  **Antigravity は頼んだ場所へ直接書く**ので共有の保存先を見る必要は無いが、
  **音と動画の道具は持っていない**(バイナリに imagegen のハンドラはあるが、
  動画は protobuf の型だけで実装が無く、実際に頼んでも何も出ない)
- **外部の相手のエラーは本文を 600 字まで返す**(`media_backends.remote_error`)。
  300 字では 429 の `Quota exceeded for metric: … limit: 0` がちょうど切れて、
  「枠を使い切った」のか「そもそも枠が無い」のか分からなかった(2 度踏んだ)。
  **鍵はヘッダで送っている**ので本文には載らない。429 には「時間をおいても直らないなら
  無料枠に含まれていない可能性」というヒントを添える
- **Codex は MCP を引けない(上流の不具合。2026-08 時点)。** `codex exec` では
  MCP の呼び出しが必ず `user cancelled MCP tool call` になる —— 非対話では答えられない
  確認の経路に入るため(openai/codex#16685、未修正)。**ブリッジの作りの問題ではない**ので
  こちらでは直せないので、**そういう相手には agent を選ばせない**
  (`Provider.can_use_mcp` → `answer.resolve_mode` が rag に倒す。画面のセレクトからも消す)。
  rag なら Chiezo 側が抜粋を集めてプロンプトに載せるので、道具が無くても根拠が付く。
  **黙って質を落とすより引き方を倒すほうがよい** —— agent のまま投げると、
  Chiezo をまったく引いていない答えが「引いたつもり」で返る。
  絵と音の生成は MCP を使わないので影響しない
- **モデル名の控え(`app/providers.py` の `models`)は古くなる。** 相手からモデルが
  消えると 404 になり、画面には「llm error 404」としか出ない。**選んでも保存してもいない
  ときは、相手に聞いて先頭へ差し替える**(`answer.ensure_model`)。404 のときは
  「モデル名が違うかもしれない」というヒントも返す
- **`bridge/` — CLI ブリッジ(別イメージ `ghcr.io/<owner>/chiezo-bridge`)。**
  Claude Code CLI / Codex CLI / Antigravity CLI を OpenAI 互換の口に見せる。
  **イメージは 1 つで、`CHIEZO_BRIDGE_CLI` で役割を決める** —— イメージは 1 回 pull すれば
  ディスクは 1 つぶんで、コンテナを何個立てても増えるのは書き込み層(数 KB)だけなので、
  分けるより 1 枚が得。
  **node は最終イメージに入れない**(claude は自己完結バイナリ、codex も vendor の
  実体を直接叩ける)。**Gemini CLI は個人向けの提供が 2026-06-18 に終了したので外した** ——
  あれだけが JS バンドルで node を要求していた(後継は Antigravity CLI)。**amd64 のみ**ビルドする
  —— codex の vendor パスに x86_64 が直書きしてあるので、arm64 を作るならそこも変える。
  **MCP は任意**(`CHIEZO_BRIDGE_MCP_URL` を空にすると繋がない)。Chiezo 専用の部品ではなく、
  他のアプリからも使えるサービスとして立てられる。
  **本体には入れない** —— `chiezo-api` が数百 MB で動く前提を崩さないため(推論を同居させないのと
  同じ理由)。既定では立たず、`docker-compose.answer.yml` のコメントを外した人だけが pull する。
  - **プロンプトの渡し方は CLI ごとに違う。** claude / codex は標準入力から読むが、
    **agy(Antigravity)は `-p` の引数でしか受け取らない** —— 標準入力は読まず
    (`-p -` は "-" をプロンプトそのものとして扱う)、プロンプトを取るファイル用のフラグも無い。
    そのため Linux の単一引数の上限(MAX_ARG_STRLEN = 128KiB)に当たる。
    **超えるぶんはファイルに書いて読ませる**(`--add-dir` で作業対象に入れ、非対話なので
    `--dangerously-skip-permissions` を付ける)。短いプロンプトは今までどおり引数で渡す ——
    道具も権限も要らない確実な経路を、長さのためだけに捨てない。
    断って 413 を返していた頃は、pta の売買提案(候補ショートリスト込みで 120KiB 超)が
    毎回失敗していた。**書き出したファイルは答えを返す前に必ず消す**(中身はプロンプトそのもの)
  - **道具は CLI 自身に引かせる**。Chiezo の MCP(`/mcp`)を CLI に繋ぐので、検索して答える
    段取りをブリッジ側で組まない。Chiezo から見ると「1 回聞いたら答えが返る」ので
    `rag` / `agent` の区別は関係なくなる
  - **組み込みの道具は全部切る**(`claude --tools ""` / `codex -s read-only`)。知識ベースに
    答えるのにシェルもファイル操作も要らず、使えると危ない
  - **認証情報はイメージに焼かない**。環境変数で受け取り、ファイルが要る Codex だけ
    `entrypoint.sh` が起動時に書いてコンテナと一緒に捨てる
  - **CLI を 1 つ足すときに触る場所は 4 つある。** `cli_bridge.py`(起動コマンド・
    モデルの控え)だけ直しても動かない —— `entrypoint.sh` の `case`(**足りないと
    「未対応の CHIEZO_BRIDGE_CLI」で起動すらしない**)、`AUTH_CHECK`(**無いと
    「接続を試す」が通らず、画面から有効にできない**)、Dockerfile(CLI の導入)。
    実際に Gemini CLI を足したとき `entrypoint.sh` が取り残されて起動しなくなったので、
    `tests/test_bridge.py` が対応表の欠けを見張っている(その CLI は提供終了で外したが、
    検査は残してある)
  - **`GET /usage` は CLI にサブスクの枠を聞く**(`USAGE_CLIS` = codex / antigravity)。
    codex は `codex app-server` に JSON-RPC で `account/rateLimits/read` を 1 往復
    (**`codex exec` は使わない** —— あちらは会話を 1 回走らせるので枠を食う。
    枠組みは実測済み: 未サインインで
    `codex account authentication required to read rate limits` が返る = メソッドは実在する。
    **サインイン済みで返る中身はまだ見ていない**)、
    antigravity は print モードのスラッシュコマンド。**claude はここに来ない** ——
    CLI に出口が無いので Chiezo が `api.anthropic.com` に直に聞く(`app/usage.py`)。
    **相手の返事の形は決め打ちにしない**(`_windows_in` が入れ子のどこにあっても
    「使用率で言う窓」「残量で言う窓」を拾う)—— 版が変わって 1 段増えるだけで
    「取れない」に変わるため。**読めなければ数字を作らず、CLI の返事をそのまま返す**
  - ファイル名が `server.py` でなく `cli_bridge.py` なのは `ingest/server.py` と衝突するため
    (テストは api / ingest / bridge を同じ pythonpath で読む)

  - `app/agent.py` — **agent モード(`/v1/ask?mode=agent`)の本体**。LLM 自身に道具を
    引かせるループ。使い方・環境変数は `docs/ai.md`「agent モード(モデルに道具を引かせる)」節が、
    なぜこの形かは `docs/design-notes.md`「agent モード: 道具をモデルに引かせる」が正。
    実装側の要点:
    - **道具の定義も実行も `app/mcp_server.py` から借りる**(`list_tools()` → OpenAI の
      function 形式、実行は `call_tool()`)。書き写すと REST・MCP・agent の三重管理になる。
      システムプロンプト前半の使い方も MCP の `INSTRUCTIONS` をそのまま使う。
      `tests/test_agent.py` が `AGENT_TOOLS` と MCP のツール名を突き合わせて落とす
    - 道具は 2 群に分かれる。`KNOWLEDGE_TOOLS`(読み取り専用。常に渡す)と
      `NOTE_TOOLS`(`remember` / `recall`。**MCP にある `update` / `forget` は渡さない** ——
      追記と違い書き換え・削除は取り消しが効かず、ローカル LLM の誤操作に対して重すぎる)。
      **後者は Chiezo で唯一の書き込みを含む**ので、
      `notes_allowed()` が「notes が有効 かつ リクエストで切られていない」ときだけ渡す。
      当初は書き込みを一切渡していなかったが、会話で「覚えておいて」と明示的に頼まれるなら
      副作用ではないので渡す。代わりに**やり取りごとに切れる**(画面のトグル・`notes=0`)、
      **何を書いたかは step に出る**、の 2 つで見えるようにしてある
    - 上限は 3 つ(`CHIEZO_AGENT_MAX_STEPS` / `_TOOL_CHARS` / `_TIMEOUT`)。**予算を
      使い切っても打ち切らず**、道具を渡さずにもう 1 回だけ聞いて答えさせる(調べただけで
      終わらせない)。**同じ引数の呼び出しは実行し直さず、前回の結果を返す**
      (`repeated_payload`。モデルは 1 回の応答に同じ呼び出しを 2 つ並べて出してくる。
      ここでエラーを返すと、手元に結果があるのに「失敗した」と受け取って別の検索を
      足しに行き、ステップを空費する)
    - **道具の失敗はモデルに返す**(`execute()` は例外にしない)。404 の candidates は
      次の手の材料になる。ToolError の文言には MCP 側の前置きが付くので
      `_tool_error_payload()` で剥がしてから渡す
    - **最終回答はストリーミングしない**。ツール呼び出しかどうかは応答を途中まで読まないと
      分からず、断片から復元すると壊れやすい。代わりに `step` イベントで進捗を流す
    - 出典は道具の応答に出てきた文書を出現順に集めたもの。**本文の番号とは対応しない**
      (生の応答に番号を振る先が無いため)。web の結果は `source: "web"` として同じ一覧に
      混ぜる(どれが外から来たか出典を見た人に分かる必要がある)
    - **web 検索の道具だけは MCP から借りない**(`app/websearch.py` で定義)。Chiezo の MCP は
      「ためた知識の引き口」であって web はその外側だから — MCP の利用者(Claude Code)は
      自前の web 検索を持っている。有効なときだけ道具一覧に足す
  - `app/websearch.py` — **web 検索の道具(既定では無効)**。`CHIEZO_WEB_SEARCH_URL` が
    機能フラグを兼ねる(未設定 = 道具ごと出さない)。**使うかどうかはやり取りごとに選べる**
    (`agent.web_allowed()`: サーバー設定 AND リクエストの `web` が false でない)。
    画面のトグルは毎回これを送る。使い方は `docs/ai.md`「web 検索で
    足りないぶんを補う」節が正。実装側の要点:
    - **これは「使う」層(= Chiezo を使う側)の機能で、Chiezo 本体の機能ではない**。
      知識ベースそのものは引き続き外を叩かない。この整理を崩さないこと
    - **本文は取りに行かない**(タイトル・要約・URL だけ)。ページ取得はスクレイピングに
      踏み込む話で、相手への負担も壊れやすさも別次元になる
    - `MIN_INTERVAL` で**自分でレート制限をかける**。ツールループはモデルの気分で何度でも
      呼ぶので、呼ばれた回数ぶん素直に外へ出さない
    - `USER_AGENT` に**個人情報を入れない**(名乗るのはプロジェクト名だけ)。
      `tests/test_chat.py` が固定している
  - `app/views/` — **人間向けの HTML を返す画面**。`APIRouter` を持ち、`main.py` の末尾で
    `include_router` する。`admin.py`(管理画面と chiezo-trigger へのプロキシ、
    Claude Code 連携設定の配布。`TRIGGER_URL` もここ)/ `browse.py`(`/search/{source}/`)/
    `chat.py`(`/ai/chat` と会話画面の JS)
  - `app/deps.py` — **REST と画面が共有する下ごしらえ**(`get_source`、ORDER BY 断片の
    `exact_title_first` / `relevance_order`、古い DB を断る `require_*`)。
    **ここは app の他モジュールを import しない** —— views が main を import すると
    main → views(router 登録)との間で循環参照になるため、共有物はここへ降ろす
  - `app/registry.py` — /data 走査・ソース登録、`SUPPORTED_SCHEMA_VERSIONS` /
    `FILTER_MIN_SCHEMA_VERSION` / `TAG_MIN_SCHEMA_VERSION`
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
  - `app/known_sources.py` — `chiezo-trigger` が未設定・到達不能なときの控えの既知ソース一覧と、
    国選択画面の大陸表示名・言語選択画面の記事数階層(`WIKIPEDIA_TIERS`)。
    初期化できるソースの正は ingest 側の `ADAPTERS` で、通常は
    `chiezo-trigger` の `GET /sources` から受け取る(`views/admin.py` の `initializable_sources()`。
    osm 国別 195 件 + wikipedia 言語版 348 件あり、api 側に複製すると必ず腐るため)
  - `app/pages.py` — 管理画面・ブラウズ画面共通の HTML 組み立てヘルパー(`page_shell`, `esc`)と、
    画面の URL(`browse_url` / `doc_url`。出典のリンクもここを通すので、移すときに漏れない)。
    **URL を HTML に埋めるときも `esc()` を通すこと** — `browse_url` の中の
    `urllib.parse.quote` は percent-encode であって HTML のエスケープではない
    (CodeQL に反射型 XSS として指摘された。`tests/test_api.py::TestUrlLayout` が固定)。
    ファビコンは `assets/icon.svg` を最小化した data URI(`FAVICON_DATA_URI`)として埋め込む
    (api イメージのビルドコンテキストは `api/` のみで `assets/` を含まないため。原本を変えたら更新)。
    iPhone の「ホーム画面に追加」用に 180×180 の PNG(`APPLE_TOUCH_ICON_PNG`)も持ち、
    `page_shell` が `<link rel="apple-touch-icon">` を出す + `main.py` が
    `/apple-touch-icon.png` で配信する — iOS は SVG や data URI のファビコンをホームアイコンに
    使わないため。角丸マスクは iOS が自前で掛けるので角丸なし・全面塗りで描いてある
    (再生成手順は README「開発 > アイコンを変えたとき」節が正)。
    共通スタイル(`PAGE_STYLE`)は狭い画面向けにメディアクエリを 2 つ持つ。
    `56rem` 以下は**表だけ**を `display: block` + `overflow-x: auto` で横スクロールさせる ——
    列の多い表(登録ソース一覧は約 800px 要る)が画面からあふれると、ページ全体が横スクロール
    して本文まで画面外へ出るため。表を `<div>` で包む必要がないので、画面ごとの HTML 組み立ては
    触らずに済む。本文の抜粋(`td.snippet`)だけは折り返す(1 行に伸ばすと表が果てしなく広くなる)。
    `40rem` 以下(スマホ)は余白を詰め、ボタンと入力欄を指で押せる大きさにする
  - `/`(GET) — `/admin` へ 302 リダイレクト
  - `/admin`・`/admin/osm`・`/admin/wikipedia`(GET) — 簡易 HTML 管理画面(画面に何が出るかは
    `docs/api-reference.md`「人間向けの画面」節が正)。実装側の要点は 3 つ: ジョブ実行中は `page_shell` の
    `refresh` で 5 秒ごとに自動リロードする / `osm_<国>` 195 件と `<lang>wiki` 348 件は
    そのまま並べると他のソースが埋もれるので `group` で 1 行に畳み、国・言語の選択だけを
    `/admin/osm`(大陸ごとの `<details>`)・`/admin/wikipedia`(`WIKIPEDIA_TIERS` の記事数階層ごと)
    へ切り出す / `?q=` の絞り込みは JS なしのサーバ側フィルタ。
    **末尾に「いま動いているビルド」を出す**(`app/build_info.py`)—— タグ(`latest`)は
    上書きされ、デプロイ先が pull し忘れても外からは見えないので、イメージ自身に
    素性を持たせる。値は CI が `--build-arg` で焼き込み(`CHIEZO_BUILD_SHA` /
    `CHIEZO_BUILD_TIME`)、**渡らなければ「不明」と出すだけでビルドは通す**
    (手元ビルドを壊さない)。Python には Go の `runtime/debug.ReadBuildInfo` に
    当たる仕組みが無いので、自動では入らない。表示は JST 固定(オフセットで持ち、
    tzdata を引かない)
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
    このフックは前方一致ではなく**構造**(登場する URL が全て Chiezo か・コマンド位置に
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
    - **書き込めるソース(`Source.mutable` = notes)の中身は例示に引き写さない**(`_quotable()`)。
      例示のタイトル・タグは DB の実データから採るが、notes はユーザーが手元で書いたメモで
      機密が混じりうる。ブロックは `--project` でリポジトリ側にも生成できるので、
      見出しやタグが載るとコミットされて意図せず共有される。`<タイトル>`・`<タグ名>` に落とす
    - **文書数は載せない**。取り込みと notes への書き込みで変わり、ブロックを貼り替えない限り
      古い数字が残る。正確な件数はブロック自身が案内している `/v1/sources` で引ける
    - 生成時刻のフッターは **JST 固定**(`JST` 定数)。人が読む行なので、api コンテナの
      TZ 次第で表記が変わらないようにする
  - `/search/{source}/`(GET) — 検索フォーム(HTML)。**画面はすべて前置きの下に置く**
    (`/admin`・`/search/…`・`/ai/…`)。以前はソース名をそのままルート直下に置いていて、
    ルートがキャッチオールになるため `ask` や `admin` という名前のソースを足せなかった。
    URL の組み立ては `app/pages.py` の `browse_url()` / `doc_url()` に閉じてある。
    `?q=` 未指定時は一覧を出さずフォームのみ表示
    (jawiki 等の大規模ソースで rank_score 順の全件一覧がフルスキャンとなりタイムアウトするため)。
    `?q=` 指定時は結果一覧を表示し、`/v1/{source}/search` と同じロジック
    (FTS または短語のタイトル前方一致フォールバック)
  - `/search/{source}/doc/{doc_id}`(GET) — 文書詳細(title/tags/opening/body/links/extra)の HTML 表示
  - `/v1/notes`(POST)・`/v1/notes/recall`(GET)・`/v1/notes/{doc_id}`(PATCH / DELETE) —
    「覚える」層の REST。読み出しはコアスキーマなので `/v1/notes/search|doc|filter|tags` と
    `/search/notes/` のブラウズ画面もそのまま効く(専用の口は追記・書き換え・削除・
    時系列の想起だけ)。PATCH は渡した項目だけを差し替える(`tags` は丸ごと置き換え、
    空文字で全部外す。updated_at が現在時刻になり recall の先頭に浮く)
  - `/v1/ask`(GET) — 「使う」層の REST。`stream=0`(既定)は JSON 一括、`stream=1` は
    SSE(`references` → `delta` × n → `done`、失敗時は `error` を挟む)。
    無効なら 503、推論サーバに繋がらなければ 502、タイムアウトは 504。
    `mode=agent` は `app/agent.py` のループへ回す(SSE は `meta` → `step` × n →
    `references` → `delta` → `done`。**流し始める前に済ませられる検査はソースだけ**なので、
    それだけ `prepare_catalog()` で先に通し、残りの失敗は `error` イベントになる)
  - `/v1/chat`(POST) — 会話の口。`messages` の**末尾が今回の発言、それより前が履歴**
    (末尾が user でなければ 400)。**サーバーは会話の状態を持たない** — 履歴はクライアントが
    持って毎回送る(読み取り専用・LAN 内・複数ワーカーの前提を崩さないため。MCP を
    ステートレスにしたのと同じ判断)。rag / agent とも `/v1/ask` と同じ実装に流す
  - `/v1/media/backends?kind=`(GET) / `/v1/media/image`・`/audio`・`/video`・`/speech`
    ・`/transcribe`(POST) / `/v1/media/jobs/{id}`(GET) / `/media/{path}`(GET)
    — **絵・音・動画・声を作る口**
    (MCP の `image_*` / `audio_*` / `video_*` / `speech_*` / `transcribe` と同じ実体)。
    知識を引くのとは別の仕事だが、**MCP の登録先を増やさない**ために同じサーバーに載せている。
    実体は `app/media.py`(ジョブと置き場)/ `app/media_backends.py`(相手ごとの作り方)
    / `app/media_providers.py`(相手の定義。ComfyUI / Codex / Antigravity / Gemini / OpenAI / ElevenLabs)。
    **kind が違っても層を分けない** —— ジョブ・置き場・掃除・中断の後始末・配信は同じ仕事で、
    違うのは頼むときの語彙(サイズ / 種類と長さ / 尺 / 声)だけ。分けると同じ後始末を
    kind の数だけ持つことになる。呼び分けは `media_backends.generate_for` の 1 か所。
    `MediaProvider.kinds` がその相手に頼めるものを持ち、一覧は kind で絞る
    (Lyria に効果音は頼めないし、自前の GPU に読み上げは頼めない)。要点:
    **待たない**(job を返し `image_status` / `audio_status` で引く。生成は数秒〜数分で、
    待つと呼び出し側が先に切れる)。**中身そのものは返さない**(1 つ 1〜2MB あり、道具の結果はまるごと
    コンテキストに載る。返すのはパスと URL)。**ジョブは SQLite に持つ** ——
    chiezo-api は `--workers 2` なので、プロセス内の辞書だと頼んだワーカーと聞かれた
    ワーカーが別のときに「そんなジョブは無い」になる。設定 DB とは別ファイル
    (あちらは CLI ブリッジが読み取り専用でマウントしている)。**中断は Exception では
    拾えない** —— MCP の接続が切れるとタスクごと畳まれ、`except Exception` を素通りして
    job が running のまま残る(実際に起きた)。`CancelledError` を捕まえて記録し、
    ワーカーごと落ちた場合に備えて読み出し時に古い running を畳む(`_reap_stale`)。
    **保存する拡張子は中身に合わせる**(Gemini の絵は JPEG のみ、音は mp3 / wav / flac)。
    **配信はパスを解いてから**
    (`../` を踏ませない)。**置き場が無ければ MCP の道具ごと出さない**(使えない道具を
    コンテナに並べない、notes と同じ扱い)。**外部の相手の鍵は「話す相手」のものを流用する**
    (`credential_from`)—— 同じ鍵を 2 か所に入れさせると、片方だけ古くなる。
    そのために `app/providers.py` に `openai` を足してある(OpenAI 互換なので話す相手にも
    そのまま使える)。**例外は「話す相手」に対応が無い相手だけ**(ElevenLabs は会話が
    できないので借り先が無く、鍵も on/off も自分で持つ = `owns_toggle`。ComfyUI と同じ扱い)。
    **on/off も「話す相手」と共通** —— そちらで無効にした相手には
    絵も音も作らせない(`media_backends.unusable_reason`)。鍵を持っている相手を止めたのに
    片方だけ動き続けるのは、止めたつもりの人にとって事故になる。**元栓(「答える」層)を
    止めたら全部止まり、MCP の道具も出さない**(`media.tools_enabled`)。
    出し分けは **401 = 鍵が無い(入れれば直る)/ 403 = 無効(画面で有効にする)**。
    **サイズの語彙はこちらが `幅x高さ` で統一**し、相手の語彙(Gemini は比率、OpenAI は
    決まった組み合わせ)への変換は各バックエンドが受け持つ。**画素をそのまま使う相手
    (`exact_sizes` = ComfyUI)は一覧以外のサイズを断る**(`media.create_job`)——
    学習解像度を外れると絵は崩壊するのに、生成は成功として返るため、受け取った側は
    絵を見るまで気づけない。GPU を回す前に断るのが親切
  - **音は `sound`(効果音 / 曲)と `seconds` で頼む**(`media.start_audio_job`)。
    同じ「音」でもモデルが別物で、相手によっては口そのものが分かれている
    (ElevenLabs は `/sound-generation` と `/music`。絵と動画はさらに別で `/flows/*`)。ComfyUI も系統でグラフが変わり、
    **Stable Audio Open は text encoder(T5)を別に読む**が ACE-Step は all-in-one。
    **どちらの系統かは名前で見分ける**(`is_audio_checkpoint` / `_is_ace`)——
    ComfyUI に「このチェックポイントは何用か」を聞く口が無く、GPU に載せてから
    間違いに気づくのは高い。**長さは黙って丸めない**(`_check_audio` が上限超えを 400 で断る)
    —— 短くして返すと、呼んだ側は頼んだ尺で出来たと思ったまま短い素材を受け取る。
    **尺を渡す口が無い相手(Lyria)に秒数を渡されたら断る**(無視すると同じ勘違いが起きる)。
    **歌詞が空なら器楽として頼む** —— ゲームの BGM に歌が乗ると台詞と喧嘩する
  - **動画は「待ち時間」「尺」「重さ」の 3 つが絵と違う**(`media.start_video_job`)。
    **上限を別に持つ**(`CHIEZO_VIDEO_TIMEOUT`、既定 1200 秒)だけでなく、
    **job を畳む猶予も別**(`STALE_AFTER_VIDEO`)—— 絵と同じ基準で畳むと、まだ相手の中で
    作っている最中の job を「中断された」と書いてしまい、出来上がった動画を取りに行けない。
    **尺は丸めない**(`_check_video`)。相手ごとに受け付ける値が飛び飛びで
    (Sora は 4/8/12、Veo は 4/6/8)、寄せると「6 秒で頼んだのに 8 秒が返る」になる ——
    数分と数十 MB を使ってから気づく。**一度に頼めるのは 2 本まで**(絵は 4 枚)。
    **相手はどこも非同期**(頼んだ時点では id しか返らない)なので、待ち方は
    `media_backends._await_remote` に 1 つだけ置く —— 覗きに行く間隔は相手が決めている
    (ElevenLabs は動画で 10 秒に 1 回まで)。**Gemini だけ口が 2 通り**で、
    Omni Flash は絵と同じ `interactions`、Veo は `:predictLongRunning` を待って
    **鍵つきで**取りに行く(署名済み URL ではない)。名前で見分ける(`_is_veo`)
  - **声は向きが逆の 2 つ**(`media.start_speech_job` / `media.transcribe`)。
    **文字起こしだけ job にしない** —— 返るのが文字(数 KB)なので置き場も掃除も配信も
    要らず、その場で返すほうが呼ぶ側の手数が少ない。そのぶん口の形も違う(multipart)。
    **自前の GPU には読み上げを頼めない**(ComfyUI 本体に TTS のノードが無く、外部の拡張
    しか無い —— 入れたものでノード名も引数も変わるので、こちらからグラフを組み立てられない)。
    **声は名前で頼める**(`_elevenlabs_voice_id` が id に直す。id を控えている人はいない)。
    一覧に無い名前は**そのまま id として渡す** —— こちらが一覧を取り損ねただけ、という
    場合に頼みごと自体を潰さない。**生の PCM には WAV の殻をかぶせる**(`_ensure_wav`)——
    Gemini は 24kHz の生 PCM を返すことがあり、そのまま保存すると拡張子も中身も
    再生できないファイルになる(受け取った側は開くまで気づけない)。
    **文字起こしに渡せるのは置き場の中か届く URL だけ** —— chiezo はコンテナの中で
    動いていて、頼んだ人のディスクは見えない(受け取れるように見せると、あるはずの
    ファイルが「見つからない」と返ってくる)
  - `/v1/capabilities`(GET) — **chiezo 経由で AI に頼めることの一覧**
    (会話・読み上げ・文字起こし・画像・動画・音楽・SE)。
    語彙は `app/capabilities.py` の 1 か所に持ち、
    **画面も REST も同じものを見る** —— 会話は `app/providers.py`、絵と音は
    `app/media_providers.py` と持ち主が分かれているので、分類まで散らすと
    「何が頼めるのか」を数える場所が無くなる。
    **分類は仕事の単位で切る(実装の単位ではない)** —— 音楽と SE は job の `kind` としては
    どちらも `audio` だが、モデルも相手も別物(Lyria は曲しか作れない)なので別に数える。
    **読み上げと文字起こしを分けてあるのも同じ理由**(仕事の向きが逆で相手も別物。
    まとめると「読み上げはできるが文字起こしはできない」相手を「声が使える」と言うことになる)。
    `supported=false`(**実装が無い**)と「相手がいない」(実装はあるが鍵が未登録・GPU が無い)
    は別扱いにする。次にすることが違うため。**いまは全部 true だが仕組みは残す** ——
    次に分類が増えたときにまた要るし、消すと 2 つがまた同じ言葉に潰れる
  - `/v1/ai/backends`(GET) / `/v1/ai/complete`(POST) — **知識ベースを介さない素の口**。
    `/v1/chat` は必ず抽出を混ぜるので、**自分のプロンプトと材料を持っているアプリ**には使えない
    (無関係な抜粋が載り、トークンも余分に使う)。借りたいのは「話せる相手と鍵」だけ、という
    使い方のために分けてある(例: tech-antenna のサマリー生成)。`backends` は管理画面で
    on にした相手を、モデル(相手に聞けた場合はその答え)・エフォート・モデル指定の要否・
    **web 検索を持つか**(`web`)つきで返す。
    `complete` は渡された `messages` をそのまま 1 往復投げる(履歴の組み立ては無い)。
    **`system` ロールを許す**のはこの口だけ —— プロンプトを組むのは呼ぶ側だから
  - `/v1/ai/failures`(GET) — **AI への問い合わせが失敗したときの控え**(新しい順)。
    無人の呼び出しのために置いてある。定期実行が朝に落ちても、呼んだ側のログに残るのは
    `llm error 502` の一行だけで、**その場に居合わせないと理由が消える**
    (実測: pta の朝の提案が `claude failed` だけを残して落ちた)。
    記録するのは相手・モデル・状態・理由と**プロンプトのバイト数**まで。
    **中身は残さない** —— プロンプトと応答には呼んだ側の材料がそのまま入る
    (保有銘柄、家庭内の通信先…)。大きさだけ残すのは、失敗が大きさに寄っているのかを
    後から見分けるため(実測では寄っていなかった: 307KB が落ちた 90 分後に 324KB が通っている)。
    置き場は `state/ai_failures.db`(`app/ai_log.py`)。**`settings.db` とは別のファイル** ——
    あちらは消してはいけない設定、こちらは消してよい観測。`MAX_ROWS` で頭打ちにする
  - **`complete` の `web=true` で相手自身の web 検索を開ける**(既定は開けない)。
    ニュースの収集のように「いまの外の情報」が要る仕事を、`/v1/chat` の抽出を混ぜずに
    頼めるようにするため(例: paper-trade-advisor の市況ニュース収集・銘柄調査)。
    **持っているのは CLI ブリッジで包んだ相手だけ**(`Provider.bridge`)——
    API で直に叩く相手は OpenAI 互換の口に検索の項目が無い。
    **頼まれて開けないときは 400 で断る。** 黙って道具無しで答えさせると、呼ぶ側は
    それを「調べた結果」として受け取り、学習データから作った話が最新の材料として
    保存されてしまう(実際に「取得不可」と答えるべき場面で古い首相の名前が返った)。
    応答の `web` に**実際に開けたか**を載せるので、呼ぶ側は取り違えを検出できる
  - `/v1/ai/usage`(GET) — **各 AI の使用量**(枠の残りと、Chiezo が使ったぶん)。
    **既定では相手へ問い合わせない**(控えを返す)—— 画面もダッシュボードも定期的に引く口で、
    引かれるたびに外へ出ると見ているだけでレート制限に当たる。取り直すのは `?refresh=1`、
    1 相手だけなら `?backend=`。中身の決めごとは上の `app/usage.py` が正
  - 管理画面は**頼めることの一覧 + 相手ごとに 1 行の表**にまとめてある
    (`app/views/ai_settings.py`)。**行の欄はその相手で何ができるかしか示さない**ので、
    そもそも何を頼めるのか(と、まだ頼めないもの)は上の一覧が受け持つ。
    話す相手と絵・音の相手を別の節に分けていた頃は、**同じ相手(鍵も on/off も共通)が
    2 か所に出ていて、どちらが効くのか読めなかった**。「できること」の欄が
    話す・絵・音を同じ書き方(`✓` / `⚠ 理由`)で並べる。
    **印に絵文字を使わない** —— 環境によっては豆腐になり、いちばん見たい列が読めなくなる。
    **無効な相手は行ごと薄くする**(`tr.off`)。ボタンの文字だけでは、行が増えるほど
    いまどちらか読み取りにくい。**「接続を試す」の戻り先には節の印を付ける**
    (`#ai-providers`)—— 付けないとページの先頭へ戻され、結果が画面外のままになる。
    自前の GPU(ComfyUI)と ElevenLabs は「話す相手」ではないので、鍵・on/off・
    「接続を試す」もこの表の自分の行に置く(`owns_toggle`)。**ElevenLabs は
    会話ができないのではなく、会話の口が「先にエージェントを作って `agent_id` で話す」形で
    `app/providers.py` の枠(OpenAI 互換に 1 往復)に入らない**、が正確なところ。
    「接続を試す」は繋がるかに加えて**チェックポイントの有無まで見る**
    (立っていてもモデルが無ければ 1 枚も描けない)。**絵と音は別のファイルが要る**ので
    どちらが何件あるかも返す
  - **生成する CLAUDE.md ブロックにも絵と音の節を足す**(`claude_config.build_block` の
    `media=True`)。**MCP を登録していて、かつこのサーバーで作れるときだけ** ——
    呼べない道具を勧めない
  - **「どれに頼むのがよいか」は種類ごとの表に持つ**(`media_providers.PREFERENCE`)。
    **画面の並び(`order`)とは別物** —— あちらは設定を探すための並びで、
    頼む順で並べ替えると、いつも同じ場所にあった行が動く。
    **`all_providers(kind)` はこの順で返す**ので、`default_backend()`(名指ししなかった
    ときの相手)も道具の一覧もここに従う。**かつては自前の GPU が既定だった**
    (外へ出さず枠も食わない)が、**出来が違う**うえ、相手を名指ししない呼び出しが
    いちばん多い —— 枠を使いたくないときは `comfyui` を名指しする。
    **順位は相手ごとではなく種類ごと** —— 相手ごとに 1 つにすると、音で先頭にした相手が
    絵でも先頭になる(ElevenLabs は曲がよくても絵の相手としては選びたくない)。
    **表に無い相手は最後尾へ黙って回る**ので、`tests/test_media.py` が欠けを見張っている
  - **ブロックは「いま頼める相手」を名指しする**(`build_block(usable=…)`。
    出どころは `capabilities.usable_now()`)。「外部の生成 AI を選べる」と抽象的に
    書くより、「いま使えるのは ElevenLabs」と書いたほうが読んだ側が動ける。
    **名指しするのは使える相手だけ** —— 鍵の無い相手を勧めると、呼んで断られるまで
    分からない。**曲と効果音・読み上げと文字起こしは相手が違いうる**ので、
    違えば書き分ける(`_who`)。名前の但し書き(`ElevenLabs(声・…)`)は落とす ——
    ブロックは毎回のコンテキストに乗るので短く保つ
  - **話せる相手がいれば「手分けして調べる」の節も足す**(`/v1/ai/complete` を curl で
    叩く手順)。**頼み先に `claude` は出さない**(`HELPER_EXCLUDED`)——
    このブロックを読むのは Claude Code なので、Chiezo 越しにまた Claude Code へ
    投げても**同じサブスクの枠を食うだけ**で、手分けの目的(自分の枠を空けて、
    別の視点も入れる)から外れる。経路は塞がない(`/v1/ai/backends` には出る)。**MCP の登録は要らない**(道具ではなく普通の POST なので、
    生成される許可ルール `curl -s "<base>/` の前方一致にそのまま載る)。
    調べものが広いときに 1 人で抱えないための節で、**枠の残りの見方
    (`/v1/ai/usage`)まで書く** —— 使い切っている相手を避けられるように。
    **例示の JSON は `json.dumps` で組む** —— 手で書くとクォートの入れ子で崩れ、
    崩れた curl をそのままコピーされる
  - **「いま頼めるか」を数えるのは `capabilities.usable_now()` 1 か所**
    (`/v1/capabilities` とブロック生成が同じものを見る)。分けると、
    画面に出る相手と設定ファイルに書かれる相手がずれる
  - `/ai/chat`(GET) — 会話画面と、JS なし用の 1 問 1 答の HTML。**Chiezo を使う側の
    機能なので `/ai/` の下**(Chiezo 本体の画面と並びで区別する)。話す相手が 2 つ以上
    設定されているときだけ、相手を選ぶセレクトが出る。見た目は
    **この画面だけ作り込んである**(`app/pages.py` の `CHAT_STYLE` を `page_shell(style=…)` で
    上乗せ。管理画面・ブラウズ画面は素っ気ないままでよいので、CSS を混ぜない)。
    入力欄は数行ぶんの高さを持ち、設定(ソース・引き方・根拠・web 検索)はその下に並ぶ。見出しは
    **`AI(<モデル名>)と話す`**(`answer.model_label()`。`CHIEZO_LLM_MODEL` が無ければ
    推論サーバの `/models` に聞き、5 分覚える。取れなければ「AI と話す」)。
    **Chiezo は AI が引く知識であって AI 自身ではない**という関係を画面にもプロンプトにも
    出すため。**返事の Markdown は画面で組み立てる**(`MARKDOWN_JS` の
    `window.chiezoMarkdown`)—— モデルは見出し・箇条書き・表・コードで返してくるのに、
    素のテキストで出していたので `**` や `|` がそのまま並んでいた。
    **外部のライブラリは読まない**(LAN 内・オフラインで動く前提。CDN に頼ると
    外に出られない環境で装飾だけが消える)。**先にエスケープしてから印を置き換える**
    ——順番を逆にすると、生成物にタグを書かれた時点で入り込む。リンクは http/https だけ通す。
    **差分ごとに全文を描き直す**(Markdown は行のまとまりで意味が決まるので、
    届いた差分だけを足すと表や箇条書きが途中で切れた形のまま残る)。
    エラーは本文とは別の要素に足す(本文は描き直されるので混ぜると消える)。
    **JS なしの 1 問 1 答は素のテキストのまま**(あちらは会話も続かない代替経路)。
    既定はサーバ側で推論を回さず、inline JS(`CHAT_JS`)が `/v1/chat?stream=1`
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
    ソースのみの例外的依存)へ切り替えた経緯は `docs/design-notes.md`「Wikipedia は
    CirrusSearch ではなく XML ダンプから作る」を参照(要点: `text` は
    折りたたみセクションを検索インデックスから除外しており本文が欠落していた)。
    `xml.etree.ElementTree`(標準ライブラリ)でストリーミング解析し、`<redirect>` を持つ
    ページは 2 パス走査(パス1: リダイレクト元→対象タイトルの収集、パス2: 本体の Doc 生成)
    で aliases に変換する(`sources/osm.py` の relation 2 パスと同じ精神)。wikitext →
    プレーンテキスト変換は、最初の見出しより前のノード列(lead section)を `opening`、
    記事全体を `strip_code(keep_template_params=True)` した結果を `body` とする。
    **その前に地の文でないものを落とす**(`_drop_non_prose`): `strip_code()` は
    **タグの中身を本文として残す**ため、`<ref>`/`<references>` を消さないと注釈や出典の
    題名が地の文に流れ込む(「NIFRELは、大阪府吹田市千里万博公園内**所在地の実際は、…
    万博記念公園の中**に所在する」)。画像・音声・動画のリンクも説明文とパラメータが残る
    (「…「チセ」|300px|thumb 北海道博物館は…」)ので外し、`__NOTOC__` 等の
    マジックワードは `_clean_text` で消す(**決まった語だけ**を消す —— `__[A-Z]+__` の形で
    消すとプログラミング記事の `__CONSTANT__` まで落ちる)。**座標の抽出は落とす前に行う**
    (消した中に `{{Coord}}` があっても座標を失わないため)。詳細と限界は
    `docs/design-notes.md`「脚注・画像の説明・マジックワードは落とす」。
    **既存の DB は作り直すまで直らない**(取り込み時に確定するテキストのため)。
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
    `緯度度`/`経度度`・`latd`/`longd` のような名前付き引数にも対応。**`基礎情報 会社` は
    `本社緯度度` / `本店緯度度` と接頭辞が付く**ので別に持っている —— 素の `緯度度` だけを
    見ていた頃は店・メーカーの記事の座標がまとめて落ちていた。Wikidata の P625 を
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
    取り込む地物の種類(地名 + POI + 交通インフラ。いずれも `name` タグ必須)は
    `docs/operations.md`「地理データの守備範囲」節が正。地名と POI は同じ docs/docs_fts に混在し `search` は
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
    「地理データの守備範囲」(`docs/operations.md`)参照 — 実測で osm_japan の 73% は店舗・施設の裾で、
    全世界の地名を得る手段としては桁違いに非効率だった。
  - `sources/__init__.py` — アダプタレジストリ(新ソースはここに 1 行追加するだけ。
    管理画面には `chiezo-trigger` の `GET /sources` 経由で自動的に出るので、
    `api/app/known_sources.py` への複製は不要)。
    `osm_<国>`(下の `osm_regions.py` から 195 件)と `<lang>wiki`(下の
    `wikipedia_editions.py` から 348 件)だけは例外で、自動生成カタログから機械的に登録している。
    **このリポジトリに入れられないソース(プライベートな情報・ライセンスの都合)は
    別コンテナのプラグインから足す**(下の `sources/remote.py`)。使い方は
    `docs/adding-a-source.md` のケース 3 が正。
  - `sources/remote.py` — **外部プラグイン**(`CHIEZO_PLUGIN_SOURCES` に
    プラグインの URL をカンマ区切り)。**取得と整形はプラグイン、DB の構築は本体**という
    割り方にしてあり、プラグインは `GET /sources`(カタログ)と `GET /fetch?source=`
    (NDJSON)の 2 つを話すだけでよい。実装側の要点:
    - **プラグインは Chiezo のコードを含まない**。だから本体のスキーマ版が上がっても
      焼き直しが要らず、`/data` の書き込み権限も要らない(継承方式の弱点がここで消える)
    - **問い合わせは import 時ではなく `get_adapter()` / `GET /sources` のとき**。
      import 時に聞きに行くと、プラグインが起動する前に本体を立てられなくなる
    - **到達不能は警告して飛ばし、応答の形の誤りは落とす**。別コンテナである以上、
      再起動中に繋がらないのは正常な状態でありうる(そこで本体を止めると、プラグインが
      1 つ死んだだけで Chiezo 全体が動かない)。一方、繋がったのに形が違うのは不具合
    - **メタ(ダンプ日付・検証条件)は NDJSON の 1 行目**。HTTP ヘッダは latin-1 しか
      運べず日本語の代表タイトルを載せられないため(ヘッダで設計して実際に踏んだ)
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
    (管理画面の初期化一覧と国・言語選択画面はこれを読む。api 側は `CHIEZO_CATALOG_TTL` 秒
    〔既定 300〕キャッシュする — 大半は焼かれた静的な表だが、プラグインはマウントで実行時に
    足せるので永久に持てない。取り直しに失敗したら**古いカタログを捨てない**
    〔控えの `KNOWN_SOURCES` に落ちると管理画面から 545 件が消える〕。アダプタは実体化せずに答える。
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
  - `gen_claude_config.sh` — Chiezo 連携用の Claude 設定生成器。**使い方・オプション一覧は
    `docs/api-reference.md`「Claude Code から使う(設定ファイル自動生成)」節が正**で、
    ここには実装側の要点だけ置く:
    - `curl` + POSIX ツールのみで動く(**既存 JSON へのマージにだけ jq か python3 が要る**。
      どちらでも同じ結果になるよう両方の実装を持ち、jq を優先する)。
      稼働中の Chiezo の `/admin/claude-config.*` を取得して書き込むだけの薄いクライアントで、
      **生成の正は api 側 `app/claude_config.py`**。ベース URL はサーバーがアクセス元 URL から
      導出するので、接続に使った URL がそのまま生成物の curl 例・許可ルールになる
    - `--with-hook` を付けたときだけ自動許可フックも設置する。**既定では設置しない** —
      Claude が打つ Bash を毎回検査して自動承認しうる仕掛けで権限ルールより影響が広く、
      中身を読んで納得してから入れられるようにするため。フック本体が Python スクリプトなので
      設置には python3 が要り、欠けていれば落とす
    - settings のマージは「コマンドが `chiezo-autoallow.py` を含む既存エントリ」を落としてから
      足すので、再実行しても重複せず、設置先を変えたときも古いパスのエントリが残らない
    - MCP サーバーの登録も**既定で行う**(`--no-mcp` で無効)。**claude CLI があれば
      `--user` / `--project` どちらのスコープも `claude mcp add --scope {user,project}` に
      任せる**(remove → add で冪等)。設定ファイルの構造を自前で知らずに済み、jq も要らない
      ため。CLI の無い環境(VS Code 拡張のみ等)では `~/.claude.json` / `.mcp.json` の
      `mcpServers` へ直接マージする(新規なら API 応答をそのまま置く)。
      **CLI 任せの副作用**: `claude mcp add --scope project` は `.mcp.json` を書き直すので、
      `mcpServers` 以外の独自キーを書いていた場合は落ちる(このファイルの仕様は
      `mcpServers` だけなので許容している)
    - **権限と MCP は「入れられないなら黙って飛ばさず落とす」**(`die`)。どちらも既定で
      入れる設定なので、飛ばすと「設定が入ったつもり」で使い始めることになる。
      外したいときは明示的に `--no-permissions` / `--no-mcp` を付ける。
      前提の検査は取得より前にまとめてあり、**実際にマージが要るときだけ**落とす
      (書き込み先が無ければ API 応答を `cp` するだけなので jq も python3 も要らない)。
      `--print` は何も書き込まないので検査しない
    - **フックと違って既定で入れる**理由: フックが opt-in なのは Bash を自動承認しうる
      security 上の判断で、MCP 登録にはその性質が無い。ツール定義は常時コンテキストに
      載るが(7 ツールで約 4.4k 字)、既定で入れている CLAUDE.md ブロック(約 4.3k 字)と
      同程度で、Chiezo を設定する時点で使う前提なのだから片方だけ渋る理由が無い
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
  - `run_tests.sh` — テスト実行のラッパー(引数はそのまま pytest へ)。手元に依存が
    揃っていればそれで、無ければ **CI と同じ Python 3.12 のイメージを組み立てて** Docker で
    回す。ホストの python が 3.12 でない環境(依存に C 拡張があるので import から落ちる)で
    準備なしにテストを通せるようにするためのもの。入れるのは `requirements-dev.txt`
    (api + ingest + pytest + ruff を 1 本にまとめたロック)—— **2 つのロックを同時に
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
- `pyproject.toml` — **開発ツールの設定だけ**(ruff / pytest)。`[project]` は持たない ——
  pip で配るライブラリではなく、依存の違う 2 つのイメージとして動くアプリだから。
  - `pythonpath = ["api", "ingest"]` で両方を import 可能にする。以前は
    `tests/conftest.py` が `sys.path` を書き換えていたが、それは設定の置き場が無いことの
    回避策だった
  - ruff は `line-length = 120`(既定の 88 は日本語コメントに短すぎる)、
    `RUF001/002/003`(全角記号を「紛らわしい Unicode」と見なす)は落とす。
    自動生成カタログ 2 つは `E501` を per-file-ignore
- `*/requirements.in` / `requirements.txt` — **直接の依存は `.in` に範囲(>=)で書き、
  実際に入る版は `.txt`(全依存 + ハッシュ)で固定する**。作り直しは
  `scripts/lock_requirements.sh`(uv pip compile)。`.txt` を手で編集しない。
  ルートの `requirements-dev.in` は api + ingest + pytest + ruff をまとめたもので、
  CI と `run_tests.sh` の Docker 経路が使う
- `.github/workflows/ci.yml` — push / PR で `ruff check` と pytest を実行し、main への
  push で `chiezo-api` / `chiezo-ingest` / `chiezo-bridge` / `chiezo-searxng` の 4 イメージを
  マルチアーキ(amd64 / arm64。bridge だけ amd64)で
  GHCR へ公開(cc-tasks / travel-log の docker-publish と同じダイジェストマージ方式。
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
- `.github/dependabot.yml` — 依存更新の週次 PR(pip×3 / docker×2 / github-actions)。
  拾いたいのは `.in` の範囲で捕まらない「範囲外の新メジャー」と上限ピンの解除、
  Actions・ベースイメージの更新。**ロック(`requirements.txt`)の更新は任せない** ——
  uv がコンパイルした形式は Dependabot が書き戻せないので、範囲が動いたら手元で
  `scripts/lock_requirements.sh` を回す

## コマンド

セットアップは README、取り込み・運用(ダンプ更新、別マシンでのビルド、既存 DB の移行)の
手順と ingest の環境変数一覧は **`docs/operations.md` が正**。
ここには開発時にしか使わないものだけ置く。

```bash
# テスト(引数はそのまま pytest へ。依存が手元に無ければ Docker で回す)
scripts/run_tests.sh
scripts/run_tests.sh tests/test_notes.py -v

# lint(CI と同じ設定。設定は pyproject.toml、版は requirements-dev.txt で固定)
ruff check .
ruff check --fix .

# 依存のロックを作り直す(.in を編集したあと。--upgrade で範囲の中の最新へ)
scripts/lock_requirements.sh

# フィクスチャ再生成
python tests/fixtures/make_fixture.py
python tests/fixtures/make_osm_fixture.py
python tests/fixtures/make_geonames_fixture.py

# api/ ingest/ を変更したときのローカルビルドでの動作確認
# (docker-compose.yml は GHCR の公開イメージを pull するので、こちらを重ねないと反映されない)
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build

# 「答える」層(推論サーバ + 検索エンジン)も立てる。GPU なら cuda を後ろに重ねる
docker compose -f docker-compose.yml -f docker-compose.answer.yml --profile answer up -d
```

実データを落とさずに取り込みを試すなら、`DUMP_FILE`(ダウンロードを飛ばして手元のファイルを使う)
と `MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータを緩める)を使う。`tests/` はこの経路を
フィクスチャで自動化したもの。

`SOURCE` に渡せる名前や、そのイメージが焼く `schema_version` はイメージ単体に聞ける
(`docs/operations.md`「別マシンでビルドして .db を配布する」節。ローカルビルド版なら
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
(ソースごとの実際の数値は `docs/operations.md`「メモリについて」の表が正)。wikipedia / geonames は
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
効いてくるのはメモリでなくディスク(jawiki.db 約 42GB)。手順は `docs/operations.md`
「別マシンでビルドして .db を配布する」と `docs/build-on-another-machine.md`。

## 実装上の約束事

- **Python は 3.12 に留める**(api/ingest の Dockerfile・CI・開発環境で揃える)。現行の最新は
  3.14 で、**2026-08 に実測した限りテストは 3.14 でも全件通る**が、依存のうち
  `mwparserfromhell` だけ cp314 の wheel がまだ無く、sdist からのビルドになる
  (純 Python のフォールバックに落とすと wikitext 解析が桁で遅くなるので、C 拡張は死守する)。
  ingest イメージにビルド道具の出し入れを常設することになり、イメージは +7MB(276→283MB)で
  済むがビルド時間を 2 アーキぶん恒久的に払う。**上流が cp314 wheel を出した時点で上げる** —
  そのときは `FROM` の行と CI の `python-version` を変えるだけで済む(dependabot が週次で見ている)。
  api 側の依存は 3.14 でも全部 wheel があるので、待っているのは ingest の都合だけ。
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
- エラーレスポンスは `{"error": "..."}` 形式。**例外の文言や相手の応答本文をそのまま
  載せない**(接続先のホスト名などが内部構成の手がかりになる。認証が無いぶん、
  読めてよいのは「繋がらない/遅い/相手が 500」の区別まで)。詳細は `log` に残し、
  応答には種別(`reason` = 例外クラス名)だけ返す。agent の step も画面へ流れるので同じ扱い。
- **ドキュメントの強調は、`**` が括弧・句読点と隣り合わないようにする。** CommonMark の
  flanking rule では、`**` の外側が文字で内側が記号だと強調として認識されず、`*` がそのまま
  表示される。日本語は句点で終え鉤括弧で括る書き方が自然なので、両側とも踏みやすい:
  - **閉じ側**: `**…です。**次の文` は閉じられない(直前が句点)。`**…です**。次の文` と書く。
    `**…「無制限」**と` も同じ(直前が閉じ括弧)。括弧を強調の内側に収めて `**…「無制限」と解釈する**` にする
  - **開始側**: `より**「…」…**` は開始できない(直前が文字・直後が開き括弧)。`より、**「…」…**`
    のように直前を句読点か空白にする
  - 検出は markdown をレンダリングして本文に `*` が残るかを見る(`cmarkgfm` で段落ごとに描画し、
    code span を除いてから探す。**行単位で描画しないこと** —— 強調が複数行にまたがる書き方が多く、
    行で切ると全部が誤検出になる)
- **製品名は `Chiezo`(大文字始まり)、識別子は `chiezo`(小文字)**。散文・見出し・画面の
  文言・LLM へのプロンプト・エラーメッセージは `Chiezo` で書く。小文字のまま据え置くのは
  **値として意味を持つもの**だけ: サービス名(`chiezo-api` / `chiezo-ingest` / `chiezo-trigger` /
  `chiezo-llm`)、イメージ名、環境変数の接頭辞 `CHIEZO_`、MCP 登録名(`claude_config.MCP_SERVER_NAME`
  = `"chiezo"`)、CLAUDE.md ブロックのマーカー(`<!-- BEGIN chiezo … -->`。変えると既存の
  埋め込みを差し替えられなくなる)、`User-Agent`(`chiezo-ingest/0.1` と揃えた機械可読トークン)、
  `CHIEZO_LLM_MODEL` の既定値。**日本語名は付けない**(表記は `Chiezo` に一本化する)。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。chiezo-trigger・chiezo-llm・searxng は
  ホストへポート公開せず、chiezo-api からのみ内部ネットワーク経由で到達可能にすること
  (別ホストの chiezo-api から使うときだけ `docker-compose.lan.yml` で開ける)。
- **待ち受けは 7010 = API・7011 = 推論・7012 = 検索エンジン・7013 = CLI ブリッジ・
  7014 = 絵と音の生成(ComfyUI)で、コンテナの内と外を同じ番号にする**。
  番号が食い違うと URL を書くたびにどちらか迷う。既定が違うイメージは環境変数で寄せる ——
  searxng は granian で動くので `GRANIAN_PORT`(SearXNG の `SEARXNG_PORT` は設定ファイルの
  値で待ち受けには使われない)。ComfyUI は `CLI_ARGS` の `--port`(既定は 8188)。
- **「使う」層は既定で無効のまま保つ**。推論を chiezo-api の中で動かさない(配信側が
  数百 MB で動く前提)。LLM を呼ぶコードは `app/answer.py` と `app/agent.py` に閉じ、
  検索・文書取得は `app/main.py` の関数(agent は MCP の道具)を再利用する。compose では
  `docker-compose.answer.yml` を重ねて profile `answer` を付けたときだけコンテナが
  起動する状態を崩さない。
  **素の既定は `rag` + `grounded=1`**(小さな機械でも安全に動く側)。潤沢な環境では
  `CHIEZO_ASK_DEFAULT_MODE` / `_GROUNDED` で倒せるが、**素の既定は変えない**。
- **外へ出るのは「使う」層だけ**。Chiezo 本体(知識ベース)は ingest がダンプを取る以外
  外を叩かない。web 検索は使う側の機能として `app/websearch.py` に閉じ、既定は無効のまま保つ。
  有効にしたときも「どれが web 由来か分かる」ことを崩さない(出典の `source` が `web`)。
- **会話の状態をサーバーに持たせない**。`/v1/chat` は履歴を毎回まるごと受け取る。
  セッションを持つと read-only・複数ワーカーの前提が崩れる(MCP をステートレスにしたのと同じ)。
- **compose は「本体 + 上書き」に保つ**。`docker-compose.yml` は検索 API・MCP としての
  Chiezo(chiezo-api + chiezo-trigger + chiezo-ingest)だけを持ち、足すものは上書きを
  重ねる:`build`(手元ビルド)→ `answer`(推論と検索エンジン)→ `cuda`(GPU)→
  `lan`(「答える」層を別ホストへ公開)。重ねる順はこの並び。
  **上書きに本体の設定を写さないこと** —— 以前 `build` が本体の完全なコピーで、
  web 検索と回答パイプラインの設定が抜けたまま取り残された。`tests/test_compose_files.py`
  が行数で見張っている。
- **「答える」層は、コンテナだけを `docker-compose.answer.yml` に置く**。chiezo-api に渡す
  設定(`CHIEZO_LLM_URL` 以下)は本体側に残す —— 推論を LAN の別マシンに任せる使い方では、
  コンテナは要らず設定だけが要るため。
- **`searxng`(web 検索の道具が引く検索エンジン)は本体の compose に置く。** 推論とは
  独立しているため —— 話す相手が Gemini や Claude Code でも web 検索は要るのに、
  「答える」層の上書きに置いていた頃は、検索を使いたいだけで数 GB の推論サーバまで
  立ち上がっていた。**profile は付けない**(本体を上げれば立つ)。使うかは
  `CHIEZO_WEB_SEARCH_URL`(既定は空)が決めるので、立っているだけでは外へ検索を投げない。
  **設定はマウントせずイメージに焼き込む**(`searxng/Dockerfile` → `chiezo-searxng`)——
  マウントだと、リポジトリを置けない環境(単体定義)では立てられなかった。
  手元で設定をいじるときだけ、compose でマウントを重ねればよい。
  設定は `searxng/settings.yml`(既定値 + Chiezo から API として引くための 3 点)。
  **SearXNG の既定は HTML しか返さない**ので `search.formats` に `json` を足してある。
  手元でマウントして試すときは**読み取り専用**にすること —— 書き込み可にするとイメージが
  ディレクトリごと uid 977 に chown し、ホスト側から編集できなくなる。
- **絵と音の生成の ComfyUI は `docker-compose.image.yml`(profile `image`)に置く**。
  GPU が要るので既定では立てない。**GPU が別マシンにあるなら重ねず、`CHIEZO_IMAGE_URL` で
  そちらを指す**(推論サーバと同じ逃げ道)。**モデルは同梱も自動取得もしない** ——
  数 GB あり、ライセンスも配布条件もモデルごとに違う。**絵・音・動画で別のファイルが要る**
  (音は `stable-audio-open`(+ `text_encoders/t5-base`)か `ace_step`。
  **動画は置き場そのものが違う** —— Wan は UNet 単体で配られるので
  `diffusion_models/` に置き、`text_encoders/umt5_xxl_*` と `vae/wan_2.1_vae` を別に読む。
  対応しているのは Wan 系だけで、3 つのうち 1 つでも欠けるとグラフごと通らない)。
  **置き場はホストの `./models/comfyui/ComfyUI/models/` の下**(上書きが `./models/comfyui` を
  コンテナの `/root` に繋ぐので、ComfyUI 本体が展開される 1 段下になる)。
- **GPU の設定は `docker-compose.cuda.yml`(上書きファイル)に閉じる**。`gpus: all` は
  GPU の無い環境では起動そのものが失敗するので、本体の compose には書かない。
  **この上書きは NVIDIA 専用**(イメージが CUDA ビルドで、`gpus: all` も NVIDIA
  Container Toolkit の経路)なのでファイル名も `gpu` ではなく `cuda` にしてある。
  llama.cpp は同じビルド番号で rocm(AMD)・vulkan(ベンダー非依存)・intel などの
  イメージも出しているが、デバイスの渡し方が違う(ROCm は `/dev/kfd` と `/dev/dri`、
  Vulkan は `/dev/dri`)ため、対応するなら別の上書きファイルを起こすこと。
- **`docker-compose.standalone.example.yml` は「`.env` もシェルの環境変数も無い環境」向けの単体定義**。
  管理画面に YAML を貼り付けて起動するタイプの環境では `${...}` を解決できず、profile も
  付けられないため、値を直接書き・使うサービスだけを並べてある(chiezo-api + chiezo-trigger)。
  **「答える」層は設定だけを載せ、コンテナは載せない** —— 推論サーバと検索エンジンは
  別サーバーのものを指せば済み、この環境で同居させる前提が無いため(本体側と同じ
  「設定は残す・コンテナは外す」の分け方)。編集箇所は先頭の置き場アンカー
  (`x-data-dir` / `x-notes-dir`)に集約し、**ホスト側は絶対パス**にする
  (貼り付けて登録する環境では相対パスの基準が読めない)。
  `docker-compose.yml` を変えたらこちらも追従させること —— 値が直書きなぶん古くなりやすい。
  **追従漏れは `tests/test_compose_files.py` が検知する**(本体の chiezo-api に渡している
  環境変数が、コメントとしてでも単体定義に出てくるかを照合。実際に 2 回取り残された)。
  **実値を書いたコピー(`docker-compose.standalone.yml`)は `.gitignore` 済み**。
  リポジトリに置くのは雛形(`.example`)だけで、置き場の絶対パス・接続先の IP を
  コミットしない(追跡したままだと、手元で書き換えたものが `git add` に巻き込まれる)。
- コード(api/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(入口。概要・セットアップ・各機能の要約とリンク)と、対応する docs/ の詳細
  (`api-reference.md` = API 仕様と画面 / `ai.md` = 「使う」層 /
  `operations.md` = 取り込みと運用 / `design-notes.md` = なぜこの形か)、および本ファイル
  (CLAUDE.md、アーキテクチャ記述)もあわせて更新すること。**README には詳細を書き戻さない**
  (人に読ませる入口として 1 画面で全体像がつかめる長さを保つ)。
  ドキュメントだけを別コミット・別対応に先送りしない。
