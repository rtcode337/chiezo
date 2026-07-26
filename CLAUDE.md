# chiezo — ローカル知識サーバー

LAN 内で動く読み取り専用の知識検索 REST API。複数のデータソース(日本語 Wikipedia = `jawiki`、
OpenStreetMap 日本抽出 = `osm_japan`、GeoNames 全世界地名辞典 = `geonames`)を
ソースごとに独立した SQLite ファイル(`/data/<source>.db`)として収容する。

## アーキテクチャ

- `api/` — **chiezo-api**: FastAPI + uvicorn の常駐コンテナ。起動時に `CHIEZO_DATA_DIR`(既定 `/data`)を走査し、
  ファイル名の stem と `meta.source` が一致する `*.db` をソースとして登録する(世代ファイル
  `jawiki-20260701.db` は登録されず、シンボリックリンク `jawiki.db` のみ登録される)。
  - `app/main.py` — ルーティング(/, /healthz, /v1/sources,
    /v1/{source}/search|doc|filter|titles|links|random,
    /admin, /admin/init/{source}, /{source}/, /{source}/doc/{doc_id})
    - `/v1/{source}/filter` — 全文検索ではなく属性(`feature` / `area` / `bbox` / `wikidata`)の
      AND での一括抽出(Overpass 相当)。`docs` の生成列への索引付き検索なので
      `schema_version` 2 以上が必要(1 の DB には 409)。条件は `build_attribute_filters()` が
      SQL 断片に変換し、`search` / `doc` からも同じ関数で併用できる
    - `doc` は同名の別地物がある場合 `alternatives` を併記する(`fetch_doc_candidates()`)。
      OSM は「博多駅」のような名前が駅とラーメン店で衝突するため、黙って 1 件返すと
      取り違えに気づけない
  - `app/registry.py` — /data 走査・ソース登録、`SUPPORTED_SCHEMA_VERSIONS` /
    `FILTER_MIN_SCHEMA_VERSION`
  - `app/db.py` — スレッドローカル immutable 接続、5 秒クエリタイムアウト(超過は 504)
  - `app/fts.py` — FTS5 エスケープ(フレーズクォート + AND 結合)と 3 文字未満の前方一致フォールバック判定
  - `app/known_sources.py` — `chiezo-trigger` が未設定・到達不能なときの控えの既知ソース一覧と、
    国選択画面の大陸表示名・言語選択画面の記事数階層(`WIKIPEDIA_TIERS`)。
    初期化できるソースの正は ingest 側の `ADAPTERS` で、通常は
    `chiezo-trigger` の `GET /sources` から受け取る(`main.initializable_sources()`。
    osm 国別 195 件 + wikipedia 言語版 348 件あり、api 側に複製すると必ず腐るため)
  - `app/pages.py` — 管理画面・ブラウズ画面共通の HTML 組み立てヘルパー(`page_shell`, `esc`)
  - `/`(GET) — `/admin` へ 302 リダイレクト
  - `/admin`(GET) — 登録ソース(name/kind/lang/文書数/dump_date/built_at/schema_version)の一覧、
    未初期化ソース(初期化できるが `/data` に `.db` が無いもの)向けの初期化ボタン、
    `chiezo-trigger` のジョブ状況(state/source/log tail)を表示する簡易 HTML 管理画面
    (実行中は 5 秒ごとに自動リロード)。`group="osm"`(= `osm_<国>`)195 件と
    `group="wikipedia"`(= `<lang>wiki`)348 件はそのまま並べると他が埋もれるため、
    それぞれ 1 行に畳んで「国を選ぶ」`/admin/osm`・「言語を選ぶ」`/admin/wikipedia` へ誘導する
  - `/admin/osm`(GET) — OSM 国別ソースの国選択画面。大陸ごとの `<details>` に畳んだ一覧で、
    国名・region・pbf サイズ・必要メモリの目安・構築済みかどうかを出し、各行から初期化できる
    (`?q=` で国名・region の絞り込み。JS なしのサーバ側フィルタ)
  - `/admin/wikipedia`(GET) — Wikipedia 言語版の言語選択画面(`/admin/osm` と同じ構図)。
    記事数の階層(`WIKIPEDIA_TIERS`: 100 万以上/10 万〜/1 万〜/1 万未満)ごとの `<details>` に
    畳んだ一覧で、言語名(日本語)・コード・自称・記事数・構築済みかどうかを出し、
    各行から初期化できる(`?q=` で言語名・コードの絞り込み)
  - `/admin/init/{source}`(POST) — `chiezo-trigger` の `POST /run/{source}` へプロキシし、
    `/admin` へ 303 リダイレクト。`CHIEZO_TRIGGER_URL` 未設定なら 503、未知ソースなら 404、
    登録済みソースなら 409
  - `/admin/claude-config`(GET/HTML)・`/admin/claude-config.txt`(GET/text/plain)・
    `/admin/claude-config.permissions.json`(GET/application/json) —
    Claude 連携設定の生成 API。現在の登録ソースから CLAUDE.md ブロックと
    **権限ファイル(`settings.json`/`settings.local.json` の `permissions.allow`)**の
    両方を生成して配信する(実ファイルは書き換えない。ホームの `~/.claude/CLAUDE.md` 等は
    クライアント側にあり api からは見えないため)。`gen_claude_config.sh` はこの 2 つの
    エンドポイントを取得して書き込む。HTML はプレビュー + コピーボタン付き。
    curl 例・許可ルールのベース URL はアクセス元 URL のプロトコル・ホスト名・ポート
    (`request_origin`: スキーム=`X-Forwarded-Proto`(あれば)、ホスト=`X-Forwarded-Host`
    (あれば)→無ければ `Host` ヘッダ。`Host` はポートを保持する)から導出するので、
    リバースプロキシ越し・非標準ポートでもそのまま到達可能な URL になる
  - `app/claude_config.py` — 上記ブロックの生成ロジック。**生成の正はここ(api 側)**に置く。
    同一プロセスの DB を schema_version と索引付きの `WHERE lat IS NOT NULL LIMIT 1` 等で
    直接引くので、HTTP プローブと違い巨大ソース(jawiki)でも timeout の false-negative が出ない
  - `/{source}/`(GET) — 検索フォーム(HTML)。`?q=` 未指定時は一覧を出さずフォームのみ表示
    (jawiki 等の大規模ソースで rank_score 順の全件一覧がフルスキャンとなりタイムアウトするため)。
    `?q=` 指定時は結果一覧を表示し、`/v1/{source}/search` と同じロジック
    (FTS または短語のタイトル前方一致フォールバック)
  - `/{source}/doc/{doc_id}`(GET) — 文書詳細(title/tags/opening/body/links/extra)の HTML 表示
