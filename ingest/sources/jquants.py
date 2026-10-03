"""J-Quants アダプタ(東証の上場銘柄一覧)。

J-Quants API(https://jpx-jquants.com/)の `/v2/equities/master` から、全上場銘柄の
基本情報(会社名・市場区分・17/33 業種・規模区分・商品区分)を取り込む。1 銘柄 = 1 文書。
株価は持たない —— 銘柄の名寄せ(コードから名前・業種を引く)と、「プライムの輸送用機器」
「TOPIX Core30」のような絞り込みに使う。

**API キーが要る。** 管理画面の長期記憶の面で登録しておくと、初期化・再構築のときに
trigger へ渡る(`credential`)。画面を通さずに回すとき(chiezo-ingest の単発実行)は
環境変数 `CHIEZO_JQUANTS_API_KEY` から読む。J-Quants は個人の私的利用に限ったサービスで、
**取り込んだデータは本人以外が見られない状態で使う**(第三者への配信・閲覧可能な公開は不可。
生成 AI に渡すときは、学習に使われない設定であることも条件)。解約・プランを下げたときは、
取り込んだ DB も消すこと(管理画面のソース削除)。条件の原文は
https://jpx-jquants.com/ja/help/usage 。このリポジトリに入るのは取りに行くコードだけで、
キーもデータも各自の手元に置く。

**無料プランは 12 週間遅れ**のことがある(新規上場がすぐには載らない)。日付は応答の
`Date`(情報適用日)の最新を世代の日付にする。
"""
from __future__ import annotations

import json
import logging
import os
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from core import Doc

log = logging.getLogger("chiezo.ingest")

# 日本時間(夏時間が無いので固定の +09:00 でよい)
JST = timezone(timedelta(hours=9))

BASE_URL = "https://api.jquants.com/v2"
MASTER_PATH = "/equities/master"
API_KEY_ENV = "CHIEZO_JQUANTS_API_KEY"
USER_AGENT = "chiezo-ingest/0.1 (https://github.com/; contact via repo issues)"
# ページを続けて取るときの間隔(秒)。相手は個人向けのサービスなので、こちらで間を空ける
PAGE_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 60
RAW_NAME = "jquants_master.jsonl"

# 商品区分コード → 名前。2026-07 の実測で全 4,449 件がこの 6 種類のどれかだった
# (011 が 3,927 件、014 が 397 件、013 が 63 件、023 が 55 件、021 が 5 件、012 が 2 件)。
# **知らないコードは名前を推測せず、コードのまま残す**
PRODUCT_NAMES = {
    "011": "普通株式",
    "012": "出資証券",
    "013": "REIT",
    "014": "国内ETF",
    "021": "外国株式",
    "023": "海外ETF",
}

# 規模区分 → 並びの重み(0〜1)。同じ語で引いたとき、大きい会社が先に来るように
SCALE_RANK = {
    "TOPIX Core30": 1.0,
    "TOPIX Large70": 0.85,
    "TOPIX Mid400": 0.7,
    "TOPIX Small 1": 0.5,
    "TOPIX Small 2": 0.4,
}
DEFAULT_RANK = 0.1

# **全部の銘柄に付けるタグ。** chiezo の `filter` は絞り込みの条件を 1 つ要求するので、
# 銘柄マスタとして全件を読みたい側(pta のユニバースなど)はこれで引く
# (`filter?tag=東証上場`、500 件ずつ `offset` で送る)
LISTED_TAG = "東証上場"


def short_code(code: str) -> str:
    """5 桁のローカルコード(4 桁 + 株式区分 1 桁)を、普段使う 4 桁にする。

    普通株は末尾が `0`。それ以外(優先株など)は対応する 4 桁表記が無いのでそのまま。
    """
    if len(code) == 5 and code.endswith("0"):
        return code[:4]
    return code


def _blank(value: object) -> str:
    """`-` や空は「無い」として扱う(J-Quants は該当なしを `-` で返す)。"""
    text = str(value or "").strip()
    return "" if text in ("", "-") else text


