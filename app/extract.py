"""抽出の指定 —— 集める層の最初の 1 回を、AI ではなく決まった手順で埋める。

**Chiezo はここで何を集めているかを知らない。** 「どのソースの・どのタグを・何件・
タグをどう読み替えるか」は依頼する側(外のアプリ)が書いた指定として渡ってきて、
こちらはそれを機械的に実行するだけ。画家も作曲家も、この層から見れば同じ形をしている。

**AI が間違えるところと、間違えないところを分ける**のが狙い。名前・年代・出典の
ような「既に手元の長期記憶に載っている事実」は引いてくれば済むのに、AI に書かせると
存在しない人物や合わない年代が混ざる。逆に、影響関係や代表作のような**書いていない
ことは引けない**ので、そちらは AI に任せる(この層は触らない)。

**指定は 1 度書けば残る。** 依頼文から指定を組み立てるのに AI を使っても、その後は
指定のほうを回すので毎回結果が変わらない。決定的なのは指定であって AI ではない。

指定の形(すべての鍵は省略可、`source` と `tag` だけ必須):

    {
      "source": "jawiki",
      "tag": "ロマン派の作曲家",
      "limit": 30,   ← 書かなければ全部。AI に読ませる側の都合で絞るときだけ書く
      "body": "opening",
      "url": "https://ja.wikipedia.org/wiki/{title}",
      "tags": [
        {"const": "作曲家"},
        {"patterns": ["^(\\\\d{3,4})年生$", "^(\\\\d{3,4})年没$"], "format": "年代:{1}-{2}"},
        {"pattern": "^(.+)出身の人物$", "format": "出身:{1}"}
      ]
    }

**並び順は指定できない。** 引く先のソースが持っている順(`rank_score` の降順 ——
Wikipedia ならページビュー、地名なら人口)をそのまま使う。「有名なほうから N 件」は
ここで既に満たされているので、指定側で並べ替えを書けるようにすると、同じことを
2 通りで書けるだけになる。
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import HTTPException

log = logging.getLogger("chiezo.app")

# **件数は既定では絞らない。** 引くのは手元の索引を 1 本引くだけで、AI の枠も時間も
# 使わない —— 30 件に絞る理由は取る側には無い(絞りたいのは AI に読ませる側)。
#
# 上限は「当たりすぎ」を止めるためだけに置く。**黙って切らずに断る** ——
# 切ったことは返り値から分からないので、絞ったつもりのない指定が
# 「そこまでしか無い」ように見えてしまう(実際に 30 件で止まっているのを、
# 機械抽出の限界だと受け取られた)。
MAX_ROWS = 20_000
# タグの読み替えの上限。指定が肥大すると、1 件あたりの正規表現の回数がそのまま伸びる
MAX_RULES = 20
MAX_PATTERNS_PER_RULE = 4
MAX_PATTERN_CHARS = 200
# 1 件から作るタグの上限(読み替えが総当たりで当たったときの歯止め)
MAX_TAGS_PER_DOC = 30
# 別の文書を引いてくる読み替えで、1 件から取れる数の上限
MAX_TAKE = 5
# つながりの見方。**相互は片側よりずっと強い** —— 有名なものどうしは互いに名前が
# 出てくるので、片側だと 12% が繋がって毛玉になった。相互は 7% まで落ちたうえ、
# 残った組は実際に関係のある相手だった(人物 30 件で実測)
LINK_KINDS = ("mutual", "out")
# 本文に使える欄。**長い本文は取らない** —— 焼くのは要点で、全文は元のソースにある
BODY_FIELDS = ("opening", "body")
DEFAULT_BODY_FIELD = "opening"
# 抽出し終えた印。次の実行は「進み具合が入っている」ほうへ進む(= AI が肉付けする)
DEFAULT_CURSOR = "抽出済み"
# 頼んだ件数のこれを下回ったときだけ、実在するタグを見せて選び直させる。
# **少し足りないだけで投げ直さない** —— 件数を満たそうとして条件のほうが広がる
# (「西洋近代絵画」と頼んだのに 14 世紀からの欧州全体になった)
RETRY_BELOW = 0.5
# 件数を書いていないときに「痩せている」と見なす数。**0 件だけが失敗ではない** ——
# 広い語のタグは実在しても数件しか付いておらず、欲しいものは時代や地域で
# 絞った名前の側にあることが多い(広いほうを書かれて 7 件しか取れなかった)
THIN_ROWS = 10


def looks_thin(spec: dict, got: int) -> bool:
    """取れた数が、頼んだものに対して少なすぎないか。

    件数を書いていれば、その数に対して。書いていなければ、絶対数で見る
    (書いていないときは「全部でこれだけ」なので、少ないのは選んだタグのせい)。
    """
    if limit := spec["limit"]:
        return got < limit * RETRY_BELOW
    return got < THIN_ROWS


def normalize(raw) -> dict | None:
    """指定を確かめて、実行できる形に整える。空なら None(抽出は使わない)。

    **壊れた指定は作る時点で断る**。実行時に落ちると、無人で回っている最中に
    「集められなかった」だけが残り、どこが悪いのかは誰も見ていない。
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("抽出の指定はオブジェクトで書いてください")

    source = str(raw.get("source") or "").strip()
    tag = str(raw.get("tag") or "").strip()
    not_tag = str(raw.get("not_tag") or "").strip()
    if not source:
        raise _bad("source(引くソース名)を入れてください")
    if not tag:
        raise _bad("tag(絞り込むタグ)を入れてください")

    # **書かなければ全部**。書いたときだけ、その数で切る
    limit = raw.get("limit")
    if limit in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise _bad("limit は数で書いてください") from None
        limit = min(max(limit, 1), MAX_ROWS)

    body_field = str(raw.get("body") or DEFAULT_BODY_FIELD).strip()
    if body_field not in BODY_FIELDS:
        raise _bad(f"body は {' / '.join(BODY_FIELDS)} のどれかにしてください")

    rules = raw.get("tags") or []
    if not isinstance(rules, list):
        raise _bad("tags は配列で書いてください")
    if len(rules) > MAX_RULES:
        raise _bad(f"tags の読み替えは {MAX_RULES} 個までです")

    spec = {
        "source": source,
        "tag": tag,
        "not_tag": not_tag,
        "limit": limit,
        "body": body_field,
        "url": str(raw.get("url") or "").strip(),
        "tags": [_normalize_rule(rule) for rule in rules],
        "cursor": str(raw.get("cursor") or DEFAULT_CURSOR).strip() or DEFAULT_CURSOR,
    }
    return spec


