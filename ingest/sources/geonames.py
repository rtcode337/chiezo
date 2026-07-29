"""GeoNames アダプタ(全世界の地名辞典)。

https://download.geonames.org/export/dump/ の `allCountries.zip`(約 400MB、約 1,200 万件)を
取り込む。OSM の大陸抽出(europe だけで 32GB)と違い、全世界をこのサイズで賄えるのは、
GeoNames が「地名辞典(gazetteer)」に振り切っていて、店舗や施設の長い裾を持たないため。
実測で osm_japan は 73% が店舗・施設だった。逆に言うと **GeoNames に店やレストランは無い**。
そこは osm_<国> の担当で、両者は競合ではなく役割分担する。

取り込むファイル:
- `allCountries.zip`     — 本体(タブ区切り 19 列)
- `alternateNamesV2.zip` — 多言語別名。「パリ」「ニューヨーク」のような日本語表記から引くために必須。
                           `isolanguage` が `wkdt` の行は wikidata の Q 番号なので `extra.wikidata` に入れ、
                           jawiki 側の `extra.wikidata` と突き合わせられるようにする。
- `countryInfo.txt`      — 国コード → 国名(`extra.area` に使う)
- `admin1CodesASCII.txt` — 1 次行政区コード → 名称

zip は展開せず Python の `zipfile` でストリーム読みする(イメージに unzip を入れないため)。
別名は件数が多い(全体で 2,000 万行規模)ので `lookup.py` の DiskMultiMap でディスクに逃がす。
"""
from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator

from core import (
    LOW_MEMORY_BUILD_GB,
    POPULARITY_LOG_MAX_COUNTRY_POPULATION,
    Doc,
    is_low_memory_build,
    normalized_popularity,
)
from lookup import BATCH_SIZE, CACHE_SIZE_KIB, DiskMultiMap

log = logging.getLogger("chiezo.ingest")

BASE_URL = "https://download.geonames.org/export/dump/"
USER_AGENT = "chiezo-ingest/0.1 (https://github.com/; contact via repo issues)"

# 本体の 19 列(https://download.geonames.org/export/dump/readme.txt)
FIELD_COUNT = 19
(
    F_ID, F_NAME, F_ASCII, F_ALT, F_LAT, F_LON, F_CLASS, F_CODE, F_CC, F_CC2,
    F_ADMIN1, F_ADMIN2, F_ADMIN3, F_ADMIN4, F_POP, F_ELEV, F_DEM, F_TZ, F_MOD,
) = range(FIELD_COUNT)

# feature class の説明(tags と opening の組み立てに使う)
CLASS_LABEL = {
    "A": "administrative",   # 国・州・行政区
    "H": "water",            # 川・湖・海
    "L": "area",             # 公園・地域
    "P": "populated place",  # 都市・町・集落
    "R": "road",             # 道路・鉄道
    "S": "spot",             # 建物・施設(空港・駅・ホテル等)
    "T": "terrain",          # 山・丘・岩
    "U": "undersea",
    "V": "vegetation",       # 森林
}

# 別名として取り込む言語。全言語(400 以上)を入れると別名だけで数千万件になり、
# 日本語で使う知識サーバとしては費用対効果が悪いため既定を絞る。
# GEONAMES_ALT_LANGS で上書きできる(カンマ区切り。"*" で全言語)。
# `wkdt`(wikidata の Q 番号)は言語ではないが同じファイルに入っているので常に拾う。
DEFAULT_ALT_LANGS = "ja,en"

# 取り込む feature class。既定は道路(R)を除く全部。R は「地点」ではなく線形で、
# 地名辞典としての価値が低いわりに件数が多い。GEONAMES_FEATURE_CLASSES で上書き可。
DEFAULT_FEATURE_CLASSES = "AHLPSTUV"


def _download(url: str, dest: Path) -> Path:
    """curl -C - で再開可能にダウンロードする(既にあれば何もしない)。"""
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists() and not part.exists():
        return dest
    log.info("downloading %s", url)
    subprocess.run(
        ["curl", "-fSL", "-A", USER_AGENT, "--retry", "5", "-C", "-", "-o", str(part), url],
        check=True,
    )
    part.rename(dest)
    return dest


def _open_zip_member(zip_path: Path, member: str) -> io.TextIOWrapper:
    """zip の中の 1 ファイルを展開せずテキストとして開く。"""
    zf = zipfile.ZipFile(zip_path)
    raw = zf.open(member)
    return io.TextIOWrapper(raw, encoding="utf-8", newline="")


