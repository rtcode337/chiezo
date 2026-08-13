# CLI ブリッジ。Claude Code / Codex CLI を OpenAI 互換の口に見せる(bridge/cli_bridge.py)。
#
# **1 つのイメージで両方を賄い、立ち上げるときに CHIEZO_BRIDGE_CLI で役割を決める。**
# 分けたほうが 1 つあたりは小さくなるが(圧縮後で claude 単体 225MB / codex 単体 234MB /
# 両方 459MB)、Chiezo は公開リポジトリで GHCR の容量・転送に制限が無く、
# **CI の入口とコードを 1 本に保てる**ほうが値打ちが大きいと判断した。
# 片方しか使わない環境で 200MB 強を惜しむなら、この Dockerfile を 2 つに割ればよい。
FROM node:20-slim

# python3: ブリッジ本体(FastAPI)。curl: healthcheck 用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

# 包む対象の CLI。認証情報は入れない(実行時に環境変数で受け取る)。
RUN npm install -g @anthropic-ai/claude-code @openai/codex

WORKDIR /srv/bridge

COPY requirements.txt .
# Debian の python3 は PEP 668 で保護されている。コンテナ丸ごとがこのアプリ専用なので、
# venv を挟まず素の環境へ入れる(--break-system-packages はそのための明示)。
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY cli_bridge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Codex の認証ファイルの置き場。中身は entrypoint.sh が実行時に書き、コンテナと一緒に捨てる。
ENV CODEX_HOME=/srv/bridge/.codex
ENV CHIEZO_BRIDGE_PORT=7013

EXPOSE 7013
CMD ["./entrypoint.sh"]