- `ingest/` — **chiezo-ingest**: ワンショット構築バッチ。
  - `main.py` — 共通フレーム: 取得 → `.building` へ構築 → FTS → 検証 → ブルーグリーン切り替え(シンボリックリンク差し替え、旧世代 1 つ保持)。
    アダプタが `EXTRA_FETCH_HOOKS`(`fetch_pageviews` / `fetch_page_props`)を持つ場合、
    `fetch()` の後にそれも呼ぶ(docs.extra 補強用の追加データ取得フック)
  - `lookup.py` — 取り込み中だけ参照する巨大な「ID → 値」対応表(リダイレクト・ページビュー・
    wikidata の Q 番号)を、メモリではなくディスク上の一時 SQLite に持つための小道具
    (`DiskLookup` / `DiskMultiMap` / ヌルオブジェクト `EMPTY`)。以前これらを dict で抱えて
    ja Wikipedia 規模(各 160〜190 万件、合計 GB 級)になり、XML 本体解析と同時に生きるため
    ホストごと OOM killer に巻き込まれて落ちた。常駐メモリは SQLite のページキャッシュ
    上限(既定 32MiB)に固定される。中断時にも消えるよう `iter_docs` の finally で必ず close する。
    これにより wikipedia 系の取り込みは軽くなり、必要メモリは 3GiB 見当に収まる
    (下記「メモリ方針」を参照)
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)。`SCHEMA_VERSION` は 2。
    `docs` に生成列(VIRTUAL)`feature` / `area` / `lat` / `lon` / `wikidata` を持ち、
    実体は従来どおり `extra`(JSON)から `json_extract` する射影+索引でしかない。
    アダプタ側は `extra` に詰めるだけでよく、`Doc` の形は変わらない
  - `sources/wikipedia.py` — Wikipedia 標準 XML ダンプアダプタ(`wiki_id` パラメータ化。
    全言語版は下の `wikipedia_editions.py` から機械的に登録される。pageview ドメインは
    URL 言語コードから `<lang>.wikipedia` を導出し、不規則な wiki だけ `WIKI_DOMAIN` で上書き)。`https://dumps.wikimedia.org/<wiki_id>/<date>/<wiki_id>-<date>-pages-articles.xml.bz2`
    (MediaWiki エクスポート形式、単一ファイル)を取得する。旧実装は CirrusSearch ダンプの `text`
    フィールドを docs.body にそのまま使っていたが、この `text` フィールドは Wikipedia の
    折りたたみ(collapsible)セクション(`{{hidden begin}}`〜`{{hidden end}}` 等)を検索
    インデックスから除外しており、例えば「ブラタモリ」の放送回一覧表のような内容が本文に
    一切含まれない欠落があったため、標準 XML ダンプ + wikitext 解析(`mwparserfromhell`。
    wikipedia 系ソースのみの例外的依存、下記参照)に切り替えた。
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
  - `sources/osm.py` — OpenStreetMap アダプタ(`region` パラメータ化、Geofabrik の
    `<region>-latest.osm.pbf` を pyosmium(libosmium バインディング)で解析)。
    Geofabrik が 2026 年に `.osm.bz2` 配布を終了し `.osm.pbf` のみになったため、標準ライブラリの
    `xml.etree` では読めなくなった。osm 系ソースに限り pyosmium への依存を許容している
    (それ以外は標準ライブラリのみの方針を維持。wikipedia 系ソースの mwparserfromhell が
    もう1つの例外、上記参照)。
    名前付き地物(`place=*` / `boundary=administrative` / 主要 `natural=*`)を地名辞典として、
    加えて主要 POI(`amenity=*` / `shop=*` / `tourism=*` / `leisure=*` / `historic=*` /
    `craft=*` / `office=*` / `healthcare=*`。`name` タグ必須)を POI 辞典として同一ソースに
    取り込む(地名と POI は同じ docs/docs_fts に混在し、`search` は両方をヒットさせる)。
    さらに交通インフラ(`VALUE_LIMITED_KEYS`: `railway` / `aeroway` / `aerialway` /
    `public_transport` / `highway` / `man_made` のうち駅・空港・IC/SA・橋・灯台などの
    「地点を指す値」だけを列挙。港は `amenity=ferry_terminal` で既に入る)も対象。値を
    限定するのは `railway=rail` や `highway=residential` のような、名前付きでも地点辞典
    として意味を成さない線形地物を除くため。`POI_KEY_SCORE` では交通インフラを POI より
    高く置く(「博多駅」で同名の飲食店を先に返す取り違えの防止)。
    OSM は node → way → relation 順で並ぶため 3 パスで読む: パス1
    (`_RelationScanHandler`)で対象 relation が参照する way ID と、`extra.area` 用の
    行政境界(既定 `admin_level=4`)relation のメンバー way を集め、パス2
    (`_AreaWayHandler`)で境界 way のノード座標から点内包判定器(`_AreaIndex`。緯度バンド
    索引付きレイキャスティング)を組み立て、パス3
    (`_MainHandler`)で pyosmium の `NodeLocationsForWays` によるノード座標自動解決を
    使いながら node/way/relation を走査して Doc を生成する。この位置インデックスの置き場は
    環境変数 `OSM_NODE_INDEX` で選ぶ。既定は RAM 上の `sparse_mmap_array` で、これが最速
    (参照ノードぶんの座標=1件16B程度を抱えるため日本抽出で5〜10GB。足りるかは
    `require_build_memory()` が事前検査する)。メモリの少ない環境向けに
    `sparse_file_array` を指定すると `<dumps>/<source>.nodeloc.idx` へ退避でき、必要メモリは
    2GiB まで下がる代わりにノード解決がランダム読みになり数倍〜10倍遅くなる(欧州大陸は
    RAM 索引が非現実的なので実質こちら)。ファイル索引の一時ファイルは `iter_docs` の finally で
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
    `wikipedia_editions.py` から 348 件)だけは例外で、自動生成カタログから機械的に登録している
  - `sources/osm_regions.py` — **自動生成物**。Geofabrik の国別抽出カタログ
    (`scripts/gen_osm_regions.py` で再生成。Geofabrik の index-v1.json + 大陸別 HTML の
    pbf サイズ + CLDR の国名/主要言語から起こす)。1 件あたり region パス・日本語表示名・
    lang・pbf サイズ・必要メモリの目安・既定のノード座標索引・検証の最低文書数を持つ。
    手で 195 行書くと region パスの綴り間違いにダウンロード時まで気づけないため機械生成にした。
    `memory_gb` は pbf 1GB あたり 5GiB(osm_japan の実績: pbf 2.3GB → 12GiB)、
    それが 12GiB を超える国は `node_index` をディスク索引にして「どのソースも 12GiB のマシンで
    構築できる」方針を保つ
  - `sources/wikipedia_editions.py` — **自動生成物**。Wikipedia 言語版カタログ
    (`scripts/gen_wikipedia_editions.py` で再生成。Wikimedia sitematrix + wikistats の記事数 +
    CLDR の言語名日本語表記から起こす)。1 件あたり URL 言語コード(`lang`。pageview ドメインの素)・
    dbname(`wiki_id` = ソース名。ダンプ URL の素)・日本語/英語の言語名・自称・記事数・
    検証の最低文書数(記事数の 50%)を持つ。言語コードは 2 系統ある点に注意
    (sitematrix は現行コード yue、URL・pageview・wikistats は歴史的コード zh-yue。
    `lang` には URL コードを入れてある。dbname はハイフンを含まない zh_yuewiki 形式なので
    世代ファイル名の区切りと衝突しない)
  - `server.py` — **chiezo-trigger**: 管理画面の初期化ボタンから叩かれる内部専用トリガー。
    ingest イメージを流用し、docker-compose.yml で CMD だけ `uvicorn server:app` に上書きする
    常駐コンテナ(`/data` に書き込み権限を持つ点だけ chiezo-ingest の one-shot 実行と異なる)。
    `POST /run/{source}` で `main.run(source, data_dir)` をバックグラウンドスレッドで実行し
    (同時実行は 1 ジョブまで、429/409 で拒否)、`GET /sources` で取り込めるソースのカタログ
    (名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
    wikipedia 言語版の表示名・自称・記事数)を返す
    (管理画面の初期化一覧と国・言語選択画面はこれを読む。アダプタは実体化せずに答える)。
    `GET /status` で state(idle/running/done/error)・
    source・started_at/finished_at・error・ログ tail(`chiezo.ingest` logger に登録した
    `_TailHandler` 経由)を返す。状態はプロセス内メモリのみ(永続化なし)。
    ホストへポート公開せず、`chiezo-api` からのみ docker 内部ネットワーク経由で到達可能
    (`chiezo-api` 側は環境変数 `CHIEZO_TRIGGER_URL` で URL を知る。未設定なら管理画面の
    初期化機能は無効)
