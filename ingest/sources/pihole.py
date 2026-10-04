"""Pi-hole アダプタ(LAN の DNS の記録から、止めたドメインと気になる通信)。

Pi-hole v6 の API(`/api/queries`・`/api/stats/database/top_domains`)から、

- **止めたドメイン** —— 直近 `BLOCKED_DAYS` 日に Pi-hole が止めたドメインを全部
- **気になる通信** —— 直近 `WINDOW_HOURS` 時間の問い合わせのうち、いつもと違う振る舞いの
  ドメイン(はじめて見た・存在しない名前が返り続ける・珍しい種別・規則正しい周期・
  毎回ちがう長い名前)

を、**1 ドメイン = 1 文書**にする。本文は観測した事実だけ(回数・端末・候補に挙げた理由)で、
何のドメインか・放置してよいかは書かない —— それを調べるのは、このソースを引く収集の AI の
仕事(機械が正確に取れるものと、取れないものを分ける。`app/extract.py` と同じ考え方)。

**接続先とパスワードが要る。** 管理画面の「API キー」の面で、名前 `pihole` に
`<URL> <パスワード>`(空白で区切る)の形で登録しておくと、初期化・再構築のときに渡る。
画面を通さずに回すとき(chiezo-ingest の単発実行)は環境変数 `CHIEZO_PIHOLE_URL` と
`CHIEZO_PIHOLE_PASSWORD` から読む。**記録は家の中の通信そのもの**なので、取り込んだ DB を
本人以外が見られる場所に置かないこと(このリポジトリに入るのは取りに行くコードだけ)。

**焼くたびに作り直す**(積み上げない)。いつもと違うかは「直近の窓」と「その前の
`BLOCKED_DAYS` 日」を比べて決めるので、前の世代を覚えておく必要が無い —— Pi-hole 自身が
長期の記録を持っている。件数は日によって増減するので、前の世代より減っても焼く。

候補の選び方と閾値は、もとにした単体のアプリ(pihole-monitor)の実測で決めたもの。
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
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from core import Doc, check_stop

log = logging.getLogger("chiezo.ingest")

JST = timezone(timedelta(hours=9))

URL_ENV = "CHIEZO_PIHOLE_URL"
PASSWORD_ENV = "CHIEZO_PIHOLE_PASSWORD"
USER_AGENT = "chiezo-ingest/0.1"
REQUEST_TIMEOUT_SECONDS = 60
RAW_NAME = "pihole.jsonl"

# 気になる通信を見る窓。**焼くのは日に 1 回が前提**なので、1 日ぶんを見れば取りこぼさない。
# 長くすると候補が増えて読めなくなり、短くすると寝ている間の出来事を見落とす
WINDOW_HOURS = 24
# 止めたドメインを数える日数と、「はじめて見た」を判定するために遡る日数
BLOCKED_DAYS = 30

# `/api/queries` が 1 回に返す上限。`length=-1` を渡しても超えられない
# (実測: 46,939 件ある日に -1 で 10,000 件しか返らなかった)。超えるぶんは `start` で送る
PAGE_ROWS = 10_000
# 1 回の取り込みで送るページ数の上限(相手を叩き続けないための歯止め)。家庭の 1 日は
# 数万件なので届かないが、届いたら新しいほうから取れたぶんだけで判定する
MAX_PAGES = 40
PAGE_INTERVAL_SECONDS = 0.2

# 存在しない名前(NXDOMAIN)を「返り続けている」と呼ぶ下限。1〜2 回は打ち間違いや
# 一時的な失敗で普通に出る
NXDOMAIN_MIN = 5
# 珍しい種別を「出ている」と呼ぶ下限。1 回では挙げない —— 実測で TXT が 1 回だけ出たものは
# 判断のしようがないノイズだった。DNS トンネリングは同じ種別を何百回と使うので取り逃がさない
RARE_QTYPE_MIN = 5
# 平常の形として扱うクエリ種別。ここに無い種別が出たら挙げる(実測では A / AAAA / HTTPS /
# PTR / SVCB でほぼ全部を占め、TXT は 46,939 件中 1 件だった)
COMMON_QTYPES = frozenset(
    {"A", "AAAA", "HTTPS", "PTR", "SVCB", "SOA", "SRV", "NS", "MX", "NAPTR", "DS", "DNSKEY"}
)

# 周期(ビーコン)。機械が鳴らす通信は間隔が揃い、人の操作で引かれる名前はばらつく。
# 揃い方は変動係数(標準偏差 ÷ 中央値)で測り、端末ごとに分けて数える
# (同じドメインを複数台が引くと、間隔が混ざって周期が消える)
BEACON_MAX_CV = 0.25
BEACON_MIN_INTERVALS = 6
# これより短い間隔は周期として扱わない(止めた名前の再試行が秒間隔で並ぶため)
BEACON_MIN_MEDIAN_SECS = 20.0
# 同時に飛ぶ問い合わせ(A / AAAA / HTTPS)を 1 回に畳む幅
BEACON_SAME_SHOT_SECS = 1.0

# ラベルの形(DNS トンネリング・DGA)。**長くて出鱈目な名前だけでは決め手にならない** ——
# CDN とクラウドも同じ形の名前を大量に使う。分かれ目は「同じ名前を繰り返し引くか」で、
# トンネリングは 1 回ごとに新しい名前を作るので、異なる名前 ÷ 問い合わせ回数が 1 に近づく
LABEL_LONG = 25
LABEL_ENTROPY = 3.5
LABEL_MIN_DISTINCT = 10
LABEL_MIN_UNIQUE_RATIO = 0.7
LABEL_PARENT_DEPTH = 3

# 候補から外す末尾。逆引きは Pi-hole 自身が家の中の端末の名前を引くために出すもので、
# 手元に名前解決が無ければ必ず NXDOMAIN になる(落とさないと、一覧が逆引きで埋まる)
EXCLUDED_SUFFIXES = (".arpa",)

# 文書に付けるタグ。**全部の文書に `Pi-hole` を付ける**(`filter` は条件を 1 つ要るので、
# 全件を読みたい側はこれで引く)
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
# Pi-hole が止めたことを表す問い合わせの状態(`/api/queries` の `status`)。
# 止め方ごとに名前が分かれている(一覧・正規表現・拒否リスト、それぞれの CNAME 版、
# 上流が止めたもの、特別扱いの名前)
BLOCKED_STATUSES = frozenset({
    "GRAVITY", "REGEX", "DENYLIST", "GRAVITY_CNAME", "REGEX_CNAME", "DENYLIST_CNAME",
    "EXTERNAL_BLOCKED_IP", "EXTERNAL_BLOCKED_NULL", "EXTERNAL_BLOCKED_NXRA",
    "EXTERNAL_BLOCKED_EDE15", "SPECIAL_DOMAIN",
})
# 1 件に書き出す端末の数(多いものから)
MAX_CLIENTS = 5


def parse_credential(raw: str | None) -> tuple[str, str]:
    """`<URL> <パスワード>` を分ける。**最初の空白で 1 回だけ切る**(パスワードに空白があってもよい)。

    パスワードを設定していない Pi-hole は URL だけでよい。
    """
    text = (raw or "").strip()
    url, _, password = text.partition(" ")
    return url.strip().rstrip("/"), password.strip()


def _excluded(domain: str) -> bool:
    return not domain or domain.endswith(EXCLUDED_SUFFIXES) or domain in ("arpa",)


def _jst(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone(JST).strftime("%Y-%m-%d %H:%M")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _interval_text(secs: float) -> str:
    if secs < 90:
        return f"{round(secs)} 秒"
    if secs < 5400:
        return f"{round(secs / 60)} 分"
    return f"{secs / 3600:.1f} 時間"


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


class PiholeAdapter:
    source = "pihole"
    source_kind = "pihole"
    lang = "ja"
    min_docs = 1
    min_build_memory_gb = 0.5
    # 日によって増減するのが本来の姿(止めた数も、気になる通信も)
    allows_shrink = True
    credential_label = "Pi-hole の接続先とパスワード(URL と空白とパスワード)"
    credential_placeholder = "http://pi.hole パスワード"
    credential_help_url = "https://docs.pi-hole.net/api/"

    def __init__(self) -> None:
        self.sample_titles: list[str] = []
        self.credential: str | None = None
        self._sid: str | None = None

    # ---- 取得 -------------------------------------------------------------

    def _target(self) -> tuple[str, str]:
        """画面から渡された接続先を先に使い、無ければ環境変数を見る。"""
        if self.credential:
            url, password = parse_credential(self.credential)
        else:
            url = (os.environ.get(URL_ENV) or "").strip().rstrip("/")
            password = os.environ.get(PASSWORD_ENV) or ""
        if not url.startswith(("http://", "https://")):
            raise SystemExit(
                "Pi-hole の接続先がありません(管理画面の API キーの面で pihole に "
                f"「URL パスワード」を登録するか、chiezo-ingest に {URL_ENV} を渡す)"
            )
        return url, password

    def _request(self, url: str, method: str = "GET", body: dict | None = None) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self._sid:
            headers["sid"] = self._sid
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def _login(self, base: str, password: str) -> None:
        """セッションを取る。**パスワードの無い Pi-hole は取らずに通る**。"""
        if not password:
            return
        try:
            body = self._request(f"{base}/api/auth", "POST", {"password": password})
        except urllib.error.HTTPError as e:
            # **パスワードや相手の応答本文は載せない**(取り込みのログは画面に出る)
            raise SystemExit(f"Pi-hole がパスワードを受け付けませんでした(HTTP {e.code})") from e
        session = body.get("session") or {}
        if not session.get("valid") or not session.get("sid"):
            raise SystemExit(
                "Pi-hole がセッションを返しませんでした(パスワードが違うか、同時に開ける"
                "セッションの上限に達しています)"
            )
        self._sid = session["sid"]

    def _logout(self, base: str) -> None:
        """セッションを閉じる。**Pi-hole は同時に開けるセッションに上限がある**(既定 16)ので、
        取り込みのたびに開けっぱなしにすると、そのうち管理画面に入れなくなる。"""
        if not self._sid:
            return
        try:
            self._request(f"{base}/api/auth", "DELETE")
        except (urllib.error.URLError, OSError, ValueError):
            log.warning("pihole: セッションを閉じられませんでした(期限が来れば Pi-hole が閉じます)")
        self._sid = None

    def _get(self, base: str, path: str, params: dict[str, str]) -> dict:
        check_stop()
        return self._request(f"{base}{path}?{urllib.parse.urlencode(params)}")

    def _top_domains(self, base: str, start: int, until: int, blocked: bool) -> list[dict]:
        """期間内のドメインごとの件数。**件数で切らない**(1 回しか出ていないものこそ要る)。"""
        body = self._get(base, "/api/stats/database/top_domains", {
            "from": str(start), "until": str(until), "count": "1000000",
            "blocked": "true" if blocked else "false",
        })
        return [d for d in body.get("domains") or [] if d.get("domain")]

    def _queries(self, base: str, start: int, until: int) -> Iterator[dict]:
        """窓の中の問い合わせを 1 件ずつ。**終わりを固定してページを送る**(取っている間に
        増えた問い合わせで、ページの切れ目がずれないように)。"""
        for page in range(MAX_PAGES):
            body = self._get(base, "/api/queries", {
                "from": str(start), "until": str(until),
                "length": str(PAGE_ROWS), "start": str(page * PAGE_ROWS),
            })
            rows = body.get("queries") or []
            yield from rows
            if len(rows) < PAGE_ROWS:
                return
            time.sleep(PAGE_INTERVAL_SECONDS)
        log.warning("pihole: ページの上限(%d 件)に達しました。新しいほうから取れたぶんで判定します",
                    MAX_PAGES * PAGE_ROWS)

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """1 行目に窓と集計(止めたドメイン・前から知っているドメイン)、以降に窓の問い合わせ。

        **毎回取り直す**(いまの写しで、古い取得を使い回す理由が無い)。
        """
        base, password = self._target()
        now = int(time.time())
        window_from = now - WINDOW_HOURS * 3600
        history_from = now - BLOCKED_DAYS * 86400
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / RAW_NAME
        part = out.with_suffix(".part")
        count = 0
        self._login(base, password)
        try:
            blocked = {
                d["domain"]: int(d.get("count") or 0)
                for d in self._top_domains(base, history_from, now, blocked=True)
            }
            # **止めたものも「前から知っている」に入れる** —— 許可したぶんだけを見ると、
            # 止め続けているドメインが窓に入った瞬間に「はじめて見た」になる
            known = sorted({
                d["domain"]
                for flag in (False, True)
                for d in self._top_domains(base, history_from, window_from, blocked=flag)
            })
            with part.open("w", encoding="utf-8") as f:
                f.write(json.dumps({"_meta": {
                    "from": window_from, "until": now, "history_from": history_from,
                    "blocked": blocked, "known": known,
                }}, ensure_ascii=False) + "\n")
                for row in self._queries(base, window_from, now):
                    if (one := _record(row)) is not None:
                        f.write(json.dumps(one, ensure_ascii=False) + "\n")
                        count += 1
        finally:
            self._logout(base)
        part.replace(out)
        date = datetime.fromtimestamp(now, UTC).astimezone(JST).strftime("%Y%m%d")
        log.info("pihole: %d queries in %d h, %d blocked domains in %d days",
                 count, WINDOW_HOURS, len(blocked), BLOCKED_DAYS)
        return out, date

    # ---- 文書 -------------------------------------------------------------

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        meta: dict = {}
        rows: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if "_meta" in row:
                    meta = row["_meta"]
                else:
                    rows.append(row)
        until = float(meta.get("until") or time.time())
        blocked: dict[str, int] = meta.get("blocked") or {}
        known = set(meta.get("known") or [])
        reasons = _reasons(rows, known, until)
        activity = _activity(rows)

        domains = sorted(
            {d for d in blocked if not _excluded(d)} | set(reasons),
            # 理由の多いもの、止めた回数の多いものを先に(同じ見出しが無いので並びは安定する)
            key=lambda d: (-len(reasons.get(d, [])), -blocked.get(d, 0), d),
        )
        top_blocked = max(blocked.values(), default=0)
        for i, domain in enumerate(domains, start=1):
            doc = _doc(i, domain, blocked.get(domain, 0), top_blocked,
                       reasons.get(domain, []), activity.get(domain), meta)
            if not self.sample_titles:
                self.sample_titles = [doc.title]
            yield doc


def _record(row: dict) -> dict | None:
    """応答の 1 件を、判定に使う項目だけにする。時刻・ドメインの無い行は捨てる。"""
    domain = (row.get("domain") or "").strip().lower()
    at = row.get("time")
    if not domain or not isinstance(at, int | float):
        return None
    client = row.get("client") or {}
    reply = row.get("reply") or {}
    return {
        "time": float(at),
        "domain": domain,
        "client": client.get("ip") or "unknown",
        "client_name": client.get("name") or "",
        "type": row.get("type") or "",
        "status": row.get("status") or "",
        "reply": reply.get("type") or "",
    }


def _reasons(rows: list[dict], known: set[str], until: float) -> dict[str, list[tuple[str, str]]]:
    """ドメイン → [(理由の種類, 観測した事実)]。**同じドメインを理由ごとに並べない**(束ねる)。"""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)

    first: dict[str, float] = {}
    for r in rows:
        if r["time"] < first.get(r["domain"], math.inf):
            first[r["domain"]] = r["time"]
    for domain, at in first.items():
        if domain not in known and not _excluded(domain):
            hours = max(0, round((until - at) / 3600))
            out[domain].append(("first_seen", f"{hours} 時間前にはじめて見た"))

    nx = Counter(r["domain"] for r in rows if r["reply"] == "NXDOMAIN")
    for domain, n in nx.items():
        if n >= NXDOMAIN_MIN and not _excluded(domain):
            out[domain].append(("nxdomain", f"存在しない名前として {n} 回返っている"))

    rare = Counter((r["domain"], r["type"]) for r in rows if r["type"] and r["type"] not in COMMON_QTYPES)
    for (domain, qtype), n in sorted(rare.items()):
        if n >= RARE_QTYPE_MIN and not _excluded(domain):
            out[domain].append(("rare_qtype", f"珍しい種別 {qtype} を {n} 回引いている"))

    timeline: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        timeline[(r["domain"], r["client"])].append(r["time"])
    beaconed: set[str] = set()
    for (domain, client), times in sorted(timeline.items()):
        if domain in beaconed or _excluded(domain):
            continue
        if (found := periodicity(sorted(times))) is not None:
            median, n = found
            beaconed.add(domain)
            out[domain].append(
                ("beacon", f"{client} が {_interval_text(median)}おきに {n} 回、規則正しく引いている")
            )

    for parent, distinct in tunneling(Counter(r["domain"] for r in rows)).items():
        out[parent].append(
            ("label_shape", f"毎回ちがう長い名前を {distinct} 個引いている(同じ名前を繰り返していない)")
        )
    return out


def _activity(rows: list[dict]) -> dict[str, dict]:
    """ドメイン → 窓の中の回数・止めた回数・端末・最初と最後。"""
    out: dict[str, dict] = {}
    for r in rows:
        a = out.setdefault(r["domain"], {
            "count": 0, "blocked": 0, "clients": Counter(), "names": {},
            "first": r["time"], "last": r["time"],
        })
        a["count"] += 1
        if r["status"] in BLOCKED_STATUSES:
            a["blocked"] += 1
        a["clients"][r["client"]] += 1
        if r["client_name"]:
            a["names"][r["client"]] = r["client_name"]
        a["first"] = min(a["first"], r["time"])
        a["last"] = max(a["last"], r["time"])
    return out


def _client_label(ip: str, names: dict[str, str]) -> str:
    name = names.get(ip)
    return f"{name}({ip})" if name and name != ip else ip


def _doc(
    doc_id: int, domain: str, blocked_days: int, top_blocked: int,
    reasons: list[tuple[str, str]], seen: dict | None, meta: dict,
) -> Doc:
    tags = [ALL_TAG]
    if blocked_days:
        tags.append(BLOCKED_TAG)
    if reasons:
        tags.append(WATCH_TAG)
        tags += [REASON_TAGS[kind] for kind, _ in reasons]

    lines = []
    if blocked_days:
        lines.append(f"Pi-hole がこの {BLOCKED_DAYS} 日に {blocked_days} 回止めた。")
    clients: list[str] = []
    if seen:
        clients = [
            f"{_client_label(ip, seen['names'])}: {n} 回"
            for ip, n in seen["clients"].most_common(MAX_CLIENTS)
        ]
        stopped = f"(うち止めたのは {seen['blocked']} 回)" if seen["blocked"] else "(止めていない)"
        lines.append(
            f"直近 {WINDOW_HOURS} 時間に {seen['count']} 回問い合わせがあった{stopped}。"
            f"{_jst(seen['first'])} から {_jst(seen['last'])} まで。"
        )
        lines.append("引いた端末: " + "、".join(clients) + "。")
    elif reasons:
        # 子の名前を束ねた親(毎回ちがう名前)は、親そのものは引かれていない
        lines.append(f"直近 {WINDOW_HOURS} 時間に、この下の名前が引かれている。")
    else:
        lines.append(f"直近 {WINDOW_HOURS} 時間には問い合わせが無かった。")
    if reasons:
        lines.append("候補に挙げた理由: " + "。".join(detail for _, detail in reasons) + "。")
    opening = "\n".join(lines)

    # 並びの重み(0〜1)。気になる通信を先に、止めたものは回数の多いほうを先に
    rank = (0.5 + min(0.5, 0.1 * len(reasons))) if reasons else (
        0.5 * math.log1p(blocked_days) / math.log1p(top_blocked) if top_blocked else 0.0
    )
    return Doc(
        doc_id=doc_id,
        title=domain,
        opening=opening,
        body=opening,
        tags=tags,
        aliases=[],
        updated_at=_iso(float(meta.get("until") or time.time())),
        rank_score=round(rank, 4),
        extra={
            "blocked_days": BLOCKED_DAYS,
            "blocked_count": blocked_days,
            "window_hours": WINDOW_HOURS,
            "queries": seen["count"] if seen else 0,
            "queries_blocked": seen["blocked"] if seen else 0,
            "first_query_at": _iso(seen["first"]) if seen else None,
            "last_query_at": _iso(seen["last"]) if seen else None,
            "clients": clients,
            "reasons": [detail for _, detail in reasons],
            "reason_kinds": [kind for kind, _ in reasons],
            # 並びの重みを整数で(`rank_score` を 1000 倍)。**引く側が「どれから調べるか」を
            # 決める鍵**にする(収集の `unreviewed_by` は脇書きの値でしか並べられない)
            "priority": round(rank * 1000),
        },
    )
