"""AI に頼めることの分類。語彙はここ 1 つだけ。

会話は `app/providers.py`、絵と音は `app/media_providers.py` と、持ち主が分かれている。
分類そのものまで散らすと「何が頼めるのか」を数える場所が無くなるので、
名前と並び順はここに集約し、画面も REST も同じものを見る。

分類は仕事の単位で切る（実装の単位ではない）。 音楽と SE は job の `kind` としては
どちらも `audio` だが、モデルも相手も別物（Lyria は曲しか作れない）なので別に数える。
逆に「絵」を静止画とアイコンに割ったりはしない —— 頼み方も相手も同じだから。

まだ無いものも並べる。 表から消すと「頼めるのか分からない」になり、聞かれるたびに
コードを読み直すことになる。`supported=False` は実装が無いという意味で、
「相手がいない」（実装はあるが鍵が未登録・GPU が無い等）とは別。
いまは全部 `True` だが、仕組みは残してある —— 次に増える分類（動画の編集など）で
また要るし、消すと「未対応」と「相手がいない」がまた同じ言葉に潰れる。
"""
from __future__ import annotations

from dataclasses import dataclass

from app import answer, media, media_providers, providers, settings_store

CHAT = "chat"
# 読み上げと文字起こしは別に数える。 同じ「声」でも仕事の向きが逆で、相手も
# モデルも別物 —— まとめると「読み上げはできるが文字起こしはできない」相手を
# 「声が使える」と言うことになる。
SPEECH = "speech"
TRANSCRIBE = "transcribe"
IMAGE = "image"
VIDEO = "video"
MUSIC = "music"
SFX = "sfx"


# 分類 → 絵と音の kind。頼む順を引くために要る(順位は kind ごとに違う)。
# 音だけ 1 つの kind が 2 つの分類に割れるので、両方から同じ kind を指す。
KIND_OF = {
    IMAGE: media_providers.KIND_IMAGE,
    VIDEO: media_providers.KIND_VIDEO,
    SPEECH: media_providers.KIND_SPEECH,
    TRANSCRIBE: media_providers.KIND_TRANSCRIBE,
    MUSIC: media_providers.KIND_AUDIO,
    SFX: media_providers.KIND_AUDIO,
}


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    # 実装があるか。無いものも並べる（上の注記）
    supported: bool = True
    # 未対応の理由・補足（画面に出す）
    note: str = ""


CAPABILITIES: tuple[Capability, ...] = (
    Capability(CHAT, "会話"),
    Capability(SPEECH, "読み上げ",
               note="文章を音声にする（TTS）。**自前の GPU は相手にできない** ——"
                    "ComfyUI 本体に TTS のノードが無く、外部の拡張しか無いため"),
    Capability(TRANSCRIBE, "文字起こし",
               note="音声を文字にする（STT）。**これだけ job にならない** ——"
                    "返るのが文字なので、送ったその場で返す"),
    Capability(IMAGE, "画像"),
    Capability(VIDEO, "動画",
               note="生成に数分・1 本で数十 MB になるので、待つ上限も置き場の"
                    "使い方も絵とは別に決めてある（CHIEZO_VIDEO_TIMEOUT）"),
    Capability(MUSIC, "音楽"),
    Capability(SFX, "SE"),
)

BY_ID = {c.id: c for c in CAPABILITIES}


def _media_capabilities(spec: media_providers.MediaProvider) -> set[str]:
    """その相手が仕組みの上で受け持つ分類（使えるかは別）。"""
    found = set()
    if media_providers.KIND_IMAGE in spec.kinds:
        found.add(IMAGE)
    if media_providers.KIND_VIDEO in spec.kinds:
        found.add(VIDEO)
    if media_providers.KIND_SPEECH in spec.kinds:
        found.add(SPEECH)
    if media_providers.KIND_TRANSCRIBE in spec.kinds:
        found.add(TRANSCRIBE)
    if media_providers.KIND_AUDIO in spec.kinds:
        sounds = media_providers.sounds_of(spec)
        if media_providers.SOUND_MUSIC in sounds:
            found.add(MUSIC)
        if media_providers.SOUND_SFX in sounds:
            found.add(SFX)
    return found