def _rows(stream: io.TextIOWrapper) -> Iterator[list[str]]:
    """GeoNames のタブ区切りを読む(引用符処理はしない = QUOTE_NONE)。"""
    for row in csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE):
        if row and not row[0].startswith("#"):
            yield row


class _TitleOwners:
    """地名 → その名前を代表する geonameid(人口最大のもの)の対応表。

    `docs.title` には UNIQUE 制約があるが、GeoNames には同名地名が大量にある
    (Paris はフランス/テキサス/オンタリオ…、San José や Springfield は数百件)。
    そのままでは全行 INSERT 後の `CREATE UNIQUE INDEX` で落ちる。

    osm アダプタと同じく「代表 1 件が素の名前を名乗り、それ以外は弁別した名前にして
    元の名前を alias に残す」方式で解決する。ただし OSM の「先勝ち」と違い、こちらは
    **人口が最大のものを代表にする**(ファイル順は geonameid 順でしかなく、
    「Paris と言えばフランス」を選べないため)。同数なら geonameid が小さいほうを採る。

    12M 件規模になるのでメモリではなくディスクの一時 SQLite に持つ(lookup.py と同じ方針)。
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.unlink(missing_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(
            "PRAGMA journal_mode=OFF;"
            "PRAGMA synchronous=OFF;"
            f"PRAGMA cache_size=-{CACHE_SIZE_KIB};"
            "CREATE TABLE owner (name TEXT PRIMARY KEY, pop INTEGER, gid INTEGER) WITHOUT ROWID;"
        )
        self._pending: list[tuple[str, int, int]] = []

    def offer(self, name: str, population: int, geoname_id: int) -> None:
        self._pending.append((name, population, geoname_id))
        if len(self._pending) >= BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        # 人口が大きいほうが勝つ。同数なら geonameid が小さいほう(古い=主要なことが多い)
        self._conn.executemany(
            "INSERT INTO owner (name, pop, gid) VALUES (?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET pop = excluded.pop, gid = excluded.gid"
            "  WHERE excluded.pop > owner.pop"
            "     OR (excluded.pop = owner.pop AND excluded.gid < owner.gid)",
            self._pending,
        )
        self._pending.clear()

    def finish(self) -> "_TitleOwners":
        self._flush()
        self._conn.commit()
        return self

    def owner_of(self, name: str) -> int | None:
        row = self._conn.execute("SELECT gid FROM owner WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def __len__(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM owner").fetchone()
        return n

    def close(self) -> None:
        self._conn.close()
        self.path.unlink(missing_ok=True)


class GeonamesAdapter:
    source_kind = "geonames"

    # 別名(2,000 万行規模)はディスクへ逃がすので常駐は小さい。構築用 SQLite の
    # ページキャッシュ + 小さめの辞書(国名・admin1)が主。low_memory(既定)は
    # キャッシュを 64MiB に絞る(main.BUILD_CACHE_KIB)ので 2GiB 宣言で足り、
    # fast は余裕を見て 3GiB。
    @property
    def min_build_memory_gb(self) -> float:
        return LOW_MEMORY_BUILD_GB if is_low_memory_build() else 3.0

    def __init__(
        self,
        source: str = "geonames",
        lang: str | None = None,
        min_docs: int | None = None,
        sample_titles: list[str] | None = None,
    ):
        self.source = source
        self.lang = lang
        self.min_docs = min_docs if min_docs is not None else 5_000_000
        self.sample_titles = sample_titles if sample_titles is not None else [
            "Paris", "Tokyo", "New York City", "London", "Sydney",
        ]
        self._alt_path: Path | None = None
        self._country_path: Path | None = None
        self._admin1_path: Path | None = None

    # ---- 取得 -------------------------------------------------------------

    def _remote_date(self, url: str) -> str:
        """Last-Modified からダンプ日付を得る。GeoNames は世代ディレクトリを持たないため。"""
        if date := os.environ.get("DUMP_DATE"):
            return date
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                last_modified = resp.headers.get("Last-Modified")
            if last_modified:
                return parsedate_to_datetime(last_modified).strftime("%Y%m%d")
        except OSError:
            log.warning("could not read Last-Modified; falling back to today")
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """本体 zip を取得する。補助ファイルは fetch_extra() 側で落とす。"""
        url = BASE_URL + "allCountries.zip"
        date = self._remote_date(url)
        return _download(url, workdir / f"{self.source}-{date}-allCountries.zip"), date

    def fetch_extra(self, workdir: Path) -> None:
        """別名・国名・行政区名を取得する(main.py の EXTRA_FETCH_HOOKS から呼ばれる)。"""
        self._alt_path = _download(
            BASE_URL + "alternateNamesV2.zip", workdir / f"{self.source}-alternateNamesV2.zip"
        )
        self._country_path = _download(
            BASE_URL + "countryInfo.txt", workdir / f"{self.source}-countryInfo.txt"
        )
        self._admin1_path = _download(
            BASE_URL + "admin1CodesASCII.txt", workdir / f"{self.source}-admin1CodesASCII.txt"
        )

    # ---- 補助データの読み込み ---------------------------------------------

    def _load_countries(self) -> dict[str, str]:
        """国コード → 国名。250 件程度なのでメモリに置いてよい。"""
        if not (self._country_path and self._country_path.exists()):
            return {}
        countries = {}
        with self._country_path.open(encoding="utf-8", newline="") as fh:
            for row in _rows(fh):
                if len(row) > 4:
                    countries[row[0]] = row[4]
        log.info("loaded %d countries", len(countries))
        return countries

    def _load_admin1(self) -> dict[str, str]:
        """`CC.A1` → 1 次行政区名。4,000 件程度なのでメモリに置いてよい。"""
        if not (self._admin1_path and self._admin1_path.exists()):
            return {}
        admin1 = {}
        with self._admin1_path.open(encoding="utf-8", newline="") as fh:
            for row in _rows(fh):
                if len(row) > 1:
                    admin1[row[0]] = row[1]
        log.info("loaded %d admin1 divisions", len(admin1))
        return admin1

    def _load_alternate_names(self, workdir: Path) -> tuple[DiskMultiMap | None, DiskMultiMap | None]:
        """別名と wikidata Q 番号を、メモリではなくディスクの一時 SQLite に載せる。

        戻り値は (別名, wikidata)。どちらも geonameid の文字列がキー。
        """
        if not (self._alt_path and self._alt_path.exists()):
            return None, None
        langs = os.environ.get("GEONAMES_ALT_LANGS", DEFAULT_ALT_LANGS)
        wanted = None if langs.strip() == "*" else {s for s in langs.split(",") if s}
        names = DiskMultiMap(workdir / f"{self.source}.altnames.db")
        wikidata = DiskMultiMap(workdir / f"{self.source}.wikidata.db")
        n = 0
        with _open_zip_member(self._alt_path, "alternateNamesV2.txt") as fh:
            for row in _rows(fh):
                if len(row) < 4:
                    continue
                geoname_id, iso, name = row[1], row[2], row[3]
                if not name:
                    continue
                if iso == "wkdt":
                    wikidata.add(geoname_id, name)
                    continue
                if wanted is not None and iso not in wanted:
                    continue
                if iso == "link":  # ウェブサイトは別名ではないので入れない
                    continue
                names.add(geoname_id, name)
                n += 1
        names.finish()
        wikidata.finish()
        log.info("loaded %d alternate names (langs=%s), %d wikidata ids", n, langs, len(wikidata))
        return names, wikidata

    # ---- Doc 生成 ---------------------------------------------------------

    def _resolve_title_owners(self, path: Path, classes: set[str]) -> _TitleOwners:
        """パス1: 同名地名の代表(人口最大)を決める。docs.title の UNIQUE 制約対策。"""
        owners = _TitleOwners(path.parent / f"{self.source}.titleowners.db")
        with _open_zip_member(path, "allCountries.txt") as fh:
            for row in _rows(fh):
                if len(row) < FIELD_COUNT or row[F_CLASS] not in classes:
                    continue
                name = row[F_NAME].strip()
                if not name:
                    continue
                try:
                    geoname_id = int(row[F_ID])
                except ValueError:
                    continue
                population = int(row[F_POP]) if row[F_POP].isdigit() else 0
                owners.offer(name, population, geoname_id)
        owners.finish()
        log.info("pass 1: %d distinct titles", len(owners))
        return owners

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        workdir = path.parent
        countries = self._load_countries()
        admin1 = self._load_admin1()
        names, wikidata = self._load_alternate_names(workdir)
        classes = set(os.environ.get("GEONAMES_FEATURE_CLASSES", DEFAULT_FEATURE_CLASSES))
        log.info("feature classes: %s", "".join(sorted(classes)))

        # 巨大なフィールドを含む行があるため上限を上げる(既定 128KB では足りないことがある)
        csv.field_size_limit(sys.maxsize)
        owners = None
        total = skipped = renamed = 0
        try:
            owners = self._resolve_title_owners(path, classes)
            with _open_zip_member(path, "allCountries.txt") as fh:
                for row in _rows(fh):
                    if len(row) < FIELD_COUNT:
                        continue
                    if row[F_CLASS] not in classes:
                        skipped += 1
                        continue
                    doc = self._build_doc(row, countries, admin1, names, wikidata, owners)
                    if doc is not None:
                        total += 1
                        if doc.title != row[F_NAME].strip():
                            renamed += 1
                        yield doc
            log.info(
                "pass 2: yielded %d docs (%d skipped by feature class, %d disambiguated)",
                total, skipped, renamed,
            )
        finally:
            # 中断時にも一時ファイルを残さない(lookup.py の作法)
            for lookup in (names, wikidata, owners):
                if lookup is not None:
                    lookup.close()

    def _build_doc(
        self,
        row: list[str],
        countries: dict[str, str],
        admin1: dict[str, str],
        names: DiskMultiMap | None,
        wikidata: DiskMultiMap | None,
        owners: _TitleOwners | None = None,
    ) -> Doc | None:
        name = row[F_NAME].strip()
        if not name:
            return None
        try:
            doc_id = int(row[F_ID])
            lat, lon = float(row[F_LAT]), float(row[F_LON])
        except ValueError:
            return None

        cc = row[F_CC]
        country = countries.get(cc, cc)
        region = admin1.get(f"{cc}.{row[F_ADMIN1]}") if row[F_ADMIN1] else None
        feature_class, feature_code = row[F_CLASS], row[F_CODE]
        population = int(row[F_POP]) if row[F_POP].isdigit() else 0

        # docs.title は UNIQUE。同名地名は代表(人口最大)だけが素の名前を名乗り、
        # それ以外は「名前 (国コード:geonameid)」に弁別する(geonameid は一意なので必ず衝突しない)。
        # 元の名前は下で alias に残すので、どちらも検索では引ける。
        title = name
        if owners is not None and owners.owner_of(name) not in (None, doc_id):
            title = f"{name} ({cc}:{doc_id})"

        # 別名: 多言語表記 + ascii 名(+ 弁別された場合は元の名前)
        aliases: list[str] = []
        if title != name:
            aliases.append(name)
        if names is not None:
            aliases.extend(names.get(row[F_ID]))
        if row[F_ASCII] and row[F_ASCII] != name:
            aliases.append(row[F_ASCII])
        # 重複を除きつつ順序は保つ。title と同じものは入れない
        seen = {title}
        aliases = [a for a in aliases if not (a in seen or seen.add(a))]

        # FTS が効くように 1 行の説明文を組み立てる(GeoNames は本文を持たないため)
        where = ", ".join(p for p in (region, country) if p)
        opening = f"{name}（{where}）" if where else name
        label = CLASS_LABEL.get(feature_class, feature_class)
        opening += f" — {feature_code} / {label}"
        if population:
            opening += f'、人口 {population:,}'

        tags = [t for t in (label, feature_code, country, region) if t]

        extra: dict = {
            "lat": lat,
            "lon": lon,
            # OSM 側と同じ key=value 形式に揃える(filter?feature= で使える)
            "feature": f"{feature_class}={feature_code}",
            "country_code": cc,
        }
        # filter?area= で使う所属地域。行政区が分かればそちらのほうが有用
        if area := (region or country):
            extra["area"] = area
        if country:
            extra["country"] = country
        if population:
            extra["population"] = population
        if row[F_TZ]:
            extra["timezone"] = row[F_TZ]
        if row[F_ELEV].lstrip("-").isdigit():
            extra["elevation"] = int(row[F_ELEV])
        if wikidata is not None and (qids := wikidata.get(row[F_ID])):
            extra["wikidata"] = qids[0]

        return Doc(
            doc_id=doc_id,
            title=title,
            opening=opening,
            body=opening,
            tags=tags,
            aliases=aliases,
            updated_at=row[F_MOD] or None,
            # 人口を素朴な人気度として使う(検索の並びに効く)。生の人口ではなく
            # 0.0〜1.0 に正規化するのは、API が bm25 に掛け合わせて並べるため
            # (生値だと人口だけで並びが決まる)。順序は対数変換で変わらない。
            rank_score=normalized_popularity(population, POPULARITY_LOG_MAX_COUNTRY_POPULATION),
            extra=extra,
        )
