"""区画 —— 定期の巡回が「どこを見るか」を割り出す。

## 何のためか

無人で回る収集に「全部を見終わった」と言わせるには、**回る先を数え上げられる形**に
しておく必要がある。1 本のカーソルは「次はどこ」しか表せないので、取りこぼしがどこかも、
一周したのかも分からない。

**割るのは対象としている空間であって、集まったものではない。** ここが要。集まった点だけ
から区画を作ると、**まだ 1 件も集めていない範囲には区画が生まれず、永遠に空のまま**になる。
だから区画の母集団は、そこに何があるかを知っている**別のソース**から取れるようにしてある
(`source`)—— 全国の飲食店を集めるなら、こちらが 1 件も持っていなくても
`osm_japan` は北海道にも店があることを知っている。

## 密度で割る

面積で等分すると使いものにならない。都市部は狭い範囲に大量の対象があり、地方は広い範囲に
少ししかない —— 同じ面積の区画を配ると、片方は 1 回で見切れず、もう片方は空振りになる。
そこで**点の数が `target` を切るまで再帰的に二分する**(k-d tree と同じ割り方)。
密なところは細かく、疎なところは粗く、かつ**全域に区画がある**状態になる。

## Chiezo は何を割っているかを知らない

`app/extract.py` と同じ線。「どのソースの・どの地物を・何件で割るか」は依頼した側が
書いた指定として渡ってくる。こちらは矩形とタグを機械的に配るだけで、その区画に
何を集めるべきかはプロンプトが持つ。

## 区画がもう一つ効くところ

巡回の都合だけではない。整理(`refine`)は今ある内容をプロンプトへ差し込むが、
全体を入れようとすると入り切らず、切ったぶんは「今回の対象外」になる
(`app/collect.py` の `MAX_MATERIAL_CHARS`)。**区画で切れば、その区画の中身は全部
見せられる** —— 漏れているものを足させるには、まず全部見えている必要がある。
"""
from __future__ import annotations

import bisect
import itertools
import logging
import math
import re

from fastapi import HTTPException

log = logging.getLogger("chiezo.app")

# 割り方。**対象の並び方で選ぶ** —— 地理的に散らばっているなら矩形、
# 既にカテゴリで分かれているならタグ、どちらでもなければ見出しの順。
BY_GEO = "geo"
BY_TAG = "tag"
BY_TITLE = "title"
# 分類のタグ × 数のタグ。**帯で割る** —— 同じ括りの中を、数の軸に沿って
# 人数が揃うところで区切る。密なところは幅が狭く、疎なところは広くなる。
#
# **数の軸の最小単位より細かくは割らない。** 「フランスの 1660 年生まれ」を 2 つに
# 分けると、その区画に「この範囲の全員」が並ばなくなり、**漏れを探す問いが
# 成り立たなくなる**(区画に居ない人を漏れとして挙げてしまう)。
BY_BAND = "band"
KINDS = (BY_GEO, BY_TAG, BY_TITLE, BY_BAND)

# 帯の鍵の区切り。分類の値に出てこない字を使う
BAND_SEP = "|"
# 数の軸を持たないものの置き場。**そこでは漏れ探しが成り立たない**(「値が
# 分からないものの集合」に漏れという概念が無い)ので、見出し順で割ってよい
BAND_UNKNOWN = "不明"
# 分類のタグを持たないものの置き場の名前(指定で変えられる)
DEFAULT_OTHER = "その他"

# 1 区画あたりの目安。**厳密な上限ではない** —— 二分では割り切れないので、
# 実際の区画はこれの半分から等倍のあいだに散らばる。
DEFAULT_TARGET = 200
MIN_TARGET = 10
MAX_TARGET = 5_000

# 区画の数の歯止め。台帳は収集 1 件ぶんの JSON に入るので、際限なく増やせない
# (実測: 9,156 区画で 1.4 MB。巡回のたびに読み書きする)。
#
# **当たったら断る。黙って減らさない。** 減らすと、そのぶんの母集団がどの区画にも
# 入らず、どの巡回にも回ってこない —— 本番でこれが起きた: 68 万件を目安 150 で
# 割ろうとして天井(当時 2,000)に当たり、1 区画 200 未満で打ち切られて、
# **残る 40 万件ぶんの範囲が台帳から丸ごと消えた**。
# 目安を勝手に上げるのも同じ筋で駄目で、頼んだ細かさと違うもので回り続ける。
#
# **20,000 は「普通の使い方では当たらない」ところ。** 目安 150 なら 300 万件、
# 目安 1,000 なら 2,000 万件まで割れる。当たったら、目安を上げるか
# `feature` / `tag` / `bbox` で母集団を絞る —— どちらも人が決めること。
MAX_PARTITIONS = 20_000

# 端数を前の帯へ入れてよい上限(`target` の何倍まで)。**ちょうどで切らない**ための遊び。
# 切りのいいところで閉じると、その次の 1 人が 1 人だけの帯になる(「アイルランド
# 1928-1928 に 1 人」)。区画は「この範囲の全員」を並べて漏れを問う単位なので、
# 1 人の区画に問う意味はほとんど無く、区画の数と 1 回ぶんの依頼だけが増える
BAND_SLACK = 1.2

# 二分の深さ。同じ座標に固まった点は割り切れないので、止まらなくならないための保険。
MAX_DEPTH = 24

# 母集団として読む点の上限。1 回きりの割り出しなので大きめでよいが、
# 指定を間違えて全国の全地物を読むと配信機のメモリに乗らない。
MAX_POINTS = 500_000

# 区画の名前に添える「このあたり」の数。**名前ではなく手がかり** ——
# 矩形だけ渡されても、AI はそこがどこか分からない。
NEARBY_SAMPLES = 5

# 見出しで割るときの鍵の長さ。長い見出しをそのまま鍵にすると台帳が太る
MAX_TITLE_KEY_CHARS = 40

# 隣とまとめてよい大きさ。`target` に届かないどころか、この割合にも満たないとき
# だけ 1 つにする。**割り直しの引き金は「育った」と「空になった」しかない**ので、
# 中身が別の区画へ移って痩せた帯も、母集団がそもそも小さい分類も、小さいまま
# 残り続ける(本番の台帳では 407 区画のうち 18 が 1 人だった)。
#
# **`target` 丸ごとを閾値にしない。** ぎりぎり届かないものまでまとめると、
# まとめた先が `target` を大きく超えて、割り直しと押し合いになる。
MERGE_RATIO = 0.8

# **範囲で区切っていない分類**(数の軸で 1 つも割れていない = その分類ぜんぶで
# 1 帯)のうち、これ以下のものは**他の分類と寄せ集めて 1 区画にする**。
# 隣とまとめる道(`_joined`)は分類をまたげない —— 1 人しかいない国はその 1 人で
# 1 区画のまま残り、**その 1 人のために 1 回ぶんの枠を使う**(本番の台帳では
# 407 区画のうち 18 が 1 人だった)。寄せ集めた区画は**値を並べて持つ**ので、
# どの分類を引き受けているかは鍵から読める(「漏れを問う」には、どこまでが
# その区画の受け持ちなのかが分かっている必要がある)。
TINY_BAND_DOCS = 5

# 1 つの鍵に並べてよい分類の数。**鍵は台帳にも依頼文にも出る** —— 際限なく
# 並べると台帳が太り、「この範囲の全員を見て漏れを挙げる」も答えられる問いでは
# なくなる。数の上限(`target` × `MERGE_RATIO`)より先にこちらが当たることがある。
MAX_BAND_VALUES = 20

# 端の開いた帯を数として扱うときの代わり。**値の軸には上限も下限も無い** ——
# 端の帯は割ったときの値より外も引き受ける(`_band_of`)ので、比較では ±∞ になる。
BAND_LOW = float("-inf")
BAND_HIGH = float("inf")


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": f"区画の指定が読めません: {message}"})


