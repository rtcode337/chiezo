"""Overture Maps Places アダプタ — 店舗・施設の POI を国単位で取り込む。

## なぜ足したか

**OSM は店舗レベルでは穴が多い。** 新宿駅まわり約 1km 四方で実測したところ、
OSM(`osm_japan`)の飲食店は 884 件だったのに対し Overture は 4,466 件あり、
`osm_japan` に入っていなかった老舗(タカノフルーツパーラー等)も揃っていた。
属性の埋まり方も違う(実測: web サイト率 OSM 18% / Overture 77%、住所率 96.5%)。
「近くの飲食店を出す」用途では、OSM だけでは実用にならなかった。

## ライセンス

**CDLA Permissive 2.0 / Apache 2.0**(元データによる)。保存も再配布もでき、
OSM(ODbL)の継承条件が付かない。**OSM のデータは一切含まない**ので、
`osm_japan` と混ぜても ODbL は伝播しない。出典表示は Overture の
[Attribution](https://docs.overturemaps.org/attribution/) に従う。

## 取り方

配布は S3 上の GeoParquet で、**DuckDB から bbox を指定して直接引ける**
(述語が押し下がるので、全世界を落とさずに国ぶんだけ抜ける)。そのため
このアダプタだけ `duckdb` に依存する(pyosmium / mwparserfromhell と同じ例外扱い)。

**リリースのバージョン文字列は毎月変わる**。決め打ちにすると翌月には落ちるので、
`s3://overturemaps-us-west-2/release/` を列挙して**いちばん新しいものを使う**。

## 品質の扱い

Overture 自身が「重複・ゴミ・属性欠損がある」と明言していて、`confidence` で
足切りする前提のデータセット。**既定で 0.5 未満を落とす**(`OVERTURE_MIN_CONFIDENCE`)。
`rank_score` にはその confidence をそのまま入れる —— 0.0〜1.0 に収まっており、
検索の並びで「確からしい地物を上に出す」という意味づけがコアスキーマの約束と合う。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from core import Doc

log = logging.getLogger("chiezo.ingest")

S3_BASE = "s3://overturemaps-us-west-2/release"
S3_REGION = "us-west-2"

# 既定の足切り。Overture は confidence 0.2 以下も残したまま配っている
DEFAULT_MIN_CONFIDENCE = 0.5

# 1 回に読み出す行数。DuckDB の結果を全部メモリに載せないための刻み
FETCH_BATCH = 50_000


def _min_confidence() -> float:
    raw = os.environ.get("OVERTURE_MIN_CONFIDENCE", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MIN_CONFIDENCE
    except ValueError:
        return DEFAULT_MIN_CONFIDENCE


class OvertureAdapter:
    """1 国ぶんの Overture Places を取り込む。

    `bbox` は (min_lon, min_lat, max_lon, max_lat)。国境ぴったりではなく矩形なので、
    隣国の縁が少し混ざる。**そこは許容している** —— 地名辞典ではなく「その辺りの店」を
    引くための索引で、隣の県の店が数件混ざっても用途を壊さない。
    """

    source_kind = "overture"
    # 取り込み自体は DuckDB が S3 から流しながら読むので、メモリは索引作りの分だけ
    min_build_memory_gb = 2.0

    def __init__(
        self,
        source: str,
        *,
        lang: str | None,
        bbox: tuple[float, float, float, float],
        min_docs: int,
        sample_titles: list[str] | None = None,
    ) -> None:
        self.source = source
        self.lang = lang
        self.bbox = bbox
        self.min_docs = min_docs
        self.sample_titles = sample_titles or []
        self._release: str | None = None

    # ---- 取得 --------------------------------------------------------------

    def _connect(self):
        try:
            import duckdb
        except ImportError as e:  # pragma: no cover - 依存が入っていない環境
            raise SystemExit(
                "overture の取り込みには duckdb が要ります(ingest/requirements.in)"
            ) from e
        conn = duckdb.connect()
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        conn.execute(f"SET s3_region='{S3_REGION}'")
        return conn

    def _latest_release(self, conn) -> str:
        """いちばん新しいリリース(`2026-08-19.0` の形)。

        **決め打ちにしない** —— 毎月出るので、書いた翌月には無くなって
        「No files found」で落ちる(実際に古い版を書いて踏んだ)。
        """
        rows = conn.execute(
            f"SELECT file FROM glob('{S3_BASE}/*') ORDER BY file DESC"
        ).fetchall()
        releases = [str(r[0]).rstrip("/").rsplit("/", 1)[-1] for r in rows]
        # `2026-08-19.0` の形だけを見る(将来別のものが並んでも拾わない)
        valid = [r for r in releases if len(r) >= 10 and r[:4].isdigit()]
        if not valid:
            raise SystemExit("overture のリリースが見つかりません(S3 の場所が変わった可能性)")
        return valid[0]

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """S3 から bbox ぶんを抜いて手元の parquet にする。

        **ダウンロードして全件から絞るのではなく、S3 上で絞ってから落とす**
        (全世界は数百 GB あるので、そうしないと現実的な時間で終わらない)。
        戻すダンプ日付はリリース名から作る(`2026-08-19.0` → `20260819`)。
        """
        conn = self._connect()
        try:
            release = self._latest_release(conn)
            self._release = release
            date = release.split(".")[0].replace("-", "")
            out = workdir / f"{self.source}-{date}.parquet"
            if out.exists():
                log.info("overture: reusing %s", out.name)
                return out, date
            min_lon, min_lat, max_lon, max_lat = self.bbox
            log.info("overture: extracting %s from release %s", self.source, release)
            conn.execute(
                f"""
                COPY (
                  SELECT
                    id,
                    names.primary AS name,
                    categories.primary AS category,
                    categories.alternate AS alt_categories,
                    confidence,
                    bbox.ymin AS lat,
                    bbox.xmin AS lon,
                    addresses[1].freeform AS address,
                    addresses[1].region AS region,
                    addresses[1].locality AS locality,
                    websites[1] AS website,
                    phones[1] AS phone
                  FROM read_parquet(
                    '{S3_BASE}/{release}/theme=places/type=place/*',
                    filename=false, hive_partitioning=1
                  )
                  WHERE bbox.xmin BETWEEN {min_lon} AND {max_lon}
                    AND bbox.ymin BETWEEN {min_lat} AND {max_lat}
                    AND names.primary IS NOT NULL
                    AND confidence >= {_min_confidence()}
                ) TO '{out}' (FORMAT PARQUET)
                """
            )
            return out, date
        finally:
            conn.close()

    # ---- 変換 --------------------------------------------------------------

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """抜いた parquet を読んで Doc にする。

        **doc_id は連番**。Overture の id は GERS の文字列(`08f2...`)で整数にできず、
        コアスキーマの doc_id は整数だから。元の id は `extra.overture_id` に残すので、
        リリースをまたいで突き合わせたいときはそちらを使う。

        **同名の地物は「名前 (連番)」に弁別する**(`docs.title` が UNIQUE)。
        チェーン店は同じ名前が何十件も並ぶので、ここを避けて通れない。元の名前は
        alias に残すので、検索では素の名前でも当たる(osm.py と同じ手当て)。
        """
        conn = self._connect()
        seen: set[str] = set()
        doc_id = 0
        try:
            cur = conn.execute(f"SELECT * FROM read_parquet('{path}')")
            while True:
                rows = cur.fetchmany(FETCH_BATCH)
                if not rows:
                    break
                columns = [d[0] for d in cur.description]
                for raw in rows:
                    row = dict(zip(columns, raw, strict=False))
                    name = (row.get("name") or "").strip()
                    if not name:
                        continue
                    lat, lon = row.get("lat"), row.get("lon")
                    if lat is None or lon is None:
                        continue
                    doc_id += 1
                    title = name
                    aliases: list[str] = []
                    if title in seen:
                        title = f"{name} ({doc_id})"
                        aliases.append(name)
                    seen.add(title)

                    category = (row.get("category") or "").strip() or None
                    address = (row.get("address") or "").strip() or None
                    region = (row.get("region") or "").strip() or None
                    locality = (row.get("locality") or "").strip() or None
                    # 本文は「引ける 1 行」。Overture は説明文を持たないので、
                    # 検索で当たるように種別・所在・住所を並べた文を組む(geonames と同じ考え方)
                    where = "、".join(p for p in (region, locality) if p) or None
                    parts = [name]
                    if category:
                        parts.append(f"種別: {category}")
                    if where:
                        parts.append(f"所在: {where}")
                    if address:
                        parts.append(f"住所: {address}")
                    parts.append(f"座標: {float(lat):.5f}, {float(lon):.5f}")
                    text = "\n".join(parts)

                    alt = row.get("alt_categories") or []
                    tags = [t for t in [category, *list(alt)] if t]
                    extra = {
                        "overture_id": row.get("id"),
                        # OSM と同じ書き方に揃える(呼ぶ側が feature で分岐できる)
                        "feature": f"category={category}" if category else None,
                        "area": region,
                        "locality": locality,
                        "lat": float(lat),
                        "lon": float(lon),
                        "address": address,
                        "website": (row.get("website") or None),
                        "phone": (row.get("phone") or None),
                        "confidence": float(row.get("confidence") or 0.0),
                    }
                    yield Doc(
                        doc_id=doc_id,
                        title=title,
                        opening=text,
                        body=text,
                        tags=tags,
                        aliases=aliases,
                        # confidence はもともと 0.0〜1.0 なので、そのまま並びに使える
                        rank_score=float(row.get("confidence") or 0.0),
                        extra={k: v for k, v in extra.items() if v is not None},
                    )
        finally:
            conn.close()

    def __repr__(self) -> str:  # pragma: no cover - ログ用
        return f"OvertureAdapter({self.source}, release={self._release})"


# 日本の矩形。北方領土・南鳥島まで含む広めの枠(端が少し余っても害はない)
JAPAN_BBOX = (122.0, 20.0, 154.0, 46.0)


def overture_japan() -> OvertureAdapter:
    return OvertureAdapter(
        "overture_japan",
        lang="ja",
        bbox=JAPAN_BBOX,
        # 実測で新宿 1km 四方に 4,466 件の飲食店があった規模。全国で数百万件は入る想定で、
        # 「明らかに取りこぼした」を捕まえる下限として 50 万件に置く
        min_docs=500_000,
        sample_titles=["セブン-イレブン"],
    )