def of_provider(provider_id: str) -> set[str]:
    """その相手が受け持つ分類。話す相手と絵・音の相手を同じ ID で束ねる。"""
    found = set()
    if providers.get(provider_id) is not None:
        found.add(CHAT)
    spec = media_providers.get(provider_id)
    if spec is not None:
        found |= _media_capabilities(spec)
    return found


def all_provider_ids() -> list[str]:
    """画面に出す相手の並び。話す相手が先、話せない相手（自前の GPU 等）が後。"""
    ids = [p.id for p in providers.all_providers()]
    ids += [p.id for p in media_providers.all_providers() if p.id not in ids]
    return ids


async def usable_now() -> dict[str, set[str]]:
    """いま実際に頼める分類を相手ごとに(`{相手 ID: {分類}}`)。

    `of_provider()` が「仕組みの上で受け持つ分類」なのに対し、こちらは
    鍵が入っていて on になっていて、置き場もあるものだけを返す。
    `/v1/capabilities`・Claude 連携設定の生成が同じものを見る ——
    数える場所を分けると、画面に出る相手と設定に書かれる相手がずれる。
    """
    usable: dict[str, set[str]] = {}
    for spec in providers.all_providers():
        if answer.load_settings(spec.id) is not None:
            usable.setdefault(spec.id, set()).add(CHAT)

    # kind と分類は 1 対 1 ではない。 音だけは 1 つの kind が音楽と SE に割れる
    # (`sounds` を見て分ける)ので、そこだけ別に数える。
    simple = {
        media_providers.KIND_IMAGE: IMAGE,
        media_providers.KIND_VIDEO: VIDEO,
        media_providers.KIND_SPEECH: SPEECH,
        media_providers.KIND_TRANSCRIBE: TRANSCRIBE,
    }
    if media.is_enabled():
        for kind in (*simple, media_providers.KIND_AUDIO):
            for entry in await media.backends(kind):
                if not entry["usable"]:
                    continue
                if kind in simple:
                    usable.setdefault(entry["id"], set()).add(simple[kind])
                    continue
                for cap_id, sound in ((MUSIC, media_providers.SOUND_MUSIC),
                                      (SFX, media_providers.SOUND_SFX)):
                    if sound in entry.get("sounds", {}):
                        usable.setdefault(entry["id"], set()).add(cap_id)
    return usable


def providers_for(usable: dict[str, set[str]], capability: str) -> list[str]:
    """その分類をいま頼める相手の表示名を、頼む順(良い順)に。

    画面の並び(`order`)ではなく `MediaProvider.preference` で並べる ——
    先に名前を出したものに頼まれるので、ここは「どれに頼むのがよいか」の順にする。
    同点なら画面の並びのまま(安定ソート)。
    """
    ready = [pid for pid in all_provider_ids() if capability in usable.get(pid, set())]
    kind = KIND_OF.get(capability, "")
    ready.sort(key=lambda pid: media_providers.preference_of(pid, kind))
    return [label_of(pid) for pid in ready]


def overview(usable: dict[str, set[str]]) -> list[dict]:
    """分類ごとの要約。`usable` は「相手 ID → いま使える分類」。

    「未対応」と「相手がいない」を混ぜない。 前者は作れば直り、後者は鍵を入れれば直る
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
    """画面に出す名前。話す相手の名前を優先（同じ ID なら同じ相手）。"""
    spec = providers.get(provider_id)
    if spec is not None:
        return spec.label
    return media_providers.label_of(provider_id)


def enabled(provider_id: str) -> bool:
    """その相手が有効か。話す相手も絵・音だけの相手も同じ 1 行の設定を見る。"""
    return settings_store.load(provider_id).enabled