def normalize(raw) -> dict | None:
    """指定を検証して、欠けている鍵を埋めた形にする。無ければ None。

    **読めない指定は黙って無視せず断る**(`app/extract.py` と同じ判断)。
    無視すると、区画を作ったつもりの収集が区画なしで回り続ける。
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("オブジェクトで書いてください")
    by = str(raw.get("by") or "").strip()
    if by not in KINDS:
        raise _bad(f"by は {' / '.join(KINDS)} のどれか(いまは {by!r})")
    try:
        target = int(raw.get("target") or DEFAULT_TARGET)
    except (TypeError, ValueError):
        raise _bad("target は数で書いてください") from None
    if not MIN_TARGET <= target <= MAX_TARGET:
        raise _bad(f"target は {MIN_TARGET}〜{MAX_TARGET} のあいだ(いまは {target})")
    spec = {
        "by": by,
        "target": target,
        # 母集団のソース。**書かなければ収集自身を見る** —— その場合、
        # まだ集めていない範囲には区画ができない(この層の存在理由そのものなので、
        # 使う側が分かって選ぶところ)
        "source": str(raw.get("source") or "").strip() or None,
        "feature": str(raw.get("feature") or "").strip() or None,
        "tag": str(raw.get("tag") or "").strip() or None,
        "bbox": _bbox(raw.get("bbox")),
        "prefix": str(raw.get("prefix") or "").strip() or None,
        # 帯で割るときの数の軸(タグの頭)と、分類を持たないものの置き場の名前
        "value": str(raw.get("value") or "").strip() or None,
        "other": str(raw.get("other") or "").strip() or DEFAULT_OTHER,
        # 巡回をどこから広げるか(矩形のときだけ)。書かなければ台帳の並び順
        "origin": _origin(raw.get("origin")),
    }
    if spec["origin"] and by != BY_GEO:
        raise _bad("origin は by=geo のときだけ使えます(距離で並べるので座標が要ります)")
    if by == BY_TAG and not spec["prefix"]:
        raise _bad("by=tag には prefix が要ります(例: 「地域:」)")
    if by == BY_BAND and not (spec["prefix"] and spec["value"]):
        raise _bad(
            "by=band には prefix(分類のタグの頭。例: 「地域:」)と"
            " value(数のタグの頭。例: 「年代:」)が要ります"
        )
    return spec


def _bbox(raw) -> list[float] | None:
    """対象としている矩形。**書かなければ母集団の外接矩形**。

    書く意味があるのは「まだ何も無い範囲まで対象に含めたい」とき —— 点から作ると、
    点の無いところは矩形の外になる。
    """
    if raw in (None, "", []):
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise _bad("bbox は [南緯, 西経, 北緯, 東経] の 4 つ")
    try:
        lat0, lon0, lat1, lon1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        raise _bad("bbox は数で書いてください") from None
    if not (lat0 < lat1 and lon0 < lon1):
        raise _bad("bbox は [南緯, 西経, 北緯, 東経] の順(南 < 北・西 < 東)")
    return [lat0, lon0, lat1, lon1]


def _origin(raw) -> list[float] | None:
    """巡回を広げ始める 1 点。**書かなければ台帳の並び順のまま**。

    書く意味があるのは「端から順ではなく、主要なところから見てほしい」とき ——
    区画は鍵の文字列順に配られるので、日本の地図なら南西の隅(八重山)から
    北上する。**一周に何十日もかかる台帳では、どこから始めるかが効く**
    (本番の食事処は 10,457 区画・1 回 5 区画・1 時間おきで、一周に 87 日)。
    """
    if raw in (None, "", []):
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise _bad("origin は [緯度, 経度] の 2 つ(例: 東京駅なら [35.681, 139.767])")
    try:
        lat, lon = (float(v) for v in raw)
    except (TypeError, ValueError):
        raise _bad("origin は数で書いてください") from None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise _bad(f"origin が地球の外にあります(いまは {lat}, {lon})")
    return [lat, lon]


def to_json(spec: dict | None) -> dict | None:
    """定義のメモへ書ける形。**空の鍵は落とす**(読むときに邪魔なだけ)。

    **既定のままの鍵も落とす** —— 書かなかったものが書いたことになって残ると、
    指定を見ただけでは「決めたのか任せたのか」が読めない。
    """
    if not spec:
        return None
    out = {k: v for k, v in spec.items() if v not in (None, "")}
    if out.get("other") == DEFAULT_OTHER:
        out.pop("other")
    return out


# ---- 割る ---------------------------------------------------------------------


def build(spec: dict, sources: dict, own: dict[str, dict] | None = None) -> list[dict]:
    """区画の一覧を作る。1 件は `{"key", "count"}`。

    `own` は収集が既に持っている文書(見出し → 文書)。母集団のソースを
    書いていないときの割り先になる。
    """
    if spec["by"] == BY_GEO:
        return _geo(spec, _points(spec, sources, own or {}))
    if spec["by"] == BY_TAG:
        return _tags(spec, sources, own or {})
    if spec["by"] == BY_BAND:
        return _bands(spec, own or {})
    return _titles(spec, sources, own or {})


def _source_of(spec: dict, sources: dict):
    """母集団のソース。書いていなければ None(収集自身を見る)。"""
    name = spec["source"]
    if not name:
        return None
    src = sources.get(name)
    if src is None:
        raise HTTPException(404, {
            "error": f"区画を割れません: ソース「{name}」がありません",
            "hint": "まだ焼いていないか、名前が違う(/v1/sources で確かめられる)",
        })
    return src


def _population_filter(spec: dict) -> tuple[str, list]:
    """母集団の絞り込み(地物とタグ)。SQL の断片と引数。"""
    where, args = "", []
    if spec["feature"]:
        where += " AND d.feature = ?"
        args.append(spec["feature"])
    if spec["tag"]:
        where += " AND d.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        args.append(spec["tag"])
    return where, args


def _points(spec: dict, sources: dict, own: dict[str, dict]) -> list[tuple[float, float]]:
    """割る元になる座標。

    **母集団のソースがあるときは `doc_coords` を引く**(schema_version 4 で入った
    座標の索引。緯度帯の走査も経度の判定も索引の中で完結する)。
    """
    from app import db

    src = _source_of(spec, sources)
    if src is None:
        return _own_points(own)
    where, args = _population_filter(spec)
    sql = (
        "SELECT c.lat, c.lon FROM doc_coords c JOIN docs d ON d.doc_id = c.doc_id"
        f" WHERE 1=1{where}"
    )
    if box := spec["bbox"]:
        sql += " AND c.lat BETWEEN ? AND ? AND c.lon BETWEEN ? AND ?"
        args += [box[0], box[2], box[1], box[3]]
    sql += " LIMIT ?"
    args.append(MAX_POINTS + 1)
    rows = db.query(src.path, sql, tuple(args))
    if len(rows) > MAX_POINTS:
        raise HTTPException(409, {
            "error": f"母集団が {MAX_POINTS:,} 件を超えています",
            "hint": "feature や tag で絞るか、bbox で範囲を区切ってください",
        })
    return [(float(r["lat"]), float(r["lon"])) for r in rows]


def _own_points(own: dict[str, dict]) -> list[tuple[float, float]]:
    """収集が既に持っている文書の座標。**座標を持たないものは数えない**。"""
    points = []
    for doc in own.values():
        lat, lon = coords_of(doc)
        if lat is not None:
            points.append((lat, lon))
    return points


def coords_of(doc: dict) -> tuple[float | None, float | None]:
    """文書の座標。`extra` に入っていなければ (None, None)。"""
    extra = doc.get("extra")
    if isinstance(extra, str):
        import json

        try:
            extra = json.loads(extra)
        except ValueError:
            return None, None
    if not isinstance(extra, dict):
        return None, None
    try:
        return float(extra["lat"]), float(extra["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None


def _geo(spec: dict, points: list[tuple[float, float]]) -> list[dict]:
    """矩形を、点の数が `target` を切るまで再帰的に二分する。

    **空の区画も leaf として残る。** 親の矩形をきっかり 2 つに割るので、
    点の無いところも必ずどれかの区画に入る —— そこへ「足すべきものが無いか」を
    調べさせるのが、この層の眼目。

    **母集団が大きすぎるときは断る**(`_must_fit`)。黙って減らすと、減らした先の
    範囲がどの区画にも入らなくなる —— そこはどの巡回にも回ってこないので、
    入っているものは誰にも見られない。
    """
    box = spec["bbox"] or _extent(points)
    if box is None:
        return []
    _must_fit(spec["target"], len(points))
    out: list[dict] = []
    _split(tuple(box), points, spec["target"], out, 0)
    return out


def _must_fit(target: int, count: int) -> None:
    """区画の数が天井に収まるか。**収まらなければ断る**。

    **黙って減らさない。** 数で打ち切ると、打ち切った先の母集団が台帳から消え、
    どの巡回にも回ってこない —— 誰にも見られないまま溜まり続ける。
    **目安を勝手に上げるのも駄目**で、頼んだ細かさと違うもので回り続ける。
    どちらを選ぶかは人が決めること(目安を上げる / 母集団を絞る)。

    **二分は割り切れない。** できる区画は目安の半分から等倍のあいだに散らばるので、
    「ちょうど収まる目安」では倍近くまで増えうる —— 余裕を見て 2 倍で数える。
    """
    room = MAX_PARTITIONS // 2
    if count <= target * room:
        return
    raise HTTPException(409, {
        "error": f"区画を割れません: 母集団 {count:,} 件を目安 {target:,} で割ると"
                 f"、区画の天井({MAX_PARTITIONS:,})を超えます",
        "hint": f"1 区画の目安(target)を {math.ceil(count / room):,} 以上にするか、"
                "feature / tag / bbox で母集団を絞ってください。"
                "**黙って粗く割ることはしません** —— 割り切れなかったぶんは"
                "どの区画にも入らず、どの巡回にも回ってこないため",
    })


def _extent(points) -> list[float] | None:
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    # 幅が 0 だと割れないので、ごく小さな余白を足す(1 点しか無いときに起きる)
    pad = 1e-6
    return [min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad]


def _split(box, points, target: int, out: list[dict], depth: int) -> None:
    # **天井に当たっても、その矩形は leaf として残す。** 黙って帰ると、この中に
    # いる点がどの区画にも入らない —— 台帳から消えた範囲は、どの巡回にも
    # 回ってこないので、入っているものは誰にも見られないまま溜まり続ける。
    # 大きい区画が 1 つできるほうが、範囲が欠けるよりましで、しかも画面で読める
    if len(out) >= MAX_PARTITIONS or len(points) <= target or depth >= MAX_DEPTH:
        out.append({"key": geo_key(box), "count": len(points)})
        return
    lat0, lon0, lat1, lon1 = box
    # **度のまま長辺を選ばない。** 経度 1 度は緯度が上がるほど短いので、
    # そのまま比べると高緯度ばかり東西に割れて、縦長の区画が並ぶ
    lat_span = lat1 - lat0
    lon_span = (lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2))
    order = (0, 1) if lat_span >= lon_span else (1, 0)
    for axis in order:
        lo, hi = (lat0, lat1) if axis == 0 else (lon0, lon1)
        cut = _cut(points, axis, lo, hi)
        if cut is None:
            continue
        left = [p for p in points if p[axis] < cut]
        right = [p for p in points if p[axis] >= cut]
        box_l = (lat0, lon0, cut, lon1) if axis == 0 else (lat0, lon0, lat1, cut)
        box_r = (cut, lon0, lat1, lon1) if axis == 0 else (lat0, cut, lat1, lon1)
        _split(box_l, left, target, out, depth + 1)
        _split(box_r, right, target, out, depth + 1)
        return
    # どちらの軸でも割れない(同じ座標に固まっている)。数が多くても leaf にする
    out.append({"key": geo_key(box), "count": len(points)})


def _cut(points, axis: int, lo: float, hi: float) -> float | None:
    """二分する位置。矩形の内側に取れなければ None。"""
    values = sorted(p[axis] for p in points)
    mid = values[len(values) // 2]
    return mid if lo < mid < hi else None


def geo_key(box) -> str:
    """矩形の鍵。小数 4 桁(約 11 m)まで —— これ以上細かい区画は作らない。"""
    lat0, lon0, lat1, lon1 = box
    return f"{lat0:.4f},{lon0:.4f}/{lat1:.4f},{lon1:.4f}"


def parse_geo_key(key: str) -> tuple[float, float, float, float] | None:
    try:
        head, tail = key.split("/")
        lat0, lon0 = (float(v) for v in head.split(","))
        lat1, lon1 = (float(v) for v in tail.split(","))
    except ValueError:
        return None
    return lat0, lon0, lat1, lon1


def _tags(spec: dict, sources: dict, own: dict[str, dict]) -> list[dict]:
    """接頭辞の付いたタグを、そのまま 1 つずつ区画にする。

    **束ねない。** タグはもともと対象が付けた区切りなので、こちらで混ぜると
    「このタグを見て」が言えなくなる(`target` はここでは効かない)。

    **多すぎたら断る。** 数で切り落としていた頃は、こぼれたタグがどの巡回にも
    回ってこなかった —— しかも多い順に採るので、**消えるのはいつも細かい分類のほう**
    (そこにこそ漏れが溜まる)。
    """
    from app import db

    prefix = spec["prefix"]
    src = _source_of(spec, sources)
    if src is not None:
        rows = db.query(
            src.path,
            "SELECT tag, docs FROM tag_counts WHERE tag LIKE ? ORDER BY docs DESC, tag LIMIT ?",
            (prefix.replace("%", "").replace("_", "") + "%", MAX_PARTITIONS + 1),
        )
        _tags_must_fit(prefix, len(rows))
        return [{"key": r["tag"], "count": int(r["docs"])} for r in rows]
    counts: dict[str, int] = {}
    for doc in own.values():
        for tag in doc.get("tags") or []:
            if str(tag).startswith(prefix):
                counts[str(tag)] = counts.get(str(tag), 0) + 1
    _tags_must_fit(prefix, len(counts))
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"key": k, "count": n} for k, n in ordered]


def _tags_must_fit(prefix: str, found: int) -> None:
    if found <= MAX_PARTITIONS:
        return
    raise HTTPException(409, {
        "error": f"区画を割れません: 「{prefix}」で始まるタグが"
                 f" {MAX_PARTITIONS:,} を超えています",
        "hint": "接頭辞をもっと狭く取ってください。**黙って多い順に切ることはしません**"
                " —— こぼれたタグはどの巡回にも回ってこないうえ、消えるのはいつも"
                "細かい分類のほう(そこにこそ漏れが溜まる)",
    })


def _titles(spec: dict, sources: dict, own: dict[str, dict]) -> list[dict]:
    """見出しの順に `target` 件ずつ区切る。鍵は「どこからどこまで」。

    **鍵に境目の見出しを使う**ので、あとで件数が増えても区画の意味は変わらない
    (番号で区切ると、1 件増えるたびに全部の区画が 1 つずつずれる)。
    """
    from app import db

    src = _source_of(spec, sources)
    if src is not None:
        where, args = _population_filter(spec)
        titles = [
            r["title"]
            for r in db.query(
                src.path,
                f"SELECT d.title FROM docs d WHERE 1=1{where} ORDER BY d.title LIMIT ?",
                (*args, MAX_POINTS),
            )
        ]
    else:
        titles = sorted(own)
    # **入り切らないなら断る**(`_must_fit`)。件数で打ち切ると、後ろの見出しが
    # どの区画にも入らず、どの巡回にも回ってこない
    _must_fit(spec["target"], len(titles))
    out = []
    for i in range(0, len(titles), spec["target"]):
        chunk = titles[i : i + spec["target"]]
        out.append({"key": title_key(chunk[0], chunk[-1]), "count": len(chunk)})
    return out


def _bands(spec: dict, own: dict[str, dict]) -> list[dict]:
    """分類ごとに、数の軸を人数で区切る。

    **人の少ない分類ほど幅が広くなる。** 連続する値を足していって `target` に
    届いたところで区切るだけなので、密なところは自然に狭くなる。

    **最小単位(値ひとつ)より細かくは割らない。** 1 つの値に `target` を超える数が
    集まっていても、そのまま 1 区画にする —— 分けると「この範囲の全員」が並ばなくなり、
    漏れを探す問いが成り立たない。

    **値を持たないものは分類ごとの置き場へ**(見出し順で `target` ごとに区切る)。
    そこは漏れ探しの対象ではないので、見出しで割ってよい。
    """
    groups: dict[str, dict[int, list[str]]] = {}
    unknown: dict[str, list[str]] = {}
    for title, doc in own.items():
        tags = doc.get("tags") or []
        name = tag_value(tags, spec["prefix"]) or spec["other"]
        if (number := _number_in(tag_value(tags, spec["value"]))) is None:
            unknown.setdefault(name, []).append(title)
        else:
            groups.setdefault(name, {}).setdefault(number, []).append(title)
    out: list[dict] = []
    for name in sorted(groups) + [n for n in sorted(unknown) if n not in groups]:
        out += _bands_of(spec, name, groups.get(name, {}))
        out += _unknown_bands(spec, name, unknown.get(name, []))
        if len(out) >= MAX_PARTITIONS:
            break
    return out[:MAX_PARTITIONS]


def _band_cap(spec: dict) -> int:
    """1 つの帯に入れてよい上限。`target` に遊び(`BAND_SLACK`)を足したもの。"""
    return max(spec["target"], round(spec["target"] * BAND_SLACK))


def _bands_of(spec: dict, name: str, by_number: dict[int, list[str]]) -> list[dict]:
    """1 つの分類ぶんの帯。連続する値を `target` に届くまで足していく。

    **端数は前の帯へ入れる**(上限に収まるときだけ)。閉じた帯は必ず `target` 以上
    なので、小さい帯になりうるのは最後の 1 つだけ —— そこを前へ寄せれば、
    1 人だけの区画は出なくなる。

    **隣との間に隙間を空けない**(`_close_gaps`)。帯は「値の詰まっているところ」で
    切るので、そのまま書くと `1689-1816` と `1818-1864` のように 1 年空く ——
    **その年に入るものは、どの区画にも入らない**うえ、**「この範囲に足すべきものが
    無いか」を問う回にも入らない**(隙間の年は誰にも聞かれない)。漏れを探すのが
    区画の眼目なので、境目は必ずどちらかのものにする。

    **端は開いたまま書く**(`-1869` / `1840-`。1 本しか無ければ `-`)。端の帯は
    割ったときの値より外も引き受けるので、閉じて書くと鍵と受け持ちがずれる。
    """
    spans: list[list] = []
    start: int | None = None
    count = 0
    for number in sorted(by_number):
        start = number if start is None else start
        count += len(by_number[number])
        if count >= spec["target"]:
            spans.append([start, number, count])
            start, count = None, 0
    if start is not None:
        end = max(by_number)
        if spans and spans[-1][2] + count <= _band_cap(spec):
            spans[-1][1], spans[-1][2] = end, spans[-1][2] + count
        else:
            spans.append([start, end, count])
    _close_gaps(spans)
    if spans:
        # **隙間を埋めてから開く**(順番を逆にすると、開いた端を次の帯の手前まで
        # 縮めることになる)
        spans[0][0] = None
        spans[-1][1] = None
    return [{"key": band_key(name, a, b), "count": n} for a, b, n in spans]


def _close_gaps(spans: list[list]) -> None:
    """帯の終わりを、次の帯の始まりの手前まで伸ばす(隙間を残さない)。

    伸ばすのは終わりだけ。**始まりを動かすと、既に入っているものが隣へ移る** ——
    どちらへ寄せるかを決められるのは境目の側だけで、そこには誰も居ない。
    """
    for left, right in itertools.pairwise(spans):
        left[1] = right[0] - 1


def _unknown_bands(spec: dict, name: str, titles: list[str]) -> list[dict]:
    """値を持たないものの置き場。**見出し順で区切る**(漏れ探しの対象ではないため)。

    ここも端数は前へ寄せる(`_bands_of` と同じ理由)。
    """
    ordered = sorted(titles)
    chunks: list[list[str]] = []
    for i in range(0, len(ordered), spec["target"]):
        chunks.append(ordered[i : i + spec["target"]])
    if len(chunks) > 1 and len(chunks[-1]) + len(chunks[-2]) <= _band_cap(spec):
        # **先に取り出してから足す**(足しながら pop すると、番号が 1 つずれる)
        tail = chunks.pop()
        chunks[-1] += tail
    return [
        {
            "key": band_key(name, BAND_UNKNOWN, title_key(chunk[0], chunk[-1])),
            "count": len(chunk),
        }
        for chunk in chunks
    ]


def band_key(name, start, end) -> str:
    """区画の鍵。

    - `フランス|1840-1869` …… 上下とも閉じた帯
    - `フランス|-1869` / `フランス|1840-` …… **端の帯は開いて書く**
    - `フランス|-` …… その分類ぜんぶで 1 帯(範囲では区切っていない)
    - `アルメニア|エストニア|-` …… 小さい分類を寄せ集めた区画(`_pooled`)
    - `フランス|不明|あ〜す` …… 値の分からないものの置き場

    **端を開いて書くのは、鍵と受け持ちを一致させるため。** 端の帯は割ったときの
    値より外も引き受ける(`_band_of`)—— 値の軸には上限も下限も無いので、
    いちばん古い帯はそれより古い年も、いちばん新しい帯はその先も取る。
    そこを `1600-1641` と閉じて書くと、**引き受けているのに誰も探しに行かない
    範囲**ができる(鍵を読んだ人も AI も、その外は別の区画のものだと思う)。

    `name` は文字列でも、分類の並びでもよい。
    """
    head = name if isinstance(name, str) else BAND_SEP.join(name)
    if start == BAND_UNKNOWN:
        return f"{head}{BAND_SEP}{BAND_UNKNOWN}{BAND_SEP}{end}"
    lo = "" if start is None else start
    hi = "" if end is None else end
    return f"{head}{BAND_SEP}{lo}-{hi}"


# 帯の範囲を表す末尾。端は空(`-1869` / `1840-` / `-`)。
_BAND_SPAN = re.compile(r"^(\d*)-(\d*)$")


def parse_band_key(key: str) -> tuple[list[str], int | None, int | None, str | None] | None:
    """`(分類の並び, 始まり, 終わり, 見出しの範囲)`。読めなければ None。

    **始まり / 終わりの None は「開いている」**(その先も引き受ける)。
    値の分からないものの置き場だけは両方 None で、見出しの範囲のほうが入る。

    **最後の一片が範囲かどうかで読み分ける。** 分類の値に `不明` が使われていても
    取り違えないため —— 範囲は必ず `数字-数字`(端は空)の形で、見出しの範囲は
    `title_key` が `〜` を挟むので、この形にはならない。
    """
    parts = key.split(BAND_SEP)
    if len(parts) < 2:
        return None
    if span := _BAND_SPAN.match(parts[-1]):
        names = parts[:-1]
        lo, hi = span.group(1), span.group(2)
        if not all(names):
            return None
        return names, int(lo) if lo else None, int(hi) if hi else None, None
    if len(parts) >= 3 and parts[-2] == BAND_UNKNOWN and all(parts[:-2]):
        return parts[:-2], None, None, parts[-1]
    return None


def band_span(parsed) -> tuple[float, float]:
    """帯の受け持ちを数の範囲で。**開いている端は ±∞**。"""
    lo, hi = parsed[1], parsed[2]
    return (BAND_LOW if lo is None else lo, BAND_HIGH if hi is None else hi)


def _number_in(value: str | None) -> int | None:
    """タグの値から最初の数を読む(`1840-1926` → `1840`)。無ければ None。"""
    found = re.search(r"\d+", value or "")
    return int(found.group()) if found else None


def tag_value(tags, prefix: str) -> str | None:
    """`地域:フランス` → `フランス`。**最初の 1 つだけ** ——
    1 文書は必ず 1 区画に入れる(またがると「一周した」が数えられない)。

    **区切りの字は値から落とす**(`BAND_SEP`)。鍵は「分類を並べて、最後に範囲」
    という形なので、値の中に区切りが混ざると**分類が 2 つに割れて読まれる** ——
    `地域:A|B` の 3 人が入るはずの区画へ、`地域:A` の人が落ちる(実際にそうなった)。
    区切りは「値に出てこない字」を選んであるが、タグを書くのは Chiezo ではないので、
    出てきたときに黙って別のものになるより、ここで潰しておくほうがよい。

    **割るときも判ずるときもここを通る**ので、落とし方が同じなら食い違わない。
    """
    head = prefix if prefix.endswith(":") else prefix + ":"
    for tag in tags:
        text = str(tag)
        if text.startswith(head):
            value = text[len(head):].strip().replace(BAND_SEP, "/")
            return value or None
    return None


def title_key(first: str, last: str) -> str:
    return f"{first[:MAX_TITLE_KEY_CHARS]}〜{last[:MAX_TITLE_KEY_CHARS]}"


def parse_title_key(key: str) -> tuple[str, str] | None:
    first, _, last = key.partition("〜")
    return (first, last) if last else None


# ---- 台帳(定義のメモに入る)---------------------------------------------------


def refresh(built: list[dict], current: list[dict], spec: dict | None = None) -> list[dict]:
    """割り直した区画に、**前の巡回の記録を引き継ぐ**。

    引き継がないと、割り直すたびに全区画が「まだ見ていない」に戻り、一周が
    永遠に終わらない。

    **鍵が変わった区画も引き継ぐ**(`spec` を渡したとき)。割られた区画の子は、
    **親の記録をそのまま写す** —— 写さないと、区画が育つたびにそこだけ
    一周が巻き戻る。親は「その子を含んでいた区画」として探す。

    **新しい台帳が覆っていない区画は、落とさずに残す**(`_uncovered`)。
    """
    seen = {p["key"]: dict(p.get("visits") or {}) for p in current}
    out = [
        {
            "key": p["key"],
            "count": int(p.get("count") or 0),
            "visits": seen.get(p["key"]) or _inherited(spec, p["key"], current),
        }
        for p in built
    ]
    return out + _uncovered(spec, out, current)


def _uncovered(spec: dict | None, built: list[dict], current: list[dict]) -> list[dict]:
    """割り直しで落ちる区画のうち、**新しい台帳がどこも覆っていないもの**。

    母集団が痩せると、そこは組み立てられずに消える —— **中身が消えたものだけに
    なった区画がそれ**(区画は生きているものだけで割る)。消すと、そこは以後どの回にも
    回ってこない: **漏れを探す仕事はそこにしか無い**ので、「この範囲に足すべきものが
    無いか」を問う回ごと消えることになる。

    **覆われているものは落とす。** 吸収先があるなら回る先は残っており、残すと
    同じ範囲が二重に並ぶ。**残すのは行き場の無いぶんだけ**。

    **覆われているかは両向きに見る。** 割られた区画は、子のどれ 1 つを取っても
    親を覆わない —— 片向きだけで見ると、**割り直すたびに親が残って二重になる**。
    新しい区画が中に入っているなら、その範囲は新しい台帳が引き受けている。

    残したものは**後ろに置く**(`partition_of` は先に当たったほうを返す)。
    新しい区画が取らなかったものだけが、ここへ落ちる。
    """
    if spec is None or not current:
        return []
    keys = {p["key"] for p in built}
    return [
        {"key": old["key"], "count": 0, "visits": dict(old.get("visits") or {})}
        for old in current
        if old["key"] not in keys
        and not any(
            _covers(spec, new["key"], old["key"]) or _covers(spec, old["key"], new["key"])
            for new in built
        )
    ]


def _inherited(spec: dict | None, key: str, current: list[dict]) -> dict:
    """その区画を含んでいた区画の記録。無ければ空(新しく始まる)。"""
    if spec is None or not current:
        return {}
    for parent in current:
        if _covers(spec, parent["key"], key):
            return dict(parent.get("visits") or {})
    return {}


def _covers(spec: dict, parent: str, child: str) -> bool:
    """`parent` の範囲が `child` を含むか。**幅で表した区画だけ**(順序のある軸と矩形)。

    分類の名前とタグは幅ではないので、含むかどうかを言えない(鍵が同じかどうかだけ)。
    """
    if spec["by"] == BY_GEO:
        a, b = parse_geo_key(parent), parse_geo_key(child)
        if a is None or b is None:
            return False
        return a[0] <= b[0] and a[1] <= b[1] and b[2] <= a[2] and b[3] <= a[3]
    if spec["by"] == BY_BAND:
        a, b = parse_band_key(parent), parse_band_key(child)
        # **分類は「同じ」ではなく「含む」で見る。** 小さい分類を寄せ集めた区画
        # (`_pooled`)は複数の値を持つので、そこから 1 つが独り立ちしたとき、
        # 等しさで見ると親が見つからず一周が巻き戻る
        if a is None or b is None or not set(b[0]) <= set(a[0]):
            return False
        if a[3] is not None or b[3] is not None:
            # 値の分からない置き場どうしは、見出しの範囲で見る
            return bool(a[3] and b[3]) and _within(parse_title_key(a[3]), parse_title_key(b[3]))
        (lo_a, hi_a), (lo_b, hi_b) = band_span(a), band_span(b)
        return lo_a <= lo_b and hi_b <= hi_a
    if spec["by"] == BY_TITLE:
        return _within(parse_title_key(parent), parse_title_key(child))
    return False


def _within(outer, inner) -> bool:
    return bool(outer and inner and outer[0] <= inner[0] and inner[1] <= outer[1])


def normalize_ledger(raw) -> list[dict]:
    """定義のメモから読んだ台帳を均す(壊れた行は落とす)。

    **記録は巡回ごとに持つ**(`visits`)。2 種類の巡回 —— ざっと全体を拾うものと、
    少数をじっくり調べるもの —— は進み方が違うので、1 つの日付では表せない。
    ざっとが一周した区画を、じっくりはまだ見ていない、が普通に起きる。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:MAX_PARTITIONS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        raw_visits = item.get("visits")
        visits = (
            {str(k): str(v) for k, v in raw_visits.items() if v}
            if isinstance(raw_visits, dict) else {}
        )
        out.append({"key": key, "count": int(item.get("count") or 0), "visits": visits})
    return out