- `scripts/` — 補助スクリプト(api/ingest 本体ではない運用ツール)
  - `gen_claude_config.sh` — chiezo 連携用の Claude 設定生成器。`curl` + POSIX ツールのみで
    動く(Python 不要。既存 settings へのマージにのみ jq を使う)。稼働中の chiezo
    (`--base-url`、既定 `http://localhost:9000`。環境変数 `CHIEZO_URL` でも指定可)の
    `/admin/claude-config.txt` と `/admin/claude-config.permissions.json` を取得して
    書き込むだけの薄いクライアント(生成の正は api 側 `app/claude_config.py`。
    ベース URL はサーバーがアクセス元 URL から導出するので、接続に使った URL が
    そのまま生成物の curl 例・許可ルールになる)。対象 CLAUDE.md へ
    `<!-- BEGIN chiezo (auto-generated) -->`〜`<!-- END chiezo -->` のマーカーブロックを
    書き込む。書き込み先は既定 `--user`(`~/.claude/CLAUDE.md`。推奨)、
    `--project`(`./CLAUDE.md`)、`--target/-o <path>`。
    共存は 2 方式: `--merge markers`(既定・冪等にブロックだけ差し替え)と `--merge headless`
    (`claude -p` に既存との統合を任せる)。既定で対象の settings に curl 許可を追記
    (`--no-permissions` で無効化)。`--offline`/`--sources`/`--no-examples` は廃止
    (生成がサーバー側になったため稼働中の chiezo が必須)。
    README の「Claude Code から使う」節と対応
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
- `tests/` — フィクスチャ(`fixtures/mini_jawiki.xml.gz` 12 文書、`fixtures/mini_osm.osm.pbf`、
  `fixtures/mini_geonames.zip` ほか geonames 一式)での API / ingest テスト
