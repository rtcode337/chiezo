"""Wikipedia 言語版カタログ。

**自動生成物。手で編集せず `python3 scripts/gen_wikipedia_editions.py` で作り直すこと。**

`sources/__init__.py` はこの表から `<lang>wiki` のアダプタを一括生成する。言語版は
300 以上あり手書きでは追随できないため、Wikimedia の sitematrix(言語版一覧)と
wikistats(記事数)、CLDR(言語名の日本語表記)から機械的に起こしている。

各項目の意味:
  lang       URL 言語コード(サブドメイン。ハイフン区切り: zh-yue 等)。
             pageview_complete のドメイン `<lang>.wikipedia` の素
  wiki_id    dbname = chiezo のソース名(zh_yuewiki 等)。ダンプ URL の素
  label      言語名の日本語表記(CLDR。無ければ英名)
  label_en   英語名(sitematrix localname)
  autonym    その言語での自称
  articles   生成時点の記事数(wikistats)。表示の目安
  min_docs   検証で要求する最低文書数(記事数の 50%。保守的な下限)

記事数は日々増えるので articles / min_docs はあくまで目安。
"""
from __future__ import annotations

from typing import NamedTuple


class WikipediaEdition(NamedTuple):
    lang: str          # URL 言語コード(zh-yue 等はハイフン区切り)
    wiki_id: str       # dbname = ソース名(区切りはアンダースコア)
    label: str         # 表示名(日本語。無ければ英名)
    label_en: str
    autonym: str       # その言語での自称
    articles: int
    min_docs: int