# **どの区画にも入らなかったぶんの置き場**(`counts_of` の中だけの鍵)。
# 台帳の鍵は必ず 1 文字以上あるので(`normalize_ledger`)、実在の鍵とぶつからない。
HOMELESS = ""


def counts_of(spec: dict, partitions: list[dict], docs: dict[str, dict]) -> dict[str, int]:
    """区画ごとの、いまの件数。**数える意味の無いときは空を返す**。

    - **母集団が外のソースなら数えない。** 区画の大きさはあちらが持っていて、
      こちらが何件集めようと点の数は変わらない
    - **母集団が読めていない回も数えない。** まだ 1 度も焼けていない回と、焼いた
      ものが読めなかった回はここでは区別が付かない —— 0 を答えにすると、
      読めなかっただけの回に「全区画が空」と映る

    **入らなかったぶんも数える**(`HOMELESS`)。**どの区画にも入らない文書は、
    どの回にも出てこない** —— `{current}` にも件数にも現れないので、巡回を何周
    しても AI の目に触れないまま残る。数えておけば `outgrown` が気づける。
    """
    if spec["source"] or not partitions or not docs:
        return {}
    counts = dict.fromkeys((p["key"] for p in partitions), 0)
    counts[HOMELESS] = 0
    # **索引は 1 回だけ組む**(`locator`)。1 件ごとに台帳を舐めていた頃は、
    # 文書 686,602 件 × 区画 10,457 個でここだけ 1 時間を超えていた
    find = locator(spec, partitions)
    for doc in docs.values():
        key = find(doc)
        counts[key if key in counts else HOMELESS] += 1
    return counts