- `.github/workflows/ci.yml` — push / PR で pytest を実行し、main への push で
  `chiezo-api` / `chiezo-ingest` の 2 イメージをマルチアーキ(amd64 / arm64)で GHCR へ公開
  (cc-tasks / travel-log の docker-publish と同じダイジェストマージ方式。
  arm64 の無料ランナーが public 限定のため、リポジトリが private の間は公開ジョブをスキップ)

## コマンド

```bash
# テスト(api/requirements.txt + ingest/requirements.txt + pytest が必要)
python -m pytest tests/ -v

# フィクスチャ再生成
python tests/fixtures/make_fixture.py
python tests/fixtures/make_osm_fixture.py
python tests/fixtures/make_geonames_fixture.py

# API 起動(Docker)
docker compose up -d

# 取り込み(本番: jawiki は 5〜6GB ダウンロード、構築 2〜6 時間、ディスク空き 80GB 推奨)
docker compose --profile ingest run --rm chiezo-ingest

# OSM 日本抽出の取り込み(.osm.pbf 2〜3GB ダウンロード、POI 込みで構築 1〜4 時間)
docker compose --profile ingest run --rm -e SOURCE=osm_japan chiezo-ingest

# 取り込み後の反映
docker compose restart chiezo-api

# 管理画面(http://localhost:9000/、/admin へリダイレクト)から未初期化ソースの初期化も可能
# (chiezo-trigger 経由。完了後は上と同様に chiezo-api の再起動が必要)

# Claude 連携設定(CLAUDE.md ブロック)を稼働中の chiezo から生成(既定は ~/.claude/CLAUDE.md)
scripts/gen_claude_config.sh --base-url http://localhost:9000       # curl のみ・追加インストール不要
```

