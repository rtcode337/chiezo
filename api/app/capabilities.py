"""**AI に頼めることの分類。語彙はここ 1 つだけ。**

会話は `app/providers.py`、絵と音は `app/media_providers.py` と、**持ち主が分かれている**。
分類そのものまで散らすと「何が頼めるのか」を数える場所が無くなるので、
名前と並び順はここに集約し、画面も REST も同じものを見る。

**分類は仕事の単位で切る（実装の単位ではない）。** 音楽と SE は job の `kind` としては
どちらも `audio` だが、**モデルも相手も別物**（Lyria は曲しか作れない）なので別に数える。
逆に「絵」を静止画とアイコンに割ったりはしない —— 頼み方も相手も同じだから。

**まだ無いものも並べる。** 表から消すと「頼めるのか分からない」になり、聞かれるたびに
コードを読み直すことになる。`supported=False` は**実装が無い**という意味で、
「相手がいない」（実装はあるが鍵が未登録・GPU が無い等）とは別。
"""
from __future__ import annotations

from dataclasses import dataclass

from app import media_providers, providers, settings_store

CHAT = "chat"
VOICE = "voice"
IMAGE = "image"
VIDEO = "video"
MUSIC = "music"
SFX = "sfx"


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    # 実装があるか。**無いものも並べる**（上の注記）
    supported: bool = True
    # 未対応の理由・補足（画面に出す）
    note: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(CHAT, "会話"),
    Capability(VOICE, "声・音声", supported=False,
               note="読み上げ(TTS)も音声入力(STT)も未実装。音声入力は"
                    "「ファイルを送る」形になるので、口の形が他と違う"),
    Capability(IMAGE, "画像"),
    Capability(VIDEO, "動画", supported=False,
               note="未実装。生成に数分・1 本で数十〜数百 MB になるので、"
                    "長時間ジョブと置き場の扱いを足す必要がある"),
    Capability(MUSIC, "音楽"),
    Capability(SFX, "SE"),
)

BY_ID = {c.id: c for c in CAPABILITIES}


def _media_capabilities(spec: media_providers.MediaProvider) -> set[str]:
    """その相手が**仕組みの上で**受け持つ分類（使えるかは別）。"""
    found = set()
    if media_providers.KIND_IMAGE in spec.kinds:
        found.add(IMAGE)
    if media_providers.KIND_AUDIO in spec.kinds:
        sounds = media_providers.sounds_of(spec)
        if media_providers.SOUND_MUSIC in sounds:
            found.add(MUSIC)
        if media_providers.SOUND_SFX in sounds:
            found.add(SFX)
    return found


def of_provider(provider_id: str) -> set[str]:
    """その相手が受け持つ分類。話す相手と絵・音の相手を**同じ ID で束ねる**。"""
    found = set()
    if providers.get(provider_id) is not None:
        found.add(CHAT)
    spec = media_providers.get(provider_id)
    if spec is not None:
        found |= _media_capabilities(spec)
    return found


def all_provider_ids() -> list[str]:
    """画面に出す相手の並び。**話す相手が先、話せない相手（自前の GPU 等）が後**。"""
    ids = [p.id for p in providers.all_providers()]
    ids += [p.id for p in media_providers.all_providers() if p.id not in ids]
    return ids


def overview(usable: dict[str, set[str]]) -> list[dict]:
    """分類ごとの要約。`usable` は「相手 ID → いま使える分類」。

    **「未対応」と「相手がいない」を混ぜない。** 前者は作れば直り、後者は鍵を入れれば直る
    —— 次にすることが違うので、同じ言葉にしない。
    """
    out = []
    for cap in CAPABILITIES:
        ready = sorted(pid for pid, caps in usable.items() if cap.id in caps)
        if not cap.supported:
            state = "未対応"
        elif ready:
            state = "使える"
        else:
            state = "相手がいない"
        out.append({
            "id": cap.id,
            "label": cap.label,
            "supported": cap.supported,
            "state": state,
            "note": cap.note,
            "providers": ready,
        })
    return out


def label_of(provider_id: str) -> str:
    """画面に出す名前。**話す相手の名前を優先**（同じ ID なら同じ相手）。"""
    spec = providers.get(provider_id)
    if spec is not None:
        return spec.label
    return media_providers.label_of(provider_id)


def enabled(provider_id: str) -> bool:
    """その相手が有効か。話す相手も絵・音だけの相手も**同じ 1 行の設定**を見る。"""
    return settings_store.load(provider_id).enabled