def outgrown(spec: dict, counts: dict[str, int]) -> bool:
    """割り直したほうがよいか。**数えられていなければ動かさない**(`counts_of`)。

    自分自身を割っているときは、育つにつれて密なところが `target` を超えていくので、
    **超えた区画が出たら割り直す**。

    **空になった区画が出たときも割り直す。** 中身が別の区画へ移ることがある
    (年代の分からなかった人に年代が入ると、本来の帯へ移る)。空の区画は回ってきても
    渡すものが無く、AI に空の範囲を見せて 1 回ぶんの枠を捨てることになる。
    割り直せば、そこは隣に吸収される。

    **吸収先が無ければ消えない**(`_uncovered`)。新しい台帳がどこも覆っていない
    範囲は残す —— 消すと、そこへ「足すべきものが無いか」を問う回ごと無くなる。

    **どの区画にも入らない文書が出たときも割り直す**(`HOMELESS`。帯で割るときだけ)。
    割った時点でその分類の全員が値を持っていると「不明」の置き場は作られないので、
    **あとから値の無い 1 件が入ると行き場が無くなる** —— 例外にはならず、
    `{current}` にも件数にも出ないまま残るので、誰も気づけない。
    割り直せばその分類に置き場ができて収まる。

    **帯のときだけ**にしてあるのは、他の割り方では割り直しても行き場が
    できないから —— 座標を持たない文書は矩形に入らないし、指定したタグを持たない
    文書はタグの区画に入らない(どちらも仕様)。そこで真にすると、
    直りようのない 1 件のために毎回全件を割り直すことになる。

    **区画が天井に当たっているときも割り直さない。** 帯は天井で打ち切られる
    (`_bands`)ので、そこで溢れたぶんは何度割り直しても行き場ができない。
    """
    limit = spec["target"] * 2
    if (
        spec["by"] == BY_BAND
        and counts.get(HOMELESS)
        and len(counts) - 1 < MAX_PARTITIONS  # HOMELESS のぶんを引く
    ):
        return True
    return any(n > limit or n == 0 for key, n in counts.items() if key != HOMELESS)