def _normalize_rule(raw) -> dict:
    """タグの読み替え 1 つぶん。

    3 通りある:
    - `const` —— 固まった 1 つ。「これは何の一覧か」を見分ける目印を付ける
    - `pattern` —— **当たったタグごとに 1 つ**。1 件に複数付きうるもの向け
    - `patterns` —— **全部当たったときだけ 1 つ**。生年と没年から年代を作るような、
      2 つ以上のタグを 1 つにまとめる読み方
    """
    if not isinstance(raw, dict):
        raise _bad("tags の要素はオブジェクトで書いてください")

    if const := str(raw.get("const") or "").strip():
        return {"kind": "const", "value": const}

    fmt = str(raw.get("format") or "").strip()
    if not fmt:
        raise _bad("読み替えには format(作るタグの形)が要ります")

    # 別の文書を引いてくる読み替え。**この文書のタグではなく、他の文書の見出し**を
    # 入れる(「〈その見出し〉の作品」を引いて代表作を出す、のような)
    if from_tag := str(raw.get("from_tag") or "").strip():
        try:
            take = min(max(int(raw.get("take") or 1), 1), MAX_TAKE)
        except (TypeError, ValueError):
            raise _bad("take は数で書いてください") from None
        return {"kind": "from_tag", "tag": from_tag, "format": fmt, "take": take}

    # 記事どうしのつながり。**この抽出に入っているものだけ**を相手にする
    if linked := str(raw.get("linked") or "").strip():
        if linked not in LINK_KINDS:
            raise _bad(f"linked は {' / '.join(LINK_KINDS)} のどれかにしてください")
        return {"kind": "linked", "how": linked, "format": fmt}

    patterns = raw.get("patterns")
    if patterns is None:
        single = str(raw.get("pattern") or "").strip()
        if not single:
            raise _bad("読み替えには pattern か patterns が要ります")
        return {"kind": "each", "patterns": [_compile(single)], "format": fmt}

    if not isinstance(patterns, list) or not patterns:
        raise _bad("patterns は 1 つ以上の配列で書いてください")
    if len(patterns) > MAX_PATTERNS_PER_RULE:
        raise _bad(f"patterns は {MAX_PATTERNS_PER_RULE} 個までです")
    return {
        "kind": "all",
        "patterns": [_compile(str(p or "")) for p in patterns],
        "format": fmt,
    }