class JquantsMasterAdapter:
    source = "jquants_master"
    source_kind = "jquants"
    lang = "ja"
    # 2026-07 の実測で 4,449 件。半分を割ったら取り違えとみなす
    min_docs = 2_000
    # 銘柄は統廃合で消えるので固定の名前は書かない。取り込んだ中から代表を選ぶ
    # (docs/adding-a-source.md「sample_titles に消えうる固有名を書かないこと」)
    min_build_memory_gb = 0.5
    credential_label = "J-Quants の API キー"
    # キーはダッシュボードで発行する。利用条件はヘルプの「利用目的・ライセンス」
    credential_help_url = "https://jpx-jquants.com/ja/help/usage"
    # 上場銘柄一覧と決算発表予定日は同じ J-Quants のキーで取る(画面では 1 つだけ登録する)
    credential_group = "jquants"

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.sample_titles: list[str] = []
        self.credential: str | None = None

    # ---- 取得 -------------------------------------------------------------

    def _api_key(self) -> str:
        """画面から渡されたキーを先に使い、無ければ環境変数を見る。"""
        key = (self.credential or os.environ.get(API_KEY_ENV) or "").strip()
        if not key:
            raise SystemExit(
                "J-Quants の API キーがありません(管理画面の長期記憶の面で登録するか、"
                f"chiezo-ingest に {API_KEY_ENV} を渡す)"
            )
        return key

    def _get(self, key: str, params: dict[str, str], path: str = MASTER_PATH) -> dict:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        req = urllib.request.Request(
            f"{self.base_url}{path}{query}",
            headers={"x-api-key": key, "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # **キーや相手の応答本文は載せない**(取り込みのログは画面に出る)
            if e.code in (401, 403):
                raise SystemExit(f"J-Quants が API キーを受け付けませんでした(HTTP {e.code})") from e
            if e.code == 429:
                raise SystemExit("J-Quants のレート制限に達しました(HTTP 429)。時間を置いて再実行してください") from e
            raise

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """全ページを取って 1 行 1 銘柄の JSON に書く。日付は応答の `Date` の最新。

        **毎回取り直す**(数百 KB で、古い取得を使い回す理由が無い)。
        """
        key = self._api_key()
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / RAW_NAME
        part = out.with_suffix(".part")
        params: dict[str, str] = {}
        latest = ""
        count = 0
        with part.open("w", encoding="utf-8") as f:
            while True:
                body = self._get(key, params)
                for row in body.get("data") or []:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    latest = max(latest, str(row.get("Date") or ""))
                    count += 1
                page = body.get("pagination_key")
                if not page:
                    break
                params["pagination_key"] = str(page)
                time.sleep(PAGE_INTERVAL_SECONDS)
        part.replace(out)
        date = latest.replace("-", "") or datetime.now(UTC).strftime("%Y%m%d")
        log.info("jquants_master: %d rows as of %s", count, date)
        return out, date

    # ---- 文書 -------------------------------------------------------------

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        rows = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        # **コード順に並べてから見出しを決める。** 優先株は普通株と同じ会社名で来るので、
        # 先に来る普通株(末尾 0)に素の名前を渡し、後のものにはコードを添える
        rows.sort(key=lambda r: str(r.get("Code") or ""))
        taken: set[str] = set()
        for row in rows:
            doc = self._build_doc(row, taken)
            if doc is None:
                continue
            taken.add(doc.title)
            if not self.sample_titles and (doc.extra or {}).get("scale") == "TOPIX Core30":
                self.sample_titles = [doc.title]
            yield doc
        if not self.sample_titles and taken:
            self.sample_titles = [sorted(taken)[0]]

    def _build_doc(self, row: dict, taken: set[str]) -> Doc | None:
        code5 = _blank(row.get("Code"))
        name = _blank(row.get("CoName"))
        if not code5 or not name:
            return None
        code = short_code(code5)
        title = name if name not in taken else f"{name}({code})"
        market = _blank(row.get("MktNm"))
        s17 = _blank(row.get("S17Nm"))
        s33 = _blank(row.get("S33Nm"))
        scale = _blank(row.get("ScaleCat"))
        product_code = _blank(row.get("ProdCat"))
        product = PRODUCT_NAMES.get(product_code, product_code)
        name_en = _blank(row.get("CoNameEn"))

        parts = [f"{name}(証券コード {code})は東証の上場銘柄"]
        if market:
            parts.append(f"市場区分は{market}")
        if product and product != "普通株式":
            parts.append(f"区分は{product}")
        if s33:
            parts.append(f"33業種は{s33}")
        if scale:
            parts.append(f"規模区分は{scale}")
        opening = "。".join(parts) + "。"

        # **全角の英数字は半角にそろえた名前も別名に持つ。** J-Quants は「ＮＯＫ」
        # 「ＳＢＩホールディングス」のように全角で返すので、半角で打つと引けなかった
        folded = unicodedata.normalize("NFKC", name)
        aliases = [
            a for a in dict.fromkeys([code, code5, name, folded, name_en]) if a and a != title
        ]
        return Doc(
            # 英字の入るコード(例: 130A0)もあるので 36 進で読む。コードは一意なので ID も一意
            doc_id=int(code5, 36),
            title=title,
            opening=opening,
            body=opening,
            tags=[t for t in dict.fromkeys([LISTED_TAG, market, s33, s17, scale, product]) if t],
            aliases=aliases,
            updated_at=_blank(row.get("Date")) or None,
            rank_score=SCALE_RANK.get(scale, DEFAULT_RANK),
            extra={
                "code": code,
                "local_code": code5,
                # 見出しは重なるとコードを添えるので、**素の会社名は別に持つ**
                # (銘柄マスタとして読む側が名前を取るのはこちら)
                "name": name,
                "name_en": name_en or None,
                "market_code": _blank(row.get("Mkt")) or None,
                "market": market or None,
                "sector17_code": _blank(row.get("S17")) or None,
                "sector17": s17 or None,
                "sector33_code": _blank(row.get("S33")) or None,
                "sector33": s33 or None,
                "scale": scale or None,
                "product_code": product_code or None,
                "product": product or None,
                "margin_code": _blank(row.get("Mrgn")) or None,
                "margin": _blank(row.get("MrgnNm")) or None,
                "as_of": _blank(row.get("Date")) or None,
            },
        )


EARNINGS_PATH = "/fins/earnings-date"
EARNINGS_RAW_NAME = "jquants_earnings.jsonl"
# 何日先までの予定を入れるか(予定日を 1 日ずつ問い合わせる)。決算の山は 2〜3 週間に集まり、
# 大手は発表の 2〜5 週間前に予定日を出すので、1 か月先まで見れば足りる
EARNINGS_WINDOW_DAYS = 31
# 予定日ごとの問い合わせの間隔(秒)。有料プランのレート制限に収まるように空ける
EARNINGS_INTERVAL_SECONDS = 1.2


class JquantsEarningsAdapter(JquantsMasterAdapter):
    """J-Quants の決算発表予定日(`/fins/earnings-date`)。1 銘柄・1 決算 = 1 文書。

    **全上場銘柄**(決算期を問わず、REIT 等も含む)の、今日から `EARNINGS_WINDOW_DAYS` 日先までの
    予定を入れる。予定日を 1 日ずつ指定して問い合わせ、**その日をいま有効な予定日とする記録**
    (延期・未定への変更を反映した最新のもの)だけを受け取る。

    **有料プラン(Light 以上)が要る。** プランの参照範囲は「予定日が公表された日」で決まり、
    無料プラン(12 週間前まで)では、発表の 2〜5 週間前に公表される大手の予定がほぼ見えない
    (実測: 11 月の決算の山の日でも数十社しか見えなかった)。**取り込みの最初に、公表日=今日で
    1 回問い合わせてプランを判別し**、参照範囲外(400)なら理由を添えて止める —— 一部しか
    入っていない予定を「全部」として配ると、読む側が「載っていない = 決算は無い」と取り違える。

    件数は時期で大きく増減する(決算の山の前は数千件、過ぎると減る)ので、前の世代より
    減っても焼く(`allows_shrink`)。「いまの予定」の写しで、積み上げるものではない。
    """

    source = "jquants_earnings"
    min_docs = 1
    allows_shrink = True

    def _check_plan(self, key: str, today: str) -> None:
        """公表日=今日で 1 回問い合わせ、参照範囲外(無料プラン)なら止める。"""
        try:
            self._get(key, {"date": today}, EARNINGS_PATH)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                raise SystemExit(
                    "J-Quants のプランでは直近に公表された決算発表予定日を参照できません"
                    "(無料プランは公表日が 12 週間より前のものだけ)。先の予定はほとんど見えないため"
                    "取り込みません。有料プラン(Light 以上)のキーを API キーの面で登録してください"
                ) from e
            raise

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        key = self._api_key()
        today = datetime.now(UTC).astimezone(JST).date()
        self._check_plan(key, today.isoformat())
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / EARNINGS_RAW_NAME
        part = out.with_suffix(".part")
        count = 0
        with part.open("w", encoding="utf-8") as f:
            for offset in range(EARNINGS_WINDOW_DAYS + 1):
                day = (today + timedelta(days=offset)).isoformat()
                params: dict[str, str] = {"scheduled_date": day}
                while True:
                    time.sleep(EARNINGS_INTERVAL_SECONDS)
                    body = self._get(key, params, EARNINGS_PATH)
                    for row in body.get("data") or []:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        count += 1
                    page = body.get("pagination_key")
                    if not page:
                        break
                    params["pagination_key"] = str(page)
        part.replace(out)
        date = today.strftime("%Y%m%d")
        log.info("jquants_earnings: %d rows for %s + %d days", count, date, EARNINGS_WINDOW_DAYS)
        return out, date

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        rows = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: (_blank(r.get("SchDate")) or "9999", str(r.get("Code") or "")))
        taken: set[str] = set()
        for i, row in enumerate(rows):
            doc = self._build_earnings_doc(i, row, taken)
            if doc is None:
                continue
            taken.add(doc.title)
            if not self.sample_titles:
                self.sample_titles = [doc.title]
            yield doc

    def _build_earnings_doc(self, i: int, row: dict, taken: set[str]) -> Doc | None:
        code5 = _blank(row.get("Code"))
        name = _blank(row.get("CoName"))
        date = _blank(row.get("SchDate"))
        if not code5 or not name or not date:
            return None
        code = short_code(code5)
        fq = _blank(row.get("FQName"))  # 1Q / 2Q / 3Q / FY
        fye = _blank(row.get("FYE"))  # 決算期末 MMDD
        title = f"{name}({code}) {fq} 決算発表予定".strip()
        if title in taken:
            title = f"{title} #{i}"
        opening = f"{name}(証券コード {code})は {date} に決算発表の予定({fq}、決算期末 {fye})。"
        folded = unicodedata.normalize("NFKC", name)
        return Doc(
            doc_id=i + 1,
            title=title,
            opening=opening,
            body=opening,
            tags=[t for t in dict.fromkeys(["決算発表予定", date, date[:7], fq]) if t],
            aliases=[a for a in dict.fromkeys([code, code5, folded]) if a and a != title],
            updated_at=date,
            rank_score=0.0,
            extra={
                "code": code, "local_code": code5, "name": name,
                "name_en": _blank(row.get("CoNameEn")) or None,
                "announcement_date": date,
                "fiscal_quarter": fq or None, "fiscal_year_end": fye or None,
                "published_at": _blank(row.get("PubDate")) or None,
            },
        )