def merged(spec: dict, partitions: list[dict]) -> list[dict]:
    """細かく切れすぎた区画を、**隣と 1 つにする**。

    まとめるのは 2 つとも満たすときだけ:

    - **周回の記録が同じ** —— 片方だけ見終えている区画をくっつけると、見ていない
      ぶんが「見終えた」に混ざる(逆に、見終えたぶんをもう一度回すことになる)。
      割り直した直後はどれも空なので、そこでは大きさだけで決まる
    - **合わせても `target` の `MERGE_RATIO` に満たない** —— くっつけた先が
      `target` を超えては、割り直しと押し合いになる

    **まとめられるのは「幅」で表した区画だけ。** 年代の帯、見出しの範囲、矩形は
    幅なので、隣り合うぶんを覆う 1 つの幅がある —— **鍵の読み方も、どの文書が
    どこへ入るかの規則も変わらない**。**幅で表していないものは対象外**:
    分類の名前(国名)とタグがそれで、2 つの名前を 1 つの名前では表せない。
    まとめるにはそこだけ別の鍵の形を作ることになり、そこまでして減らす区画ではない。

    年代の帯をまとめると**あいだの空きも埋まる**。帯は「値が詰まっているところ」で
    切るので `1689-1816` と `1818-1864` のように 1 年空くことがあり、そこに入る
    値を持った文書はどの区画にも入らない(`_band_of` が None を返す)。

    **分類をまたぐぶんは、そのあと `_pooled` が引き受ける。** 隣とつなぐ道では
    1 人しかいない分類が最後まで残るので、そこだけ別の鍵の形(値を並べて持つ)を作る。
    """
    if spec["by"] == BY_TAG:
        return partitions
    limit = spec["target"] * MERGE_RATIO
    out: list[dict] = []
    for p in partitions:
        one = {"key": p["key"], "count": int(p.get("count") or 0),
               "visits": dict(p.get("visits") or {})}
        joined = _joined(spec, out[-1], one, limit) if out else None
        if joined is None:
            out.append(one)
        else:
            out[-1] = joined
    # **隣とつないでから寄せ集める。** 2 本の帯が 1 本になって初めて
    # 「範囲で区切っていない分類」になるものがある
    return _pooled(spec, out, limit)


