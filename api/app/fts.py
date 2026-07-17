"""FTS5 クエリのエスケープとフォールバック判定(設計書 §5.1)。

- ユーザー入力はそのまま MATCH に渡さず、各語をフレーズとしてクォートする
  (`"` で囲み、内部の `"` は除去)。スペース区切りは AND 結合。
- trigram トークナイザは 3 文字未満の語を扱えないため、3 文字以上の語が
  ひとつも無い場合はタイトル前方一致検索へフォールバックする。
"""
from __future__ import annotations

MIN_TRIGRAM_LEN = 3


def split_terms(query: str) -> list[str]:
    return [t for t in query.split() if t]


def build_match_query(query: str) -> str | None:
    """FTS5 の MATCH 式を返す。trigram で検索可能な語が無ければ None(=フォールバック)。

    3 文字未満の語は trigram では何にもマッチしないため、AND 結合から除外する。
    """
    usable = [t for t in split_terms(query) if len(t) >= MIN_TRIGRAM_LEN]
    if not usable:
        return None
    return " AND ".join('"' + t.replace('"', "") + '"' for t in usable)


def escape_like(value: str) -> str:
    """LIKE パターン用に % _ \\ をエスケープする(ESCAPE '\\' と併用)。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
