---
paths:
  - "ingest/**"
  - "tests/test_ingest*.py"
  - "tests/test_download.py"
  - "tests/test_zim_download.py"
  - "tests/test_remote_plugins.py"
  - "tests/test_osm_mirror.py"
  - "tests/test_source_credentials.py"
  - "tests/fixtures/**"
  - "scripts/gen_osm_regions.py"
  - "scripts/gen_wikipedia_editions.py"
  - "scripts/refresh_rank_score.py"
---

# 取り込み(ingest/)とメモリ方針

chiezo-ingest(ワンショット構築バッチ)と chiezo-trigger、ソースのアダプタ。

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
  - `core.py` — コアスキーマ DDL と `Doc` 型(全ソース共通)。`SCHEMA_VERSION` は 4。
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
    (同じ doc_id が二度来たら最後の 1 件が残る)ため。
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
    **同じ 1 回の走査で代表画像も拾う**(`page_image_free` → `extra.image`)——
    数百 MiB のダンプを 2 度解かないため。値は Commons の URL で、
    **API は叩かずに手元で組む**(置き場はファイル名の MD5 の頭 1 文字 / 頭 2 文字で
    決まる規則。`commons_url`)。**読む側が画像を出すのに Wikipedia を叩かなくて済む**
    ようにするための値で、画像そのものは持たない(指すだけ)。
    **`page_image_free` のほうだけを読む** —— `page_image` には非フリーの局所
    アップロードが入り、そちらは Commons に無いので URL を組めない。
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
  - `sources/overture.py` — Overture Maps Places アダプタ(店舗・施設の POI)。
    **OSM は店舗レベルでは穴が多い**ので足した —— 新宿駅まわり約 1km 四方の実測で、
    `osm_japan` の飲食店 884 件に対し Overture は 4,466 件あり、`osm_japan` に
    入っていなかった老舗も揃っていた(web サイト率も OSM 18% / Overture 77%)。
    **ライセンスは CDLA Permissive 2.0 / Apache 2.0** で保存も再配布もでき、
    **OSM のデータを一切含まない**ので `osm_japan` と混ぜても ODbL は伝播しない。
    配布が S3 の GeoParquet なので、**このアダプタだけ `duckdb` に依存する**。
    **同じ名前の見分けは、その行だけで決まる札にする**(`_titled`)——
    流しながら通し番号を振っていた頃は、**取り込み直すたびに番号がずれた**
    (韓国ぶんを外した回で、名前が変わっただけの店が「新しい店」として収集へ
    85,818 件足された。見出しが同一性の鍵なので、番号が変われば別物に見える)。
    札は町の名前を先に使い、同じ町に同名があるときだけ GERS の id の頭 8 桁を足す。
    **入れる範囲は国で決める**(`country="JP"`。Overture の `addresses[].country`)——
    矩形は S3 から読む量を抑えるための粗い枠でしかなく、日本の枠
    (122-154E / 20-46N)には**韓国が丸ごと入る**。実測(2026-08-19.0)で枠の中身は
    JP 2,678,886 に対し KR 327,344・RU 6,595・CN 4,133・KP 413 で、**11% が
    日本ではない**。この索引を母集団にした収集では巡回の 13% がそこに使われていた。
    **国が空のものは落とす**(枠の中で 302 万件のうち 3 件しかない)
    (pyosmium / mwparserfromhell と同じ「そのソースだけの依存」)。
    **全世界は数百 GB あるので、落としてから絞るのではなく S3 上で bbox を絞ってから落とす**
    (DuckDB の述語の押し下げ)。**リリースのバージョン文字列は毎月変わる**ので
    決め打ちにせず、`release/` を列挙していちばん新しいものを使う(古い版を書いて
    「No files found」で落ちるのを実際に踏んだ)。Overture 自身が「重複・ゴミ・属性欠損が
    ある」と明言していて `confidence` での足切りが前提なので、**既定で 0.5 未満を落とす**
    (`OVERTURE_MIN_CONFIDENCE`)。その confidence をそのまま `rank_score` に入れる ——
    もともと 0.0〜1.0 で、「確からしい地物を上に出す」がコアスキーマの約束と合う。
    **分類の列は版で違う**(`_category_columns`)—— 2026-09 の版から `categories` が
    無くなり `taxonomy`(`primary` / `hierarchy` / `alternates`)に替わった。古い列を
    名指ししたままだと取り込みが列が無いと言って落ちるので、**列の定義を先に読んで
    あるほうを使う**。新しい版では**上位の分類(`hierarchy`)もタグにする** ——
    古い版では和食の店にも `restaurant` が別名として付いていて、呼ぶ側はそれで
    飲食店を引いている。語彙も動いている(`noodles_restaurant` が消えて
    `ramen_restaurant` などに分かれた)。**営業状態(`operating_status`)は読まない** ——
    列はあるが、日本の飲食店 49 万件で値が入っているのは 3 件(2026-09-23.1 で実測)。
    閉店の見分けには使えない
  - `sources/holidays.py` — 日本の祝日アダプタ(`holidays_jp`。内閣府「国民の祝日」の CSV)。
    1 日 = 1 文書で、`doc_id` は日付の数字(`20260921`)、見出しは `YYYY-MM-DD 名前`。
    タグは `祝日`(全件を `filter` で引くための共通のタグ)と年・年月。CSV は CP932 で読む。
    キーは要らず、世代の日付は `Last-Modified` から取る。**公表は翌年の分まで**なので、
    それより先の年が無いのは欠けではない。古い祝日は消えないので `sample_titles` は固定の 1 件でよい
  - `sources/isj.py` — 日本の住所の位置アダプタ(`address_jp`。国交省「街区レベル位置参照情報」)。
    県ごとの ZIP(Shift_JIS の CSV)を 47 本落とし、**代表の行だけ**を 1 街区 1 文書にする
    (同じ街区に地番ごとの行が並ぶ。全部採ると 3 割増える)。見出しは人が読む形、**引く鍵は別名**
    (`block_keys`。小字・`大字`・郡を省いた形も)。**鍵の作り方は `app/resolve.py` の
    `address_block_key` と同じにする**(別のイメージで互いを import できないので書き写してあり、
    `tests/test_ingest_isj.py` が両方を通して突き合わせる)。版は年 1 度上がるので、知っている版
    から先を HEAD で確かめる。`sample_titles` は取り込んだ中から選ぶ(街区は区画整理で変わる)
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
    `app/known_sources.py` への複製は不要)。
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
    - **ダンプ日付は 8 桁のほか秒まで入れた 14 桁も許す**。世代ファイル名になる値で、
      **1 日に何度も焼くもの**(集める層)では日付だけだと前世代を上書きしてしまう
    - **空のカタログは正常**。配るソースが実行時に決まるプラグインでは、
      まだ何も無い状態が起動直後の普通の姿になる。
      ここで落としていた頃は、配るものが 1 つも無いだけでカタログ取得が丸ごと 500 に
      なっていた
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
  - `download.py` — **ダンプのダウンロードの共通部品**(osm / geonames / wikipedia の 5 か所)。
    **落とすのは今までどおり curl**(`-C -` の再開・`--retry` は curl のほうが確か)で、ここは
    書き込み中の `.part` の大きさを数秒おきに見張るだけ。**curl の出力は実行ログにも画面にも
    届かない**ので、数 GB を落としているあいだログが「downloading …」の 1 行で止まり、
    進んでいるのか詰まっているのか、どのくらい遅いのかが読めなかった。
    進み具合は `core.report_progress` に置き(名乗った 1 本ごと。止める印と同じ置き場)、
    trigger の `/status` が走っている 1 本の `progress` として配り、管理画面の取り込みの塊が
    棒と「量 / 全体(割合) / 速さ / 残り時間」で出す(`admin._progress_html`)。
    **速さは直近 30 秒で測る**(始まりからの平均だと、途中で遅くなっても数字に出ない)。
    ログへは 1 分に 1 行(画面の実行ログは末尾の数十行しか持たないので、細かく書くと押し流す)。
    **止める印も見る** —— curl を待つだけだと落とし切るまで「止める」が効かなかった。
    降りるときは curl を終わらせ、`.part` は残す(次の回が続きから落とす)
    **続きは同じ URL からしかつながない**(`.part.url` に控える。控えの無い途中も捨てる)。配布元(ミラー)を
    切り替えると、日付が同じならファイル名も同じなのに中身は別物で、`-C -` が壊れたファイルを
    作る。osm の配布元は**管理画面で選ぶ**(記憶の面・OSM の面の「OpenStreetMap の配布元」。
    `settings_store.osm_mirror`。自由入力にせず Geofabrik / openstreetmap.fr の 2 択)。
    選んだものは API キーと同じ道で、取り込みを起こすたびに `POST /run` の本文
    (`osm_download_base`。trigger は https だけ受ける)で渡り、アダプタの `download_base` に入る。
    選んでいなければ trigger の `OSM_DOWNLOAD_BASE`、それも無ければ Geofabrik(Geofabrik は
    大きなファイルを何度も落とす IP を絞ることがあり、実測で IPv4 が 0.02 MiB/s まで落ちた)
  - **取り込みは途中で降ろせる**(`core.request_stop` / `check_stop` /
    trigger の `POST /stop` / 画面の「止める」)。**殺すのではなく、安全なところで
    降りる** —— 走っているのは daemon スレッドで、外から止める手段はそもそも無い。
    印を立てて、取り込みの側が区切りのいいところで見に行く(文書 1,000 件ごとと、
    素材を落としながら、段の変わり目)。降りるのは**切り替えより前**なので、
    **いま配信している世代はそのまま**。**`Stopped` では `on_broken` を呼ばない**
    —— あれは「焼けない素材」を次の回に拾わせないための仕掛けで、ここで呼ぶと
    集めた 1 回ぶんが捨てられる(押し直せば AI を呼び直さずに続きから焼ける)。
    **`stopped` は `error` と分ける**(人が降ろしたのと落ちたのとでは、次にすることが
    逆になる)。**すぐには止まらないことがある** —— 外から素材が届くのを待っている
    最中は、動いているのは向こうでこちらは待っているだけなので、印を見る手が無い
    **収集の取り込みは本体のほうも止める**(管理画面の「止める」が
    `ai_inflight.ask_caller_to_stop("collect:<名前>")` を呼ぶ)。本体が AI に集めさせて
    いるあいだ取り込みは返事を待っているだけで、印だけでは区画ごとに呼び続けた ——
    走っている往復に止める印を書き、`_collect_items` は区画を呼ぶ前に
    `caller_stop_wanted` を見て降りる(集めたぶんは焼く)
  - `zim_download.py` — **Wikipedia のオフライン版(Kiwix の ZIM)を落とす 1 本**(trigger の `POST /zim/run`・様子は `GET /zim`)。
    chiezo-app が記事の絵を引く置き場(`CHIEZO_ZIM_DIR`、既定 `/data/zim`。`app/wiki_zim.py`)へ、Kiwix の一覧から
    日本語版の画像入りの一番新しい版を `download.fetch` で落とす(進み具合・止める・続きからはダンプと同じ)。
    **ダンプの枠で走り、ダンプの取り込みとは並べない**(どちらもディスクと回線を大きく使う。job 名 `wikipedia_zim`)。
    空きが足りなければ始めない、揃ったら古い版を消す。一覧は 6 時間覚え、`GET /zim` は**外を待たない**(`peek`。
    状況の面から聞かれるので、まだ読んでいなければ null を返して裏で読む)
  - `server.py` — **chiezo-trigger**: 管理画面の初期化・再構築ボタンから叩かれる内部専用トリガー。
    ingest イメージを流用し、docker-compose.yml で CMD だけ `uvicorn server:app` に上書きする
    常駐コンテナ(`/data` に書き込み権限を持つ点だけ chiezo-ingest の one-shot 実行と異なる)。
    `POST /run/{source}` で `main.run(source, data_dir)` をバックグラウンドスレッドで実行し
    (同時に走らせるのは `CHIEZO_INGEST_SLOTS` 本まで・既定 1。埋まっていれば 429、
    **同じソースは 1 本ずつ**〔409。ブルーグリーンの切り替えが同じリンクを取り合う〕、
    **ダンプの取り込みも 1 本ずつ**〔429。`dumps/` の付属データを共有する〕)。
    **ダンプは別の枠で走る**(`CHIEZO_INGEST_DUMP_LANE`、既定で有効。`ADAPTERS` に載っているもの)
    —— 再構築は何時間もかかり、1 つの枠を取り合うと、そのあいだ収集も一切流れなかった。
    **1 本終わるたびに chiezo-app へ合図を送る**(`_tell_app_finished` →
    `POST {CHIEZO_APP_URL}/v1/ingest/finished`。どう終わったかは問わない)。
    あちらが待ち行列の次の 1 本をすぐ流すため。**届かなくても何もしない**(5 秒で諦める。
    あちらの時計が次の周で気づく)
    ダンプは専用の 1 枠を使い、`CHIEZO_INGEST_SLOTS` は収集・プラグインのぶんに残る。
    `/status` は各 1 本に `lane`(`dump` / `general`)、全体に `dump_lane` を載せ、
    `GET /sources` はダンプのソースに `dump: true` を付ける。**app の空きの数え方は
    `ingest_queue.general_jobs` / `is_full` / `dump_busy` に集める**(管理画面のボタンも
    そのソースの枠で決める。名乗らない古い trigger は今までどおり全部を数える)。
    小さい機械では `0` で 1 つの枠に戻せる(再構築と収集の焼き込みが同時にメモリを使うため)。
    **状態・ログ・止める印は 1 本ごと**(`_jobs` / `core.bind` / `POST /stop?source=`)——
    ログは書いたスレッドが名乗った 1 本に付け、名乗らないスレッド(アダプタが
    自前で立てるもの)の行は走っている全部に付ける。`/status` は `jobs` と `recent` に
    加えて**頭に 1 本ぶんの形も残す**(古い app が読めるように)。
    `GET /sources` で取り込めるソースのカタログ
    (名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
    wikipedia 言語版の表示名・自称・記事数)と、このイメージが焼くスキーマバージョン
    (`schema_version` = `core.SCHEMA_VERSION`。管理画面の「最新のスキーマバージョン」表示の正)を返す
    (管理画面の初期化一覧と国・言語選択画面はこれを読む。app 側は `CHIEZO_CATALOG_TTL` 秒
    〔既定 300〕キャッシュする — 大半は焼かれた静的な表だが、プラグインはマウントで実行時に
    足せるので永久に持てない。取り直しに失敗したら**古いカタログを捨てない**
    〔控えの `KNOWN_SOURCES` に落ちると管理画面から 545 件が消える〕。アダプタは実体化せずに答える。
    osm の `node_index` はカタログの既定を実行時設定〔`OSM_NODE_INDEX` >
    `BUILD_PROFILE=low_memory`〕で解決してから返す — 管理画面の必要メモリ表示を
    実際の実行条件と一致させるため)。
    **落ちた回は別に控える**(`/status` の `last_failure`)—— 状態もログも
    「いまの 1 本」ぶんしか持たないので、**次の取り込みが始まった瞬間に理由が
    読めなくなっていた**(本番で、落ちた 56 秒後に次が始まって何も残らなかった)。
    無人で回る層は、その場に居合わせない人が後から原因を追う。
        `GET /status` で state(idle/running/done/error)・
    source・started_at/finished_at・error・ログ tail(`chiezo.ingest` logger に登録した
    `_TailHandler` 経由)を返す。状態はプロセス内メモリのみ(永続化なし)。
    ホストへポート公開せず、`chiezo-app` からのみ docker 内部ネットワーク経由で到達可能
    (`chiezo-app` 側は環境変数 `CHIEZO_TRIGGER_URL` で URL を知る。未設定なら管理画面の
    初期化機能は無効)

## メモリ方針: 「足りると確認できたときだけ取り込む」

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
SQLite ファイルで、配信側 chiezo-app は read-only immutable で開くだけの常駐 80〜150MB。
効いてくるのはメモリでなくディスク(jawiki.db 約 42GB)。手順は `docs/operations.md`
「別マシンでビルドして .db を配布する」と `docs/build-on-another-machine.md`。