def _pooled(spec: dict, partitions: list[dict], limit: float) -> list[dict]:
    """**範囲で区切っていない小さい分類**を、値を並べた 1 区画に寄せ集める。

    隣とつなぐ道(`_joined`)は分類をまたげない —— 2 つの名前を 1 つの名前では
    表せないからで、まとめるにはそこだけ別の鍵の形が要る。それが
    `アルメニア|エストニア|-` で、**引き受けている分類を全部並べて持つ**。
    並べて持たないと `{partition}` に「どこまでがこの区画か」を書けず、
    **漏れを問う**という区画の眼目が成り立たない。

    寄せ集める相手は 3 つとも満たすものだけ:

    - **数の軸で 1 つも割れていない**(その分類ぜんぶで 1 帯)。割れている分類を
      混ぜると、「この範囲の全員」が並ばなくなる —— 別の帯にいる人を漏れとして挙げる
    - **`TINY_BAND_DOCS` 以下**。大きい分類は 1 つで 1 区画ぶんの仕事がある
    - **周回の記録が同じ**(`_joined` と同じ理由。見ていないぶんが
      「見終えた」に混ざる)。**記録ごとに分けてから寄せる** —— 隣とつなぐ道の
      ように並び順のまま見ると、見た分類と見ていない分類が交互に並んだ一周の
      途中では 1 つも寄せられない

    **数と値の数の両方で頭打ちにする。** 1 人の分類が 100 並ぶと、数では
    `target` に届かないのに鍵が読めない長さになる。

    **鍵を書き換えるのは、実際に 2 つ以上を寄せたときだけ。** 寄せる相手が
    いなかったものまで `-` に直すと、割り直していない台帳で端の書き方だけが
    まだらになる(次の割り直しでどのみち揃う)。

    **出来たものは後ろへ置く。** `_band_of` は分類の名前で引くので並びは効かないが、
    残りの区画の並びを動かさないほうが、画面で見たときに追いやすい。
    """
    if spec["by"] != BY_BAND:
        return partitions

    # 分類ごとの帯の数。2 本以上あるものは「範囲で区切っている」ので触らない
    bands: dict[str, int] = {}
    for p in partitions:
        parsed = parse_band_key(p["key"])
        if parsed is None or parsed[3] is not None:
            continue
        for name in parsed[0]:
            bands[name] = bands.get(name, 0) + 1

    def values_of(p: dict) -> list[str] | None:
        parsed = parse_band_key(p["key"])
        if parsed is None or parsed[3] is not None:
            return None
        if int(p.get("count") or 0) > TINY_BAND_DOCS:
            return None
        if any(bands.get(name) != 1 for name in parsed[0]):
            return None
        return parsed[0]

    rest: list[dict] = []
    buckets: dict[tuple, list[tuple[list[str], dict]]] = {}
    for p in partitions:
        names = values_of(p)
        if names is None:
            rest.append(p)
            continue
        visits = dict(p.get("visits") or {})
        buckets.setdefault(tuple(sorted(visits.items())), []).append((names, p))

    made: list[dict] = []
    for bucket in buckets.values():
        group: dict | None = None
        for names, p in bucket:
            count = int(p.get("count") or 0)
            if (
                group is not None
                and group["count"] + count < limit
                and len(group["names"]) + len(names) <= MAX_BAND_VALUES
            ):
                group["names"] += names
                group["count"] += count
                continue
            if group is not None:
                made.append(_pooled_row(group))
            group = {"names": list(names), "count": count,
                     "visits": dict(p.get("visits") or {}), "was": p}
        if group is not None:
            made.append(_pooled_row(group))
    return rest + made


def _pooled_row(group: dict) -> dict:
    """寄せ集めた 1 区画。**1 つしか集まらなかったら、鍵はそのまま**。"""
    if len(group["names"]) == 1:
        return group["was"]
    return {
        "key": band_key(group["names"], None, None),
        "count": group["count"],
        "visits": group["visits"],
    }


def _joined(spec: dict, left: dict, right: dict, limit: float) -> dict | None:
    """2 つを 1 つにできるなら、その区画。できなければ None。

    **出来た鍵が両方を含んでいることを確かめる。** まとめるのは隣どうしのつもりでも、
    台帳に並んでいる順が範囲の順とは限らない(**覆われていない区画は後ろへ足す**ので、
    そこだけ順が崩れる)。順が逆のまま帯を組むと `1900-1850` のような**中身を 1 つも
    拾えない鍵**になり、その範囲の文書はどの区画にも入らなくなる。
    """
    if left["visits"] != right["visits"]:
        return None
    count = left["count"] + right["count"]
    if count >= limit:
        return None
    key = _joined_key(spec, left["key"], right["key"])
    if key is None or not (
        _covers(spec, key, left["key"]) and _covers(spec, key, right["key"])
    ):
        return None
    return {"key": key, "count": count, "visits": right["visits"]}


def _joined_key(spec: dict, left: str, right: str) -> str | None:
    """2 つの範囲を覆う 1 つの鍵。**1 つの範囲にならないなら None**。"""
    if spec["by"] == BY_GEO:
        return _joined_box(parse_geo_key(left), parse_geo_key(right))
    if spec["by"] == BY_TITLE:
        a, b = parse_title_key(left), parse_title_key(right)
        return title_key(a[0], b[1]) if a and b else None
    a, b = parse_band_key(left), parse_band_key(right)
    if a is None or b is None or a[0] != b[0]:
        # 分類が違うものはここではまたがない(1 つの範囲では言えなくなる)。
        # またぐ道は `_pooled` が持っていて、あちらは値を並べた別の鍵の形を作る
        return None
    if a[3] is None and b[3] is None:
        return band_key(a[0], a[1], b[2])
    if a[3] is not None and b[3] is not None:
        # 値の分からないものの置き場どうしは、見出しの範囲でつなぐ
        lo, hi = parse_title_key(a[3]), parse_title_key(b[3])
        return band_key(a[0], BAND_UNKNOWN, title_key(lo[0], hi[1])) if lo and hi else None
    # 年代の帯と「不明」の置き場は、1 つの範囲にならない
    return None


def _joined_box(a, b) -> str | None:
    """2 つの矩形を覆う矩形。**辺がぴったり合うときだけ**(割った親へ戻るとき)。

    ずれたまま囲む矩形を作ると、**隣の区画へ食い込む** —— そこの文書が 2 つの
    区画に入ることになり、どちらで見せるかが並び順で決まってしまう。
    親を二分して作る割り方なので、ぴったり合う組は普通に隣り合う。
    """
    if a is None or b is None:
        return None
    if a[0] == b[0] and a[2] == b[2] and a[3] == b[1]:
        # 緯度がそろっていて、経度が接している
        return geo_key((a[0], a[1], a[2], b[3]))
    if a[1] == b[1] and a[3] == b[3] and a[2] == b[0]:
        # 経度がそろっていて、緯度が接している
        return geo_key((a[0], a[1], b[2], b[3]))
    return None


def counted(partitions: list[dict], counts: dict[str, int]) -> list[dict]:
    """いまの件数を台帳へ書き戻す。**割り直しはしない**。

    台帳の数は割ったときの写しで、以後は更新されない —— 中身が別の区画へ移っても
    次に割り直すまで古い数が出続ける。実際、一周目に配った 6 区画は全員が本来の
    帯へ移って空になったのに、画面には割ったときの 25〜27 が出たままだった。
    割り直すかどうかの判定でどのみち数えているので、同じ数を書き戻す。
    """
    if not counts:
        return partitions
    return [{**p, "count": counts.get(p["key"], 0)} for p in partitions]


def pick(
    partitions: list[dict], sweep_name: str, count: int = 1, spec: dict | None = None
) -> list[str]:
    """次に見る区画を、古い順に `count` 件。**まだ見ていないものが先**。

    一周の速さは巡回の側が決める(ここは順番だけを持つ)。

    **`origin` があれば、そこから近い順に広げる**(矩形のときだけ)。既定は鍵の
    文字列順で、矩形の鍵は南西の角から始まるので**日本の地図なら八重山から北上する** ——
    一周に何十日もかかる台帳では、主要なところに着くのが何か月も先になる
    (本番の食事処は 10,457 区画・1 回 5 区画・1 時間おきで、一周に 87 日)。

    **2 周目からは何もしなくてよい。** 1 周目を近い順に回れば印の時刻も近い順に
    付くので、「古い順」がそのまま同じ広がり方をなぞる。
    """
    near = _distance_from(spec, partitions)
    ordered = sorted(
        partitions,
        key=lambda p: ((p.get("visits") or {}).get(sweep_name) or "", near(p), p["key"]),
    )
    return [p["key"] for p in ordered[: max(0, count)]]


def _distance_from(spec: dict | None, partitions: list[dict]):
    """区画を `origin` からの遠さで測る道具。指定が無ければどれも同じ(並びは鍵の順)。

    **測り方は粗くてよい** —— 要るのは順番だけで、距離そのものは誰にも見せない。
    経度は緯度によって詰まるので、緯度の余弦を掛けて縦横の縮尺だけ合わせる
    (地球を平らだと思って測る近似。国 1 つぶんの範囲では順番が狂わない)。

    **測るのは区画の真ん中まで。** 角までにすると、原点を含む大きな区画が
    「遠い」ことになって後回しになる。
    """
    if not spec or spec.get("by") != BY_GEO or not spec.get("origin"):
        return lambda p: 0.0
    lat0, lon0 = spec["origin"]
    scale = math.cos(math.radians(lat0))
    far: dict[str, float] = {}
    for p in partitions:
        box = parse_geo_key(p["key"])
        if box is None:
            # 読めない鍵は末尾へ(並べるためだけの値なので、落とさず後ろに置く)
            far[p["key"]] = math.inf
            continue
        dy = (box[0] + box[2]) / 2 - lat0
        dx = ((box[1] + box[3]) / 2 - lon0) * scale
        far[p["key"]] = dy * dy + dx * dx
    return lambda p: far.get(p["key"], math.inf)