def _compile(pattern: str) -> re.Pattern:
    if not pattern:
        raise _bad("pattern が空です")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise _bad(f"pattern は {MAX_PATTERN_CHARS} 文字までです")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise _bad(f"pattern が正規表現として読めません: {e}") from None


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": message})


def to_json(spec: dict | None) -> dict | None:
    """定義に持たせる形(正規表現は書いた文字列のまま残す)。"""
    if not spec:
        return None
    rules = []
    for rule in spec["tags"]:
        if rule["kind"] == "const":
            rules.append({"const": rule["value"]})
        elif rule["kind"] == "from_tag":
            rules.append({
                "from_tag": rule["tag"], "format": rule["format"], "take": rule["take"]
            })
        elif rule["kind"] == "linked":
            rules.append({"linked": rule["how"], "format": rule["format"]})
        elif rule["kind"] == "each":
            rules.append({"pattern": rule["patterns"][0].pattern, "format": rule["format"]})
        else:
            rules.append({
                "patterns": [p.pattern for p in rule["patterns"]],
                "format": rule["format"],
            })
    return {
        "source": spec["source"],
        "tag": spec["tag"],
        "not_tag": spec["not_tag"],
        "limit": spec["limit"],
        "body": spec["body"],
        "url": spec["url"],
        "tags": rules,
        "cursor": spec["cursor"],
    }


def _doc_ids(spec: dict, sources: dict):
    """指定に当たる doc_id を返す SELECT を組む。"""
    from app.main import build_doc_id_set

    src = sources.get(spec["source"])
    if src is None:
        raise HTTPException(404, {
            "error": f"抽出できません: ソース「{spec['source']}」がありません",
            "hint": "まだ焼いていないか、名前が違う(/v1/sources で確かめられる)",
        })

    id_set = build_doc_id_set(src, tag=spec["tag"])
    if id_set is None:
        raise HTTPException(409, {
            "error": f"ソース「{spec['source']}」はタグで絞り込めません",
            "hint": "タグの転置表が入る前のスキーマで焼かれている。取り込み直すと使える",
        })
    set_sql, params = id_set
    # 外したいタグは EXCEPT で引く。**カテゴリは持ち主の職業を選ばない** ——
    # 仕事で括ったカテゴリにも、それで知られていない人が入っている。人気の順に
    # 取ると本業の人より先にそちらが並ぶ(上位 30 件のうち 3 件がそうだった)
    if excluded := split_tags(spec["not_tag"]):
        set_sql += (
            f" EXCEPT SELECT doc_id FROM doc_tags WHERE tag IN ({','.join('?' * len(excluded))})"
        )
        params = [*params, *excluded]
    return src, set_sql, list(params)


