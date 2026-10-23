"""CHIEZO_DATA_DIR/*.db の走査とソース登録(設計書 §5)。

起動時に CHIEZO_DATA_DIR を走査し、各 DB の meta を読んでソースとして登録する。
`<source>.db` というファイル名(通常は世代 DB へのシンボリックリンク)のみを
登録対象とし、`jawiki-20260701.db` のような世代ファイル自体は無視する
(ファイル名の拡張子を除いた部分と meta.source が一致するものだけを登録)。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from app import machine_store, notes

log = logging.getLogger("chiezo.app")


def previous_generation(current: Path) -> Path | None:
    """1 つ前の世代のファイル。**シンボリックリンクの指す先は外す**。

    世代は `<ソース名>-<日付>.db` で、切り替えのときに 1 つ前だけ残る
    (`ingest/main.py` の `switch_db`)。名前に入っている日付が並び順そのものなので、
    新しい順に並べて 2 番目を採ればよい。
    """
    try:
        live = current.resolve()
        peers = sorted(
            (p for p in live.parent.glob(f"{live.name.split('-')[0]}-*.db") if p != live),
            reverse=True,
        )
    except OSError:
        return None
    return peers[0] if peers else None


def generation_stamp(path: Path) -> str:
    """世代ファイルの名前に入っている日付(`<名前>-<日付>.db` の日付)。"""
    return path.stem.partition("-")[2]


@dataclass(frozen=True)
class GenerationInfo:
    """世代ファイル 1 つの素性(管理画面の「1 つ前」に添える)。

    読めなかったところは空(`None` / 空文字)のまま返す —— 添え物なので、
    読めないことで画面を落とさない。
    """
    path: Path
    size: int | None
    schema_version: int | None
    built_at: str
    doc_count: int | None

    @property
    def supported(self) -> bool:
        """戻したあとに登録できる世代か。読めなかったものも戻せない側に倒す。"""
        return self.schema_version in SUPPORTED_SCHEMA_VERSIONS


# 世代の件数を数える持ち時間(秒)。画面を開くたびに走るので短く切る。
# 切れたら件数は出さない(`doc_count=None`)
GENERATION_COUNT_TIMEOUT = 1.0
# 締め切りを確かめる間隔(SQLite の命令の数)
GENERATION_PROGRESS_STEP = 10_000

# 世代ファイルの素性の控え。世代は焼き上がったら書き換わらないので、
# (場所, inode, 大きさ, 更新時刻) が同じなら読み直さない —— 画面を開くたびに
# 全ソースの 1 つ前を COUNT すると、大きいソースの数だけ待たされる
_generation_cache: dict[tuple, GenerationInfo] = {}


def generation_info(path: Path) -> GenerationInfo:
    """世代ファイルの大きさ・schema_version・焼いた時刻・件数を読む。

    読み取り専用で開き、安い問い合わせだけ流す(meta の 1 行と COUNT)。
    COUNT は持ち時間で切る(`GENERATION_COUNT_TIMEOUT`)。どこかで失敗しても、
    読めたところまでを返す。
    """
    try:
        st = path.stat()
    except OSError:
        return GenerationInfo(path, None, None, "", None)
    key = (str(path), st.st_ino, st.st_size, st.st_mtime_ns)
    if (cached := _generation_cache.get(key)) is not None:
        return cached
    schema_version: int | None = None
    built_at = ""
    doc_count: int | None = None
    try:
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.Error:
        return GenerationInfo(path, st.st_size, None, "", None)
    try:
        row = conn.execute("SELECT schema_version, built_at FROM meta").fetchone()
        if row is not None:
            schema_version = row[0] if isinstance(row[0], int) else None
            built_at = row[1] or ""
        # 締め切りは COUNT にだけ掛ける(meta は 1 行なので切る理由が無い)
        deadline = time.monotonic() + GENERATION_COUNT_TIMEOUT
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, GENERATION_PROGRESS_STEP,
        )
        (doc_count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
    except sqlite3.Error as e:
        log.info("generation %s: %s", path.name, e)
    finally:
        conn.close()
    info = GenerationInfo(path, st.st_size, schema_version, built_at, doc_count)
    _generation_cache[key] = info
    return info


def unsupported_sources(data_dir: Path, registered: set[str] | dict) -> dict[str, object]:
    """`<ソース名>.db` なのに登録されなかったもののうち、schema_version が古くて
    読めないもの(名前 → 版)。管理画面で「取り込み直す」と知らせるため。

    見るのは登録されなかった名前だけなので、ふだんは 1 つも開かない。
    """
    found: dict[str, object] = {}
    if not data_dir.is_dir():
        return found
    for db_path in sorted(data_dir.glob("*.db")):
        # 世代ファイル(`<名前>-<日付>.db`)は見ない。ソースの名前に `-` は入らない
        if db_path.stem in registered or "-" in db_path.stem:
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        except sqlite3.Error:
            continue
        try:
            row = conn.execute("SELECT source, schema_version FROM meta").fetchone()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        if row and row[0] == db_path.stem and row[1] not in SUPPORTED_SCHEMA_VERSIONS:
            found[db_path.stem] = row[1]
    return found


@dataclass
class Source:
    name: str
    kind: str
    lang: str | None
    dump_date: str | None
    schema_version: int
    built_at: str
    doc_count: int
    path: Path
    # 追記されうるソース(notes)。`immutable=1` で開けないので db 側に伝える必要がある。
    # 通常のソースは ingest のブルーグリーンでしか変わらないので False。
    mutable: bool = False


# 読める schema_version。4 だけ(ingest の `core.SCHEMA_VERSION` と揃える)。
# 3 以前の DB は登録しない —— 足りない表(doc_tags・tag_counts・doc_coords)や索引を
# 前提に組んだ問い合わせが通らないため。古い DB を見つけたら警告を出して飛ばし、
# 管理画面にも「取り込み直す」と出す(`unsupported_sources`)。
SUPPORTED_SCHEMA_VERSIONS = {4}


def unsupported_note(schema_version: object) -> str:
    """読めない schema_version の DB に添える一言(ログと管理画面で同じものを出す)。"""
    return (
        f"schema_version={schema_version} は読めません"
        f"(対応は {', '.join(map(str, sorted(SUPPORTED_SCHEMA_VERSIONS)))})。取り込み直してください"
    )


def _load_source(db_path: Path, mutable: bool = False) -> Source | None:
    uri = f"file:{db_path}?mode=ro" if mutable else f"file:{db_path}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        log.warning("skipping %s: cannot open (%s)", db_path.name, e)
        return None
    try:
        row = conn.execute(
            "SELECT source, source_kind, lang, dump_date, schema_version, built_at FROM meta"
        ).fetchone()
        if row is None:
            log.warning("skipping %s: empty meta table", db_path.name)
            return None
        source, kind, lang, dump_date, schema_version, built_at = row
        if db_path.stem != source:
            # 世代ファイル(jawiki-20260701.db 等)は登録しない
            return None
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            log.warning(
                "skipping %s: unsupported schema_version=%s (supported: %s); re-run ingest",
                db_path.name, schema_version, sorted(SUPPORTED_SCHEMA_VERSIONS),
            )
            return None
        (doc_count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        return Source(
            name=source,
            kind=kind,
            lang=lang,
            dump_date=dump_date,
            schema_version=schema_version,
            built_at=built_at,
            doc_count=doc_count,
            path=db_path,
            mutable=mutable,
        )
    except sqlite3.Error as e:
        log.warning("skipping %s: %s", db_path.name, e)
        return None
    finally:
        conn.close()


def data_dir_fingerprint(data_dir: Path) -> dict[str, tuple[int, int, int, int]]:
    """世代切り替え検知用の指紋: `*.db` それぞれのリンク先実体の (dev, ino, size, mtime)。

    シンボリックリンクの差し替え(ino が変わる)だけでなく、コピー途中のファイル
    (size / mtime が動き続ける)も指紋の変化として現れるので、書き込みが落ち着くまで
    呼び出し側が再走査を繰り返す形で安定を待てる。stat のみでクエリは発行しない。
    """
    fp: dict[str, tuple[int, int, int, int]] = {}
    if not data_dir.is_dir():
        return fp
    for p in sorted(data_dir.glob("*.db")):
        try:
            st = p.stat()
        except OSError:
            continue  # 走査中に消えた(旧世代の削除など)
        fp[p.name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    return fp


# 消せない置き場。**外に素材が無いもの**は、消したら取り戻せない ——
# 短期記憶は書き込みが直接届くソースで、設定の置き場には収集の定義が入っている
# (どちらも取り込みで焼き直せない)。**名前で持つ**のは、この判断を画面と口の
# 両方から同じものとして引くため。
SYSTEM_SOURCES = (*notes.SYSTEM_STORES, machine_store.SOURCE_NAME)


def blocked_from_deleting(name: str, used_by: str = "") -> str:
    """そのソースを消せない理由(消せるなら空)。**判断は 1 か所に持つ**。

    **書き込める置き場は消せない**(`SYSTEM_SOURCES`)—— 取り込みで焼き直せないので、
    消した時点で中身がどこにも無くなる。

    **収集が使っているソースも消せない**(`used_by`)。焼いた DB だけ消しても、
    次の巡回でまた焼かれる —— 消えたように見えて戻ってくるほうが分かりにくい。
    消すなら収集ごと(`POST /admin/collect/{name}/delete` が両方まとめて消す)。
    **定義が無くなれば消せる** —— 定義だけ先に消えて DB が残った状態を、
    画面から片付けられなくなるため(実際にそうなって、手で消すことになった)。

    **ここで判ずるのは「消してよいか」だけ。** 実際に消すのは取り込み側で、
    `chiezo-app` は `corpus/` を読み取り専用でマウントしている(長期記憶へ書けるのは
    ingest だけ、という線の裏返し)。
    """
    if name in SYSTEM_SOURCES or notes.is_store(name):
        return "書き込める置き場なので消せません(取り込みで焼き直せないため)"
    if used_by:
        return f"収集「{used_by}」が使っています(消すなら収集ごと消してください)"
    return ""


def scan_source(db_path: Path, mutable: bool = False) -> Source | None:
    """1 つのファイルだけを見る。**置き場を名指しで足すときに使う**。

    ディレクトリごと舐めない理由 —— 設定の置き場(`CHIEZO_STATE_DIR`)には使用量や
    失敗の控えのようにソースではない DB も並ぶので、舐めると再走査のたびに
    「meta が無い」の警告がそのぶん出る。
    """
    return _load_source(db_path, mutable) if db_path.is_file() else None


def scan_sources(data_dir: Path, mutable: bool = False) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    if not data_dir.is_dir():
        log.warning("data dir does not exist: %s", data_dir)
        return sources
    for db_path in sorted(data_dir.glob("*.db")):
        src = _load_source(db_path, mutable)
        if src is not None:
            sources[src.name] = src
            log.info(
                "registered source %s (kind=%s docs=%d dump_date=%s)",
                src.name, src.kind, src.doc_count, src.dump_date,
            )
    return sources
