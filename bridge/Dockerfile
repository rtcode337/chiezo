# CLI ブリッジ。Claude Code / Codex CLI / Antigravity CLI / Gemini CLI を
# OpenAI 互換の口に見せる (bridge/cli_bridge.py)。
#
# 1 つのイメージで全部を賄い、立ち上げるときに CHIEZO_BRIDGE_CLI で役割を決める。
# イメージは 1 回 pull すればディスクは 1 つぶんで、コンテナを何個立てても増えるのは
# 書き込み層(数 KB)だけ —— CLI ごとにイメージを分けるより、まとめて 1 枚のほうが得。
#
# node は最終イメージに入れない。 claude は自己完結バイナリ、codex も
# vendor の実体バイナリを直接叩けるので、node が要るのは npm install のときだけ。
# 以前は node:20-slim をそのまま実行ステージにしていて、使わない node と npm で
# 圧縮後 41MB を配っていた。
#
# 以前は Gemini CLI(JS バンドルなので実行にも node が要る)のために node を持ち込んで
# いたが、個人向けの提供が 2026-06-18 に終了したので落とした(後継は Antigravity CLI)。
# そのぶんイメージは 1.56GB → 1.27GB に戻る。

# ---- CLI を取り出すためだけのステージ ----
FROM node:20-slim AS cli

RUN npm install -g @anthropic-ai/claude-code @openai/codex

# Antigravity CLI は単一の Go バイナリで、npm では配布されていない。
# インストーラが受け付けるのは --dir と --help だけ(--skip-aliases 等は無い)。
#
# ca-certificates を先に入れる(node:20-slim には入っていない)。無いと curl が
# 証明書を開けずに失敗する。そして `curl | bash` は curl が落ちても成功扱いになるので、
# 一度ファイルへ落としてから実行し、最後に agy が動くことまで確かめる ——
# ここを素通りさせると、CLI の入っていないイメージが黙って出来上がる(実際に踏んだ)。
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh; \
    bash /tmp/agy-install.sh --dir /usr/local/bin; \
    /usr/local/bin/agy --version; \
    rm -f /tmp/agy-install.sh; \
    rm -rf /var/lib/apt/lists/*

# ---- 実行ステージ ----
# node:20-slim と同じ Debian 12(bookworm)にする。 claude.exe はそこでビルドされた
# glibc に動的リンクしているので、ディストリビューションを跨ぐと動かなくなる。
FROM debian:bookworm-slim

# python3: ブリッジ本体(FastAPI)。curl: healthcheck 用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

# CLI 4 種。認証情報は入れない(実行時に設定 DB か環境変数から受け取る)。
COPY --from=cli /usr/local/lib/node_modules/@anthropic-ai/claude-code \
                /usr/local/lib/node_modules/@anthropic-ai/claude-code
COPY --from=cli /usr/local/lib/node_modules/@openai/codex \
                /usr/local/lib/node_modules/@openai/codex
COPY --from=cli /usr/local/bin/agy /usr/local/bin/agy

# npm が張るのと同じ symlink を自分で張る。codex は **node を通す codex.js ではなく
# vendor の実体バイナリ**を指す(node を最終イメージに置かないため)。
RUN set -eux; \
    ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude; \
    ln -s /usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex \
          /usr/local/bin/codex

WORKDIR /srv/bridge

COPY requirements.txt .
# Debian の python3 は PEP 668 で保護されている。コンテナ丸ごとがこのアプリ専用なので、
# venv を挟まず素の環境へ入れる(--break-system-packages はそのための明示)。
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY cli_bridge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Codex の認証ファイルの置き場。中身は entrypoint/server が実行時に書き、コンテナと一緒に捨てる。
ENV CODEX_HOME=/srv/bridge/.codex
# Antigravity は API キー方式を持たず、コンテナ内で 1 回サインインした結果を
# ホーム配下のキャッシュに置く。**ここを書き込み可能なボリュームにバインドすれば
# コンテナを作り直しても消えない**(compose がそうしている)。
ENV HOME=/srv/bridge/home
ENV CHIEZO_BRIDGE_PORT=7013

# 非 root で動かす。 claude は権限確認を飛ばす指定(--permission-mode
# bypassPermissions)を root では拒む ——
#   --dangerously-skip-permissions cannot be used with root/sudo privileges
# 非対話で動かす以上この指定は外せないので、root のままだと使えない。
# しかも `claude auth status` は root でも通るため、**管理画面の「接続を試す」は
# 成功するのに生成だけが 502 で落ちる**という分かりにくい壊れ方をする(実際に踏んだ)。
#
# uid はホストに実在しない 10001。 1000 はホストの1人目のユーザーの番号なので、
# それで動かすと万一コンテナから抜け出されたときにその人のファイルを触れる立場になる。
# サインイン結果を残すホームは名前付きボリュームにしてあり(compose 参照)、
# その場合はイメージ側の所有者がそのまま入るので、ホスト側で chown して回らなくてよい。
# ホストのディレクトリをバインドするなら、先に 10001 へ chown すること。
RUN useradd --uid 10001 --home-dir /srv/bridge/home --shell /usr/sbin/nologin bridge \
    && mkdir -p /srv/bridge/.codex /srv/bridge/home \
    && chmod 700 /srv/bridge/.codex \
    && chown -R bridge:bridge /srv/bridge
USER bridge

EXPOSE 7013
CMD ["./entrypoint.sh"]