def count(spec: dict, sources: dict) -> int:
    """指定が当たる件数。**取れた数ではなく、当たっている数**。

    取った数だけを見せると、絞られていることに気づけない。
    """
    from app import db

    src, set_sql, params = _doc_ids(spec, sources)
    (matched,) = db.query(src.path, f"SELECT COUNT(*) FROM ({set_sql})", tuple(params))[0]
    return matched


def run(spec: dict, sources: dict) -> tuple[list[dict], str]:
    """指定どおりに引いて、集める層が読む形(items)にして返す。

    返す形は AI に書かせたときとまったく同じ(`title` / `body` / `tags` / `url`)。
    後ろの工程から見れば、誰が作ったものかは区別が付かない。

    **件数を書いていなければ全部取る。** 当たりすぎているときは黙って切らずに断る ——
    切ったことは返り値から分からないので、絞ったつもりのない指定が「そこまでしか無い」
    ように見えてしまう。
    """
    from app import db

    src, set_sql, params = _doc_ids(spec, sources)
    limit = spec["limit"]
    if limit is None:
        matched = count(spec, sources)
        if matched > MAX_ROWS:
            raise HTTPException(409, {
                "error": f"この指定は {matched:,} 件に当たります"
                         f"(一度に取れるのは {MAX_ROWS:,} 件まで)",
                "hint": "タグを絞るか、limit に取る件数を書いてください",
            })
        limit = MAX_ROWS

    rows = db.query(
        src.path,
        f"SELECT title, {spec['body']} AS body, tags, extra, links FROM docs"
        f" WHERE doc_id IN ({set_sql}) ORDER BY rank_score DESC, title LIMIT ?",
        (*params, limit),
    )

    # つながりは**この抽出に入っているものだけ**が相手なので、先に全部読んでから作る
    rows = [dict(row) for row in rows]
    context = {
        "src": src,
        "links": {
            (row.get("title") or "").strip(): _link_set(row.get("links")) for row in rows
        },
    }
    items = [_to_item(row, spec, context) for row in rows]
    items = [item for item in items if item]
    log.info(
        "extract %s tag=%r: %d docs -> %d items", spec["source"], spec["tag"], len(rows), len(items)
    )
    return items, spec["cursor"]


def split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _link_set(raw) -> set[str]:
    """記事から出ているリンク先。節への飛び先(`記事名#節名`)は記事名まで畳む。"""
    return {str(link).split("#", 1)[0].strip() for link in _json_list(raw)}


def _to_item(row: dict, spec: dict, context: dict) -> dict | None:
    """1 行を 1 件にする。本文が空のものは落とす(焼く側でも落ちる)。"""
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    if not title or not body:
        return None

    tags = [str(t) for t in _json_list(row.get("tags"))]
    extra = _json_map(row.get("extra"))

    item = {
        "title": title,
        "body": body,
        "tags": _apply_rules(spec["tags"], tags, title, context),
    }
    if template := spec["url"]:
        item["url"] = _fill(template, {"title": title, **{
            k: str(v) for k, v in extra.items() if isinstance(v, (str, int, float))
        }})
    return item