def due(partitions: list[dict], sweep_name: str, spec: dict | None = None) -> str | None:
    """次に見る区画を 1 つ。無ければ None。"""
    picked = pick(partitions, sweep_name, 1, spec)
    return picked[0] if picked else None


def mark_visited(
    partitions: list[dict], keys: list[str], sweep_name: str, at: str
) -> list[dict]:
    """見終わった印を付ける。知らない鍵は黙って無視する(割り直しと行き違う)。"""
    marked = set(keys)
    return [
        {**p, "visits": {**(p.get("visits") or {}), sweep_name: at}}
        if p["key"] in marked else p
        for p in partitions
    ]


def oldest_visit(partitions: list[dict], sweep_name: str) -> str | None:
    """その巡回がいちばん長く見ていない区画の、前回の時刻。まだ一周していなければ None。

    **一周したあとの進み具合はこれで読む。** 「見終えた区画 / 全区画」は一周すると
    総数に張り付いて動かなくなる —— 区画は消えないので、2 周目からは
    「どこまで来たか」ではなく「いちばん古いところがいつのものか」が知りたい値になる
    (次に見るのは必ずそこ。`pick` が古い順に配る)。
    """
    visits = [(p.get("visits") or {}).get(sweep_name) or "" for p in partitions]
    if not visits or not all(visits):
        return None
    return min(visits)


def forget_visits(partitions: list[dict], keys: list[str], sweep_name: str) -> list[dict]:
    """その巡回が見た印を、名指しの区画から外す。

    **最後の 1 回をやり直すときに要る** —— 印が残ったままだと、やり直した回が
    次の区画へ進んでしまい、直したかったところを見ないまま一周が進む。
    """
    targets = set(keys)
    return [
        {**p, "visits": {k: v for k, v in (p.get("visits") or {}).items() if k != sweep_name}}
        if p["key"] in targets else p
        for p in partitions
    ]


def forget_all_visits(partitions: list[dict], sweep_name: str) -> list[dict]:
    """その巡回の印を、**台帳ぜんたいから**外す(一周をやり直す)。

    **最後の 1 回だけ戻す口とは別に要る。** 母集団が入れ替わったあとは、
    どの区画の「見た」も当てにならない —— 1 区画ずつ戻していては追いつかない。
    """
    return forget_visits(partitions, [p["key"] for p in partitions], sweep_name)


def cleared_where_changed(before: list[dict], after: list[dict]) -> list[dict]:
    """**中身が動いた区画の印を、どの巡回のぶんも外す**。

    機械で名簿を作り直すと、区画の母集団が入れ替わる —— **入れ替わったのに
    「見た」が残ると、その区画は一周が終わるまで誰にも見られない**。
    割られた区画の子は親の記録を写す作り(`_inherited`)なので、なおさら残る:
    1.7 万件の上で見終えた 1 区画が 68 万件に膨らんで割れても、子の全部が
    「見た」を引き継ぐ。

    **見るのは件数**(前の台帳に同じ鍵があって、数も同じなら触らない)。
    中身が丸ごと入れ替わって数だけ同じ、は起こりうるが、機械で引く回は
    足すだけなので数が動かないなら顔ぶれも動いていない。
    """
    was = {p["key"]: int(p.get("count") or 0) for p in before}
    return [
        p if was.get(p["key"]) == int(p.get("count") or 0) else {**p, "visits": {}}
        for p in after
    ]


def progress(partitions: list[dict], sweep_name: str) -> tuple[int, int]:
    """(その巡回が一度でも見た区画, 全区画)。「一周したか」を出すのに使う。"""
    seen = sum(1 for p in partitions if (p.get("visits") or {}).get(sweep_name))
    return seen, len(partitions)


# ---- プロンプトへ渡す ----------------------------------------------------------


def belongs(spec: dict, key: str, doc: dict) -> bool:
    """その文書がこの区画に入るか。`{current}` を区画のぶんだけに絞るのに使う。"""
    if spec["by"] == BY_GEO:
        box = parse_geo_key(key)
        lat, lon = coords_of(doc)
        if box is None or lat is None:
            return False
        return box[0] <= lat <= box[2] and box[1] <= lon <= box[3]
    if spec["by"] == BY_TAG:
        return key in [str(t) for t in (doc.get("tags") or [])]
    bounds = parse_title_key(key)
    title = str(doc.get("title") or "")
    return bool(bounds) and bounds[0] <= title <= bounds[1]


# 矩形を引くための格子の細かさ(片側の升目の数の上限)。区画の数の平方根を使うので
# 普通はここに当たらない —— 桁外れの台帳でも索引そのものが太らないための頭打ち。
GRID_SIDE_MAX = 256

# 1 つの矩形が占めてよい升目の数。これを超える矩形(割り直しで残った広い範囲など)は
# 格子に撒かずに脇へ寄せ、升目が外れたときだけ見る —— 撒くと索引が升目の数だけ太る。
GRID_SPREAD_MAX = 64


def locator(spec: dict, partitions: list[dict]):
    """**区画を引く道具を、1 回だけ組んでから渡す。**

    `partition_of` は 1 件ごとに台帳を頭から舐め、そのたびに鍵の文字列を数へ
    直していた。**区画が増えるほど 1 件が重くなる** —— 本番の食事処は文書
    686,602 件・区画 10,457 個で、1 件あたり平均 5,000 回の突き合わせになる。
    しかも 1 回の巡回で 6 周する(件数を数えるのに 1 周、区画ごとの `{current}` を
    切り出すのに 5 周)ので、**1 回が 8.7 時間**かかっていた。そのうち AI は
    十数分で、残りは全部ここだった(実測。手元で 1 周 0.7 時間、配信機はその倍)。

    引く前に索引を組めば、1 件の費用が区画の数に依らなくなる。**組むのは呼ぶ側**
    —— 全件を舐める側(`counts_of` / `scoped_docs`)が 1 回組んで使い回す。

    索引の形は割り方ごとに違う:

    - **矩形** …… 鍵を先に全部数へ直し、外接矩形を格子で切って升目ごとに候補を持つ。
      引くときは自分の升目に入っている矩形だけを見る
    - **見出し** …… 始まりを並べて二分探索(`partition_of` は 1 件ごとに
      並べ替えていた)
    - **タグ** …… 鍵から台帳の位置を引く辞書。先に並んでいるほうが勝つ、は変えない
    - **帯** …… 分類ごとに帯と置き場を分けて並べておく
    """
    if spec["by"] == BY_GEO:
        return _geo_locator(partitions)
    if spec["by"] == BY_TAG:
        return _tag_locator(partitions)
    if spec["by"] == BY_BAND:
        return _band_locator(spec, partitions)
    return _title_locator(partitions)


def _geo_locator(partitions: list[dict]):
    """矩形。**格子で候補を絞る**(升目に入っていない矩形は見ない)。"""
    boxes = [
        (box, p["key"]) for p in partitions if (box := parse_geo_key(p["key"])) is not None
    ]
    if not boxes:
        return lambda doc: None
    south = min(b[0] for b, _ in boxes)
    west = min(b[1] for b, _ in boxes)
    north = max(b[2] for b, _ in boxes)
    east = max(b[3] for b, _ in boxes)
    side = max(1, min(GRID_SIDE_MAX, math.isqrt(len(boxes)) or 1))
    # 幅が 0 の軸(1 列しか無い台帳)でも 0 除算にしない
    height = (north - south) / side or 1.0
    width = (east - west) / side or 1.0

    def cell(lat: float, lon: float) -> tuple[int, int]:
        row = min(side - 1, max(0, int((lat - south) / height)))
        col = min(side - 1, max(0, int((lon - west) / width)))
        return row, col

    cells: dict[tuple[int, int], list[int]] = {}
    wide: list[int] = []
    for index, (box, _key) in enumerate(boxes):
        lo, hi = cell(box[0], box[1]), cell(box[2], box[3])
        spread = (hi[0] - lo[0] + 1) * (hi[1] - lo[1] + 1)
        if spread > GRID_SPREAD_MAX:
            wide.append(index)
            continue
        for row in range(lo[0], hi[0] + 1):
            for col in range(lo[1], hi[1] + 1):
                cells.setdefault((row, col), []).append(index)

    def find(doc: dict) -> str | None:
        lat, lon = coords_of(doc)
        if lat is None or lon is None:
            return None
        # **台帳の並び順で先に当たったほうを返す**(`partition_of` と同じ)
        for index in sorted({*cells.get(cell(lat, lon), ()), *wide}):
            box, key = boxes[index]
            if box[0] <= lat <= box[2] and box[1] <= lon <= box[3]:
                return key
        return None

    return find


def _tag_locator(partitions: list[dict]):
    """タグ。**先に並んでいる区画が勝つ**のは変えない(鍵 → 台帳の位置)。"""
    at: dict[str, int] = {}
    for index, p in enumerate(partitions):
        at.setdefault(p["key"], index)

    def find(doc: dict) -> str | None:
        best = None
        for tag in doc.get("tags") or []:
            index = at.get(str(tag))
            if index is not None and (best is None or index < best):
                best = index
        return partitions[best]["key"] if best is not None else None

    return find


