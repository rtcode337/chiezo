# 運用

取り込み・更新・移行・配布と、運用上の前提(メモリ・セキュリティ)をまとめます。
なぜその方針なのかは [設計メモ](design-notes.md) が正です。

## ダンプ更新(ブルーグリーン)

ingest は毎回 `data/corpus/<source>-<date>.db` を新規構築し、検証が通ったらシンボリックリンク
`data/corpus/<source>.db` を差し替えます。旧世代は 1 つだけ残します。差し替えは chiezo-app が
数秒以内に自動検知して新しい DB へ切り替わるので、再起動も停止時間もありません。

```bash
docker compose --profile ingest run --rm chiezo-ingest
```

月次 cron の例:

```cron
0 3 1 * * cd /opt/chiezo && docker compose --profile ingest run --rm chiezo-ingest
```

中断しても運用 DB は壊れません(`.building` の一時ファイルに構築するため)。
再実行すれば最初からやり直します。

### ingest の環境変数

| 変数 | 説明 |
|---|---|
| `SOURCE` | 取り込むソース名(必須。compose 既定は `jawiki`。`-e SOURCE=osm_japan` 等で上書き) |
| `DUMP_DATE` | ダンプ日付 `YYYYMMDD` を固定(省略時は最新を自動検出。osm 系は常に latest を取得するため世代ラベルの上書きのみ) |
| `DUMP_FILE` | ダウンロードをスキップし既存ファイルを使う(カンマ区切りで複数シャード指定可) |
| `PAGEVIEW_PERIOD` | ページビュー突合対象の年月 `YYYY-MM` を固定(省略時は最新月を自動検出) |
| `MIN_DOCS` / `SAMPLE_TITLES` | 検証パラメータの上書き(小規模データでの動作確認用) |
| `ALLOW_SHRINK` | `1` で、いまの世代の半分を切る件数でも焼く(既定では断る。意図して大きく減らすときだけ) |
| `OSM_DOWNLOAD_BASE` | OpenStreetMap の配布元の頭(既定は Geofabrik `https://download.geofabrik.de/`)。**ふだんは管理画面で選ぶ**(記憶の面・OSM の面の「OpenStreetMap の配布元」。選んだものが取り込みを起こすたびに渡り、この環境変数より優先される)。この変数は画面で選んでいないとき・画面を通さない単発の取り込みでの既定。Geofabrik と同じ並び(`<頭><地域>-latest.osm.pbf`)のミラーを指す。例 `https://download.openstreetmap.fr/extracts/`。**Geofabrik は大きなファイルを何度も落とす IP を絞ることがある**(実測で IPv4 が 0.02 MiB/s まで落ち、同じ時刻にミラーなら 3 MiB/s 出た)ので、そのときの逃げ道。**抽出の切り方は配布元ごとに少し違う** —— openstreetmap.fr の日本は北方領土(OSM ではロシアのサハリン州)を含み、硫黄島・南鳥島・沖ノ鳥島を含まない。地域の名前も配布元ごとに違いうるので、日本以外は確かめてから使う。配布元を変えたとき、前の配布元から途中まで落としたファイルは捨てて最初から落とす(続きにつなぐと壊れる。どこから落としたかの控えが無い途中のファイルも捨てる) |
| `OSM_AREA_ADMIN_LEVEL` | `extra.area` に入れる行政区の admin_level(既定 4 = 都道府県。`0` で境界パス省略) |
| `BUILD_PROFILE` | 構築プロファイル。既定は `low_memory` = **どのソースも 2GiB で構築できる**(構築用 SQLite キャッシュを絞り、osm のノード座標索引をディスクへ。osm は数倍〜10 倍遅い)。メモリの潤沢なビルド機では `fast` を実行時に明示すると速度優先になる([メモリについて](#メモリについて)) |
| `OSM_NODE_INDEX` | osm のノード座標索引の置き場(`sparse_mmap_array`〈RAM・速い〉/ `sparse_file_array`〈ディスク・省メモリ・遅い〉)。明示指定は `BUILD_PROFILE` より優先。未指定なら low_memory はディスク、fast はソースごとの既定(RAM 索引が 12GiB を超える国のみディスク) |
| `GEONAMES_ALT_LANGS` | geonames で取り込む別名の言語(カンマ区切り。既定 `ja,en`、`*` で全 400 言語超) |
| `GEONAMES_FEATURE_CLASSES` | geonames で取り込む feature class(既定 `AHLPSTUV` = 道路 `R` 以外すべて) |
| `OVERTURE_MIN_CONFIDENCE` | overture で落とす `confidence` の下限(既定 `0.5`) |
| `CHIEZO_PLUGIN_TIMEOUT` | プラグイン(`CHIEZO_PLUGIN_SOURCES`)のカタログを取りに行くときの上限秒数(既定 5) |
| `CHIEZO_JQUANTS_API_KEY` | J-Quants の API キー。`jquants_master` を画面を通さずに単発で取り込むときだけ要る(画面から回すなら登録したキーが渡る。[J-Quants](#j-quants)) |
| `BUILD_MEMORY_GB` / `SKIP_MEMORY_CHECK` | 構築前メモリ検査の必要量を上書き / 検査を無効化([メモリについて](#メモリについて)) |

### ソースごとの取り込みの注意

#### J-Quants

`jquants_master` は J-Quants API の `/v2/equities/master` から、東証の全上場銘柄
(約 4,400 件)を 1 銘柄 1 文書で取り込みます。見出しは会社名で、証券コード(4 桁と
5 桁のローカルコード)と英語名を別名に持つので、コードからも引けます。J-Quants は
英数字を全角で返す(「ＮＯＫ」)ので、半角にそろえた名前も別名に持ちます。市場区分・
17/33 業種・規模区分・商品区分はタグにもなるので、`filter?tag=輸送用機器` や
`filter?tag=TOPIX Core30` で列挙できます。**全銘柄に `東証上場` のタグが付く**ので、
銘柄マスタとして全件を読みたいときは `filter?tag=東証上場`(500 件ずつ `offset` で送る)で引けます。優先株のように普通株と同じ会社名の銘柄は、
見出しにコードを添えて分けます。株価は持ちません。

**API キーは管理画面の「API キー」の面で、名前 `jquants` に登録します**。
`jquants_master` と `jquants_earnings` は同じキーを使うので、1 回登録すれば両方に渡ります
(長期記憶の面には、未登録のときだけ案内が出ます)。
登録したキーは画面に二度と出ず、初期化・再構築のたびに chiezo-trigger へ渡ります
(`POST /run/{source}` の本文。trigger は環境変数に置かず、その 1 本の取り込みにだけ渡し、
状態にもログにも残しません)。キーの置き場は設定の置き場(`CHIEZO_STATE_DIR`)の
`settings.db` です。画面を通さず chiezo-ingest で単発に回すときだけ、
`CHIEZO_JQUANTS_API_KEY` で渡してください。

J-Quants は個人の私的利用に限るサービスで、次の条件があります
([原文](https://jpx-jquants.com/ja/help/usage))。

- 取り込んだデータを本人以外が見られる状態にしない(Chiezo は認証なしで LAN に出るので、
  同じ LAN を他の人が使う環境では取り込まない)
- 生成 AI に渡すときは、自分の分析目的であり、学習に使われない設定であり、
  結果を配信・公開しないこと
- 解約やプランを下げたときは、取り込んだ DB も消す(管理画面のソース削除)

無料プランはデータが 12 週間遅れることがあり、新規上場はすぐには載りません。
世代の日付は応答の情報適用日(`Date`)の最新です。

`jquants_earnings` は同じキーで、決算発表予定日(`/fins/earnings-date`)を 1 銘柄・1 決算 1 文書で
取り込みます。**全上場銘柄**(決算期を問わず)の、今日から 31 日先までの予定です。予定日を 1 日ずつ
指定して問い合わせ、その日をいま有効な予定日とする記録(延期や未定への変更を反映した最新のもの)を
受け取ります。証券コードを別名に持ち、発表日・その月(`2026-11`)・決算区分(`2Q` など)をタグにするので、
`filter?tag=2026-11` で「来月決算の会社」を列挙できます。

**全部が見えるのは有料プラン(Light 以上)のキーだけです。** プランの参照範囲は「予定日が公表された日」で
決まり、無料プラン(公表日が 12 週間より前のものだけ)では、発表の 2〜5 週間前に公表される大手の予定が
ほぼ見えません(2026-10 の実測: 11 月の決算の山の日でも、見えたのは数十社)。取り込みの最初に
公表日=今日で 1 回問い合わせてプランを判別します(参照範囲外の 400 なら無料プラン)。
**無料プランでも止めずに、見える分だけ取り込みます。** そのかわり、どのプランで取ったかを
見出し「J-Quants 決算発表予定日の取得状況」・タグ `取得状況` の 1 件に残します(脇書きの `plan` が
`free` / `paid`、`coverage` が `partial` / `full`)—— 一部しか入っていない予定を黙って全部として配ると、
読む側が「載っていない = 決算は無い」と取り違えるためです。読む側のアプリはこれを見て、売買提案の AI に
「一部しか載っていない」と添えて渡します。
予定日ごとに 1 回、計 32 回ほど問い合わせます。有料プランなら 1 分ほど、無料プランは 1 分に 5 回ほどで
429 になるので間隔を 13 秒に広げ、7 分ほどかかります。**それでも 429 が返ったら、待って問い合わせ直します**
(1 分・2 分・4 分…と倍にして 5 回まで。相手が `Retry-After` を名乗ればその秒数)。同じキーを別のところ
(別のアプリのバッチなど)で同時に使うと、こちらが間隔を空けていても当たるためです。待っている間も「止める」は効きます。

**件数は時期で大きく増減する**(決算の山の前は数千件、過ぎると減る)ので、前の世代より大きく減っても
焼きます(アダプタの `allows_shrink`)。予定日は発表の数週間前に出ることが多いので、定期再構築は
「1 日おき」にしておくと、出たばかりの予定も翌日には入ります。

### 日本の祝日

`holidays_jp` は内閣府の `https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv`(Shift_JIS、1,000 行ほど)を
1 回取るだけです。キーは要りません。世代の日付は CSV の `Last-Modified` から取ります。
翌年の祝日は例年 2 月ごろに載るので、定期再構築は「30 日おき」で足ります。
データは「政府標準利用規約(第 2.0 版)」で公開されており、読む側のアプリは出典(内閣府「国民の祝日について」)を示してください。

### 日本の住所の位置

`address_jp` は国土交通省「街区レベル位置参照情報」を都道府県ごとの ZIP
(`https://nlftp.mlit.go.jp/isj/dls/data/<版>/<県番号>000-<版>.zip`、Shift_JIS の CSV)で 47 本取ります。
キーは要りません。**版は年に 1 度上がる**(令和 7 年度 = `24.0a`)ので、知っている版から先を確かめて
いちばん新しいものを使います(`ISJ_VERSION` で固定もできる)。世代の日付は東京都の ZIP の `Last-Modified` です。
全国で圧縮 128MB・数百万街区あり、**同じ街区の代表の行だけ**を 1 文書にします。
新しい版は例年 5 月ごろに出るので、定期再構築は「90 日おき」で足ります。
**都市計画区域の外は入っていません**(街区レベルの整備範囲)。
加工して使うときは、出典を「街区レベル位置参照情報 国土交通省」と書き、整備年(版)を添える約束です
(位置参照情報利用約款 第 2 条 3。脇書きの `version` に版が入っています)。

**Wikipedia 系**は標準 XML ダンプ(`<wiki_id>-<date>-pages-articles.xml.bz2`、jawiki で
確認時点 4.4GB)を取得し、wikitext をプレーンテキスト化して取り込みます
(CirrusSearch ダンプを使わない理由は
[設計メモ](design-notes.md#wikipedia-は-cirrussearch-ではなく-xml-ダンプから作る))。
記事本文に加えて Wikimedia の月次ページビューダンプ(全プロジェクト合算で圧縮 5〜6GB)も
ダウンロードし、`page_id` で突合して知名度(`rank_score`)と `extra.pageviews_month` に
します。この分、初回取り込みのダウンロード量・所要時間は見積もりよりやや増えます。

**OSM 系**(`osm_japan` 等)は Geofabrik の地域抽出
`https://download.geofabrik.de/<region>-latest.osm.pbf` を取り込みます。取り込むのは
`name` タグを持つ「名前付き地物」だけです(地名・行政界・主要な自然地物 + 主要 POI +
交通インフラ。住所補間・逆ジオコーディングはできません。内部の走査方法は
[設計メモ](design-notes.md#osm-は-pyosmium-で-3-パス読む))。
ファイルを 3 パス読むため、構築時間はダウンロード後さらに 2〜6 時間程度かかります
(既定の `low_memory` プロファイルではノード座標索引がディスクに置かれるため、
さらに数倍〜10 倍かかります)。メモリの潤沢なビルド機で `BUILD_PROFILE=fast` を付けると
索引が RAM 上(`sparse_mmap_array`)になり最速です(日本抽出で 5〜10GB 使います)。

## Wikipedia のオフライン版を置く

記事の写真(Wikimedia の絵)を 1 枚ずつ外へ取りに行くと、作り置きの無い縮小版を作らせる頼みになり、
Wikimedia に 429 で絞られて埋まらなくなります。[Kiwix](https://download.kiwix.org/zim/wikipedia/) が配る
Wikipedia のオフライン版(ZIM)には記事の絵が縮めた形で入っているので、置いておくとそこから引きます
(無い絵だけ外へ取りに行きます)。

**管理画面の記憶の面(長期記憶の下の「Wikipedia の画像(Kiwix のオフライン版)」)から落とせます。**
置いてある版と、Kiwix の一番新しい版(日本語版の画像入り `wikipedia_ja_all_maxi_<年月>.zim`)と「最新版を落とす」が
出ます。押すと chiezo-trigger がダンプの枠で落とし(ダンプの取り込みとは並べません)、進み具合・止める・続きから
落とすはダンプのダウンロードと同じです(状況の面の取り込みの塊に「ソース: wikipedia_zim」として出ます)。
置き場の空きが足りなければ押せず、揃ったら古い版を消します。Kiwix の一覧は trigger が 6 時間覚えます。
**定期の取り直しも決められます**(長期記憶の定期再構築と同じ「何日おき・何時」。新しい版が出ていれば落とし、
持っていればすぐ終わります)。データを持ち込む操作なので、初期化・再構築と同じ記憶の面に置いています。

手で置くこともできます(chiezo-trigger を立てていない構成など)。

```bash
mkdir -p data/zim
# 日本語版の画像入り(maxi)。nopic・mini には絵が入っていません。年月は一覧で新しいものを選ぶ
curl -L -o data/zim/wikipedia_ja_all_maxi_2026-06.zim.part \
  https://download.kiwix.org/zim/wikipedia/wikipedia_ja_all_maxi_2026-06.zim
mv data/zim/wikipedia_ja_all_maxi_2026-06.zim.part data/zim/wikipedia_ja_all_maxi_2026-06.zim
```

- **解凍しません。** ZIM は 1 件ずつ取り出して読める形式で、置き場はファイルそのもの(日本語版で 30GB ほど)だけです
- 置き場は `CHIEZO_ZIM_DIR`(既定 `/data/zim` = ホストの `data/zim`)。`.zim` が複数あれば全部から引きます(ファイル名の新しいほうが先)
- 落としている途中のファイルを `.zim` の名前で置かないでください(読みに行って失敗します)。上のように別の名前で落としてから付け替えます
- 新しい版に替えるときは、新しいファイルを置いてから古いものを消します。再起動は要りません(次に引くときに開き直します)
- 置いているかは記憶の面の「Wikipedia の画像」の節と `GET /v1/thumbs/health` の `offline` で確かめられます。
  絵の依頼の控えに「オフライン版から取った」と出れば、そこから引けています
- **置いていれば、オフライン版に無い Wikimedia の絵は取りに行かずに諦めます**(オフライン版を作ったあとの記事や差し替えられた絵)。置くのは Wikimedia に頼まないためで、無い 1 枚のために頼むと 429 で絞られます。新しい版を置けばその場で引けます。置いていなければ今までどおり Wikimedia へ取りに行きます

## 古いスキーマの DB(schema_version 3 以前)

app が読めるのは最新のスキーマバージョン(4)の DB だけです。それより古い DB は登録されず、
ログに `unsupported schema_version=… re-run ingest` が出て、管理画面の記憶の面にも
「登録できなかったソース」として並びます。**取り込み直す(再構築する)と読めるようになります**。

1 つ前の世代が古い版のときは「DBを1つ前へ戻す」は使えません(戻すとソースごと見えなくなる
ため)。記憶の面のボタンの位置に、その旨と 1 つ前の世代の中身が出ます。

## 既存 DB の rank_score を入れ直す

検索の並びに使う `rank_score` は「0.0〜1.0 に正規化した知名度」という約束ですが、
古い DB はこの約束を満たしていません:

- `<lang>wiki` — 全記事 0.0 固定(XML ダンプに人気度が無いため入れていなかった)。
  月間ページビューは `extra.pageviews_month` にあるので、そこから計算できます
- `geonames` — 人口の生値(最大 14 億)が入っています。`extra.population` から入れ直します
- `osm_<国>` — 元から 0.0〜1.0 なので対象外です

いずれも取り込み済みの `extra` を読み直すだけで、**ダンプの取り直しは要りません**。

```bash
docker compose stop chiezo-app
python3 scripts/refresh_rank_score.py data/jawiki.db data/geonames.db
docker compose start chiezo-app
```

jawiki は数分〜十数分かかります。SSH 越しに実行するなら、接続が切れても止まらないよう
`tmux` の中で回すか `nohup … &` にしてください(途中で落ちても、もう一度流し直せば
同じ結果になります)。

スキーマは変わらないので `schema_version` も上がりません。入れ直していない DB でも
API は壊れません(`rank_score` を 0〜1 に丸めてから使うので、実質 bm25 だけの並びに
戻るだけです)。何度実行しても結果は同じです。

## 地理データの守備範囲(geonames と osm の分担)

「全世界の地名に答える」用途と「特定の国を店舗レベルまで掘る」用途は、データ源を分けています。

| | `geonames` | `osm_<地域>` |
|---|---|---|
| 範囲 | **全世界**(約 1,200 万件) | 取り込んだ地域のみ |
| ダンプ | 約 400MB + 別名 191MB | 国単位で 2〜5GB(大陸単位は 32GB) |
| 持っているもの | 地名・座標・国/行政区・人口・多言語別名・timezone | 地名 + **店舗/施設/交通** の詳細、住所・電話・営業時間 |
| 持っていないもの | 店舗・レストラン・営業時間 | 取り込んでいない国のすべて |

大陸単位の OSM(`europe-latest.osm.pbf`)を 1 ソースとして取り込む案は**廃止しました**
(理由は [設計メモ](design-notes.md#地理データの守備範囲geonames-と-osm-の分担))。
推奨構成は:

- **全世界の問い合わせ** → `geonames`(1 ソースで賄う)
- **店舗レベルの詳細が要る国** → その国だけ `osm_<国>` を取り込む(管理画面 `/admin/osm` から選ぶか
  `-e SOURCE=osm_<国>`)。既定(`low_memory`)ではどの国もメモリ 2GiB で焼けます。
  メモリの潤沢なビルド機で `-e BUILD_PROFILE=fast` を付けると、多くの国は RAM 索引・
  12GiB 予算で数時間に短縮できます(フランス・ドイツ・カナダ・アメリカ・ロシアのような
  大きい国だけは fast でもディスク索引が既定です)。
- **事物の解説** → `jawiki`(座標と wikidata の Q 番号を持つ)

`geonames` と `jawiki` はどちらも `extra.wikidata` に Q 番号を持つので、
`filter?wikidata=Q90` で相互に引き当てられます。

## メモリについて

ingest は**開始前にメモリを検査し、足りなければダウンロードもせず即座に中止**します
(なぜ `mem_limit` で締めないのかは [設計メモ](design-notes.md#メモリ方針))。
取り込み中の巨大な対応表はディスク上の一時 SQLite に逃がしてあるので、常駐メモリは
コーパス規模によらず一定です。一時ファイルは終了・中断のいずれでも自動削除されます。

構築には速度優先とメモリ優先の 2 つのプロファイルがあり、環境変数 `BUILD_PROFILE` で
切り替えます:

- `low_memory`(既定) — メモリ優先。**どのソースも 2 GiB で構築できます**。構築用
  SQLite キャッシュを 64MiB に絞り、osm のノード座標索引をディスクに置きます。
  osm は数倍〜10 倍遅くなります(wikipedia / geonames はほぼ変わりません)。
  何も指定しなければこちらなので、メモリ 2 GiB 級のサーバや開発機でもそのまま取り込めます
- `fast` — 速度優先。必要メモリは下表のとおり(最大 12 GiB)。メモリの潤沢な
  ビルド機で焼くときに、実行時の引数として明示します(compose には書かず、
  `docker run -e BUILD_PROFILE=fast …` のようにその実行だけに付けるのがおすすめです。
  手順は[別マシンでビルドして .db を配布する](#別マシンでビルドして-db-を配布する))

`fast` での必要メモリの目安:

| ソース | 必要メモリの目安 | 内訳 |
|---|---|---|
| `jawiki` | 3 GiB | 巨大な対応表はディスクへ逃がしてあるので軽い。実測ピークは 1GiB 未満 |
| `osm_japan` | 12 GiB | ノード座標索引を RAM に持つため(実測 5〜10GB) |
| `osm_<他の国>` | 3〜12 GiB | pbf 1GB あたり RAM 索引 5GiB 見当。国ごとの目安は `/admin/osm` に出ます |
| `geonames` | 3 GiB | 別名(2,000 万行規模)はディスクへ逃がすため軽い |

RAM 索引が 12GiB に収まらない国(フランス・ドイツ・カナダ・アメリカ・ロシア)は、
`fast` でも**ディスク索引(`sparse_file_array`)が既定**です。なお `OSM_NODE_INDEX` の
明示指定はプロファイルよりも優先されるので、`OSM_NODE_INDEX=sparse_mmap_array` を付ければ
「既定の省メモリのまま、この 1 ソースだけ RAM 索引で速く」という使い方もできます。

足りない場合のメッセージと対処:

```
not enough memory to build osm_japan: 2.0 GiB available < 12.0 GiB required.
```

1. `BUILD_PROFILE=fast` を付けているなら外す(既定の `low_memory` は全ソース 2 GiB で
   構築できます)
2. メモリの多いマシンで焼いて `.db` をコピーする(下記)
3. `BUILD_MEMORY_GB=<n>` で必要量を上書き、`SKIP_MEMORY_CHECK=1` で検査を無効化(見積もりが
   実態と合わないと分かっている場合のみ)

**配信側はメモリ数百 MB で動きます。** chiezo-app は読み取り専用の immutable SQLite を開くだけなので、
1GB 級の小型機でも配信できます。効いてくるのはメモリではなくディスク(jawiki.db 約 42GB の空き)。

## 別マシンでビルドして .db を配布する

Chiezo の DB は**自己完結した単一の SQLite ファイル**なので、配布は「ファイルをコピーするだけ」です。
export/import も配信機でのビルドも要りません(SQLite のファイル形式は OS・CPU アーキ非依存です)。
メモリの少ない配信機と、メモリの多いビルド機を分けられます。

ビルド機に要るのは docker だけで、リポジトリも compose も設定ファイルも要りません。

```bash
# 1. イメージを最新にする。ローカルに古い latest が残っていると、それで焼いてしまう
#    (タグは latest のままなので、古いかどうかは見た目では分かりません)
docker pull ghcr.io/rtcode337/chiezo-ingest:latest

# 2. そのイメージが作る DB の schema_version を確かめる(取り込みを走らせずに聞けます)。
#    配信中の chiezo-app が期待するより古いと新機能が使えないので、数時間かける前にここで確認
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import core; print(core.SCHEMA_VERSION)"

# 3. 取り込む。数時間かかるので必ず -d(デタッチ)で回すこと。前景(--rm -it)だと
#    ターミナルを閉じた・スリープした瞬間に構築ごと消えます
docker run -d --name chiezo-build -e SOURCE=jawiki -e CHIEZO_DATA_DIR=/data \
  -v /path/to/chiezo-data:/data ghcr.io/rtcode337/chiezo-ingest:latest
docker logs -f chiezo-build                      # 進捗(Ctrl-C で抜けても構築は続く)
docker wait chiezo-build && docker rm chiezo-build

# 4. 出来た世代ファイルを配信機へコピーし、<ソース名>.db として見えるようにする
#    (リンクを差し替えれば chiezo-app が数秒以内に自動で読み込む。再起動は不要)
cp jawiki-20260701.db /path/to/chiezo/data/
ln -sfn jawiki-20260701.db /path/to/chiezo/data/jawiki.db
```

**jawiki 以外を焼くときに書き換えるのは次の 3 か所だけです**(他はそのままで動きます):

| | jawiki | osm_japan の場合 |
|---|---|---|
| 3. の `SOURCE=` | `jawiki` | `osm_japan`(`osm_france` 等、国名を変えるだけ) |
| 4. のコピー元ファイル名 | `jawiki-<日付>.db` | `osm_japan-<日付>.db` |
| 4. の `ln -sfn` の 2 つめの引数 | `jawiki.db` | `osm_japan.db` |

世代ファイル名の日付は取り込み時に決まるので、`ls /path/to/chiezo-data/*.db` で実際の名前を
確認してからコピーしてください。ビルド機にメモリの余裕があるなら `-e BUILD_PROFILE=fast` を
足すと速く焼けます(ソースによって必要メモリが違う点は上の
[メモリについて](#メモリについて)の表を参照。付けなければ既定の `low_memory` でどのソースも
2 GiB で焼けます)。`SOURCE` に渡せる名前(`osm_<国>` 195 件 + `<lang>wiki` 348 件 + geonames)は
ビルド機だけでも引けます:

```bash
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import sources; print('\n'.join(sources.ADAPTERS))" | grep france
```

ビルド機がインターネットに出られない・GHCR を使いたくない場合は、イメージをファイルで持ち込めます:

```bash
docker pull ghcr.io/rtcode337/chiezo-ingest:latest   # または docker-compose.build.yml を重ねてローカルビルド
mkdir -p handoff                                     # 持ち出す成果物の置き場(リポジトリ管理外)
docker save ghcr.io/rtcode337/chiezo-ingest:latest | gzip -1 > handoff/chiezo-ingest-image.tar.gz
# ビルド機で: docker load -i chiezo-ingest-image.tar.gz
```

詳しい手順(必要ディスク・所要時間、Docker Desktop/WSL2 のメモリ設定、後片付け、環境変数一覧)は
[`docs/build-on-another-machine.md`](build-on-another-machine.md) にまとめてあります
(取り込みが触るのは公開ダンプと指定した data フォルダだけで、認証情報や個人ファイルは読みません)。

## 動いているビルドを確かめる

管理画面(`/admin`)の末尾に「いま動いているビルド」として、**ビルド日時(JST)と
ビルド元のコミット**が出る(`2026-08-15 00:12 JST (dbdb1fb)`)。手元の `git log -1` と
見比べれば、変更が反映済みかが分かる。

```bash
docker compose pull && docker compose up -d
```

このあと画面のビルドが新しくなっていなければ、**古いイメージのまま動いている**。
タグ(`latest`)は上書きされるので、タグだけでは判別できない。

**並ぶのは 3 つ**(`/admin/server`): chiezo-app・取り込み(chiezo-trigger)・
立っている CLI ブリッジ。**イメージは別々に焼かれるので、片方だけ古いままが普通に
起きる** —— app だけ入れ替えて取り込みを忘れると、直したはずの不具合が残ったままになる。
**立っていないものは行ごと出ない**ので、出ていない = そのコンテナが動いていない、と読む。

値は CI がイメージへ焼き込む(`--build-arg BUILD_SHA=... BUILD_TIME=...` →
`CHIEZO_BUILD_SHA` / `CHIEZO_BUILD_TIME`)。**手元でビルドしたイメージでは渡らないので
「不明(ビルド情報なし)」と出る**。手元ビルドでも出したいなら、こう渡す。

```bash
docker build ./api \
  --build-arg BUILD_SHA="$(git rev-parse HEAD)" \
  --build-arg BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

## どのコンテナが何に要るか

読むだけなら `chiezo-app` の 1 つで動きます。追加のコンテナが要るのは、長期記憶へ
書き込むとき(初期化・再構築・削除)だけです。

| コンテナ | 何に要るか | 既定 |
|---|---|---|
| `chiezo-app` | 短期記憶の読み書き、長期記憶の読み、画面・MCP・REST の全部 | 常駐 |
| `chiezo-init` | `data/` の下に `corpus/` `notes/` `state/` を作り、所有者を合わせる。手で用意すれば無くてもよい | 起動時に 1 回 |
| `chiezo-trigger` | 長期記憶への書き込みを起こす(初期化・再構築・削除) | 常駐(軽い) |
| `chiezo-ingest` | 管理画面を使わず CLI から取り込むとき | 起動しない(profile `ingest`) |
| `searxng` | web 検索の道具が引く検索エンジン。`CHIEZO_WEB_SEARCH_URL` が空なら使わない | 常駐 |

**走っている取り込みは、管理画面の「止める」で降ろせます。** 殺すのではなく印を立てて、
取り込みの側が区切りのいいところで降ります —— 降りるのは**切り替えより前**なので、
いま配信している世代はそのまま残り、集めた素材も捨てません(押し直せば AI を
呼び直さずに続きから焼けます)。**すぐには止まらないことがあります** ——
外から素材が届くのを待っている最中は、動いているのは向こうなので、届き始めてから
降りることになります。
**収集の取り込みは、AI に集めさせているほうも止めます** —— 本体が区画ごとに AI を
呼んでいるあいだ取り込みは待っているだけなので、押したときに本体へも「止めて」を
伝え、走っている往復を止めて次の区画を呼ばせません(それまでに集めたぶんは焼きます)。

取り込みを一切しない構成にするなら、`chiezo-trigger` のサービスごと消して構いません
(`chiezo-app` はこれに依存していません)。その状態では管理画面の初期化・再構築・削除の
ボタンが押せなくなり、なぜ押せないのかも画面に出ます。読み取りと短期記憶への書き込みは
今までどおり動きます。

## chiezo-trigger(管理画面からの初期化・再構築)

`chiezo-ingest` と同じイメージを使い回し、CMD だけ `server.py`(FastAPI)の起動に差し替えた
常駐コンテナです。`/data` に書き込み権限を持ち、`POST /run/{source}`(取得に API キーが要る
ソースには本文 `{"credential": "…"}` を付けられる)で ingest の `run()` を
バックグラウンドスレッドで実行、`GET /status` で走っているもの(`jobs`)・終わった回
(`recent`)・最後に落ちた回(`last_failure`)をログ tail つきで返します。

**ダンプを落としている間は、走っている 1 本に `progress` が載ります**(落とした量・全体の大きさ・
直近 30 秒の速さ・残り時間。`ingest/download.py`)。管理画面の状況の面の取り込みの塊に、
進み具合の棒と合わせて出ます(自動では読み直さないので、見たいときにブラウザで読み直す)。
実行ログにも 1 分ごとに同じ 1 行が残ります。落とすのは今までどおり curl(`-C -` で再開・
`--retry 5`)で、ここは書き込み中のファイルの大きさを見張るだけです。**ダウンロード中の
「止める」も効きます** —— curl を終わらせ、途中のぶん(`.part`)は残すので、次の回は続きから落とします。

**同時に走らせられる本数は `CHIEZO_INGEST_SLOTS`(既定 1)**。1 本ごとにメモリを
食うので、並べたい機械でだけ上げてください(大きな収集を焼く回は 1 本で GB 単位、
前の世代も読むと倍近くになります)。上げても、**同じソースは 1 本ずつ**
(ブルーグリーンの切り替えが同じリンクを取り合う)、**ダンプの取り込みも 1 本ずつ**
(ダウンロードの置き場を共有する)です。埋まっているときの `POST /run` は 429、
同じソースが走っていれば 409 で断ります。

**ダンプの取り込み(初期化・再構築)は別の枠で走ります**(`CHIEZO_INGEST_DUMP_LANE`、既定で有効)。
再構築は何時間もかかるので、1 つの枠を取り合うと、そのあいだ収集も一切流れませんでした。
ダンプは専用の 1 枠を使い、`CHIEZO_INGEST_SLOTS` は収集・プラグインのぶんにまるごと残ります。
ダンプかどうかは取り込み側のアダプタ一覧(`ADAPTERS`)に載っているかで決まり、`GET /status` の
各 1 本に `lane`(`dump` / `general`)、全体に `dump_lane` が載ります。
Wikipedia のオフライン版を落とす 1 本(`POST /zim/run`。様子は `GET /zim`)もダンプの枠で走ります。管理画面のボタンも、
そのソースが使う枠が空いているかで押せるかを決めます。**再構築と収集の焼き込みが同時に
メモリを使う**ので、2 本並べられないほど小さい機械では `CHIEZO_INGEST_DUMP_LANE=0` にして、
今までどおり 1 つの枠を取り合わせてください。`POST /stop?source=<名前>` で 1 本だけ
降ろせます(名前を書かなければ走っている全部)。

**収集の回は、埋まっていても断られません。** chiezo-app が取り込みの待ち行列に並べ、
空いた周(1 分ごと)に先頭から流します。待ち行列は管理画面の状況の面に出ます。`GET /sources` は取り込める
ソースのカタログ(名前・kind・lang と、osm 国別ソースの表示名・region・pbf サイズ・必要メモリ、
wikipedia 言語版の表示名・自称・記事数)と、このイメージが焼くスキーマバージョン
(`schema_version`)を返し、管理画面の初期化一覧・国選択画面・言語選択画面・
「最新のスキーマバージョン」表示はこれを読んで組み立てます。

ホストへはポート公開せず、docker の内部ネットワーク経由で `chiezo-app` からのみ到達できます
(`chiezo-app` の環境変数 `CHIEZO_TRIGGER_URL=http://chiezo-trigger:8080`)。管理画面の
「初期化」ボタン(`POST /admin/init/{source}`)と「再構築」ボタン
(`POST /admin/rebuild/{source}`。登録済みソースのみ受け付ける)はこのサービスへのプロキシです。
**このサービスが居なければ、管理画面の初期化・再構築・削除のボタンは押せません**
(`CHIEZO_TRIGGER_URL` が未設定のときも、設定してあって到達できないときも同じ扱いです)。
押せてしまうと返るのは 502 だけで、なぜ動かないのかが画面から読めないためで、
代わりに「読むだけならこのままで動きます」と状態欄に出します。

### 定期再構築

**ダンプのソースは、何日おき・何時に取り直すかを決めておけます**(管理画面の長期記憶の表の
「定期再構築」の列。1・7・14・30 日おきと、時刻〈日本時間〉を選ぶ)。決めておくと、chiezo-app の
時計(`main._run_rebuilds`、60 秒ごとに見る)がその時刻に再構築を起こします
(`POST /admin/rebuild-schedule`。間隔を「なし」にすると外れる)。

- **次に走る時刻は「前に起こした日 + 間隔」の、決めた時刻**です。前の回がダンプの枠の空き待ちで
  遅れても、次の回は決めた時刻に戻ります。決めた直後は、次に来るその時刻に 1 回目が走ります
- **ダンプの枠が空いているときだけ起こします**(ダンプは 1 本ずつ)。埋まっていれば次の周に回し、
  予定は起こせたときにだけ進めるので、飛ばされることはありません。1 周に起こすのは 1 本だけです
- **キーの要るソース(`jquants_master` など)は、画面で登録したキーがそのまま渡ります**
- 集めたソースは収集の巡回が回すので、この列では決められません。予定は設定の置き場
  (`CHIEZO_STATE_DIR`)に持つので、それが無い構成では使えません
- 目安: J-Quants の上場銘柄一覧は営業日ごと、OSM・GeoNames は日々、Wikipedia は月 2 回ダンプが
  出ます。大きいソース(jawiki など)は数時間かかり、その間ほかのダンプは待たされます

`GET /sources` の結果は chiezo-app が `CHIEZO_CATALOG_TTL` 秒(既定 300)キャッシュします。
大半はイメージに焼かれた静的な表ですが、プラグインはマウントで実行時に足せるので、
永久に持つと足したソースが管理画面に出ません。取り直しに失敗したときは**古いカタログを
そのまま使い続けます**(trigger が一時的に落ちただけで一覧が空になるのを避けるため)。
0 以下にすると取り直しません。この値と `CHIEZO_RESCAN_INTERVAL`・`CHIEZO_MCP_ALLOWED_HOSTS`・
`CHIEZO_ALLOWED_HOSTS`・`CHIEZO_ALLOWED_ORIGINS` は
compose が `.env` から受け取ります(`.env.example` の「1. Chiezo 本体」)。

新ソースは `ingest/sources/__init__.py` の `ADAPTERS` に追加するだけで管理画面にも出ます
(`chiezo-app` は ingest のコードを import しませんが、上記の `GET /sources` 経由で名前を受け取ります)。
`app/known_sources.py` の `KNOWN_SOURCES` は、`chiezo-trigger` が未設定・到達不能なときに
管理画面を空にしないための控えです(必須の複製ではありません)。

## `.env` を置けない環境で起動する(単体定義)

管理画面に YAML を貼り付けて起動するタイプの実行環境では、`.env` もシェルの環境変数も
無いため `${...}` を解決できず、`--profile` も付けられません。この場合は値を直接書いた
[`docker-compose.standalone.example.yml`](../docker-compose.standalone.example.yml) を使います。

```bash
# 中身をコピーして、先頭の置き場を実際の絶対パスに書き換えてから貼り付ける
cp docker-compose.standalone.example.yml docker-compose.standalone.yml   # コピー先は .gitignore 済み
cat docker-compose.standalone.yml
```

編集するのは先頭の置き場の行(`x-data-volume` と、同じ親に揃える `x-corpus-volume` ほか)
だけです。**ホスト側は絶対パスで書きます** —— 貼り付けて登録する環境では相対パス(`./data`)の
基準が読めないためです。用意するのは親のディレクトリだけで、下の `corpus/`・`notes/`・`state/` は
`chiezo-init` が起動時に作ります(取り込んだ `.db` は `corpus/` に置く)。

起動するのは `chiezo-app`・`chiezo-trigger`・`searxng`(と、置き場を用意する `chiezo-init`)で、通常の `docker-compose.yml` と同じく
trigger はホストへ公開されず、管理画面の初期化ボタンから内部ネットワーク経由で叩かれます。
**推論サーバは設定だけを載せてあり、コンテナは含みません** —— 別サーバーで
動かしているものを指せば済み(chiezo-app は OpenAI 互換の口を叩くだけ)、
貼り付けて動かすような実行環境で同居させる前提も無いためです。同じホストに立てるなら、
上書きを重ねられる環境で `docker-compose.llm.yml` を使ってください。

`cpu_shares` のように受け付けない実行環境がある項目は、行ごと消しても動きます。
**`docker-compose.yml` を変えたらこのファイルも追従させてください**(値が直書きのぶん、
黙って古くなります)。追従漏れは `tests/test_compose_files.py` が検知します —— 本体が
`chiezo-app` に渡している設定が、コメントとしてでもこのファイルに出てくるかを照合します。

## ソースの追加・削除

`data/` に `<source>.db` を置く(または消す)だけです(chiezo-app が数秒以内に自動で検知します)。
新しい種類のソースの取り込み方は [adding-a-source.md](adding-a-source.md) を参照してください。

**このリポジトリに入れられないソース**(公開できないプライベートな情報など)は、
別リポジトリのプラグインとして書いて足せます。**プラグインは別イメージ・別コンテナの
サービス**で、本体からは HTTP で見えます(取得と整形はプラグイン、DB の構築は本体)。

```yaml
# 別リポジトリ側の docker-compose.plugin.yml(Chiezo の compose に重ねる)
services:
  my-plugin:
    image: ghcr.io/<自分のアカウント>/my-plugin:latest   # 本体とは別に pull する
    restart: unless-stopped
  chiezo-trigger:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080     # カンマ区切りで複数可
    depends_on: [my-plugin]
  chiezo-ingest:
    environment:
      - CHIEZO_PLUGIN_SOURCES=http://my-plugin:8080
```

```bash
# .env
COMPOSE_FILE=docker-compose.yml:../my-plugin/docker-compose.plugin.yml
```

プラグインが落ちていても本体は動きます(そのソースが一覧に出なくなるだけ)。

これで管理画面の「初期化」「再構築」ボタンからも回せます。Chiezo 側にはコードもデータも
入りません。そもそも**配信側はソース種別を知らない**ので、管理画面から回す必要が無ければ
「別リポジトリで `.db` を焼いて `data/` に置く」だけで動きます(設定も不要)。
手順は [adding-a-source.md のケース 3](adding-a-source.md) が正です。

## セキュリティ

認証はありません。LAN 内利用が前提です。ルーターでポート 7010 を外部に開放しないでください。
必要ならホストの LAN インターフェースのみに bind するよう compose の `ports` を
`"<LAN の IP>:7010:7010"` の形式に変更してください。

**ブラウザを踏み台にした悪用は入口で止めます**(`app/web_guard.py`)。認証が無いので、
LAN の端末で開いている任意のページが Chiezo を叩けてしまうためです。

- **Host の検証(DNS リバインディング対策)** —— 既定では検査しません。VPN 越しの名前や
  後から付けた名前で開いたときに、気づかないうちに締め出されるのを避けるためです。
  開く名前を絞りたいときは `CHIEZO_ALLOWED_HOSTS` に書いてください(カンマ区切り。
  `名前` / `名前:ポート` / `名前:*` / `*.例`)。書くと、IP アドレス(`192.0.2.10:7010`)・
  `localhost`・ドットの無い名前(`chiezo-app` などコンテナのサービス名)と、書いた名前だけを通し、
  ほかは 421 で断ります —— 攻撃に使われるのは、途中から LAN のアドレスへ向け直した
  外の名前のほうだからです(`CHIEZO_MCP_ALLOWED_HOSTS` に書いてある名前も通します)
- **別のサイトからの書き込みを断る(CSRF 対策)** —— `POST`・`PUT`・`PATCH`・`DELETE` で、
  `Sec-Fetch-Site: cross-site` が付いたもの、または `Origin` が自分(届いた Host と同じ名前と
  ポート)でも `CHIEZO_ALLOWED_ORIGINS` に書いたものでもないものは 403 で断ります。
  **どちらのヘッダも付けない相手(curl・他のアプリのサーバー側の HTTP クライアント・
  MCP クライアント)はこれまでどおり通ります**

`/mcp` も同じ判定の内側にあり、MCP SDK 側にも上と同じ判定を渡しています。`/mcp` だけをさらに絞りたい場合は `CHIEZO_MCP_ALLOWED_HOSTS` に許可する
Host をカンマ区切りで指定してください(例: `<LAN の IP>:7010,localhost:*`。末尾 `:*` でポート任意。
書けば `/mcp` はこの一覧だけを通します)。

外の URL を取りに行く道(フィード・サムネイル・URL で渡された文字起こしや絵の元)は、
名前を引いて**手元・LAN・予約済みのアドレスを指すものを断ります**。転送も 1 段ずつ確かめ、
読むのは上限まで(`app/outbound.py`)。Chiezo 自身の `/media/…` を URL で渡されたときだけは、
取りに行かずに置き場から読みます。

**生成物の置き場(`/media/…`)は文書として開かれても動かない形で配ります**
(`X-Content-Type-Options: nosniff` と `Content-Security-Policy: sandbox`)。
持ち込み(`POST /v1/media/upload`)の拡張子は一覧にあるもの(画像・音・動画・文章)だけを使い、
それ以外は種類ごとの既定(`.png` / `.mp3` / `.mp4` / `.txt`)に付け替えます。SVG は受けません。
持ち込みと文字起こしの multipart はどちらも 128 MiB を超えると 413 にします。

**ブラウザのページから直接読ませたいときは `CHIEZO_CORS_ORIGINS` にオリジンを書きます**
(カンマ区切り。例: `https://travel.example.test,http://localhost:7040`)。
**既定は空で、誰にも開けません** —— ここは認証を持たない読み取り口なので、`*` で開けると
「その端末で開いている任意のページ」が中身を読めることになります。一致はスキームと
ポートまで含めた完全一致で、開けるのは `GET` と事前確認(preflight)だけ、
資格情報(Cookie)は許しません。

**https のページから読ませるなら、chiezo 側も https で出す必要があります。**
ブラウザは https のページから `http://…` への fetch を mixed content として止めるので、
`Access-Control-Allow-Origin` を返しても届きません。リバースプロキシの配下に置いて
証明書を付けてください(ポートを直接開けるのではなく、既存のプロキシに
`/`→`chiezo-app:7010` の経路を足す形)。

「使う」層(`/v1/ask`・`/v1/chat`・`/ai/chat`)も同じ方針です。推論コンテナ `chiezo-llm` は
ホストへポートを公開せず、chiezo-app からのみ到達できます。ただし到達できる相手は誰でも
推論を起動できる(比較的重い処理を認証なしで回せる)点は、初期化ボタンと同様に留意してください。
質問文が外部へ送られることはありません(推論も検索もローカルで完結します)。

`chiezo-trigger` はホストへポートを公開していません(`docker-compose.yml` に `ports:` の記載なし)。
`chiezo-app` からのみ内部ネットワーク経由で到達できます。管理画面の初期化ボタンは
`chiezo-app` にも認証を課していないため、`/admin` に到達できる相手は誰でも初期化を起動できます
(LAN 内前提のこのサービス全体の認証なし方針と同一線上ですが、ダウンロード・構築という
比較的重い処理を誰でも起動できる点は留意してください)。

書き込めるソース(`chiezo_memory` と名前付きの置き場 `/v1/stores/…`)も同じで、到達できる相手は誰でも書けます。
