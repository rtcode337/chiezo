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

import logging
import math

from fastapi import HTTPException

log = logging.getLogger("chiezo.app")

# 割り方。**対象の並び方で選ぶ** —— 地理的に散らばっているなら矩形、
# 既にカテゴリで分かれているならタグ、どちらでもなければ見出しの順。
BY_GEO = "geo"
BY_TAG = "tag"
BY_TITLE = "title"
KINDS = (BY_GEO, BY_TAG, BY_TITLE)

# 1 区画あたりの目安。**厳密な上限ではない** —— 二分では割り切れないので、
# 実際の区画はこれの半分から等倍のあいだに散らばる。
DEFAULT_TARGET = 200
MIN_TARGET = 10
MAX_TARGET = 5_000

# 区画の数の歯止め。台帳は定義のメモ(notes の 1 件)に入るので、際限なく増やせない。
# 2,000 区画 = 1 日 300 区画見ても 1 週間で一周できない数なので、普通の使い方では当たらない。
MAX_PARTITIONS = 2_000

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
    }
    if by == BY_TAG and not spec["prefix"]:
        raise _bad("by=tag には prefix が要ります(例: 「地域:」)")
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


def to_json(spec: dict | None) -> dict | None:
    """定義のメモへ書ける形。**空の鍵は落とす**(読むときに邪魔なだけ)。"""
    if not spec:
        return None
    return {k: v for k, v in spec.items() if v not in (None, "")}


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
    """
    box = spec["bbox"] or _extent(points)
    if box is None:
        return []
    out: list[dict] = []
    _split(tuple(box), points, spec["target"], out, 0)
    return out


def _extent(points) -> list[float] | None:
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    # 幅が 0 だと割れないので、ごく小さな余白を足す(1 点しか無いときに起きる)
    pad = 1e-6
    return [min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad]


def _split(box, points, target: int, out: list[dict], depth: int) -> None:
    if len(out) >= MAX_PARTITIONS:
        return
    if len(points) <= target or depth >= MAX_DEPTH:
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
    """
    from app import db

    prefix = spec["prefix"]
    src = _source_of(spec, sources)
    if src is not None:
        rows = db.query(
            src.path,
            "SELECT tag, docs FROM tag_counts WHERE tag LIKE ? ORDER BY docs DESC, tag LIMIT ?",
            (prefix.replace("%", "").replace("_", "") + "%", MAX_PARTITIONS),
        )
        return [{"key": r["tag"], "count": int(r["docs"])} for r in rows]
    counts: dict[str, int] = {}
    for doc in own.values():
        for tag in doc.get("tags") or []:
            if str(tag).startswith(prefix):
                counts[str(tag)] = counts.get(str(tag), 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"key": k, "count": n} for k, n in ordered[:MAX_PARTITIONS]]


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
    out = []
    for i in range(0, len(titles), spec["target"]):
        chunk = titles[i : i + spec["target"]]
        out.append({"key": title_key(chunk[0], chunk[-1]), "count": len(chunk)})
        if len(out) >= MAX_PARTITIONS:
            break
    return out


def title_key(first: str, last: str) -> str:
    return f"{first[:MAX_TITLE_KEY_CHARS]}〜{last[:MAX_TITLE_KEY_CHARS]}"


def parse_title_key(key: str) -> tuple[str, str] | None:
    first, _, last = key.partition("〜")
    return (first, last) if last else None


# ---- 台帳(定義のメモに入る)---------------------------------------------------


def refresh(built: list[dict], current: list[dict]) -> list[dict]:
    """割り直した区画に、**前の巡回の記録を引き継ぐ**。

    引き継がないと、割り直すたびに全区画が「まだ見ていない」に戻り、一周が
    永遠に終わらない。鍵が変わった区画(割られた・統合された)は新しく始まる。
    """
    seen = {p["key"]: dict(p.get("visits") or {}) for p in current}
    return [
        {"key": p["key"], "count": int(p.get("count") or 0), "visits": seen.get(p["key"], {})}
        for p in built
    ]


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


def outgrown(spec: dict, partitions: list[dict], docs: dict[str, dict]) -> bool:
    """割り直したほうがよいか。**自分自身が母集団のときだけ意味がある**。

    母集団を外のソースから取っているなら、こちらが何件集めようと点の数は変わらない
    —— 区画は動かないほうがよい(動かすと巡回の記録が毎回リセットされる)。
    自分自身を割っているときは、育つにつれて密なところが `target` を超えていくので、
    **超えた区画が出たら割り直す**。
    """
    if spec["source"] or not partitions:
        return False
    limit = spec["target"] * 2
    counts = dict.fromkeys((p["key"] for p in partitions), 0)
    for doc in docs.values():
        key = partition_of(spec, partitions, doc)
        if key in counts:
            counts[key] += 1
    return any(n > limit for n in counts.values())


def pick(partitions: list[dict], sweep_name: str, count: int = 1) -> list[str]:
    """次に見る区画を、古い順に `count` 件。**まだ見ていないものが先**。

    一周の速さは巡回の側が決める(ここは順番だけを持つ)。
    """
    ordered = sorted(
        partitions, key=lambda p: ((p.get("visits") or {}).get(sweep_name) or "", p["key"])
    )
    return [p["key"] for p in ordered[: max(0, count)]]


def due(partitions: list[dict], sweep_name: str) -> str | None:
    """次に見る区画を 1 つ。無ければ None。"""
    picked = pick(partitions, sweep_name, 1)
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
    """
    if not partitions:
        return None
    if spec["by"] != BY_TITLE:
        for p in partitions:
            if belongs(spec, p["key"], doc):
                return p["key"]
        return None
    title = str(doc.get("title") or "")
    starts = sorted(
        (bounds[0], p["key"])
        for p in partitions
        if (bounds := parse_title_key(p["key"]))
    )
    if not starts:
        return None
    picked = starts[0][1]
    for start, key in starts:
        if start > title:
            break
        picked = key
    return picked


def describe(spec: dict, key: str, sources: dict) -> str:
    """`{partition}` に差し込む文。

    **矩形だけ渡しても、そこがどこか分からない。** 母集団のソースから近くのものを
    数件添えて、どのあたりの話かを伝える(名前ではなく手がかり)。
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
    bounds = parse_title_key(key)
    return f"見出しが「{bounds[0]}」から「{bounds[1]}」までのもの" if bounds else key


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
