#!/usr/bin/env bash
# テストを実行する。手元に依存が揃っていればそのまま、無ければ Docker で回す。
#
#   scripts/run_tests.sh                          # 全テスト
#   scripts/run_tests.sh tests/test_notes.py -v   # 引数はそのまま pytest へ渡る
#   CHIEZO_TEST_RUNNER=docker scripts/run_tests.sh  # 手元の環境を無視して Docker で
#   CHIEZO_TEST_RUNNER=local  scripts/run_tests.sh  # Docker へ落ちずに失敗させる
#
# Chiezo は **Python 3.12** 前提(api/Dockerfile・ingest/Dockerfile・CI がその系列)で、
# ホストの python はそれより新しいことがある。依存には C 拡張(pyosmium, pydantic-core)が
# 含まれるので、バージョンが違うと import から落ちる。Docker 経路はそのときの逃げ道で、
# CI と同じ Python・同じ requirements で回すためのもの。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RUNNER=${CHIEZO_TEST_RUNNER:-auto}
IMAGE=${CHIEZO_TEST_IMAGE:-chiezo-tests:local}
BASE_IMAGE=${CHIEZO_TEST_BASE_IMAGE:-python:3.12-slim}

# 引数が無ければ tests/ 全部。あるものはそのまま pytest の引数として渡す。
if [ "$#" -eq 0 ]; then
    set -- tests/
fi

# 依存が揃っている python を探す(.venv があればそれを優先)。
# 一部だけ入った環境を掴むと collect の途中で落ちるので、実際に import して確かめる。
find_local_python() {
    for py in "$ROOT/.venv/bin/python" python3 python; do
        command -v "$py" >/dev/null 2>&1 || continue
        if "$py" -c 'import pytest, fastapi, mcp, osmium, mwparserfromhell' >/dev/null 2>&1; then
            echo "$py"
            return 0
        fi
    done
    return 1
}

run_in_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "テストを実行できません: 手元に依存が揃っておらず docker も見つかりません。" >&2
        echo "README「開発」節の venv を作るか、docker を入れてください。" >&2
        exit 1
    fi

    # 依存だけを焼いたイメージを作る(requirements が変わらなければ層のキャッシュで一瞬)。
    # ビルドコンテキストはロック 1 つだけの一時ディレクトリにする —— リポジトリの
    # ルートを渡すと data/ の .db(数十 GB)まで docker daemon へ送ることになるため。
    # trap はスクリプト終了時に呼ばれる(= 関数の外)ので、変数はグローバルにしておく。
    ctx=$(mktemp -d)
    trap 'rm -rf "${ctx:-}"' EXIT
    # api と ingest の依存 + pytest + ruff を 1 本にまとめたロック(CI と同じもの)。
    # 2 つのロックを同時に渡すと、共通の依存(fastapi 等)が二重指定になって pip が断る。
    cp "$ROOT/requirements-dev.txt" "$ctx/requirements-dev.txt"
    cat >"$ctx/Dockerfile" <<EOF
FROM $BASE_IMAGE
# libexpat1: pyosmium の実行時依存(ingest/Dockerfile と同じ理由)
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \\
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
EOF
    docker build -t "$IMAGE" "$ctx" >/dev/null

    # リポジトリはバインドマウント(コピーではないので data/ が大きくても関係ない)。
    # __pycache__ が root 所有で残らないよう、実行ユーザーは呼び出し元に合わせる。
    docker run --rm \
        -u "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$ROOT:/w" -w /w \
        "$IMAGE" python -m pytest "$@"
}

case "$RUNNER" in
    local)
        py=$(find_local_python) || {
            echo "手元の python に依存が揃っていません(pytest / fastapi / mcp / osmium / mwparserfromhell)。" >&2
            exit 1
        }
        exec "$py" -m pytest "$@"
        ;;
    docker)
        run_in_docker "$@"
        ;;
    auto)
        if py=$(find_local_python); then
            exec "$py" -m pytest "$@"
        fi
        echo "手元に依存が見つからないので Docker($BASE_IMAGE)で実行します。" >&2
        run_in_docker "$@"
        ;;
    *)
        echo "CHIEZO_TEST_RUNNER は auto / local / docker のいずれか(受け取った値: $RUNNER)" >&2
        exit 1
        ;;
esac
