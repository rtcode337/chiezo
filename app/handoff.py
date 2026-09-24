"""手で回す巡回の預かり場所(`state/handoff/`)。

**web の画面から使う AI に頼む道。** 鍵も API も持たない代わりに、調べものは
そちらのほうが速いことがある(Gemini の web 版など)。Chiezo からは呼べないので、
**依頼文をファイルにして人に渡し、答えのファイルを読み込む**形にする。

割り切りは 1 つ ——**両端はいつもと同じ部品を使う**。依頼文は
`collect.build_messages` がそのまま組み、答えは `collect.parse_response` が
そのまま読む。だから焼く道も、墓標も、URL の重複も、未精査の印外しも、区画の印も、
AI に頼んだ回とまったく同じところを通る。**真ん中だけが人**になる。

決めごと:

- **1 つの収集に、預かれる束は 1 つだけ。** 「作る → 渡す → 答えを読む」が一巡
  するまで次を作らない —— 溜まると、どの束の答えなのかが分からなくなる。
- **束には、そのとき見せた区画と見出しを控える。** あとで答えが返ったときに
  印を付ける先(区画)と、未精査の印を外す先(見出し)がそこにしかない。
  **区画は束を作ったときのものを使う** —— 答えが返るまでに台帳が変われば
  選び直しになるが、それでは「渡した範囲」と「印を付ける範囲」がずれる。
- **作った時刻も控える。** 人の手が入るぶん、答えが返るまでに何時間も空く ——
  そのあいだに別の巡回が同じ区画を書き換えていることがあるので、読み込むときに
  「作ってから N 回焼き直されています」と添える(**断りはしない**。直す回なので
  上書きで筋は通るし、断ると持ち帰った答えが捨て場所を失う)。
- **中身はファイル、目録は設定の置き場**(`app/machine_store.py`)。束の本文は
  区画 5 つで 200 KB になるので、定義と同じ行に混ぜない(あちらは収集を 1 つ
  読むたびに全部を運ぶ)。
- **答えは預かってから焼く。** 読み込んだその場で長期記憶へ書けるのは取り込みだけ
  なので、items を控えて取り込みを 1 本起こし、素材を組むところで読み出す
  (割り込みや「この区画だけ走らせる」と同じ形)。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from app import machine_store, settings_store

log = logging.getLogger("chiezo.handoff")

KIND = "handoff"

# 束の本文の天井。**区画 5 つで 200 KB 前後**(実測の素材から)なので、
# その数倍を置く。超えるほどの束は、渡す相手の側で切られて静かに欠ける。
MAX_BODY_BYTES = 4 * 1024 * 1024

# 読み込む答えの天井。**外から来るファイル**なので、こちらで切る。
MAX_ANSWER_BYTES = 8 * 1024 * 1024

# 貼り付け用の一言。**ファイルだけでは動かない** —— web の画面は「添付を見て
# 何をするか」を本文で言わないと、要約や感想を返してくる。
# **答えの形をここでも言う**(ファイルの中にも書いてあるが、読む前に効く)。
PASTE_NOTE = (
    "添付のファイルを最後まで読んで、書いてあるとおりに調べてください。"
    "答えは説明を付けず、ファイルに書かれた形の JSON だけを"
    " answer.json というファイルにして返してください。"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dir_path() -> Path | None:
    """束の置き場。設定の置き場が無ければ None(機能ごと無効)。"""
    d = settings_store.state_dir()
    return d / "handoff" if d else None


def is_enabled() -> bool:
    return dir_path() is not None


def _safe(name: str) -> str:
    """ファイル名にしてよい形。**収集の名前はソース名と同じ規則**なので普通は
    そのまま通るが、置き場を作るのはこちらなので、ここでも確かめる。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", name)[:60] or "collection"


def _body_path(name: str) -> Path | None:
    d = dir_path()
    return d / f"{_safe(name)}.md" if d else None


def _answer_path(name: str) -> Path | None:
    d = dir_path()
    return d / f"{_safe(name)}.answer.json" if d else None


def get(name: str) -> dict | None:
    """いま預かっている束(無ければ None)。**中身は含まない**(目録だけ)。"""
    body = machine_store.get(KIND, name)
    if not body:
        return None
    try:
        found = json.loads(body)
    except ValueError:
        # **読めなければ「無い」として扱う。** 束は作り直せる(定義と違って
        # 人に直させる価値が無い)—— 残すと、押しても何も起きない状態になる
        log.warning("handoff for %s is unreadable; treating it as empty", name)
        return None
    return found if isinstance(found, dict) else None


def body_of(name: str) -> str:
    """束の本文(無ければ空)。"""
    path = _body_path(name)
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def put(
    name: str,
    *,
    sweep: str,
    keys: list[str],
    shown: list[str],
    body: str,
    docs: int = 0,
    generation: str = "",
) -> dict:
    """束を 1 つ預かる(既にあれば置き換える)。目録を返す。"""
    path = _body_path(name)
    if path is None:
        raise RuntimeError("CHIEZO_STATE_DIR が無いので、束を置く場所がありません")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    meta = {
        "sweep": sweep,
        "keys": list(keys or []),
        "shown": list(shown or []),
        "created_at": _now(),
        "bytes": len(body.encode()),
        "docs": docs,
        # 作ったときに配っていた世代。読み込むときに「その後に焼き直されたか」を言う
        "generation": generation,
        "answered_at": "",
        "answer_note": "",
        "answer_items": 0,
    }
    machine_store.put(KIND, name, json.dumps(meta, ensure_ascii=False, indent=2))
    _drop_answer(name)
    return meta


def _drop_answer(name: str) -> None:
    path = _answer_path(name)
    if path is not None and path.exists():
        path.unlink()


def answered(name: str, items: list[dict], note: str = "") -> dict:
    """答えを預かる。**まだ焼かない** —— 長期記憶へ書けるのは取り込みだけなので、
    ここでは控えるところまでで、焼くのは取り込みが素材を取りに来たとき。
    """
    meta = get(name)
    if meta is None:
        raise KeyError(name)
    path = _answer_path(name)
    if path is None:
        raise RuntimeError("CHIEZO_STATE_DIR が無いので、答えを置く場所がありません")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    meta = {**meta, "answered_at": _now(), "answer_items": len(items), "answer_note": note}
    machine_store.put(KIND, name, json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def take(name: str) -> tuple[list[dict], dict | None]:
    """預かった答えを取り出す(取り込みが素材を組むときに 1 度だけ)。

    **取り出したら束ごと片付ける。** 残すと、次の取り込みが同じ答えをもう一度
    焼く —— 焼き直し自体は同じ結果になるが、区画の印と履歴が二重になる。
    """
    meta = get(name)
    path = _answer_path(name)
    if meta is None or path is None or not path.exists():
        return [], meta
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        log.warning("handoff answer for %s is unreadable", name)
        items = []
    drop(name)
    return (items if isinstance(items, list) else []), meta


def drop(name: str) -> None:
    """束を捨てる(答えごと)。**無くても咎めない**。"""
    machine_store.drop(KIND, name)
    for path in (_body_path(name), _answer_path(name)):
        if path is not None and path.exists():
            path.unlink()


def waiting(name: str) -> bool:
    """答え待ちの束があるか(答えが入ったものは、まだ焼いていなくても除く)。"""
    meta = get(name)
    return bool(meta and not meta.get("answered_at"))


def ready(name: str) -> bool:
    """焼くのを待っている答えがあるか。"""
    meta = get(name)
    return bool(meta and meta.get("answered_at"))