def _json_list(raw) -> list:
    """`docs.tags` は JSON の配列を文字列で持っている。読めなければ空。"""
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_map(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _apply_rules(rules: list[dict], tags: list[str], title: str, context: dict) -> list[str]:
    """引いてきたタグを、依頼した側が読める形に読み替える。

    **元のタグは持ち越さない。** カテゴリはソースの都合で付いているもので、
    そのまま渡すと読む側が「どれが意味のあるタグか」を選ぶことになる。
    """
    out: list[str] = []
    for rule in rules:
        if rule["kind"] == "const":
            out.append(rule["value"])
        elif rule["kind"] == "each":
            pattern = rule["patterns"][0]
            for tag in tags:
                if found := pattern.match(tag):
                    out.append(_fill_groups(rule["format"], [found]))
        elif rule["kind"] == "from_tag":
            out.extend(_from_tag(rule, title, context))
        elif rule["kind"] == "linked":
            out.extend(_linked(rule, title, context))
        else:
            found = [_first_match(pattern, tags) for pattern in rule["patterns"]]
            # 1 つでも当たらなければ作らない(「1840-」のような半端を残さない)
            if all(found):
                out.append(_fill_groups(rule["format"], found))

    seen, unique = set(), []
    for tag in out:
        if tag and tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique[:MAX_TAGS_PER_DOC]


def _from_tag(rule: dict, title: str, context: dict) -> list[str]:
    """この文書の見出しから作ったタグで、別の文書を引く。

    「<その人>の作品」「<その人>の楽曲」のようなカテゴリを持つソースなら、そこから
    人気の順に取れば代表作になる(人物 30 件で実測したとき 20 件が持っていた)。
    **無ければ何も作らない**。
    """
    from app import db

    src = context["src"]
    tag = _fill(rule["tag"], {"title": title})
    rows = db.query(
        src.path,
        "SELECT title FROM docs WHERE doc_id IN"
        " (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY rank_score DESC, title LIMIT ?",
        (tag, rule["take"]),
    )
    return [_fill(rule["format"], {"1": row[0]}) for row in rows]


def _linked(rule: dict, title: str, context: dict) -> list[str]:
    """この抽出に入っている文書のうち、記事がリンクしている相手。

    **相互リンクを既定にする。** 片側だけだと、有名なものどうしが軒並み繋がって
    毛玉になる(実測で 12% が繋がった)。相互は 7% まで落ちて、しかも残った組は
    実際に関係のある相手だった。
    """
    links = context["links"]
    mine = links.get(title, set())
    others = [
        other
        for other in links
        if other != title and other in mine
        and (rule["how"] == "out" or title in links.get(other, set()))
    ]
    return [_fill(rule["format"], {"1": other}) for other in others]


def _first_match(pattern: re.Pattern, tags: list[str]):
    for tag in tags:
        if found := pattern.match(tag):
            return found
    return None


def _fill_groups(template: str, matches: list) -> str:
    """`{1}` `{2}` … を、捕獲した中身で埋める。

    当たった順に、それぞれの捕獲を並べたものを 1・2・3… と数える
    (`patterns` を 2 つ書いても、1 つの pattern に括弧を 2 つ書いても同じ番号になる)。
    括弧を書いていなければ、当たった部分そのものを 1 つとして数える。
    """
    values = {}
    for found in matches:
        for group in found.groups() or (found.group(0),):
            values[str(len(values) + 1)] = group or ""
    return _fill(template, values)


def _fill(template: str, values: dict[str, str]) -> str:
    """`{名前}` を置き換える。知らない名前はそのまま残す(消して詰めない)。"""
    def swap(found: re.Match) -> str:
        return values.get(found.group(1), found.group(0))

    return re.sub(r"\{([^{}]+)\}", swap, template)

# ---- 依頼文から指定を書かせる ------------------------------------------------

SPEC_GUIDE = """依頼を読んで、**まず「手元の索引から機械的に引けるか」を決めてください**。

引けるなら、抽出の指定を書いてください。引けないなら
`{"extract": null, "reason": "引けない理由"}` を返してください。指定さえ書ければ、
そのあとは AI を呼ばずに何万件でも一度に取れます(1000 件を 10 件ずつ AI に書かせると
100 回かかりますが、指定なら 1 秒で終わります)。

**決める前に、道具で確かめてください。** あなたには Chiezo の道具が渡してあります ——
どんなタグが実在するか(tags)、そのタグに何件当たるか(filter)、記事から何が
リンクされているか(links)、1 件がどんなタグを持っているか(doc)。
**タグ名は当てずっぽうでは当たりません**(完全一致でしか引けないので、それらしい
名前を書くと静かな 0 件になります)。実在する名前を引いてから書いてください。

抽出の指定は次の形の JSON です。

{
  "source": "引くソース名",
  "tag": "絞り込むタグ(完全一致。カンマ区切りで複数書くと、そのどれかを持つもの)",
  "not_tag": "外すタグ(カンマ区切り。これを持つものは、tag に当たっていても取らない)",
  "limit": 30,   ← 書かなければ全部。AI に読ませる側の都合で絞るときだけ書く
  "body": "opening(冒頭。既定) か body(全文)",
  "url": "出典の作り方。{title} と、そのソースが extra に持っている値を差し込める",
  "tags": [
    {"const": "そのまま付ける固定のタグ"},
    {"pattern": "^(.+)出身の人物$", "format": "出身:{1}"},
    {"patterns": ["^(\\d{3,4})年生$", "^(\\d{3,4})年没$"], "format": "年代:{1}-{2}"},
    {"from_tag": "{title}の楽曲", "format": "代表曲:{1}", "take": 1},
    {"linked": "mutual", "format": "関連:{1}"}
  ],
  "cursor": "抽出し終えた印(次の実行はここから肉付けになる)"
}

タグの読み替えは 5 通りです。
- const: 固定の 1 つ。読む側が「これは何の一覧か」を見分ける目印に使う
- pattern: **当たったタグごとに 1 つ**作る。1 件に複数付きうるもの向け
- patterns: **全部当たったときだけ 1 つ**作る。2 つのタグを 1 つにまとめる読み方。
  1 つでも当たらなければ作らない(半端なタグを残さない)
- from_tag: **この文書の見出しから作ったタグで、別の文書を引く**(`{title}` が入る)。
  「<その人>の楽曲」「<その人>の作品」のようなカテゴリがあるソースでは、人気の順に
  取れば代表作になる。`take` で何件取るか(既定 1)。そのカテゴリが無ければ何も作らない
- linked: **この抽出に入っている文書のうち、記事がリンクしている相手**を出す。
  `mutual`(相互にリンクしているものだけ)と `out`(こちらから張っているもの)。
  片側だと有名なものどうしが軒並み繋がって毛玉になるので、**まず mutual を使う**
`{1}` `{2}` は、当たった順に括弧で捕まえた中身が入ります。

**書いていないことは引けませんが、書いてあることは全部引けます。** 関係や代表作も、
カテゴリやリンクの形で書いてあれば取れます。AI に書かせるのは、そのどれでも
取れないものだけにしてください。

守ること。
- source は実在するソース名。tag は**そのソースに実在するタグ**(前方一致や部分一致
  ではなく完全一致で引くので、それらしい名前を作ると 0 件になる)
- **カテゴリは持ち主の職業を選ばない。** 仕事で括ったカテゴリにも、それで知られて
  いない人が入っている。人気の順に取るとそちらが先に並ぶので、混ぜたくないものが
  はっきりしているときは `not_tag` で外す
- 並び順は書けない。ソースが持っている順(ページビューや人口の多い順)で上から取る
- **件数は絞らなくてよい。** 引くのは手元の索引を 1 本引くだけで、AI の枠も時間も
  使わない。「有名なほうから N 件」と頼まれたときだけ limit を書く
- 元のタグは持ち越さない。読む側が要るタグだけを読み替えで作る

出力は JSON だけ。前置き・説明・コードブロックの記号は付けない。"""


def build_draft_messages(want: str, sources: dict, current: dict | None = None) -> list[dict]:
    """依頼文から指定を書かせるときの本文。

    **どのソースがあるかは渡す**(名前を知らなければ実在しないソースを書く)。
    タグまでは渡さない —— 1 つのソースに数十万のタグがあり、渡しきれない。
    書かせた指定は実際に引いてみて、0 件なら候補を添えて返す(`similar_tags`)。
    """
    catalog = ", ".join(
        f"{name}({src.kind})" for name, src in sorted(sources.items())
    ) or "(まだ 1 つも焼かれていません)"
    parts = [f"集めたいもの: {want.strip()}", f"\n引けるソース: {catalog}"]
    if current:
        parts.append("\nいまの指定:\n" + json.dumps(current, ensure_ascii=False, indent=2))
    parts.append("\nこれを踏まえた抽出の指定を書いて。")
    return [
        {"role": "system", "content": SPEC_GUIDE},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_draft(content: str) -> dict:
    """AI の答えから指定を取り出す。前置きやコードブロックが混ざっても拾う。

    **「機械では引けない」も答えのうち**(`{"extract": null, "reason": …}`)。
    引けないものを無理に指定へ落とすと、当たらないタグで静かな 0 件になる。
    """
    stripped = re.sub(r"```(?:json)?", "", content or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(502, {"error": "AI が抽出の指定を返しませんでした"})
    try:
        value = json.loads(stripped[start : end + 1])
    except ValueError as e:
        raise HTTPException(502, {
            "error": "AI の答えを JSON として読めませんでした",
            "reason": type(e).__name__,
        }) from None
    if not isinstance(value, dict):
        raise HTTPException(502, {"error": "AI の答えがオブジェクトではありません"})
    return value


def similar_tags(spec: dict, sources: dict, limit: int = 15) -> list[dict]:
    """書かれたタグに似た、**実在するタグ**を文書数つきで返す。

    **書いた本人には確かめようがない** —— タグは完全一致でしか引けないので、
    それらしい名前を書いた瞬間に静かな 0 件になる。広い語も同じで、実在はするが
    数件しか付いておらず、欲しいものは時代や地域で絞った名前の側にある。
    実在する名前を数と一緒に返して、選び直せるようにする。
    """
    from app import db

    src = sources.get(spec["source"])
    if src is None:
        return []
    # 書かれたタグを部分一致で探す。長い語ほど当たらないので、短くしながら試す
    wanted = spec["tag"].split(",")[0].strip()
    for length in range(len(wanted), 1, -1):
        rows = db.query(
            src.path,
            "SELECT tag, docs FROM tag_counts WHERE tag LIKE ? ORDER BY docs DESC LIMIT ?",
            (f"%{wanted[:length]}%", limit),
        )
        if rows:
            return [{"tag": row[0], "docs": row[1]} for row in rows]
    return []


def build_retry_messages(want: str, spec: dict, total: int, candidates: list[dict]) -> list[dict]:
    """取れた数が足りなかったときに、**実在するタグを見せて選び直させる**本文。

    1 度だけ投げ直す。タグ名は手元にしか無く、書く側は当てるしかない ——
    当てさせるより、実在するものを見せたほうが早いし確かめられる。
    """
    listed = "\n".join(f"- {c['tag']}({c['docs']} 件)" for c in candidates)
    return [
        {"role": "system", "content": SPEC_GUIDE},
        {
            "role": "user",
            "content": (
                f"集めたいもの: {want.strip()}\n\n"
                "さきほどの指定:\n"
                + json.dumps(to_json(spec), ensure_ascii=False, indent=2)
                + f"\n\nこの指定では {total} 件しか取れませんでした"
                f"(頼まれた件数は {spec['limit']} 件)。\n"
                "実在するタグは次のとおりです(文書数つき)。この中から選び直してください。\n"
                "1 つで足りなければ、カンマ区切りで複数書けます(そのどれかを持つものが取れます)。\n"
                f"{listed}\n\n"
                "**頼まれた範囲から外れるタグは足さないでください。**"
                "件数を満たすことより、頼まれたものだけが入っていることのほうが大事です"
                "(足りなければ足りないままで構いません)。\n"
                "選び直した指定を JSON で返してください。"
            ),
        },
    ]