ingest の主な環境変数: `SOURCE`(必須)、`DUMP_DATE`(日付固定)、`DUMP_FILE`(ダウンロードスキップ)、
`MIN_DOCS` / `SAMPLE_TITLES`(検証パラメータ上書き。小規模データでの動作確認用)、
`PAGEVIEW_PERIOD`(ページビュー突合対象の年月 `YYYY-MM` を固定。省略時は最新月を自動検出)、
`OSM_AREA_ADMIN_LEVEL`(`extra.area` に入れる行政区の admin_level。既定 4 = 都道府県。
`0` で境界ポリゴンのパスごと省略)、
`OSM_NODE_INDEX`(osm のノード座標索引の置き場。既定 `sparse_mmap_array`=RAM、
`sparse_file_array`=ディスク)、`GEONAMES_ALT_LANGS`(取り込む別名の言語。既定 `ja,en`、
`*` で全言語)、`GEONAMES_FEATURE_CLASSES`(取り込む feature class。既定 `AHLPSTUV` =
道路 `R` 以外)、`BUILD_MEMORY_GB` / `SKIP_MEMORY_CHECK`(構築前メモリ検査の上書き / 無効化)。
OSM 系ソースでは Geofabrik が latest 1 世代のみ配布のため、`DUMP_DATE` は取得対象の固定ではなく
世代ファイル名ラベルの上書きとしてのみ機能する。

