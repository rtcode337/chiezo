"""SQLite 接続管理とクエリ実行(設計書 §5.2)。

- 接続はソースごと・スレッドごとに読み取り専用 (immutable=1) で開く。
- ただし追記されうる DB(notes)だけは `mode=ro` で開く(下の `set_mutable_paths`)。
- 全クエリに 5 秒のタイムアウト(progress handler で打ち切り)。超過は QueryTimeout。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

QUERY_TIMEOUT_SECONDS = 5.0
# 1 行ずつ回すときの持ち時間。**読み口の 5 秒はここに当てない** —— あれは人が
# 待っている問い合わせを守る数で、こちらは取り込みの中で動く背景の仕事
STREAM_TIMEOUT_SECONDS = 600.0
_PROGRESS_STEP = 50_000  # この命令数ごとにタイムアウト判定

_local = threading.local()


class QueryTimeout(Exception):
    """クエリがタイムアウトした(HTTP 504 に対応)。"""


# 追記されうる DB のパス。ソース走査のたびに main.refresh_sources が入れ替える。
_mutable_paths: set[str] = set()


def set_mutable_paths(paths) -> None:
    """`immutable=1` で開いてはいけない DB を登録する(実体は notes だけ)。

    `immutable=1` は「このファイルは開いている間 1 バイトも変わらない」という宣言で、
    SQLite はそれを信じてロックも WAL の確認も一切しない。書き込みが起きる DB をこれで
    開くと、読み手が中途半端なページを掴んで壊れた結果や例外を返す。notes は追記される
    ので、そこだけ通常の読み取り専用(`mode=ro`)に落とす。
    巨大な `/data` 側は今までどおり immutable のまま(42GB を毎回ロックさせない)。
    """
    global _mutable_paths
    changed = {str(p) for p in paths}
    _mutable_paths = changed


def is_mutable(db_path: Path) -> bool:
    return str(db_path) in _mutable_paths


def get_connection(db_path: Path) -> sqlite3.Connection:
    """スレッドローカルに読み取り専用接続をキャッシュして返す。

    キャッシュにはリンク先の実体 (st_dev, st_ino) と開き方(immutable かどうか)を添えて
    持ち、呼び出しごとに現在の状態と突き合わせる。ブルーグリーン切り替えでシンボリック
    リンクが別の世代ファイルへ差し替わったら、古い実体への接続を閉じて開き直す
    (immutable 接続は開いた時点のファイルを掴み続けるため、これをしないと再起動まで
    旧世代を読み続ける)。開き方が変わったときも同様に開き直す(notes の有効化)。
    stat 1 回の上乗せはクエリ本体に比べて無視できる。
    """
    conns: dict[str, tuple[sqlite3.Connection, tuple[int, int] | None, bool]] = (
        getattr(_local, "conns", None) or {}
    )
    if not hasattr(_local, "conns"):
        _local.conns = conns
    key = str(db_path)
    try:
        st = os.stat(db_path)  # シンボリックリンクは辿って実体を見る
        ident = (st.st_dev, st.st_ino)
    except OSError:
        ident = None
    mutable = is_mutable(db_path)
    cached = conns.get(key)
    if cached is not None and (cached[1] != ident or cached[2] != mutable):
        cached[0].close()
        del conns[key]
        cached = None
    if cached is None:
        conns[key] = (_open(db_path), ident, mutable)
    return conns[key][0]


def _open(db_path: Path, same_thread: bool = True) -> sqlite3.Connection:
    """読み取り専用で 1 本開く。**開き方はここだけ**(キャッシュ側と流し込み側で共有)。

    `same_thread=False` は**呼ぶスレッドが変わりうる読み手のため**(`stream`)。
    """
    # 追記される DB は immutable にできない(上の set_mutable_paths 参照)
    uri = f"file:{db_path}?mode=ro" if is_mutable(db_path) else f"file:{db_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    # title の前方一致 (LIKE 'prefix%') を idx_docs_title の範囲検索へ最適化するため。
    # SQLite は case_sensitive_like=OFF(既定)+ BINARY インデックスだと LIKE 前方一致を
    # 範囲検索に落とせず全走査になる(百万件規模でタイムアウト)。ON にすると BINARY
    # インデックスで範囲検索が効く。副作用として LIKE の ASCII 大小同一視は無効になるが、
    # 用途は titles / search フォールバック等の前方一致のみで実害はない。
    conn.execute("PRAGMA case_sensitive_like=ON")
    return conn


def close_thread_connections() -> None:
    conns = getattr(_local, "conns", None)
    if conns:
        for entry in conns.values():
            entry[0].close()
        conns.clear()


def stream(
    db_path: Path,
    sql: str,
    params: tuple | dict = (),
    timeout: float = STREAM_TIMEOUT_SECONDS,
) -> Iterator[sqlite3.Row]:
    """1 行ずつ返す(`fetchall` しない)。

    **数十万行を丸ごと持てない読み手のためのもの。** 集める層は焼き直しのたびに
    前世代の全文書を舐めるので、`query` で受けると行の数だけメモリが要る ——
    50 万件の地図の名簿で 1.8 GB になった(実測)。

    **持ち時間は読み口より長い**(`STREAM_TIMEOUT_SECONDS`)。使うのは取り込みの
    中で動く背景の仕事で、誰も応答を待っていない。途中まで返してから切れることが
    あるので、呼ぶ側はそれを「全部読めた」と取り違えないこと(数え直す側で件数を見る)。

    **測るのは「次の 1 行が出てくるまで」で、流し終えるまでではない。**
    1 行ずつ返すあいだ、時間を使っているのは読み手のほうで SQLite は止まっている ——
    全体に締め切りを掛けると、**行数が多いほど読み手のせいで切れる**。
    実際、60 万件を焼く回が 10 分 47 秒で `query timeout` になった(読み手は
    焼く素材を組んで HTTP で流していただけで、どの問い合わせも詰まっていない)。
    ここで止めたいのは**行が出てこない問い合わせ**なので、1 行返るたびに測り直す。

    **接続はこの 1 本のために開く**(スレッドごとの使い回しを借りない)。理由が 2 つ:

    1. **読み手のスレッドは途中で変わる。** 素材を HTTP で流す道は Starlette が
       `iterate_in_threadpool` で回し、**1 行ごとに別のワーカースレッドへ移りうる**
       —— 借りた接続はそれを作ったスレッドでしか使えないので、移った瞬間に
       `SQLite objects created in a thread can only be used in that same thread` で
       落ちる。**流し始めたあとなのでステータスは変えられず**、受け取る側には
       「短いだけの正しい素材」として届く(本番で 68.6 万件のうち 24.3 万件で
       切れた。取り込み側の `min_docs` が最後の歯止めになった)。
    2. **締め切りを他の問い合わせに漏らさない。** `set_progress_handler` は接続に
       掛かるので、使い回しの 1 本に掛けると同じスレッドの他の問い合わせまで
       この締め切りで切られる。

    使い終えたら閉じる(流し切らずに捨てられても、生成器の後始末で閉じる)。
    """
    conn = _open(db_path, same_thread=False)
    deadline = time.monotonic() + timeout

    def _check() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_check, _PROGRESS_STEP)
    try:
        for row in conn.execute(sql, params):
            yield row
            # 読み手が返ってきてから測り直す(上の理由)
            deadline = time.monotonic() + timeout
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryTimeout() from e
        raise
    finally:
        conn.close()


# 読むだけの SQL に通す操作。**これ以外は authorizer が落とす** ——
# 接続は読み取り専用で開いているので書き込みはそもそも通らないが、**ATTACH は通る**
# (別のファイルを読み取り専用で足せる)。設定 DB のような、ソースではないものを
# 開かせないための一段。
_SELECT_ACTIONS = {
    getattr(sqlite3, name)
    for name in ("SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE")
    if hasattr(sqlite3, name)
}

# 1 回に返す行数。**書いた側が LIMIT を忘れても切る**(忘れると全件が文字列になって返る)
SELECT_LIMIT_DEFAULT = 50
SELECT_LIMIT_MAX = 500


def _reads_only(action, _arg1, _arg2, _db, _trigger):
    return sqlite3.SQLITE_OK if action in _SELECT_ACTIONS else sqlite3.SQLITE_DENY


def select(
    db_path: Path,
    sql: str,
    limit: int = SELECT_LIMIT_DEFAULT,
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> tuple[list[str], list[sqlite3.Row], bool]:
    """読むだけの SQL を 1 文流す。列名・行・切ったかどうかを返す。

    **AI に SQL を書かせるための口**(`search` / `filter` では数えられないこと ——
    タグの共起、期間ごとの件数、上位 N —— を数えるため)。守りは 3 つ:

    - **接続は読み取り専用**(`get_connection`。`immutable=1` か `mode=ro`)
    - **authorizer で SELECT 以外を落とす**。とくに `ATTACH` —— 読み取り専用でも
      別のファイルは足せるので、ソースではない DB を開かれる道が残る
    - **時間と行数で切る**(進み具合のハンドラと `LIMIT`)

    **囲って LIMIT を付けるのも守りのうち。** `SELECT * FROM (<渡された文>) LIMIT ?`
    にすると、書き手が忘れても切れるうえ、**問い合わせでない文はそこで構文エラー**になる。
    """
    limit = min(max(int(limit), 1), SELECT_LIMIT_MAX)
    conn = get_connection(db_path)
    deadline = time.monotonic() + timeout

    def _check() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_check, _PROGRESS_STEP)
    conn.set_authorizer(_reads_only)
    try:
        cur = conn.execute(f"SELECT * FROM ({sql}) LIMIT ?", (limit + 1,))
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description or []]
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryTimeout() from e
        raise
    finally:
        # **必ず外す。** 接続はスレッドごとに使い回すので、付けたままにすると
        # 次の検索まで SELECT しか通らなくなる
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)
    return columns, rows[:limit], len(rows) > limit


def query(
    db_path: Path,
    sql: str,
    params: tuple | dict = (),
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> list[sqlite3.Row]:
    conn = get_connection(db_path)
    deadline = time.monotonic() + timeout

    def _check() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_check, _PROGRESS_STEP)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryTimeout() from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