WIKIPEDIA_EDITIONS: dict[str, WikipediaEdition] = {
    "enwiki": WikipediaEdition(
        lang="en", wiki_id="enwiki",
        label="英語", label_en="English", autonym="English",
        articles=7213388, min_docs=3606694,
    ),
    "cebwiki": WikipediaEdition(
        lang="ceb", wiki_id="cebwiki",
        label="セブアノ語", label_en="Cebuano", autonym="Cebuano",
        articles=6115358, min_docs=3057679,
    ),
    "dewiki": WikipediaEdition(
        lang="de", wiki_id="dewiki",
        label="ドイツ語", label_en="German", autonym="Deutsch",
        articles=3138349, min_docs=1569174,
    ),
    "frwiki": WikipediaEdition(
        lang="fr", wiki_id="frwiki",
        label="フランス語", label_en="French", autonym="français",
        articles=2771023, min_docs=1385511,
    ),
    "svwiki": WikipediaEdition(
        lang="sv", wiki_id="svwiki",
        label="スウェーデン語", label_en="Swedish", autonym="svenska",
        articles=2626665, min_docs=1313332,
    ),
    "nlwiki": WikipediaEdition(
        lang="nl", wiki_id="nlwiki",
        label="オランダ語", label_en="Dutch", autonym="Nederlands",
        articles=2223739, min_docs=1111869,
    ),
    "eswiki": WikipediaEdition(
        lang="es", wiki_id="eswiki",
        label="スペイン語", label_en="Spanish", autonym="español",
        articles=2127367, min_docs=1063683,
    ),
    "ruwiki": WikipediaEdition(
        lang="ru", wiki_id="ruwiki",
        label="ロシア語", label_en="Russian", autonym="русский",
        articles=2111081, min_docs=1055540,
    ),
    "itwiki": WikipediaEdition(
        lang="it", wiki_id="itwiki",
        label="イタリア語", label_en="Italian", autonym="italiano",
        articles=1979049, min_docs=989524,
    ),
    "plwiki": WikipediaEdition(
        lang="pl", wiki_id="plwiki",
        label="ポーランド語", label_en="Polish", autonym="polski",
        articles=1702538, min_docs=851269,
    ),
    "arzwiki": WikipediaEdition(
        lang="arz", wiki_id="arzwiki",
        label="エジプト・アラビア語", label_en="Egyptian Arabic", autonym="مصرى",
        articles=1632501, min_docs=816250,
    ),
    "zhwiki": WikipediaEdition(
        lang="zh", wiki_id="zhwiki",
        label="中国語", label_en="Chinese", autonym="中文",
        articles=1545288, min_docs=772644,
    ),
    "jawiki": WikipediaEdition(
        lang="ja", wiki_id="jawiki",
        label="日本語", label_en="Japanese", autonym="日本語",
        articles=1511352, min_docs=755676,
    ),
    "ukwiki": WikipediaEdition(
        lang="uk", wiki_id="ukwiki",
        label="ウクライナ語", label_en="Ukrainian", autonym="українська",
        articles=1429025, min_docs=714512,
    ),
    "arwiki": WikipediaEdition(
        lang="ar", wiki_id="arwiki",
        label="アラビア語", label_en="Arabic", autonym="العربية",
        articles=1325511, min_docs=662755,
    ),
    "viwiki": WikipediaEdition(
        lang="vi", wiki_id="viwiki",
        label="ベトナム語", label_en="Vietnamese", autonym="Tiếng Việt",
        articles=1303787, min_docs=651893,
    ),
    "warwiki": WikipediaEdition(
        lang="war", wiki_id="warwiki",
        label="ワライ語", label_en="Waray", autonym="Winaray",
        articles=1266918, min_docs=633459,
    ),
    "ptwiki": WikipediaEdition(
        lang="pt", wiki_id="ptwiki",
        label="ポルトガル語", label_en="Portuguese", autonym="português",
        articles=1178670, min_docs=589335,
    ),
    "fawiki": WikipediaEdition(
        lang="fa", wiki_id="fawiki",
        label="ペルシア語", label_en="Persian", autonym="فارسی",
        articles=1080730, min_docs=540365,
    ),
    "cewiki": WikipediaEdition(
        lang="ce", wiki_id="cewiki",
        label="チェチェン語", label_en="Chechen", autonym="нохчийн",
        articles=866469, min_docs=433234,
    ),
    "cawiki": WikipediaEdition(
        lang="ca", wiki_id="cawiki",
        label="カタロニア語", label_en="Catalan", autonym="català",
        articles=799136, min_docs=399568,
    ),
    "idwiki": WikipediaEdition(
        lang="id", wiki_id="idwiki",
        label="インドネシア語", label_en="Indonesian", autonym="Bahasa Indonesia",
        articles=786443, min_docs=393221,
    ),
    "kowiki": WikipediaEdition(
        lang="ko", wiki_id="kowiki",
        label="韓国語", label_en="Korean", autonym="한국어",
        articles=755164, min_docs=377582,
    ),
    "ttwiki": WikipediaEdition(
        lang="tt", wiki_id="ttwiki",
        label="タタール語", label_en="Tatar", autonym="татарча / tatarça",
        articles=708953, min_docs=354476,
    ),
    "srwiki": WikipediaEdition(
        lang="sr", wiki_id="srwiki",
        label="セルビア語", label_en="Serbian", autonym="српски / srpski",
        articles=706766, min_docs=353383,
    ),
    "trwiki": WikipediaEdition(
        lang="tr", wiki_id="trwiki",
        label="トルコ語", label_en="Turkish", autonym="Türkçe",
        articles=691763, min_docs=345881,
    ),
    "nowiki": WikipediaEdition(
        lang="no", wiki_id="nowiki",
        label="ノルウェー語", label_en="Norwegian", autonym="norsk",
        articles=686934, min_docs=343467,
    ),
    "urwiki": WikipediaEdition(
        lang="ur", wiki_id="urwiki",
        label="ウルドゥー語", label_en="Urdu", autonym="اردو",
        articles=663567, min_docs=331783,
    ),
    "fiwiki": WikipediaEdition(
        lang="fi", wiki_id="fiwiki",
        label="フィンランド語", label_en="Finnish", autonym="suomi",
        articles=621879, min_docs=310939,
    ),
    "cswiki": WikipediaEdition(
        lang="cs", wiki_id="cswiki",
        label="チェコ語", label_en="Czech", autonym="čeština",
        articles=595760, min_docs=297880,
    ),
    "huwiki": WikipediaEdition(
        lang="hu", wiki_id="huwiki",
        label="ハンガリー語", label_en="Hungarian", autonym="magyar",
        articles=571571, min_docs=285785,
    ),
    "rowiki": WikipediaEdition(
        lang="ro", wiki_id="rowiki",
        label="ルーマニア語", label_en="Romanian", autonym="română",
        articles=546139, min_docs=273069,
    ),
    "euwiki": WikipediaEdition(
        lang="eu", wiki_id="euwiki",
        label="バスク語", label_en="Basque", autonym="euskara",
        articles=492756, min_docs=246378,
    ),
    "shwiki": WikipediaEdition(
        lang="sh", wiki_id="shwiki",
        label="セルボ・クロアチア語", label_en="Serbo-Croatian", autonym="srpskohrvatski / српскохрватски",
        articles=461747, min_docs=230873,
    ),
    "mswiki": WikipediaEdition(
        lang="ms", wiki_id="mswiki",
        label="マレー語", label_en="Malay", autonym="Bahasa Melayu",
        articles=440210, min_docs=220105,
    ),
    "zh_min_nanwiki": WikipediaEdition(
        lang="zh-min-nan", wiki_id="zh_min_nanwiki",
        label="閩南語", label_en="Minnan", autonym="閩南語 / Bân-lâm-gí",
        articles=434349, min_docs=217174,
    ),
    "hewiki": WikipediaEdition(
        lang="he", wiki_id="hewiki",
        label="ヘブライ語", label_en="Hebrew", autonym="עברית",
        articles=401526, min_docs=200763,
    ),
    "eowiki": WikipediaEdition(
        lang="eo", wiki_id="eowiki",
        label="エスペラント語", label_en="Esperanto", autonym="Esperanto",
        articles=388073, min_docs=194036,
    ),
    "uzwiki": WikipediaEdition(
        lang="uz", wiki_id="uzwiki",
        label="ウズベク語", label_en="Uzbek", autonym="oʻzbekcha / ўзбекча",
        articles=351556, min_docs=175778,
    ),
    "hywiki": WikipediaEdition(
        lang="hy", wiki_id="hywiki",
        label="アルメニア語", label_en="Armenian", autonym="հայերեն",
        articles=330136, min_docs=165068,
    ),
    "dawiki": WikipediaEdition(
        lang="da", wiki_id="dawiki",
        label="デンマーク語", label_en="Danish", autonym="dansk",
        articles=315146, min_docs=157573,
    ),
    "bgwiki": WikipediaEdition(
        lang="bg", wiki_id="bgwiki",
        label="ブルガリア語", label_en="Bulgarian", autonym="български",
        articles=311322, min_docs=155661,
    ),
    "cywiki": WikipediaEdition(
        lang="cy", wiki_id="cywiki",
        label="ウェールズ語", label_en="Welsh", autonym="Cymraeg",
        articles=284370, min_docs=142185,
    ),
    "simplewiki": WikipediaEdition(
        lang="simple", wiki_id="simplewiki",
        label="Simple English", label_en="Simple English", autonym="Simple English",
        articles=283644, min_docs=141822,
    ),
    "elwiki": WikipediaEdition(
        lang="el", wiki_id="elwiki",
        label="ギリシャ語", label_en="Greek", autonym="Ελληνικά",
        articles=271243, min_docs=135621,
    ),
    "bewiki": WikipediaEdition(
        lang="be", wiki_id="bewiki",
        label="ベラルーシ語", label_en="Belarusian", autonym="беларуская",
        articles=265031, min_docs=132515,
    ),
    "etwiki": WikipediaEdition(
        lang="et", wiki_id="etwiki",
        label="エストニア語", label_en="Estonian", autonym="eesti",
        articles=261058, min_docs=130529,
    ),
    "skwiki": WikipediaEdition(
        lang="sk", wiki_id="skwiki",
        label="スロバキア語", label_en="Slovak", autonym="slovenčina",
        articles=260634, min_docs=130317,
    ),
    "azbwiki": WikipediaEdition(
        lang="azb", wiki_id="azbwiki",
        label="South Azerbaijani", label_en="South Azerbaijani", autonym="تۆرکجه",
        articles=244746, min_docs=122373,
    ),
    "kkwiki": WikipediaEdition(
        lang="kk", wiki_id="kkwiki",
        label="カザフ語", label_en="Kazakh", autonym="қазақша",
        articles=244367, min_docs=122183,
    ),
    "hrwiki": WikipediaEdition(
        lang="hr", wiki_id="hrwiki",
        label="クロアチア語", label_en="Croatian", autonym="hrvatski",
        articles=234389, min_docs=117194,
    ),
    "glwiki": WikipediaEdition(
        lang="gl", wiki_id="glwiki",
        label="ガリシア語", label_en="Galician", autonym="galego",
        articles=232611, min_docs=116305,
    ),
    "minwiki": WikipediaEdition(
        lang="min", wiki_id="minwiki",
        label="ミナンカバウ語", label_en="Minangkabau", autonym="Minangkabau",
        articles=229803, min_docs=114901,
    ),
    "ltwiki": WikipediaEdition(
        lang="lt", wiki_id="ltwiki",
        label="リトアニア語", label_en="Lithuanian", autonym="lietuvių",
        articles=226392, min_docs=113196,
    ),
    "azwiki": WikipediaEdition(
        lang="az", wiki_id="azwiki",
        label="アゼルバイジャン語", label_en="Azerbaijani", autonym="azərbaycanca",
        articles=216282, min_docs=108141,
    ),
    "slwiki": WikipediaEdition(
        lang="sl", wiki_id="slwiki",
        label="スロベニア語", label_en="Slovenian", autonym="slovenščina",
        articles=198424, min_docs=99212,
    ),
    "kawiki": WikipediaEdition(
        lang="ka", wiki_id="kawiki",
        label="ジョージア語", label_en="Georgian", autonym="ქართული",
        articles=197569, min_docs=98784,
    ),
    "bnwiki": WikipediaEdition(
        lang="bn", wiki_id="bnwiki",
        label="ベンガル語", label_en="Bangla", autonym="বাংলা",
        articles=189562, min_docs=94781,
    ),
    "tawiki": WikipediaEdition(
        lang="ta", wiki_id="tawiki",
        label="タミル語", label_en="Tamil", autonym="தமிழ்",
        articles=187972, min_docs=93986,
    ),
    "thwiki": WikipediaEdition(
        lang="th", wiki_id="thwiki",
        label="タイ語", label_en="Thai", autonym="ไทย",
        articles=185501, min_docs=92750,
    ),
    "lldwiki": WikipediaEdition(
        lang="lld", wiki_id="lldwiki",
        label="Ladin", label_en="Ladin", autonym="Ladin",
        articles=183208, min_docs=91604,
    ),
    "nnwiki": WikipediaEdition(
        lang="nn", wiki_id="nnwiki",
        label="ノルウェー語(ニーノシュク)", label_en="Norwegian Nynorsk", autonym="norsk nynorsk",
        articles=178150, min_docs=89075,
    ),
    "hiwiki": WikipediaEdition(
        lang="hi", wiki_id="hiwiki",
        label="ヒンディー語", label_en="Hindi", autonym="हिन्दी",
        articles=170574, min_docs=85287,
    ),
    "mkwiki": WikipediaEdition(
        lang="mk", wiki_id="mkwiki",
        label="マケドニア語", label_en="Macedonian", autonym="македонски",
        articles=163234, min_docs=81617,
    ),
    "zh_yuewiki": WikipediaEdition(
        lang="zh-yue", wiki_id="zh_yuewiki",
        label="広東語", label_en="Cantonese", autonym="粵語",
        articles=151239, min_docs=75619,
    ),
    "lvwiki": WikipediaEdition(
        lang="lv", wiki_id="lvwiki",
        label="ラトビア語", label_en="Latvian", autonym="latviešu",
        articles=144482, min_docs=72241,
    ),
    "lawiki": WikipediaEdition(
        lang="la", wiki_id="lawiki",
        label="ラテン語", label_en="Latin", autonym="Latina",
        articles=142014, min_docs=71007,
    ),
    "astwiki": WikipediaEdition(
        lang="ast", wiki_id="astwiki",
        label="アストゥリアス語", label_en="Asturian", autonym="asturianu",
        articles=139171, min_docs=69585,
    ),
    "afwiki": WikipediaEdition(
        lang="af", wiki_id="afwiki",
        label="アフリカーンス語", label_en="Afrikaans", autonym="Afrikaans",
        articles=129885, min_docs=64942,
    ),
    "tewiki": WikipediaEdition(
        lang="te", wiki_id="tewiki",
        label="テルグ語", label_en="Telugu", autonym="తెలుగు",
        articles=126361, min_docs=63180,
    ),
    "swwiki": WikipediaEdition(
        lang="sw", wiki_id="swwiki",
        label="スワヒリ語", label_en="Swahili", autonym="Kiswahili",
        articles=121255, min_docs=60627,
    ),
    "tgwiki": WikipediaEdition(
        lang="tg", wiki_id="tgwiki",
        label="タジク語", label_en="Tajik", autonym="тоҷикӣ",
        articles=118774, min_docs=59387,
    ),
    "mywiki": WikipediaEdition(
        lang="my", wiki_id="mywiki",
        label="ミャンマー語", label_en="Burmese", autonym="မြန်မာဘာသာ",
        articles=111335, min_docs=55667,
    ),
    "hawiki": WikipediaEdition(
        lang="ha", wiki_id="hawiki",
        label="ハウサ語", label_en="Hausa", autonym="Hausa",
        articles=106573, min_docs=53286,
    ),
    "sqwiki": WikipediaEdition(
        lang="sq", wiki_id="sqwiki",
        label="アルバニア語", label_en="Albanian", autonym="shqip",
        articles=106087, min_docs=53043,
    ),
    "mgwiki": WikipediaEdition(
        lang="mg", wiki_id="mgwiki",
        label="マダガスカル語", label_en="Malagasy", autonym="Malagasy",
        articles=103502, min_docs=51751,
    ),
    "mrwiki": WikipediaEdition(
        lang="mr", wiki_id="mrwiki",
        label="マラーティー語", label_en="Marathi", autonym="मराठी",
        articles=102406, min_docs=51203,
    ),
    "bswiki": WikipediaEdition(
        lang="bs", wiki_id="bswiki",
        label="ボスニア語", label_en="Bosnian", autonym="bosanski",
        articles=98561, min_docs=49280,
    ),
    "kuwiki": WikipediaEdition(
        lang="ku", wiki_id="kuwiki",
        label="クルド語", label_en="Kurdish", autonym="kurdî",
        articles=91917, min_docs=45958,
    ),
    "brwiki": WikipediaEdition(
        lang="br", wiki_id="brwiki",
        label="ブルトン語", label_en="Breton", autonym="brezhoneg",
        articles=91474, min_docs=45737,
    ),
    "be_x_oldwiki": WikipediaEdition(
        lang="be-tarask", wiki_id="be_x_oldwiki",
        label="Belarusian (Taraškievica orthography)", label_en="Belarusian (Taraškievica orthography)", autonym="беларуская (тарашкевіца)",
        articles=91073, min_docs=45536,
    ),
    "ocwiki": WikipediaEdition(
        lang="oc", wiki_id="ocwiki",
        label="オック語", label_en="Occitan", autonym="occitan",
        articles=90820, min_docs=45410,
    ),
    "mlwiki": WikipediaEdition(
        lang="ml", wiki_id="mlwiki",
        label="マラヤーラム語", label_en="Malayalam", autonym="മലയാളം",
        articles=88251, min_docs=44125,
    ),
    "anwiki": WikipediaEdition(
        lang="an", wiki_id="anwiki",
        label="アラゴン語", label_en="Aragonese", autonym="aragonés",
        articles=86295, min_docs=43147,
    ),
    "ndswiki": WikipediaEdition(
        lang="nds", wiki_id="ndswiki",
        label="低地ドイツ語", label_en="Low German", autonym="Plattdüütsch",
        articles=85912, min_docs=42956,
    ),
    "ckbwiki": WikipediaEdition(
        lang="ckb", wiki_id="ckbwiki",
        label="中央クルド語", label_en="Central Kurdish", autonym="کوردی",
        articles=83441, min_docs=41720,
    ),
    "lmowiki": WikipediaEdition(
        lang="lmo", wiki_id="lmowiki",
        label="ロンバルド語", label_en="Lombard", autonym="lombard",
        articles=80175, min_docs=40087,
    ),
    "kywiki": WikipediaEdition(
        lang="ky", wiki_id="kywiki",
        label="キルギス語", label_en="Kyrgyz", autonym="кыргызча",
        articles=76508, min_docs=38254,
    ),
    "pnbwiki": WikipediaEdition(
        lang="pnb", wiki_id="pnbwiki",
        label="Western Punjabi", label_en="Western Punjabi", autonym="پنجابی",
        articles=75663, min_docs=37831,
    ),
    "jvwiki": WikipediaEdition(
        lang="jv", wiki_id="jvwiki",
        label="ジャワ語", label_en="Javanese", autonym="Jawa",
        articles=75332, min_docs=37666,
    ),
    "newwiki": WikipediaEdition(
        lang="new", wiki_id="newwiki",
        label="ネワール語", label_en="Newari", autonym="नेपाल भाषा",
        articles=74249, min_docs=37124,
    ),
    "htwiki": WikipediaEdition(
        lang="ht", wiki_id="htwiki",
        label="ハイチ・クレオール語", label_en="Haitian Creole", autonym="Kreyòl ayisyen",
        articles=72039, min_docs=36019,
    ),
    "pmswiki": WikipediaEdition(
        lang="pms", wiki_id="pmswiki",
        label="ピエモンテ語", label_en="Piedmontese", autonym="Piemontèis",
        articles=71542, min_docs=35771,
    ),
    "vecwiki": WikipediaEdition(
        lang="vec", wiki_id="vecwiki",
        label="ヴェネト語", label_en="Venetian", autonym="vèneto",
        articles=69619, min_docs=34809,
    ),
    "lbwiki": WikipediaEdition(
        lang="lb", wiki_id="lbwiki",
        label="ルクセンブルク語", label_en="Luxembourgish", autonym="Lëtzebuergesch",
        articles=67548, min_docs=33774,
    ),
    "mznwiki": WikipediaEdition(
        lang="mzn", wiki_id="mznwiki",
        label="マーザンダラーン語", label_en="Mazanderani", autonym="مازِرونی",
        articles=64729, min_docs=32364,
    ),
    "bawiki": WikipediaEdition(
        lang="ba", wiki_id="bawiki",
        label="バシキール語", label_en="Bashkir", autonym="башҡортса",
        articles=64289, min_docs=32144,
    ),
    "gawiki": WikipediaEdition(
        lang="ga", wiki_id="gawiki",
        label="アイルランド語", label_en="Irish", autonym="Gaeilge",
        articles=64223, min_docs=32111,
    ),
    "iowiki": WikipediaEdition(
        lang="io", wiki_id="iowiki",
        label="イド語", label_en="Ido", autonym="Ido",
        articles=63033, min_docs=31516,
    ),
    "suwiki": WikipediaEdition(
        lang="su", wiki_id="suwiki",
        label="スンダ語", label_en="Sundanese", autonym="Sunda",
        articles=62946, min_docs=31473,
    ),
    "iswiki": WikipediaEdition(
        lang="is", wiki_id="iswiki",
        label="アイスランド語", label_en="Icelandic", autonym="íslenska",
        articles=61163, min_docs=30581,
    ),
    "szlwiki": WikipediaEdition(
        lang="szl", wiki_id="szlwiki",
        label="シレジア語", label_en="Silesian", autonym="ślůnski",
        articles=60877, min_docs=30438,
    ),
    "fywiki": WikipediaEdition(
        lang="fy", wiki_id="fywiki",
        label="西フリジア語", label_en="Western Frisian", autonym="Frysk",
        articles=60386, min_docs=30193,
    ),
    "pawiki": WikipediaEdition(
        lang="pa", wiki_id="pawiki",
        label="パンジャブ語", label_en="Punjabi", autonym="ਪੰਜਾਬੀ",
        articles=59630, min_docs=29815,
    ),
    "cvwiki": WikipediaEdition(
        lang="cv", wiki_id="cvwiki",
        label="チュヴァシ語", label_en="Chuvash", autonym="чӑвашла",
        articles=59108, min_docs=29554,
    ),
    "vowiki": WikipediaEdition(
        lang="vo", wiki_id="vowiki",
        label="ヴォラピュク語", label_en="Volapük", autonym="Volapük",
        articles=53665, min_docs=26832,
    ),
    "tlwiki": WikipediaEdition(
        lang="tl", wiki_id="tlwiki",
        label="タガログ語", label_en="Tagalog", autonym="Tagalog",
        articles=49150, min_docs=24575,
    ),
    "glkwiki": WikipediaEdition(
        lang="glk", wiki_id="glkwiki",
        label="ギラキ語", label_en="Gilaki", autonym="گیلکی",
        articles=48314, min_docs=24157,
    ),
    "wuuwiki": WikipediaEdition(
        lang="wuu", wiki_id="wuuwiki",
        label="呉語", label_en="Wu", autonym="吴语",
        articles=48289, min_docs=24144,
    ),
    "igwiki": WikipediaEdition(
        lang="ig", wiki_id="igwiki",
        label="イボ語", label_en="Igbo", autonym="Igbo",
        articles=48055, min_docs=24027,
    ),
    "diqwiki": WikipediaEdition(
        lang="diq", wiki_id="diqwiki",
        label="Dimli", label_en="Dimli", autonym="Zazaki",
        articles=42710, min_docs=21355,
    ),
    "banwiki": WikipediaEdition(
        lang="ban", wiki_id="banwiki",
        label="バリ語", label_en="Balinese", autonym="Basa Bali",
        articles=38353, min_docs=19176,
    ),
    "yowiki": WikipediaEdition(
        lang="yo", wiki_id="yowiki",
        label="ヨルバ語", label_en="Yoruba", autonym="Yorùbá",
        articles=38336, min_docs=19168,
    ),
    "knwiki": WikipediaEdition(
        lang="kn", wiki_id="knwiki",
        label="カンナダ語", label_en="Kannada", autonym="ಕನ್ನಡ",
        articles=35226, min_docs=17613,
    ),
    "scowiki": WikipediaEdition(
        lang="sco", wiki_id="scowiki",
        label="スコットランド語", label_en="Scots", autonym="Scots",
        articles=34256, min_docs=17128,
    ),
    "alswiki": WikipediaEdition(
        lang="als", wiki_id="alswiki",
        label="スイスドイツ語", label_en="Alemannic", autonym="Alemannisch",
        articles=31766, min_docs=15883,
    ),
    "guwiki": WikipediaEdition(
        lang="gu", wiki_id="guwiki",
        label="グジャラート語", label_en="Gujarati", autonym="ગુજરાતી",
        articles=30862, min_docs=15431,
    ),
    "iawiki": WikipediaEdition(
        lang="ia", wiki_id="iawiki",
        label="インターリングア", label_en="Interlingua", autonym="interlingua",
        articles=30477, min_docs=15238,
    ),
    "newiki": WikipediaEdition(
        lang="ne", wiki_id="newiki",
        label="ネパール語", label_en="Nepali", autonym="नेपाली",
        articles=29942, min_docs=14971,
    ),
    "avkwiki": WikipediaEdition(
        lang="avk", wiki_id="avkwiki",
        label="コタヴァ", label_en="Kotava", autonym="Kotava",
        articles=29905, min_docs=14952,
    ),
    "crhwiki": WikipediaEdition(
        lang="crh", wiki_id="crhwiki",
        label="クリミア・タタール語", label_en="Crimean Tatar", autonym="qırımtatarca",
        articles=29718, min_docs=14859,
    ),
    "mnwiki": WikipediaEdition(
        lang="mn", wiki_id="mnwiki",
        label="モンゴル語", label_en="Mongolian", autonym="монгол",
        articles=27886, min_docs=13943,
    ),
    "barwiki": WikipediaEdition(
        lang="bar", wiki_id="barwiki",
        label="バイエルン・オーストリア語", label_en="Bavarian", autonym="Boarisch",
        articles=27232, min_docs=13616,
    ),
    "scnwiki": WikipediaEdition(
        lang="scn", wiki_id="scnwiki",
        label="シチリア語", label_en="Sicilian", autonym="sicilianu",
        articles=26312, min_docs=13156,
    ),
    "siwiki": WikipediaEdition(
        lang="si", wiki_id="siwiki",
        label="シンハラ語", label_en="Sinhala", autonym="සිංහල",
        articles=25610, min_docs=12805,
    ),
    "bpywiki": WikipediaEdition(
        lang="bpy", wiki_id="bpywiki",
        label="ビシュヌプリヤ・マニプリ語", label_en="Bishnupriya", autonym="বিষ্ণুপ্রিয়া মণিপুরী",
        articles=25097, min_docs=12548,
    ),
    "aswiki": WikipediaEdition(
        lang="as", wiki_id="aswiki",
        label="アッサム語", label_en="Assamese", autonym="অসমীয়া",
        articles=24720, min_docs=12360,
    ),
    "skrwiki": WikipediaEdition(
        lang="skr", wiki_id="skrwiki",
        label="Saraiki", label_en="Saraiki", autonym="سرائیکی",
        articles=24646, min_docs=12323,
    ),
    "quwiki": WikipediaEdition(
        lang="qu", wiki_id="quwiki",
        label="ケチュア語", label_en="Quechua", autonym="Runa Simi",
        articles=24545, min_docs=12272,
    ),
    "nvwiki": WikipediaEdition(
        lang="nv", wiki_id="nvwiki",
        label="ナバホ語", label_en="Navajo", autonym="Diné bizaad",
        articles=22667, min_docs=11333,
    ),
    "xmfwiki": WikipediaEdition(
        lang="xmf", wiki_id="xmfwiki",
        label="メグレル語", label_en="Mingrelian", autonym="მარგალური",
        articles=22407, min_docs=11203,
    ),
    "bclwiki": WikipediaEdition(
        lang="bcl", wiki_id="bclwiki",
        label="Central Bikol", label_en="Central Bikol", autonym="Bikol Central",
        articles=22360, min_docs=11180,
    ),
    "sdwiki": WikipediaEdition(
        lang="sd", wiki_id="sdwiki",
        label="シンド語", label_en="Sindhi", autonym="سنڌي",
        articles=22038, min_docs=11019,
    ),
    "oswiki": WikipediaEdition(
        lang="os", wiki_id="oswiki",
        label="オセット語", label_en="Ossetic", autonym="ирон",
        articles=21756, min_docs=10878,
    ),
    "pswiki": WikipediaEdition(
        lang="ps", wiki_id="pswiki",
        label="パシュトゥー語", label_en="Pashto", autonym="پښتو",
        articles=21326, min_docs=10663,
    ),
    "frrwiki": WikipediaEdition(
        lang="frr", wiki_id="frrwiki",
        label="北フリジア語", label_en="Northern Frisian", autonym="Nordfriisk",
        articles=21281, min_docs=10640,
    ),
    "orwiki": WikipediaEdition(
        lang="or", wiki_id="orwiki",
        label="オディア語", label_en="Odia", autonym="ଓଡ଼ିଆ",
        articles=20971, min_docs=10485,
    ),
    "shnwiki": WikipediaEdition(
        lang="shn", wiki_id="shnwiki",
        label="シャン語", label_en="Shan", autonym="တႆး",
        articles=20927, min_docs=10463,
    ),
    "ffwiki": WikipediaEdition(
        lang="ff", wiki_id="ffwiki",
        label="フラ語", label_en="Fula", autonym="Fulfulde",
        articles=20667, min_docs=10333,
    ),
    "tumwiki": WikipediaEdition(
        lang="tum", wiki_id="tumwiki",
        label="トゥンブカ語", label_en="Tumbuka", autonym="chiTumbuka",
        articles=19283, min_docs=9641,
    ),
    "arywiki": WikipediaEdition(
        lang="ary", wiki_id="arywiki",
        label="モロッコ・アラビア語", label_en="Moroccan Arabic", autonym="الدارجة",
        articles=19229, min_docs=9614,
    ),
    "sahwiki": WikipediaEdition(
        lang="sah", wiki_id="sahwiki",
        label="サハ語", label_en="Yakut", autonym="саха тыла",
        articles=18218, min_docs=9109,
    ),
    "bat_smgwiki": WikipediaEdition(
        lang="bat-smg", wiki_id="bat_smgwiki",
        label="サモギティア語", label_en="Samogitian", autonym="žemaitėška",
        articles=17275, min_docs=8637,
    ),
    "cdowiki": WikipediaEdition(
        lang="cdo", wiki_id="cdowiki",
        label="Mindong", label_en="Mindong", autonym="閩東語 / Mìng-dĕ̤ng-ngṳ̄",
        articles=16742, min_docs=8371,
    ),
    "gdwiki": WikipediaEdition(
        lang="gd", wiki_id="gdwiki",
        label="スコットランド・ゲール語", label_en="Scottish Gaelic", autonym="Gàidhlig",
        articles=16060, min_docs=8030,
    ),
    "bugwiki": WikipediaEdition(
        lang="bug", wiki_id="bugwiki",
        label="ブギ語", label_en="Buginese", autonym="Basa Ugi",
        articles=15959, min_docs=7979,
    ),
    "yiwiki": WikipediaEdition(
        lang="yi", wiki_id="yiwiki",
        label="イディッシュ語", label_en="Yiddish", autonym="ייִדיש",
        articles=15760, min_docs=7880,
    ),
    "amwiki": WikipediaEdition(
        lang="am", wiki_id="amwiki",
        label="アムハラ語", label_en="Amharic", autonym="አማርኛ",
        articles=15700, min_docs=7850,
    ),
    "satwiki": WikipediaEdition(
        lang="sat", wiki_id="satwiki",
        label="サンターリー語", label_en="Santali", autonym="ᱥᱟᱱᱛᱟᱲᱤ",
        articles=15631, min_docs=7815,
    ),
    "ilowiki": WikipediaEdition(
        lang="ilo", wiki_id="ilowiki",
        label="イロカノ語", label_en="Iloko", autonym="Ilokano",
        articles=15526, min_docs=7763,
    ),
    "kaawiki": WikipediaEdition(
        lang="kaa", wiki_id="kaawiki",
        label="カラカルパク語", label_en="Kara-Kalpak", autonym="Qaraqalpaqsha",
        articles=15454, min_docs=7727,
    ),
    "liwiki": WikipediaEdition(
        lang="li", wiki_id="liwiki",
        label="リンブルフ語", label_en="Limburgish", autonym="Limburgs",
        articles=15214, min_docs=7607,
    ),
    "gorwiki": WikipediaEdition(
        lang="gor", wiki_id="gorwiki",
        label="ゴロンタロ語", label_en="Gorontalo", autonym="Bahasa Hulontalo",
        articles=15109, min_docs=7554,
    ),
    "napwiki": WikipediaEdition(
        lang="nap", wiki_id="napwiki",
        label="ナポリ語", label_en="Neapolitan", autonym="Napulitano",
        articles=14966, min_docs=7483,
    ),
    "dagwiki": WikipediaEdition(
        lang="dag", wiki_id="dagwiki",
        label="Dagbani", label_en="Dagbani", autonym="dagbanli",
        articles=14496, min_docs=7248,
    ),
    "maiwiki": WikipediaEdition(
        lang="mai", wiki_id="maiwiki",
        label="マイティリー語", label_en="Maithili", autonym="मैथिली",
        articles=14355, min_docs=7177,
    ),
    "hsbwiki": WikipediaEdition(
        lang="hsb", wiki_id="hsbwiki",
        label="高地ソルブ語", label_en="Upper Sorbian", autonym="hornjoserbsce",
        articles=14272, min_docs=7136,
    ),
    "fowiki": WikipediaEdition(
        lang="fo", wiki_id="fowiki",
        label="フェロー語", label_en="Faroese", autonym="føroyskt",
        articles=14219, min_docs=7109,
    ),
    "emlwiki": WikipediaEdition(
        lang="eml", wiki_id="emlwiki",
        label="Emiliano-Romagnolo", label_en="Emiliano-Romagnolo", autonym="emiliàn e rumagnòl",
        articles=14198, min_docs=7099,
    ),
    "zh_classicalwiki": WikipediaEdition(
        lang="zh-classical", wiki_id="zh_classicalwiki",
        label="漢文", label_en="Literary Chinese", autonym="文言",
        articles=14118, min_docs=7059,
    ),
    "hywwiki": WikipediaEdition(
        lang="hyw", wiki_id="hywwiki",
        label="Western Armenian", label_en="Western Armenian", autonym="Արեւմտահայերէն",
        articles=13999, min_docs=6999,
    ),
    "map_bmswiki": WikipediaEdition(
        lang="map-bms", wiki_id="map_bmswiki",
        label="Banyumasan", label_en="Banyumasan", autonym="Basa Banyumasan",
        articles=13975, min_docs=6987,
    ),
    "iewiki": WikipediaEdition(
        lang="ie", wiki_id="iewiki",
        label="インターリング", label_en="Interlingue", autonym="Interlingue",
        articles=13722, min_docs=6861,
    ),
    "acewiki": WikipediaEdition(
        lang="ace", wiki_id="acewiki",
        label="アチェ語", label_en="Acehnese", autonym="Acèh",
        articles=13073, min_docs=6536,
    ),
    "wawiki": WikipediaEdition(
        lang="wa", wiki_id="wawiki",
        label="ワロン語", label_en="Walloon", autonym="walon",
        articles=12997, min_docs=6498,
    ),
    "sawiki": WikipediaEdition(
        lang="sa", wiki_id="sawiki",
        label="サンスクリット語", label_en="Sanskrit", autonym="संस्कृतम्",
        articles=12518, min_docs=6259,
    ),
    "zuwiki": WikipediaEdition(
        lang="zu", wiki_id="zuwiki",
        label="ズールー語", label_en="Zulu", autonym="isiZulu",
        articles=12442, min_docs=6221,
    ),
    "hifwiki": WikipediaEdition(
        lang="hif", wiki_id="hifwiki",
        label="フィジー・ヒンディー語", label_en="Fiji Hindi", autonym="Fiji Hindi",
        articles=12393, min_docs=6196,
    ),
    "sowiki": WikipediaEdition(
        lang="so", wiki_id="sowiki",
        label="ソマリ語", label_en="Somali", autonym="Soomaaliga",
        articles=12281, min_docs=6140,
    ),
    "zghwiki": WikipediaEdition(
        lang="zgh", wiki_id="zghwiki",
        label="標準モロッコ タマジクト語", label_en="Standard Moroccan Tamazight", autonym="ⵜⴰⵎⴰⵣⵉⵖⵜ ⵜⴰⵏⴰⵡⴰⵢⵜ",
        articles=12242, min_docs=6121,
    ),
    "bjnwiki": WikipediaEdition(
        lang="bjn", wiki_id="bjnwiki",
        label="バンジャル語", label_en="Banjar", autonym="Banjar",
        articles=12221, min_docs=6110,
    ),
    "kmwiki": WikipediaEdition(
        lang="km", wiki_id="kmwiki",
        label="クメール語", label_en="Khmer", autonym="ភាសាខ្មែរ",
        articles=12151, min_docs=6075,
    ),
    "snwiki": WikipediaEdition(
        lang="sn", wiki_id="snwiki",
        label="ショナ語", label_en="Shona", autonym="chiShona",
        articles=11716, min_docs=5858,
    ),
    "lijwiki": WikipediaEdition(
        lang="lij", wiki_id="lijwiki",
        label="リグリア語", label_en="Ligurian", autonym="Ligure",
        articles=11577, min_docs=5788,
    ),
    "mhrwiki": WikipediaEdition(
        lang="mhr", wiki_id="mhrwiki",
        label="Eastern Mari", label_en="Eastern Mari", autonym="олык марий",
        articles=11325, min_docs=5662,
    ),
    "kswiki": WikipediaEdition(
        lang="ks", wiki_id="kswiki",
        label="カシミール語", label_en="Kashmiri", autonym="کٲشُر",
        articles=11239, min_docs=5619,
    ),
    "shiwiki": WikipediaEdition(
        lang="shi", wiki_id="shiwiki",
        label="タシルハイト語", label_en="Tachelhit", autonym="Taclḥit",
        articles=10893, min_docs=5446,
    ),
    "mniwiki": WikipediaEdition(
        lang="mni", wiki_id="mniwiki",
        label="マニプリ語", label_en="Manipuri", autonym="ꯃꯤꯇꯩ ꯂꯣꯟ",
        articles=10548, min_docs=5274,
    ),
    "hakwiki": WikipediaEdition(
        lang="hak", wiki_id="hakwiki",
        label="客家語", label_en="Hakka Chinese", autonym="客家語 / Hak-kâ-ngî",
        articles=10469, min_docs=5234,
    ),
    "mrjwiki": WikipediaEdition(
        lang="mrj", wiki_id="mrjwiki",
        label="山地マリ語", label_en="Western Mari", autonym="кырык мары",
        articles=10430, min_docs=5215,
    ),
    "ruewiki": WikipediaEdition(
        lang="rue", wiki_id="ruewiki",
        label="ルシン語", label_en="Rusyn", autonym="русиньскый",
        articles=10276, min_docs=5138,
    ),
    "pamwiki": WikipediaEdition(
        lang="pam", wiki_id="pamwiki",
        label="パンパンガ語", label_en="Pampanga", autonym="Kapampangan",
        articles=10221, min_docs=5110,
    ),
    "tlywiki": WikipediaEdition(
        lang="tly", wiki_id="tlywiki",
        label="タリシュ語", label_en="Talysh", autonym="tolışi",
        articles=10160, min_docs=5080,
    ),
    "rwwiki": WikipediaEdition(
        lang="rw", wiki_id="rwwiki",
        label="キニアルワンダ語", label_en="Kinyarwanda", autonym="Ikinyarwanda",
        articles=9870, min_docs=4935,
    ),
    "ugwiki": WikipediaEdition(
        lang="ug", wiki_id="ugwiki",
        label="ウイグル語", label_en="Uyghur", autonym="ئۇيغۇرچە / Uyghurche",
        articles=9723, min_docs=4861,
    ),
    "roa_tarawiki": WikipediaEdition(
        lang="roa-tara", wiki_id="roa_tarawiki",
        label="Tarantino", label_en="Tarantino", autonym="tarandíne",
        articles=9508, min_docs=4754,
    ),
    "cowiki": WikipediaEdition(
        lang="co", wiki_id="cowiki",
        label="コルシカ語", label_en="Corsican", autonym="corsu",
        articles=9257, min_docs=4628,
    ),
    "bhwiki": WikipediaEdition(
        lang="bh", wiki_id="bhwiki",
        label="Bhojpuri", label_en="Bhojpuri", autonym="भोजपुरी",
        articles=9145, min_docs=4572,
    ),
    "nsowiki": WikipediaEdition(
        lang="nso", wiki_id="nsowiki",
        label="北部ソト語", label_en="Northern Sotho", autonym="Sesotho sa Leboa",
        articles=8948, min_docs=4474,
    ),
    "vlswiki": WikipediaEdition(
        lang="vls", wiki_id="vlswiki",
        label="西フラマン語", label_en="West Flemish", autonym="West-Vlams",
        articles=8360, min_docs=4180,
    ),
    "nds_nlwiki": WikipediaEdition(
        lang="nds-nl", wiki_id="nds_nlwiki",
        label="Low Saxon", label_en="Low Saxon", autonym="Nedersaksies",
        articles=8106, min_docs=4053,
    ),
    "bowiki": WikipediaEdition(
        lang="bo", wiki_id="bowiki",
        label="チベット語", label_en="Tibetan", autonym="བོད་ཡིག",
        articles=8072, min_docs=4036,
    ),
    "miwiki": WikipediaEdition(
        lang="mi", wiki_id="miwiki",
        label="マオリ語", label_en="Māori", autonym="Māori",
        articles=8053, min_docs=4026,
    ),
    "sewiki": WikipediaEdition(
        lang="se", wiki_id="sewiki",
        label="北サーミ語", label_en="Northern Sami", autonym="davvisámegiella",
        articles=7907, min_docs=3953,
    ),
    "mtwiki": WikipediaEdition(
        lang="mt", wiki_id="mtwiki",
        label="マルタ語", label_en="Maltese", autonym="Malti",
        articles=7886, min_docs=3943,
    ),
    "myvwiki": WikipediaEdition(
        lang="myv", wiki_id="myvwiki",
        label="エルジャ語", label_en="Erzya", autonym="эрзянь",
        articles=7873, min_docs=3936,
    ),
    "scwiki": WikipediaEdition(
        lang="sc", wiki_id="scwiki",
        label="サルデーニャ語", label_en="Sardinian", autonym="sardu",
        articles=7827, min_docs=3913,
    ),
    "mdfwiki": WikipediaEdition(
        lang="mdf", wiki_id="mdfwiki",
        label="モクシャ語", label_en="Moksha", autonym="мокшень",
        articles=7629, min_docs=3814,
    ),
    "zeawiki": WikipediaEdition(
        lang="zea", wiki_id="zeawiki",
        label="ゼーラント語", label_en="Zeelandic", autonym="Zeêuws",
        articles=7408, min_docs=3704,
    ),
    "kwwiki": WikipediaEdition(
        lang="kw", wiki_id="kwwiki",
        label="コーンウォール語", label_en="Cornish", autonym="kernowek",
        articles=7256, min_docs=3628,
    ),
    "tkwiki": WikipediaEdition(
        lang="tk", wiki_id="tkwiki",
        label="トルクメン語", label_en="Turkmen", autonym="Türkmençe",
        articles=7207, min_docs=3603,
    ),
    "kabwiki": WikipediaEdition(
        lang="kab", wiki_id="kabwiki",
        label="カビル語", label_en="Kabyle", autonym="Taqbaylit",
        articles=7151, min_docs=3575,
    ),
    "gvwiki": WikipediaEdition(
        lang="gv", wiki_id="gvwiki",
        label="マン島語", label_en="Manx", autonym="Gaelg",
        articles=7113, min_docs=3556,
    ),
    "vepwiki": WikipediaEdition(
        lang="vep", wiki_id="vepwiki",
        label="ヴェプス語", label_en="Veps", autonym="vepsän kel’",
        articles=7112, min_docs=3556,
    ),
    "fiu_vrowiki": WikipediaEdition(
        lang="fiu-vro", wiki_id="fiu_vrowiki",
        label="ヴォロ語", label_en="Võro", autonym="võro",
        articles=6909, min_docs=3454,
    ),
    "ganwiki": WikipediaEdition(
        lang="gan", wiki_id="ganwiki",
        label="贛語", label_en="Gan", autonym="贛語",
        articles=6817, min_docs=3408,
    ),
    "smnwiki": WikipediaEdition(
        lang="smn", wiki_id="smnwiki",
        label="イナリ・サーミ語", label_en="Inari Sami", autonym="anarâškielâ",
        articles=6719, min_docs=3359,
    ),
    "abwiki": WikipediaEdition(
        lang="ab", wiki_id="abwiki",
        label="アブハズ語", label_en="Abkhazian", autonym="аԥсшәа",
        articles=6708, min_docs=3354,
    ),
    "pcdwiki": WikipediaEdition(
        lang="pcd", wiki_id="pcdwiki",
        label="ピカルディ語", label_en="Picard", autonym="Picard",
        articles=6125, min_docs=3062,
    ),
    "gnwiki": WikipediaEdition(
        lang="gn", wiki_id="gnwiki",
        label="グアラニー語", label_en="Guarani", autonym="Avañe'ẽ",
        articles=6034, min_docs=3017,
    ),
    "udmwiki": WikipediaEdition(
        lang="udm", wiki_id="udmwiki",
        label="ウドムルト語", label_en="Udmurt", autonym="удмурт",
        articles=5915, min_docs=2957,
    ),
    "frpwiki": WikipediaEdition(
        lang="frp", wiki_id="frpwiki",
        label="アルピタン語", label_en="Arpitan", autonym="arpetan",
        articles=5829, min_docs=2914,
    ),
    "kvwiki": WikipediaEdition(
        lang="kv", wiki_id="kvwiki",
        label="コミ語", label_en="Komi", autonym="коми",
        articles=5819, min_docs=2909,
    ),
    "csbwiki": WikipediaEdition(
        lang="csb", wiki_id="csbwiki",
        label="カシューブ語", label_en="Kashubian", autonym="kaszëbsczi",
        articles=5720, min_docs=2860,
    ),
    "lgwiki": WikipediaEdition(
        lang="lg", wiki_id="lgwiki",
        label="ガンダ語", label_en="Ganda", autonym="Luganda",
        articles=5706, min_docs=2853,
    ),
    "papwiki": WikipediaEdition(
        lang="pap", wiki_id="papwiki",
        label="パピアメント語", label_en="Papiamento", autonym="Papiamentu",
        articles=5620, min_docs=2810,
    ),
    "lowiki": WikipediaEdition(
        lang="lo", wiki_id="lowiki",
        label="ラオ語", label_en="Lao", autonym="ລາວ",
        articles=5588, min_docs=2794,
    ),
    "gpewiki": WikipediaEdition(
        lang="gpe", wiki_id="gpewiki",
        label="Ghanaian Pidgin", label_en="Ghanaian Pidgin", autonym="Ghanaian Pidgin",
        articles=5527, min_docs=2763,
    ),
    "madwiki": WikipediaEdition(
        lang="mad", wiki_id="madwiki",
        label="マドゥラ語", label_en="Madurese", autonym="Madhurâ",
        articles=5383, min_docs=2691,
    ),
    "tnwiki": WikipediaEdition(
        lang="tn", wiki_id="tnwiki",
        label="ツワナ語", label_en="Tswana", autonym="Setswana",
        articles=5340, min_docs=2670,
    ),
    "furwiki": WikipediaEdition(
        lang="fur", wiki_id="furwiki",
        label="フリウリ語", label_en="Friulian", autonym="furlan",
        articles=5291, min_docs=2645,
    ),
    "aywiki": WikipediaEdition(
        lang="ay", wiki_id="aywiki",
        label="アイマラ語", label_en="Aymara", autonym="Aymar aru",
        articles=5286, min_docs=2643,
    ),
    "lnwiki": WikipediaEdition(
        lang="ln", wiki_id="lnwiki",
        label="リンガラ語", label_en="Lingala", autonym="lingála",
        articles=5229, min_docs=2614,
    ),
    "angwiki": WikipediaEdition(
        lang="ang", wiki_id="angwiki",
        label="古英語", label_en="Old English", autonym="Ænglisc",
        articles=5226, min_docs=2613,
    ),
    "nrmwiki": WikipediaEdition(
        lang="nrm", wiki_id="nrmwiki",
        label="Norman", label_en="Norman", autonym="Nouormand",
        articles=5060, min_docs=2530,
    ),
    "olowiki": WikipediaEdition(
        lang="olo", wiki_id="olowiki",
        label="Livvi-Karelian", label_en="Livvi-Karelian", autonym="livvinkarjala",
        articles=5022, min_docs=2511,
    ),
    "twwiki": WikipediaEdition(
        lang="tw", wiki_id="twwiki",
        label="トウィ語", label_en="Twi", autonym="Twi",
        articles=4735, min_docs=2367,
    ),
    "lfnwiki": WikipediaEdition(
        lang="lfn", wiki_id="lfnwiki",
        label="リングア・フランカ・ノバ", label_en="Lingua Franca Nova", autonym="Lingua Franca Nova",
        articles=4671, min_docs=2335,
    ),
    "tokwiki": WikipediaEdition(
        lang="tok", wiki_id="tokwiki",
        label="トキポナ語", label_en="Toki Pona", autonym="toki pona",
        articles=4478, min_docs=2239,
    ),
    "lezwiki": WikipediaEdition(
        lang="lez", wiki_id="lezwiki",
        label="レズギ語", label_en="Lezghian", autonym="лезги",
        articles=4476, min_docs=2238,
    ),
    "fonwiki": WikipediaEdition(
        lang="fon", wiki_id="fonwiki",
        label="フォン語", label_en="Fon", autonym="fɔ̀ngbè",
        articles=4459, min_docs=2229,
    ),
    "mwlwiki": WikipediaEdition(
        lang="mwl", wiki_id="mwlwiki",
        label="ミランダ語", label_en="Mirandese", autonym="Mirandés",
        articles=4340, min_docs=2170,
    ),
    "extwiki": WikipediaEdition(
        lang="ext", wiki_id="extwiki",
        label="エストレマドゥーラ語", label_en="Extremaduran", autonym="estremeñu",
        articles=4241, min_docs=2120,
    ),
    "mnwwiki": WikipediaEdition(
        lang="mnw", wiki_id="mnwwiki",
        label="Mon", label_en="Mon", autonym="ဘာသာမန်",
        articles=4217, min_docs=2108,
    ),
    "stqwiki": WikipediaEdition(
        lang="stq", wiki_id="stqwiki",
        label="ザーターフリジア語", label_en="Saterland Frisian", autonym="Seeltersk",
        articles=4188, min_docs=2094,
    ),
    "tyvwiki": WikipediaEdition(
        lang="tyv", wiki_id="tyvwiki",
        label="トゥヴァ語", label_en="Tuvinian", autonym="тыва дыл",
        articles=4127, min_docs=2063,
    ),
    "ladwiki": WikipediaEdition(
        lang="lad", wiki_id="ladwiki",
        label="ラディノ語", label_en="Ladino", autonym="Ladino",
        articles=4078, min_docs=2039,
    ),
    "avwiki": WikipediaEdition(
        lang="av", wiki_id="avwiki",
        label="アヴァル語", label_en="Avaric", autonym="авар",
        articles=4016, min_docs=2008,
    ),
    "rmwiki": WikipediaEdition(
        lang="rm", wiki_id="rmwiki",
        label="ロマンシュ語", label_en="Romansh", autonym="rumantsch",
        articles=3867, min_docs=1933,
    ),
    "nahwiki": WikipediaEdition(
        lang="nah", wiki_id="nahwiki",
        label="Nahuatl", label_en="Nahuatl", autonym="Nāhuatl",
        articles=3844, min_docs=1922,
    ),
    "dtywiki": WikipediaEdition(
        lang="dty", wiki_id="dtywiki",
        label="Doteli", label_en="Doteli", autonym="डोटेली",
        articles=3742, min_docs=1871,
    ),
    "tcywiki": WikipediaEdition(
        lang="tcy", wiki_id="tcywiki",
        label="トゥル語", label_en="Tulu", autonym="ತುಳು",
        articles=3670, min_docs=1835,
    ),
    "gomwiki": WikipediaEdition(
        lang="gom", wiki_id="gomwiki",
        label="Goan Konkani", label_en="Goan Konkani", autonym="गोंयची कोंकणी / Gõychi Konknni",
        articles=3643, min_docs=1821,
    ),
    "koiwiki": WikipediaEdition(
        lang="koi", wiki_id="koiwiki",
        label="コミ・ペルミャク語", label_en="Komi-Permyak", autonym="перем коми",
        articles=3472, min_docs=1736,
    ),
    "dsbwiki": WikipediaEdition(
        lang="dsb", wiki_id="dsbwiki",
        label="低地ソルブ語", label_en="Lower Sorbian", autonym="dolnoserbski",
        articles=3445, min_docs=1722,
    ),
    "dgawiki": WikipediaEdition(
        lang="dga", wiki_id="dgawiki",
        label="Southern Dagaare", label_en="Southern Dagaare", autonym="Dagaare",
        articles=3431, min_docs=1715,
    ),
    "bewwiki": WikipediaEdition(
        lang="bew", wiki_id="bewwiki",
        label="ベタウィ語", label_en="Betawi", autonym="Betawi",
        articles=3334, min_docs=1667,
    ),
    "dvwiki": WikipediaEdition(
        lang="dv", wiki_id="dvwiki",
        label="ディベヒ語", label_en="Divehi", autonym="ދިވެހިބަސް",
        articles=3218, min_docs=1609,
    ),
    "blkwiki": WikipediaEdition(
        lang="blk", wiki_id="blkwiki",
        label="Pa'O", label_en="Pa'O", autonym="ပအိုဝ်ႏဘာႏသာႏ",
        articles=3107, min_docs=1553,
    ),
    "kshwiki": WikipediaEdition(
        lang="ksh", wiki_id="kshwiki",
        label="ケルン語", label_en="Colognian", autonym="Ripoarisch",
        articles=3038, min_docs=1519,
    ),
    "zawiki": WikipediaEdition(
        lang="za", wiki_id="zawiki",
        label="チワン語", label_en="Zhuang", autonym="Vahcuengh",
        articles=3020, min_docs=1510,
    ),
    "gagwiki": WikipediaEdition(
        lang="gag", wiki_id="gagwiki",
        label="ガガウズ語", label_en="Gagauz", autonym="Gagauz",
        articles=3008, min_docs=1504,
    ),
    "bxrwiki": WikipediaEdition(
        lang="bxr", wiki_id="bxrwiki",
        label="Russia Buriat", label_en="Russia Buriat", autonym="буряад",
        articles=2919, min_docs=1459,
    ),
    "kgewiki": WikipediaEdition(
        lang="kge", wiki_id="kgewiki",
        label="Komering", label_en="Komering", autonym="Kumoring",
        articles=2909, min_docs=1454,
    ),
    "awawiki": WikipediaEdition(
        lang="awa", wiki_id="awawiki",
        label="アワディー語", label_en="Awadhi", autonym="अवधी",
        articles=2908, min_docs=1454,
    ),
    "hawwiki": WikipediaEdition(
        lang="haw", wiki_id="hawwiki",
        label="ハワイ語", label_en="Hawaiian", autonym="Hawaiʻi",
        articles=2895, min_docs=1447,
    ),
    "krcwiki": WikipediaEdition(
        lang="krc", wiki_id="krcwiki",
        label="カラチャイ・バルカル語", label_en="Karachay-Balkar", autonym="къарачай-малкъар",
        articles=2867, min_docs=1433,
    ),
    "pflwiki": WikipediaEdition(
        lang="pfl", wiki_id="pflwiki",
        label="プファルツ語", label_en="Palatine German", autonym="Pälzisch",
        articles=2865, min_docs=1432,
    ),
    "cbk_zamwiki": WikipediaEdition(
        lang="cbk-zam", wiki_id="cbk_zamwiki",
        label="Chavacano", label_en="Chavacano", autonym="Chavacano de Zamboanga",
        articles=2855, min_docs=1427,
    ),
    "szywiki": WikipediaEdition(
        lang="szy", wiki_id="szywiki",
        label="Sakizaya", label_en="Sakizaya", autonym="Sakizaya",
        articles=2745, min_docs=1372,
    ),
    "pagwiki": WikipediaEdition(
        lang="pag", wiki_id="pagwiki",
        label="パンガシナン語", label_en="Pangasinan", autonym="Pangasinan",
        articles=2651, min_docs=1325,
    ),
    "taywiki": WikipediaEdition(
        lang="tay", wiki_id="taywiki",
        label="Atayal", label_en="Atayal", autonym="Tayal",
        articles=2582, min_docs=1291,
    ),
    "inhwiki": WikipediaEdition(
        lang="inh", wiki_id="inhwiki",
        label="イングーシ語", label_en="Ingush", autonym="гӀалгӀай",
        articles=2557, min_docs=1278,
    ),
    "kiwiki": WikipediaEdition(
        lang="ki", wiki_id="kiwiki",
        label="キクユ語", label_en="Kikuyu", autonym="Gĩkũyũ",
        articles=2531, min_docs=1265,
    ),
    "xhwiki": WikipediaEdition(
        lang="xh", wiki_id="xhwiki",
        label="コサ語", label_en="Xhosa", autonym="isiXhosa",
        articles=2507, min_docs=1253,
    ),
    "ibawiki": WikipediaEdition(
        lang="iba", wiki_id="ibawiki",
        label="イバン語", label_en="Iban", autonym="Jaku Iban",
        articles=2491, min_docs=1245,
    ),
    "atjwiki": WikipediaEdition(
        lang="atj", wiki_id="atjwiki",
        label="アティカメク語", label_en="Atikamekw", autonym="Atikamekw",
        articles=2080, min_docs=1040,
    ),
    "novwiki": WikipediaEdition(
        lang="nov", wiki_id="novwiki",
        label="ノヴィアル", label_en="Novial", autonym="Novial",
        articles=2069, min_docs=1034,
    ),
    "towiki": WikipediaEdition(
        lang="to", wiki_id="towiki",
        label="トンガ語", label_en="Tongan", autonym="lea faka-Tonga",
        articles=2047, min_docs=1023,
    ),
    "pdcwiki": WikipediaEdition(
        lang="pdc", wiki_id="pdcwiki",
        label="ペンシルベニア・ドイツ語", label_en="Pennsylvania German", autonym="Deitsch",
        articles=2029, min_docs=1014,
    ),
    "kncwiki": WikipediaEdition(
        lang="knc", wiki_id="kncwiki",
        label="Central Kanuri", label_en="Central Kanuri", autonym="Yerwa Kanuri",
        articles=2026, min_docs=1013,
    ),
    "stwiki": WikipediaEdition(
        lang="st", wiki_id="stwiki",
        label="南部ソト語", label_en="Southern Sotho", autonym="Sesotho",
        articles=1972, min_docs=986,
    ),
    "dtpwiki": WikipediaEdition(
        lang="dtp", wiki_id="dtpwiki",
        label="中央ドゥスン語", label_en="Central Dusun", autonym="Kadazandusun",
        articles=1970, min_docs=985,
    ),
    "omwiki": WikipediaEdition(
        lang="om", wiki_id="omwiki",
        label="オロモ語", label_en="Oromo", autonym="Oromoo",
        articles=1969, min_docs=984,
    ),
    "kgwiki": WikipediaEdition(
        lang="kg", wiki_id="kgwiki",
        label="コンゴ語", label_en="Kongo", autonym="Kongo",
        articles=1953, min_docs=976,
    ),
    "arcwiki": WikipediaEdition(
        lang="arc", wiki_id="arcwiki",
        label="アラム語", label_en="Aramaic", autonym="ܐܪܡܝܐ",
        articles=1920, min_docs=960,
    ),
    "kcgwiki": WikipediaEdition(
        lang="kcg", wiki_id="kcgwiki",
        label="カタブ語", label_en="Tyap", autonym="Tyap",
        articles=1882, min_docs=941,
    ),
    "fatwiki": WikipediaEdition(
        lang="fat", wiki_id="fatwiki",
        label="ファンティー語", label_en="Fanti", autonym="mfantse",
        articles=1796, min_docs=898,
    ),
    "niawiki": WikipediaEdition(
        lang="nia", wiki_id="niawiki",
        label="ニアス語", label_en="Nias", autonym="Li Niha",
        articles=1778, min_docs=889,
    ),
    "wowiki": WikipediaEdition(
        lang="wo", wiki_id="wowiki",
        label="ウォロフ語", label_en="Wolof", autonym="Wolof",
        articles=1742, min_docs=871,
    ),
    "kaiwiki": WikipediaEdition(
        lang="kai", wiki_id="kaiwiki",
        label="Karekare", label_en="Karekare", autonym="Karai-karai",
        articles=1734, min_docs=867,
    ),
    "fjwiki": WikipediaEdition(
        lang="fj", wiki_id="fjwiki",
        label="フィジー語", label_en="Fijian", autonym="Na Vosa Vakaviti",
        articles=1726, min_docs=863,
    ),
    "jamwiki": WikipediaEdition(
        lang="jam", wiki_id="jamwiki",
        label="ジャマイカ・クレオール語", label_en="Jamaican Creole English", autonym="Patois",
        articles=1719, min_docs=859,
    ),
    "kbpwiki": WikipediaEdition(
        lang="kbp", wiki_id="kbpwiki",
        label="Kabiye", label_en="Kabiye", autonym="Kabɩyɛ",
        articles=1714, min_docs=857,
    ),
    "guwwiki": WikipediaEdition(
        lang="guw", wiki_id="guwwiki",
        label="Gun", label_en="Gun", autonym="gungbe",
        articles=1698, min_docs=849,
    ),
    "anpwiki": WikipediaEdition(
        lang="anp", wiki_id="anpwiki",
        label="アンギカ語", label_en="Angika", autonym="अंगिका",
        articles=1688, min_docs=844,
    ),
    "kbdwiki": WikipediaEdition(
        lang="kbd", wiki_id="kbdwiki",
        label="カバルド語", label_en="Kabardian", autonym="адыгэбзэ",
        articles=1669, min_docs=834,
    ),
    "nqowiki": WikipediaEdition(
        lang="nqo", wiki_id="nqowiki",
        label="ンコ語", label_en="N’Ko", autonym="ߒߞߏ",
        articles=1617, min_docs=808,
    ),
    "pcmwiki": WikipediaEdition(
        lang="pcm", wiki_id="pcmwiki",
        label="ナイジェリア・ピジン語", label_en="Nigerian Pidgin", autonym="Naijá",
        articles=1612, min_docs=806,
    ),
    "iglwiki": WikipediaEdition(
        lang="igl", wiki_id="iglwiki",
        label="Igala", label_en="Igala", autonym="Igala",
        articles=1606, min_docs=803,
    ),
    "xalwiki": WikipediaEdition(
        lang="xal", wiki_id="xalwiki",
        label="カルムイク語", label_en="Kalmyk", autonym="хальмг",
        articles=1575, min_docs=787,
    ),
    "tetwiki": WikipediaEdition(
        lang="tet", wiki_id="tetwiki",
        label="テトゥン語", label_en="Tetum", autonym="tetun",
        articles=1543, min_docs=771,
    ),
    "rkiwiki": WikipediaEdition(
        lang="rki", wiki_id="rkiwiki",
        label="Arakanese", label_en="Arakanese", autonym="ရခိုင်",
        articles=1531, min_docs=765,
    ),
    "biwiki": WikipediaEdition(
        lang="bi", wiki_id="biwiki",
        label="ビスラマ語", label_en="Bislama", autonym="Bislama",
        articles=1486, min_docs=743,
    ),
    "isvwiki": WikipediaEdition(
        lang="isv", wiki_id="isvwiki",
        label="Interslavic", label_en="Interslavic", autonym="medžuslovjansky",
        articles=1482, min_docs=741,
    ),
    "cuwiki": WikipediaEdition(
        lang="cu", wiki_id="cuwiki",
        label="教会スラブ語", label_en="Church Slavic", autonym="словѣньскъ / ⰔⰎⰑⰂⰡⰐⰠⰔⰍⰟ",
        articles=1479, min_docs=739,
    ),
    "bbcwiki": WikipediaEdition(
        lang="bbc", wiki_id="bbcwiki",
        label="トバ・バタク語", label_en="Batak Toba", autonym="Batak Toba",
        articles=1448, min_docs=724,
    ),
    "rskwiki": WikipediaEdition(
        lang="rsk", wiki_id="rskwiki",
        label="Pannonian Rusyn", label_en="Pannonian Rusyn", autonym="руски",
        articles=1435, min_docs=717,
    ),
    "tpiwiki": WikipediaEdition(
        lang="tpi", wiki_id="tpiwiki",
        label="トク・ピシン語", label_en="Tok Pisin", autonym="Tok Pisin",
        articles=1416, min_docs=708,
    ),
    "roa_rupwiki": WikipediaEdition(
        lang="roa-rup", wiki_id="roa_rupwiki",
        label="アルーマニア語", label_en="Aromanian", autonym="armãneashti",
        articles=1389, min_docs=694,
    ),
    "gurwiki": WikipediaEdition(
        lang="gur", wiki_id="gurwiki",
        label="フラフラ語", label_en="Frafra", autonym="farefare",
        articles=1383, min_docs=691,
    ),
    "kuswiki": WikipediaEdition(
        lang="kus", wiki_id="kuswiki",
        label="Kusaal", label_en="Kusaal", autonym="Kʋsaal",
        articles=1379, min_docs=689,
    ),
    "jbowiki": WikipediaEdition(
        lang="jbo", wiki_id="jbowiki",
        label="ロジバン語", label_en="Lojban", autonym="la .lojban.",
        articles=1356, min_docs=678,
    ),
    "eewiki": WikipediaEdition(
        lang="ee", wiki_id="eewiki",
        label="エウェ語", label_en="Ewe", autonym="eʋegbe",
        articles=1351, min_docs=675,
    ),
    "moswiki": WikipediaEdition(
        lang="mos", wiki_id="moswiki",
        label="モシ語", label_en="Mossi", autonym="moore",
        articles=1325, min_docs=662,
    ),
    "tywiki": WikipediaEdition(
        lang="ty", wiki_id="tywiki",
        label="タヒチ語", label_en="Tahitian", autonym="reo tahiti",
        articles=1252, min_docs=626,
    ),
    "sylwiki": WikipediaEdition(
        lang="syl", wiki_id="sylwiki",
        label="Sylheti", label_en="Sylheti", autonym="ꠍꠤꠟꠐꠤ",
        articles=1231, min_docs=615,
    ),
    "btmwiki": WikipediaEdition(
        lang="btm", wiki_id="btmwiki",
        label="Batak Mandailing", label_en="Batak Mandailing", autonym="Batak Mandailing",
        articles=1229, min_docs=614,
    ),
    "smwiki": WikipediaEdition(
        lang="sm", wiki_id="smwiki",
        label="サモア語", label_en="Samoan", autonym="Gagana Samoa",
        articles=1209, min_docs=604,
    ),
    "trvwiki": WikipediaEdition(
        lang="trv", wiki_id="trvwiki",
        label="タロコ語", label_en="Taroko", autonym="Seediq",
        articles=1201, min_docs=600,
    ),
    "sswiki": WikipediaEdition(
        lang="ss", wiki_id="sswiki",
        label="スワジ語", label_en="Swati", autonym="SiSwati",
        articles=1160, min_docs=580,
    ),
    "ltgwiki": WikipediaEdition(
        lang="ltg", wiki_id="ltgwiki",
        label="ラトガリア語", label_en="Latgalian", autonym="latgaļu",
        articles=1154, min_docs=577,
    ),
    "amiwiki": WikipediaEdition(
        lang="ami", wiki_id="amiwiki",
        label="Amis", label_en="Amis", autonym="Pangcah",
        articles=1149, min_docs=574,
    ),
    "srnwiki": WikipediaEdition(
        lang="srn", wiki_id="srnwiki",
        label="スリナム語", label_en="Sranan Tongo", autonym="Sranantongo",
        articles=1133, min_docs=566,
    ),
    "nywiki": WikipediaEdition(
        lang="ny", wiki_id="nywiki",
        label="ニャンジャ語", label_en="Nyanja", autonym="Chi-Chewa",
        articles=1123, min_docs=561,
    ),
    "altwiki": WikipediaEdition(
        lang="alt", wiki_id="altwiki",
        label="南アルタイ語", label_en="Southern Altai", autonym="алтай тил",
        articles=1110, min_docs=555,
    ),
    "tswiki": WikipediaEdition(
        lang="ts", wiki_id="tswiki",
        label="ツォンガ語", label_en="Tsonga", autonym="Xitsonga",
        articles=1098, min_docs=549,
    ),
    "gcrwiki": WikipediaEdition(
        lang="gcr", wiki_id="gcrwiki",
        label="Guianan Creole", label_en="Guianan Creole", autonym="kriyòl gwiyannen",
        articles=1077, min_docs=538,
    ),
    "lbewiki": WikipediaEdition(
        lang="lbe", wiki_id="lbewiki",
        label="Lak", label_en="Lak", autonym="лакку",
        articles=1069, min_docs=534,
    ),
    "chrwiki": WikipediaEdition(
        lang="chr", wiki_id="chrwiki",
        label="チェロキー語", label_en="Cherokee", autonym="ᏣᎳᎩ",
        articles=1034, min_docs=517,
    ),
    "gotwiki": WikipediaEdition(
        lang="got", wiki_id="gotwiki",
        label="ゴート語", label_en="Gothic", autonym="𐌲𐌿𐍄𐌹𐍃𐌺",
        articles=1022, min_docs=511,
    ),
    "nupwiki": WikipediaEdition(
        lang="nup", wiki_id="nupwiki",
        label="Nupe", label_en="Nupe", autonym="Nupe",
        articles=962, min_docs=481,
    ),
    "bmwiki": WikipediaEdition(
        lang="bm", wiki_id="bmwiki",
        label="バンバラ語", label_en="Bambara", autonym="bamanankan",
        articles=930, min_docs=465,
    ),
    "vewiki": WikipediaEdition(
        lang="ve", wiki_id="vewiki",
        label="ベンダ語", label_en="Venda", autonym="Tshivenda",
        articles=903, min_docs=451,
    ),
    "rmywiki": WikipediaEdition(
        lang="rmy", wiki_id="rmywiki",
        label="Vlax Romani", label_en="Vlax Romani", autonym="romani čhib",
        articles=758, min_docs=379,
    ),
    "rnwiki": WikipediaEdition(
        lang="rn", wiki_id="rnwiki",
        label="ルンディ語", label_en="Rundi", autonym="ikirundi",
        articles=729, min_docs=364,
    ),
    "chywiki": WikipediaEdition(
        lang="chy", wiki_id="chywiki",
        label="シャイアン語", label_en="Cheyenne", autonym="Tsetsêhestâhese",
        articles=718, min_docs=359,
    ),
    "gucwiki": WikipediaEdition(
        lang="guc", wiki_id="gucwiki",
        label="ワユ語", label_en="Wayuu", autonym="wayuunaiki",
        articles=705, min_docs=352,
    ),
    "pntwiki": WikipediaEdition(
        lang="pnt", wiki_id="pntwiki",
        label="ポントス・ギリシャ語", label_en="Pontic", autonym="Ποντιακά",
        articles=672, min_docs=336,
    ),
    "adywiki": WikipediaEdition(
        lang="ady", wiki_id="adywiki",
        label="アディゲ語", label_en="Adyghe", autonym="адыгабзэ",
        articles=646, min_docs=323,
    ),
    "ikwiki": WikipediaEdition(
        lang="ik", wiki_id="ikwiki",
        label="イヌピアック語", label_en="Inupiaq", autonym="Iñupiatun",
        articles=597, min_docs=298,
    ),
    "chwiki": WikipediaEdition(
        lang="ch", wiki_id="chwiki",
        label="チャモロ語", label_en="Chamorro", autonym="Chamoru",
        articles=581, min_docs=290,
    ),
    "tddwiki": WikipediaEdition(
        lang="tdd", wiki_id="tddwiki",
        label="Tai Nuea", label_en="Tai Nuea", autonym="ᥖᥭᥰ ᥖᥬᥲ ᥑᥨᥒᥰ",
        articles=448, min_docs=224,
    ),
    "annwiki": WikipediaEdition(
        lang="ann", wiki_id="annwiki",
        label="オボロ語", label_en="Obolo", autonym="Obolo",
        articles=436, min_docs=218,
    ),
    "iuwiki": WikipediaEdition(
        lang="iu", wiki_id="iuwiki",
        label="イヌクティトット語", label_en="Inuktitut", autonym="ᐃᓄᒃᑎᑐᑦ / inuktitut",
        articles=435, min_docs=217,
    ),
    "pwnwiki": WikipediaEdition(
        lang="pwn", wiki_id="pwnwiki",
        label="Paiwan", label_en="Paiwan", autonym="pinayuanan",
        articles=394, min_docs=197,
    ),
    "dzwiki": WikipediaEdition(
        lang="dz", wiki_id="dzwiki",
        label="ゾンカ語", label_en="Dzongkha", autonym="ཇོང་ཁ",
        articles=383, min_docs=191,
    ),
    "sgwiki": WikipediaEdition(
        lang="sg", wiki_id="sgwiki",
        label="サンゴ語", label_en="Sango", autonym="Sängö",
        articles=374, min_docs=187,
    ),
    "tiwiki": WikipediaEdition(
        lang="ti", wiki_id="tiwiki",
        label="ティグリニア語", label_en="Tigrinya", autonym="ትግርኛ",
        articles=365, min_docs=182,
    ),
    "dinwiki": WikipediaEdition(
        lang="din", wiki_id="dinwiki",
        label="ディンカ語", label_en="Dinka", autonym="Thuɔŋjäŋ",
        articles=340, min_docs=170,
    ),
    "nrwiki": WikipediaEdition(
        lang="nr", wiki_id="nrwiki",
        label="南ンデベレ語", label_en="South Ndebele", autonym="isiNdebele seSewula",
        articles=327, min_docs=163,
    ),
    "piwiki": WikipediaEdition(
        lang="pi", wiki_id="piwiki",
        label="パーリ語", label_en="Pali", autonym="पालि",
        articles=302, min_docs=151,
    ),
    "kajwiki": WikipediaEdition(
        lang="kaj", wiki_id="kajwiki",
        label="カジェ語", label_en="Jju", autonym="Jju",
        articles=299, min_docs=149,
    ),
    "pplwiki": WikipediaEdition(
        lang="ppl", wiki_id="pplwiki",
        label="Nawat", label_en="Nawat", autonym="Nawat",
        articles=255, min_docs=127,
    ),
    "bdrwiki": WikipediaEdition(
        lang="bdr", wiki_id="bdrwiki",
        label="West Coast Bajau", label_en="West Coast Bajau", autonym="Bajau Sama",
        articles=242, min_docs=121,
    ),
    "tigwiki": WikipediaEdition(
        lang="tig", wiki_id="tigwiki",
        label="ティグレ語", label_en="Tigre", autonym="ትግሬ",
        articles=62, min_docs=31,
    ),
    "bolwiki": WikipediaEdition(
        lang="bol", wiki_id="bolwiki",
        label="Bole", label_en="Bole", autonym="bòo pìkkà",
        articles=0, min_docs=1,
    ),
    "magwiki": WikipediaEdition(
        lang="mag", wiki_id="magwiki",
        label="マガヒー語", label_en="Magahi", autonym="मगही",
        articles=0, min_docs=1,
    ),
}
