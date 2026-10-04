"""外向きの道具 —— LAN の Pi-hole から DNS の記録を引き、ドメインごとの集計を積み上げる。

## 何のためか

`app/feeds.py` が RSS の見出しを機械的に溜める道具なら、こちらは **Pi-hole(v6 の API)の
問い合わせの記録**を機械的に溜める道具。巡回の引き方が「Pi-hole から引く」
(`Sweep.use_pihole`)のときに走り、AI は呼ばない。何のドメインか・放置してよいかの
判断は、同じ収集の別の巡回が AI に頼む(名簿と肉付けを分けるのと同じ形)。

## 何を溜めるか

1 ドメイン 1 文書。溜めるのは**止めたドメイン**と**いつもと違う振る舞いのドメイン**
(はじめて見た・存在しない名前が返り続ける・珍しい種別・規則正しい周期・毎回ちがう
長い名前)だけで、普通の通信は溜めない(家庭の 1 日で数千のドメインが出てくるが、
見たいのはそのごく一部)。**一度溜めたドメインは、また出てくるたびに集計を足す**。

## 集計は積み上げる

脇書きに、最初と最後に見た時刻・問い合わせと止めた回数・アクセス元ごとの回数を持つ。
**前の回の値に今回のぶんを足して返す**(`merge`)—— 機械で引く回の脇書きは焼くときに
運んできた値で入れ替わる(`collect._with_facts`)ので、足し算はこちらで済ませておく。

**どこから読むかは文書が覚えている。** ドメインごとの「最後に見た時刻」より後の行だけを
足すので、カーソルを別に持たない —— 落ちた回があっても、次の回が続きから足す
(Pi-hole は 1 日ぶんを読み直しても負担にならない)。

## いつもと違うかの判定

直近 `WINDOW_HOURS` 時間の記録で見る(足す範囲とは別。周期のように、ある程度の長さを
見ないと分からないものがあるため)。「はじめて見た」は、その前の `HISTORY_DAYS` 日の
Pi-hole 自身の長期の記録と比べる。閾値はもとにした単体のアプリ(pihole-monitor)の実測で
決めたもの。

## 外へ出る以上は守る

- **接続先とパスワードは API キーの面に置く**(名前 `pihole`、`<URL> <パスワード>`)
- **取り込みのたびにセッションを閉じる** —— Pi-hole は同時に開けるセッションに上限がある
- **記録は家の中の通信そのもの** —— 溜めた収集を本人以外が見られる場所に置かない
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from itertools import pairwise

from fastapi import HTTPException

from app import settings_store

log = logging.getLogger("chiezo.app")

# API キーの面での名前(`settings_store.api_key`)と、画面に出す案内
KEY_NAME = "pihole"
KEY_LABEL = "Pi-hole の接続先とパスワード(URL と空白とパスワード)"
KEY_PLACEHOLDER = "http://pi.hole パスワード"
KEY_HELP_URL = "https://docs.pi-hole.net/api/"
# 画面を通さずに試すとき(キーの置き場が無い構成)
URL_ENV = "CHIEZO_PIHOLE_URL"
PASSWORD_ENV = "CHIEZO_PIHOLE_PASSWORD"

USER_AGENT = "chiezo (local knowledge server)"
TIMEOUT = 60

# いつもと違うかを見る窓
WINDOW_HOURS = 24
# 「はじめて見た」を判定するために遡る日数(Pi-hole の長期の記録)
HISTORY_DAYS = 30
# まだ 1 件も溜めていないとき、どこまで遡って足すか
FIRST_RUN_HOURS = 24 * 7
# 読み直しの重なり(秒)。Pi-hole の記録は時刻順に確定するとは限らない
OVERLAP_SECS = 60

# `/api/queries` が 1 回に返す上限。`length=-1` を渡しても超えられない
# (実測: 46,939 件ある日に -1 で 10,000 件しか返らなかった)
PAGE_ROWS = 10_000
# 1 回に送るページの上限(相手を叩き続けないための歯止め)。届いたら新しいほうから
# 取れたぶんだけで足す(古いほうは数えない、と控えに書く)
MAX_PAGES = 40

NXDOMAIN_MIN = 5
RARE_QTYPE_MIN = 5
COMMON_QTYPES = frozenset(
    {"A", "AAAA", "HTTPS", "PTR", "SVCB", "SOA", "SRV", "NS", "MX", "NAPTR", "DS", "DNSKEY"}
)
# 周期(ビーコン)。揃い方は変動係数(標準偏差 ÷ 中央値)で、端末ごとに分けて測る
BEACON_MAX_CV = 0.25
BEACON_MIN_INTERVALS = 6
BEACON_MIN_MEDIAN_SECS = 20.0
BEACON_SAME_SHOT_SECS = 1.0
# ラベルの形(DNS トンネリング・DGA)。CDN も長い名前を使うので、分かれ目は
# 「同じ名前を繰り返し引くか」(異なる名前 ÷ 問い合わせ回数)
LABEL_LONG = 25
LABEL_ENTROPY = 3.5
LABEL_MIN_DISTINCT = 10
LABEL_MIN_UNIQUE_RATIO = 0.7
LABEL_PARENT_DEPTH = 3

# 逆引きは Pi-hole 自身が家の中の端末の名前を引くために出すもので、手元に名前解決が
# 無ければ必ず NXDOMAIN になる(落とさないと、一覧が逆引きで埋まる)
EXCLUDED_SUFFIXES = (".arpa",)

# Pi-hole が止めたことを表す状態(`/api/queries` の `status`)
BLOCKED_STATUSES = frozenset({
    "GRAVITY", "REGEX", "DENYLIST", "GRAVITY_CNAME", "REGEX_CNAME", "DENYLIST_CNAME",
    "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXRA",
    "EXTERNAL_BLOCKED_EDE15", "SPECIAL_DOMAIN",
})

ALL_TAG = "Pi-hole"
BLOCKED_TAG = "ブロック"
WATCH_TAG = "気になる通信"
REASON_TAGS = {
    "first_seen": "理由:はじめて見た",
    "nxdomain": "理由:存在しない名前",
    "rare_qtype": "理由:珍しい種別",
    "beacon": "理由:規則正しい周期",
    "label_shape": "理由:毎回ちがう名前",
}
# アクセス元を何件まで持つか(脇書きの並びの天井 `collect.MAX_CARRIED_ITEMS` の内側)
MAX_CLIENTS = 20


# ---- 接続 -------------------------------------------------------------------

def parse_credential(raw: str | None) -> tuple[str, str]:
    """`<URL> <パスワード>` を分ける。**最初の空白で 1 回だけ切る**(パスワードに空白があってもよい)。"""
    url, _, password = (raw or "").strip().partition(" ")
    return url.strip().rstrip("/"), password.strip()


def target() -> tuple[str, str]:
    """接続先とパスワード。API キーの面を先に見て、無ければ環境変数。"""
    # 置き場の無い構成では None(環境変数へ倒す)
    if stored := settings_store.api_key(KEY_NAME):
        url, password = parse_credential(stored)
    else:
        url = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
        password = os.environ.get(PASSWORD_ENV) or ""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(409, {
            "error": "Pi-hole の接続先がありません",
            "hint": f"管理画面の API キーの面で、名前 {KEY_NAME} に「URL パスワード」を登録してください",
        })
    return url, password


class _Session:
    """Pi-hole とのやり取り 1 回ぶん。**終わったら必ずセッションを閉じる**。"""

    def __init__(self, base: str, password: str) -> None:
        self.base = base
        self.password = password
        self.sid: str | None = None

    def _request(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.sid:
            headers["sid"] = self.sid
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def __enter__(self) -> _Session:
        if not self.password:
            return self
        try:
            body = self._request("/api/auth", "POST", {"password": self.password})
        except urllib.error.HTTPError as e:
            # **パスワードも相手の応答本文も載せない**(控えは画面に出る)
            raise HTTPException(502, {"error": f"Pi-hole がパスワードを受け付けませんでした(HTTP {e.code})"}) from e
        except (urllib.error.URLError, OSError) as e:
            raise HTTPException(502, {"error": "Pi-hole に繋がりませんでした", "reason": type(e).__name__}) from e
        session = body.get("session") or {}
        if not session.get("valid") or not session.get("sid"):
            raise HTTPException(502, {
                "error": "Pi-hole がセッションを返しませんでした",
                "hint": "パスワードが違うか、同時に開けるセッションの上限に達しています",
            })
        self.sid = session["sid"]
        return self

    def __exit__(self, *_exc) -> None:
        if not self.sid:
            return
        try:
            self._request("/api/auth", "DELETE")
        except (urllib.error.URLError, OSError, ValueError):
            log.warning("pihole: セッションを閉じられませんでした(期限が来れば Pi-hole が閉じます)")
        self.sid = None

    def get(self, path: str, params: dict[str, str]) -> dict:
        try:
            return self._request(f"{path}?{urllib.parse.urlencode(params)}")
        except urllib.error.HTTPError as e:
            raise HTTPException(502, {"error": f"Pi-hole が {e.code} を返しました({path})"}) from e
        except (urllib.error.URLError, OSError) as e:
            raise HTTPException(502, {"error": "Pi-hole に繋がりませんでした", "reason": type(e).__name__}) from e

    def domains(self, start: int, until: int) -> set[str]:
        """期間内に出てきたドメイン(止めたぶんも)。**件数で切らない**(1 回だけのものこそ要る)。"""
        out: set[str] = set()
        for blocked in ("false", "true"):
            body = self.get("/api/stats/database/top_domains", {
                "from": str(start), "until": str(until), "count": "1000000", "blocked": blocked,
            })
            out.update(d["domain"] for d in body.get("domains") or [] if d.get("domain"))
        return out

    def queries(self, start: int, until: int) -> tuple[list[dict], bool]:
        """期間内の問い合わせ。**終わりを固定してページを送る**(取っている間に増えた問い合わせで、
        ページの切れ目がずれないように)。2 つ目は上限で打ち切ったか。"""
        rows: list[dict] = []
        for page in range(MAX_PAGES):
            body = self.get("/api/queries", {
                "from": str(start), "until": str(until),
                "length": str(PAGE_ROWS), "start": str(page * PAGE_ROWS),
            })
            got = body.get("queries") or []
            rows.extend(r for raw in got if (r := _record(raw)) is not None)
            if len(got) < PAGE_ROWS:
                return rows, False
        return rows, True


def _record(row: dict) -> dict | None:
    domain = str(row.get("domain") or "").strip().lower()
    at = row.get("time")
    if not domain or not isinstance(at, int | float):
        return None
    client = row.get("client") or {}
    reply = row.get("reply") or {}
    return {
        "time": float(at),
        "domain": domain,
        "client": str(client.get("ip") or "unknown"),
        "client_name": str(client.get("name") or ""),
        "type": str(row.get("type") or ""),
        "blocked": str(row.get("status") or "") in BLOCKED_STATUSES,
        "reply": str(reply.get("type") or ""),
    }


# ---- 判定 -------------------------------------------------------------------

def _excluded(domain: str) -> bool:
    return not domain or domain == "arpa" or domain.endswith(EXCLUDED_SUFFIXES)


def entropy(label: str) -> float:
    """1 文字あたりの情報量(シャノンエントロピー)。出鱈目な文字列ほど大きい。"""
    if not label:
        return 0.0
    n = len(label)
    return -sum(c / n * math.log2(c / n) for c in Counter(label).values())


def periodicity(times: list[float]) -> tuple[float, int] | None:
    """時刻の並びが機械的に等間隔なら (間隔の中央値, 観測回数)。"""
    gaps = [b - a for a, b in pairwise(times) if b - a > BEACON_SAME_SHOT_SECS]
    if len(gaps) < BEACON_MIN_INTERVALS:
        return None
    median = statistics.median(gaps)
    if median < BEACON_MIN_MEDIAN_SECS:
        return None
    cv = statistics.pstdev(gaps) / median
    return (median, len(gaps) + 1) if cv <= BEACON_MAX_CV else None


def tunneling(counts: dict[str, int]) -> dict[str, int]:
    """1 つの親の下に、毎回ちがう長くて出鱈目な名前が並んでいるもの → 親: 異なる名前の数。"""
    by_parent: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for domain, n in counts.items():
        if _excluded(domain):
            continue
        labels = domain.split(".")
        if len(labels) <= LABEL_PARENT_DEPTH:
            continue
        if len(labels[0]) < LABEL_LONG or entropy(labels[0]) < LABEL_ENTROPY:
            continue
        entry = by_parent[".".join(labels[-LABEL_PARENT_DEPTH:])]
        entry[0] += 1
        entry[1] += n
    return {
        parent: distinct
        for parent, (distinct, queries) in by_parent.items()
        if distinct >= LABEL_MIN_DISTINCT and queries > 0 and distinct / queries >= LABEL_MIN_UNIQUE_RATIO
    }


def _interval_text(secs: float) -> str:
    if secs < 90:
        return f"{round(secs)} 秒"
    if secs < 5400:
        return f"{round(secs / 60)} 分"
    return f"{secs / 3600:.1f} 時間"


def reasons(window: list[dict], known: set[str], now: float) -> dict[str, list[tuple[str, str]]]:
    """ドメイン → [(理由の種類, 観測した事実)]。窓の中の記録で見る。"""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)

    first: dict[str, float] = {}
    for r in window:
        first[r["domain"]] = min(first.get(r["domain"], math.inf), r["time"])
    for domain, at in first.items():
        if domain not in known and not _excluded(domain):
            out[domain].append(("first_seen", f"{max(0, round((now - at) / 3600))} 時間前にはじめて見た"))

    for domain, n in Counter(r["domain"] for r in window if r["reply"] == "NXDOMAIN").items():
        if n >= NXDOMAIN_MIN and not _excluded(domain):
            out[domain].append(("nxdomain", f"存在しない名前として {n} 回返っている"))

    rare = Counter((r["domain"], r["type"]) for r in window if r["type"] and r["type"] not in COMMON_QTYPES)
    for (domain, qtype), n in sorted(rare.items()):
        if n >= RARE_QTYPE_MIN and not _excluded(domain):
            out[domain].append(("rare_qtype", f"珍しい種別 {qtype} を {n} 回引いている"))

    timeline: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in window:
        timeline[(r["domain"], r["client"])].append(r["time"])
    beaconed: set[str] = set()
    for (domain, client), times in sorted(timeline.items()):
        if domain in beaconed or _excluded(domain):
            continue
        if (found := periodicity(sorted(times))) is not None:
            beaconed.add(domain)
            out[domain].append(
                ("beacon", f"{client} が {_interval_text(found[0])}おきに {found[1]} 回、規則正しく引いている")
            )

    for parent, distinct in tunneling(Counter(r["domain"] for r in window)).items():
        out[parent].append(("label_shape", f"毎回ちがう長い名前を {distinct} 個引いている"))
    return out


# ---- 集計 -------------------------------------------------------------------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _ts(raw) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _count(raw) -> int:
    return int(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0


def merge(before: dict, rows: list[dict]) -> dict:
    """前の回の集計(脇書き)に、今回の行を足す。**`last_seen` より後の行だけ**を数える
    (重なりで読み直したぶんを二重に数えない)。足すものが無ければ空。"""
    since = _ts(before.get("last_seen")) or -math.inf
    fresh = [r for r in rows if r["time"] > since]
    if not fresh:
        return {}
    clients: Counter = Counter()
    names: dict[str, str] = {}
    for ip, n, name in zip(
        before.get("clients") or [], before.get("client_counts") or [],
        (before.get("client_names") or []) + [""] * MAX_CLIENTS, strict=False,
    ):
        if isinstance(ip, str):
            clients[ip] += _count(n)
            if isinstance(name, str) and name:
                names[ip] = name
    for r in fresh:
        clients[r["client"]] += 1
        if r["client_name"]:
            names[r["client"]] = r["client_name"]
    top = clients.most_common(MAX_CLIENTS)
    first = _ts(before.get("first_seen"))
    times = [r["time"] for r in fresh]
    return {
        "first_seen": _iso(min(times) if first is None else min(first, *times)),
        "last_seen": _iso(max(times)),
        "queries": _count(before.get("queries")) + len(fresh),
        "blocked": _count(before.get("blocked")) + sum(1 for r in fresh if r["blocked"]),
        "clients": [ip for ip, _ in top],
        "client_counts": [n for _, n in top],
        "client_names": [names.get(ip, "") for ip, _ in top],
    }


def _priority(extra: dict, now: float) -> int:
    """調べる順の重み。**最近気になる通信だったものを先に、止めた回数の多いものを次に**。"""
    watched = _ts(extra.get("watched_at"))
    if watched is not None and now - watched < 7 * 86400:
        return 500 + 100 * min(4, len(extra.get("reasons") or []))
    return min(499, round(100 * math.log10(1 + _count(extra.get("blocked")))))


def _opening(domain: str, extra: dict, why: list[tuple[str, str]]) -> str:
    lines = []
    if blocked := _count(extra.get("blocked")):
        lines.append(f"Pi-hole が {blocked} 回止めた。")
    lines.append(
        f"{extra.get('first_seen', '')[:16].replace('T', ' ')} から "
        f"{extra.get('last_seen', '')[:16].replace('T', ' ')}(UTC)までに "
        f"{_count(extra.get('queries'))} 回問い合わせがあった。"
    )
    if why:
        lines.append("候補に挙げた理由: " + "。".join(detail for _, detail in why) + "。")
    return "\n".join(lines) or f"{domain} の記録。"


def build_items(
    previous: dict[str, dict], rows: list[dict], window: list[dict], known: set[str], now: float,
) -> list[dict]:
    """集める層が読む形(`title` / `body` / `tags` / `extra`)にする。

    **新しく溜めるのは止めたものと気になる通信だけ。** 既に溜めているドメインは、
    出てきたら集計を足す(普通の通信に戻っていても追い続ける)。
    **タグは最初の 1 回だけ付く**(足すだけの回は既にある 1 件のタグに触らない)ので、
    いまの状態は脇書きで読む(`blocked` / `watched_at` / `reasons`)。
    """
    why = reasons(window, known, now)
    by_domain: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if not _excluded(r["domain"]):
            by_domain[r["domain"]].append(r)
    candidates = set(by_domain) | set(why)
    items = []
    for domain in sorted(candidates):
        doc = previous.get(domain)
        before = doc.get("extra") if isinstance(doc, dict) and isinstance(doc.get("extra"), dict) else {}
        merged = merge(before, by_domain.get(domain, []))
        flagged = why.get(domain, [])
        tracked = doc is not None
        if not tracked and not flagged and not _count(merged.get("blocked")):
            continue
        if not merged and not flagged:
            continue
        extra = dict(merged)
        if flagged:
            extra["reasons"] = [detail for _, detail in flagged]
            extra["watched_at"] = _iso(now)
        extra["priority"] = _priority({**before, **extra}, now)
        tags = [ALL_TAG]
        if _count({**before, **extra}.get("blocked")):
            tags.append(BLOCKED_TAG)
        if flagged:
            tags.append(WATCH_TAG)
            tags += [REASON_TAGS[kind] for kind, _ in flagged]
        items.append({
            "title": domain,
            "body": _opening(domain, {**before, **extra}, flagged),
            "tags": tags,
            "extra": {k: v for k, v in extra.items() if v is not None},
        })
    return items


def harvest(previous: dict[str, dict]) -> tuple[list[dict], str]:
    """1 回ぶん引いて、集める層が読む形にする。2 つ目は控えに添える一言。

    **どこから読むかは文書の `last_seen` で決める**(いちばん新しいもの)。
    まだ 1 件も無ければ `FIRST_RUN_HOURS` 遡る。いつもと違うかを見る窓
    (`WINDOW_HOURS`)のほうが長ければ、そちらまで読む(足すのは `last_seen` より後だけ)。
    """
    base, password = target()
    now = time.time()
    latest = max(
        (t for doc in previous.values() if isinstance(doc, dict)
         if (t := _ts((doc.get("extra") or {}).get("last_seen"))) is not None),
        default=None,
    )
    window_from = now - WINDOW_HOURS * 3600
    since = (latest - OVERLAP_SECS) if latest is not None else now - FIRST_RUN_HOURS * 3600
    with _Session(base, password) as pihole:
        rows, capped = pihole.queries(int(min(since, window_from)), int(now))
        known = pihole.domains(int(now - HISTORY_DAYS * 86400), int(window_from))
    window = [r for r in rows if r["time"] >= window_from]
    items = build_items(previous, rows, window, known, now)
    note = f"{len(rows)} 件の問い合わせから {len(items)} ドメイン"
    if capped:
        note += f"(上限 {MAX_PAGES * PAGE_ROWS} 件で打ち切り。古いほうは数えていません)"
    return items, note
