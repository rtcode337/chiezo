#!/usr/bin/env bash
# requirements.in(範囲指定)から requirements.txt(版とハッシュを固定したロック)を作る。
#
#   scripts/lock_requirements.sh            # 3 つとも作り直す
#   scripts/lock_requirements.sh --upgrade  # 範囲の中で最新へ上げ直す
#
# 直接の依存を足す・範囲を動かすときは **.in を編集してからこれを回す**。
# .txt を手で書き換えないこと(推移的な依存とハッシュが合わなくなる)。
#
# ロックがあるとイメージが再現する(同じコミット = 同じ版)一方、上流の新版は
# 入らなくなる。破壊的変更への追従は CI の test-latest ジョブ(週 1)が受け持つ。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PYTHON_VERSION=3.12   # api/ingest の Dockerfile・CI と揃える

if ! command -v uv >/dev/null 2>&1; then
    echo "uv が要ります: pip install uv(または https://docs.astral.sh/uv/)" >&2
    exit 1
fi

for target in api/requirements ingest/requirements requirements-dev; do
    echo "==> $target.txt" >&2
    uv pip compile \
        --quiet --generate-hashes --python-version "$PYTHON_VERSION" \
        --output-file "$ROOT/$target.txt" "$ROOT/$target.in" "$@"
done