def _title_locator(partitions: list[dict]):
    """見出し。**区切りは「どこから始まるか」で読む**(`partition_of` の規則のまま)。"""
    starts = sorted(
        (bounds[0], p["key"]) for p in partitions if (bounds := parse_title_key(p["key"]))
    )
    heads = [head for head, _key in starts]
    keys = [key for _head, key in starts]

    def find(doc: dict) -> str | None:
        if not keys:
            return None
        title = str(doc.get("title") or "")
        # 始まりが自分以下のうち、いちばん後ろ。手前が無ければ先頭の区画
        return keys[max(bisect.bisect_right(heads, title) - 1, 0)]

    return find


def _band_locator(spec: dict, partitions: list[dict]):
    """帯。**分類ごとに、数の帯と「不明」の置き場を分けて並べておく。**"""
    bands: dict[str, list[tuple[float, str]]] = {}
    unknown: dict[str, list[tuple[str, str]]] = {}
    for p in partitions:
        parsed = parse_band_key(p["key"])
        if parsed is None:
            continue
        for name in parsed[0]:
            if parsed[3] is not None:
                unknown.setdefault(name, []).append((parsed[3], p["key"]))
            else:
                bands.setdefault(name, []).append((band_span(parsed)[0], p["key"]))
    for rows in bands.values():
        rows.sort()
    for rows in unknown.values():
        rows.sort()

    def find(doc: dict) -> str | None:
        tags = doc.get("tags") or []
        name = tag_value(tags, spec["prefix"]) or spec["other"]
        number = _number_in(tag_value(tags, spec["value"]))
        here = bands.get(name) or []
        if number is not None:
            if not here:
                # 値はあるが、その分類にまだ帯が無い(割り直しの前に入った)
                return None
            # 始まりが自分以下のうち、いちばん大きい帯。手前が無ければいちばん下の帯
            at = bisect.bisect_right([start for start, _ in here], float(number))
            return here[max(at - 1, 0)][1]
        piles = unknown.get(name) or []
        if not piles:
            return None
        title = str(doc.get("title") or "")
        at = bisect.bisect_right([bounds for bounds, _ in piles], title)
        return piles[max(at - 1, 0)][1]

    return find


def partition_of(spec: dict, partitions: list[dict], doc: dict) -> str | None:
    """その文書がどの区画のものか。**入るところが無ければ None**。

    **見出しで割った区画は、鍵の範囲だけを見てはいけない。** 鍵は
    「その区画の最初の見出し〜最後の見出し」なので、**区画と区画のあいだは
    誰のものでもない** —— あとから足した見出しがそこへ落ちると、以後どの回にも
    出てこなくなる(`{current}` にも入らないので、AI からも見えない)。
    本番の台帳では境目が 324 か所あり、足した見出しのおよそ 25 件に 1 件が当たる。

    **区切りは「どこから始まるか」で読む。** いちばん近い手前の区画に入れれば、
    見出しの線の上に隙間が無くなる(最初の区画より手前も、その区画のもの)。

    矩形とタグはそのまま —— 矩形は親を割って作るので隙間が無く、タグは
    「そのタグを持つものだけ」が初めから約束。

    **1 件だけ引くための口。** 呼ぶたびに索引を組むので、**全件を舐めるところでは
    `locator()` を 1 回呼んで使い回すこと**(速さの本体はそちら)。
    """
    if not partitions:
        return None
    return locator(spec, partitions)(doc)


def describe(spec: dict, key: str, sources: dict, partitions: list[dict] | None = None) -> str:
    """`{partition}` に差し込む文。

    **矩形だけ渡しても、そこがどこか分からない。** 母集団のソースから近くのものを
    数件添えて、どのあたりの話かを伝える(名前ではなく手がかり)。

    **台帳を渡すと、範囲の端を「どこまでか」で言える**(`partitions`)。範囲で割った
    区画は、鍵に書いてある値が**その区画の実際の受け持ちとは限らない** —— 端の区画は
    その外側も引き受け、見出しで割った区画は次の始まりの手前までを引き受ける。
    鍵のまま伝えると、**引き受けているのに誰も探しに行かない範囲**ができる。
    """
    if spec["by"] == BY_GEO:
        box = parse_geo_key(key)
        if box is None:
            return key
        where = (
            f"緯度 {box[0]:.4f}〜{box[2]:.4f} / 経度 {box[1]:.4f}〜{box[3]:.4f} の範囲"
        )
        if nearby := _nearby(spec, box, sources):
            where += "(このあたり: " + "・".join(nearby) + ")"
        return where
    if spec["by"] == BY_TAG:
        return f"タグ「{key}」が付くもの"
    if spec["by"] == BY_BAND:
        parsed = parse_band_key(key)
        if parsed is None:
            return key
        names, first, last, bounds = parsed
        where = f"「{spec['prefix'].rstrip(':')}」が{'・'.join(names)}のもの"
        if bounds is not None:
            return (
                f"{where}のうち、「{spec['value'].rstrip(':')}」が分かっていないもの"
                f"(見出しが「{parse_title_key(bounds)[0]}」から"
                f"「{parse_title_key(bounds)[1]}」まで)"
            )
        return _band_where(where, spec["value"].rstrip(":"), names, first, last, partitions)
    bounds = parse_title_key(key)
    return _title_where(bounds, partitions) if bounds else key


def _band_where(
    where: str,
    value: str,
    names: list[str],
    first: int | None,
    last: int | None,
    partitions: list[dict] | None,
) -> str:
    """帯の受け持ちを言葉にする。

    **値の軸に上限も下限も無い。** どこからどこまでが有りうる値かは指定できないので、
    端の帯は割ったときの値より外も引き受ける(`_band_of`)—— そこを鍵のまま
    「1600〜1641」と伝えると、**引き受けているのに誰も探しに行かない範囲**ができる。
    端は開いたまま「以上」「以下」で言い、**帯が 1 つしか無いなら値では絞らない**。

    **端かどうかは鍵から読む**(`-1869` / `1840-`)。**台帳からも見るのは、
    割り直す前の台帳が端も閉じた鍵で書かれているから** —— 鍵だけを見ると、
    書き換わるまでのあいだ端の帯が外側を引き受けていることを言えない。
    """
    lowest, highest = first is None, last is None
    if not (lowest and highest):
        starts = sorted(
            band_span(parsed)[0]
            for p in partitions or ()
            if (parsed := parse_band_key(p["key"]))
            and set(parsed[0]) == set(names)
            and parsed[3] is None
        )
        here = band_span((names, first, last, None))[0]
        lowest = lowest or (bool(starts) and here <= starts[0])
        highest = highest or (bool(starts) and here >= starts[-1])
    if lowest and highest:
        return f"{where}すべて(「{value}」は問いません)"
    if lowest:
        return f"{where}で、「{value}」が {last} 以下のもの"
    if highest:
        return f"{where}で、「{value}」が {first} 以上のもの"
    span = f"{first}" if first == last else f"{first}〜{last}"
    return f"{where}で、「{value}」が {span} のもの"


def _title_where(bounds: tuple[str, str], partitions: list[dict] | None) -> str:
    """見出しの範囲の受け持ちを言葉にする。

    **区切りは「どこから始まるか」で読む**(`partition_of`)ので、区画は鍵の終わりでは
    なく**次の区画の始まりの手前まで**を引き受ける。鍵のまま伝えると、境目に入る
    見出しは誰にも探されない(先頭の区画はそれより前も、末尾はその先も引き受ける)。
    """
    starts = sorted(
        parsed[0] for p in partitions or () if (parsed := parse_title_key(p["key"]))
    )
    if not starts:
        return f"見出しが「{bounds[0]}」から「{bounds[1]}」までのもの"
    following = [s for s in starts if s > bounds[0]]
    lowest = bounds[0] <= starts[0]
    if lowest and not following:
        return "見出しは問いません(区画はこの 1 つだけです)"
    if lowest:
        return f"見出しが「{following[0]}」より前のもの"
    if not following:
        return f"見出しが「{bounds[0]}」以降のもの"
    return f"見出しが「{bounds[0]}」から「{following[0]}」の手前までのもの"


def _nearby(spec: dict, box, sources: dict) -> list[str]:
    """矩形の中にある、母集団のよく知られたもの。引けなければ空。"""
    from app import db

    src = _source_of(spec, sources)
    if src is None:
        return []
    where, args = _population_filter(spec)
    try:
        rows = db.query(
            src.path,
            "SELECT d.title FROM doc_coords c JOIN docs d ON d.doc_id = c.doc_id"
            f" WHERE c.lat BETWEEN ? AND ? AND c.lon BETWEEN ? AND ?{where}"
            " ORDER BY d.rank_score DESC LIMIT ?",
            (box[0], box[2], box[1], box[3], *args, NEARBY_SAMPLES),
        )
    # 手がかりが取れなくても巡回は続ける(添えものであって、区画の中身ではない)
    except Exception:
        log.warning("partition nearby lookup failed", exc_info=True)
        return []
    return [r["title"] for r in rows]