### メモリ方針: 「足りると確認できたときだけ取り込む」

**取り込み系コンテナに `mem_limit` は課さない。** 上限で締めても、足りないときは OOM killer が
数時間かけた構築を最後に殺すだけで得がない(実際に 1GiB で締めて osm のノード座標索引が
入りきらず落ちた)。代わりに `main.require_build_memory()` が**構築開始前**にメモリを検査し、
足りなければダウンロードもせず `SystemExit` する。判断材料は `/proc/meminfo` の `MemAvailable` と
cgroup 上限(`memory.max`)の小さいほう(`available_memory_bytes()`)。

必要量は各アダプタの `min_build_memory_gb`(`core.SourceAdapter` の一部)で宣言する:
- `WikipediaAdapter` = 3GiB 固定。巨大な対応表は `lookup.py` でディスクに逃がしてあるため軽い。
- `GeonamesAdapter` = 3GiB 固定。別名(2,000 万行規模)を同様にディスクへ逃がしてあるため軽い。
- `OsmAdapter` は**プロパティ**で、使う索引方式(`node_index_kind` = 環境変数 `OSM_NODE_INDEX` >
  ソースごとの `default_node_index`)がファイル索引なら 2GiB、RAM 索引なら `ram_index_memory_gb`。
  osm_japan は RAM 索引が既定で 12GiB。国別ソースの `ram_index_memory_gb` / `default_node_index` は
  `sources/osm_regions.py`(自動生成カタログ)が pbf サイズから決める — RAM 索引の見積もりが
  12GiB を超える国(仏独加米露)はディスク索引が既定になり、2GiB で焼ける代わりに遅い。
  osm ソースは**国スケールに留める**こと。大陸スケールはディスク索引にしても
  ディスク 300GB・構築 1 日以上が要るため現実的でなく、全世界カバーは `geonames` の担当。
  **既定設定でどのソースも 12GiB 以内に収まること**をテストで担保している
  (`test_no_source_requires_more_than_12gb_by_default`)。

`.db` は自己完結した単一 SQLite ファイルなので、**メモリの多いマシンで焼いて配信機へコピー**すれば
よい(`docker save` した ingest イメージ + `handoff/BUILD-ON-ANOTHER-MACHINE.md` だけ持ち出せば、
ビルド機にリポジトリを置かずに完結する)。配信側 chiezo-api は read-only immutable SQLite を
開くだけで常駐 80〜150MB のため、**メモリ数百 MB の小型機でも動く**(効くのはメモリでなく
ディスクで、jawiki.db 約42GB の空きが要る)。この非対称性が設計の要なので壊さないこと。

## 実装上の約束事

- コアスキーマ(meta / docs / aliases / docs_fts)は全ソース共通。ソース固有情報は `docs.extra`(JSON)へ。
  変更は最終手段で、`schema_version` を上げ api 側で複数バージョン対応する。
- ソース間で JOIN しない。API はソース種別を意識せず docs/aliases/docs_fts のみ参照する。
- FTS5 は trigram。ユーザー入力は必ずフレーズエスケープしてから MATCH に渡す(`app/fts.py` 経由)。
- 運用 DB は読み取り専用(`immutable=1`)。更新はブルーグリーン(別ファイル構築 → シンボリックリンク差し替え → api 再起動)のみ。
  `/data` への書き込み権限を持つのは chiezo-ingest(one-shot)と chiezo-trigger(常駐)だけで、
  chiezo-api は引き続き read-only マウント。
- エラーレスポンスは `{"error": "..."}` 形式。
- 認証なし・LAN 内前提。ルーターでポート開放しないこと。chiezo-trigger はホストへポート公開せず、
  chiezo-api からのみ内部ネットワーク経由で到達可能にすること。
- コード(api/ ingest/ の挙動・エンドポイント・環境変数など)を変更したら、同じ変更で
  README.md(セットアップ・API 仕様・運用手順)と本ファイル(CLAUDE.md、アーキテクチャ記述)も
  あわせて更新すること。ドキュメントだけを別コミット・別対応に先送りしない。
