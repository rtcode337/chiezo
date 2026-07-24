"""GeoNames のミニフィクスチャを生成する。

本物の allCountries.zip は約 400MB あるためテストには使えない。ここでは同じ構造
(タブ区切り 19 列の allCountries.txt を含む zip、別名の alternateNamesV2.txt を含む zip、
countryInfo.txt、admin1CodesASCII.txt)を持つ極小のダンプを組み立てる。

再生成: python tests/fixtures/make_geonames_fixture.py
"""
from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# geonameid, name, asciiname, alternatenames, lat, lon, class, code, cc, cc2,
# admin1..4, population, elevation, dem, timezone, modification date
PLACES = [
    (2988507, "Paris", "Paris", "", 48.85341, 2.3488, "P", "PPLC", "FR", "",
     "11", "75", "", "", 2138551, "", "42", "Europe/Paris", "2026-01-15"),
    (1850147, "Tokyo", "Tokyo", "", 35.6895, 139.69171, "P", "PPLC", "JP", "",
     "40", "", "", "", 8336599, "", "44", "Asia/Tokyo", "2026-02-20"),
    (5128581, "New York City", "New York City", "", 40.71427, -74.00597, "P", "PPL", "US", "",
     "NY", "061", "", "", 8804190, "10", "10", "America/New_York", "2026-03-01"),
    (2643743, "London", "London", "", 51.50853, -0.12574, "P", "PPLC", "GB", "",
     "ENG", "GLA", "", "", 8961989, "", "25", "Europe/London", "2026-01-30"),
    (3138415, "Galdhopiggen", "Galdhopiggen", "", 61.63639, 8.31278, "T", "MT", "NO", "",
     "34", "", "", "", 0, "2469", "2400", "Europe/Oslo", "2025-11-11"),
    # feature class R(道路)は既定で取り込まれないことの確認用
    (9999001, "Some Road", "Some Road", "", 10.0, 10.0, "R", "RD", "FR", "",
     "11", "", "", "", 0, "", "0", "Europe/Paris", "2025-05-05"),
    # 座標が壊れている行はスキップされることの確認用
    (9999002, "Broken", "Broken", "", "n/a", "n/a", "P", "PPL", "FR", "",
     "11", "", "", "", 0, "", "0", "Europe/Paris", "2025-05-05"),
    # --- 同名地名(docs.title の UNIQUE 制約対策の確認用)---------------------
    # GeoNames には同名が大量にある。人口最大のものが素の名前を名乗り、それ以外は
    # 弁別されること。ここでは Paris(仏, 213万)が代表で、下の 2 件が弁別される側。
    (4717560, "Paris", "Paris", "", 33.66094, -95.55551, "P", "PPL", "US", "",
     "TX", "277", "", "", 24782, "", "170", "America/Chicago", "2026-01-10"),
    (6942553, "Paris", "Paris", "", 43.2, -80.38328, "P", "PPL", "CA", "",
     "08", "", "", "", 12310, "", "250", "America/Toronto", "2025-09-09"),
    # 人口がすべて 0 の同名 3 件 = 同数のとき geonameid が小さいほうが代表になること
    (7000010, "Springfield", "Springfield", "", 1.0, 1.0, "P", "PPL", "US", "",
     "IL", "", "", "", 0, "", "0", "America/Chicago", "2025-01-01"),
    (7000020, "Springfield", "Springfield", "", 2.0, 2.0, "P", "PPL", "US", "",
     "MO", "", "", "", 0, "", "0", "America/Chicago", "2025-01-01"),
]

# alternateNameId, geonameid, isolanguage, alternate name, ...
ALTERNATE_NAMES = [
    (1, 2988507, "ja", "パリ"),
    (2, 2988507, "en", "Paris"),
    (3, 2988507, "wkdt", "Q90"),
    (4, 2988507, "ru", "Париж"),        # 既定の言語外なので取り込まれない
    (5, 2988507, "link", "https://example.com/paris"),  # 別名ではないので除外
    (6, 1850147, "ja", "東京"),
    (7, 1850147, "wkdt", "Q1490"),
    (8, 5128581, "ja", "ニューヨーク"),
    (9, 3138415, "ja", "ガルフピッゲン"),
]

COUNTRY_INFO = """\
#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\tArea(in sq km)\tPopulation
FR\tFRA\t250\tFR\tFrance\tParis\t547030\t64768389
JP\tJPN\t392\tJA\tJapan\tTokyo\t377835\t127288000
US\tUSA\t840\tUS\tUnited States\tWashington\t9629091\t310232863
GB\tGBR\t826\tUK\tUnited Kingdom\tLondon\t244820\t62348447
NO\tNOR\t578\tNO\tNorway\tOslo\t324220\t5009150
"""

ADMIN1_CODES = """\
FR.11\tÎle-de-France\tIle-de-France\t3012874
JP.40\tTokyo\tTokyo\t1850147
US.NY\tNew York\tNew York\t5128638
GB.ENG\tEngland\tEngland\t6269131
NO.34\tInnlandet\tInnlandet\t3162046
"""


def _tsv(rows) -> str:
    return "".join("\t".join(str(c) for c in row) + "\n" for row in rows)


def main() -> None:
    main_zip = HERE / "mini_geonames.zip"
    with zipfile.ZipFile(main_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("allCountries.txt", _tsv(PLACES))
    print(f"wrote {main_zip} ({main_zip.stat().st_size} bytes, {len(PLACES)} rows)")

    alt_rows = [(a, g, iso, name, "", "", "", "", "", "") for a, g, iso, name in ALTERNATE_NAMES]
    alt_zip = HERE / "mini_geonames_alternate.zip"
    with zipfile.ZipFile(alt_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("alternateNamesV2.txt", _tsv(alt_rows))
    print(f"wrote {alt_zip} ({alt_zip.stat().st_size} bytes, {len(alt_rows)} rows)")

    (HERE / "mini_geonames_countryInfo.txt").write_text(COUNTRY_INFO, encoding="utf-8")
    (HERE / "mini_geonames_admin1.txt").write_text(ADMIN1_CODES, encoding="utf-8")
    print("wrote countryInfo / admin1 fixtures")


if __name__ == "__main__":
    main()
