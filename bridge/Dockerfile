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
# **node の版が CLI の版の天井になる。** npm は engines に合う中でいちばん新しい版を
# 選ぶので、古い node のままだと**黙って古い CLI が入る** —— claude-code は 2.1.198 から
# `node >= 22` を要求していて、node 20 では 2.1.197 で頭打ちになっていた
# (下限を付けて初めて気づいた。それまでは「キャッシュのせい」に見えていた)。
# **実行ステージと同じ Debian(bookworm)のタグを選ぶ** —— claude.exe はここで
# ビルドされた glibc に動的リンクしているので、ディストリビューションを跨ぐと動かない。
FROM node:26-bookworm-slim AS cli

# **版は固定する**(`@<版>`)。範囲で入れると、ビルドした日によって中身が変わり、
# 「何も変えていないのに道具の扱いが変わった」を追えなくなる —— ブリッジは CLI の
# 権限まわりのフラグ(`--tools` / `--permission-mode` / `--disable`)に頼って道具を
# 絞っているので、黙って版が動くのがいちばん困る。上げるときはこの 2 行を書き換える
# (Dependabot は npm install の版を追わないので、人が上げる)。
# 以前は「下限だけ持って最新を入れる」形で、ビルドキャッシュに当たると古いまま
# 出来上がる罠を下限の検査で避けていた。固定すれば ARG が層の鍵になるので要らない。
#
# 選び方の目安(実測の記録):
# codex … 0.156.1 より古いと、新しいモデルを名指ししても
# 「The '…' model requires a newer version of Codex.」で 400 になる。alpha は選ばない。
# claude … 別名(`opus` など)が指す先は CLI が決めるので、古いと新しい世代を選べない。
# `opus` が Opus 5.5 を指すのは 2.1.280 から(CHANGELOG)。print モードの `/usage` が
# 枠を返すのは 2.1.267 から。原則 `stable` のタグの版を選ぶ(`latest` は `next` と同じ版を
# 指していることがあり、巻き戻されることがある)。
# 固定した日(2026-10)の `stable` / `latest`: claude 2.1.287 / codex 0.162.1。
ARG CLAUDE_VERSION=2.1.287
ARG CODEX_VERSION=0.162.1
RUN set -eux; \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" "@openai/codex@${CODEX_VERSION}"; \
    installed="$(codex --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1)"; \
    echo "codex ${installed} (pinned ${CODEX_VERSION})"; \
    test "${installed}" = "${CODEX_VERSION}"; \
    claude_version="$(node -p "require('/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json').version")"; \
    echo "claude ${claude_version} (pinned ${CLAUDE_VERSION})"; \
    test "${claude_version}" = "${CLAUDE_VERSION}"

# Antigravity CLI は単一の Go バイナリで、npm では配布されていない。
# インストーラが受け付けるのは --dir と --help だけ(--skip-aliases 等は無い)。
#
# ca-certificates を先に入れる(node の slim には入っていない)。無いと curl が
# 証明書を開けずに失敗する。そして `curl | bash` は curl が落ちても成功扱いになるので、
# 一度ファイルへ落としてから実行し、最後に agy が動くことまで確かめる ——
# ここを素通りさせると、CLI の入っていないイメージが黙って出来上がる(実際に踏んだ)。
#
# インストーラは常に最新版を入れる(版の指定は無い)が、この RUN は入力が変わらないので
# ビルドキャッシュに当たると古い agy のまま出来上がる。AGY_MIN_VERSION を上げると
# 層が作り直され、入った版がそれ未満なら失敗させる —— 「版を上げたつもりで古いまま」を
# 防ぐための仕掛けで、版そのものが直してくれるわけではない。
# **agy だけは版を固定できない**(インストーラに版の指定が無い)。下限の検査で
# 「古いまま出来上がる」だけは防ぎ、上げるときは AGY_MIN_VERSION を書き換える。
# 「invalid arguments: - missing properties 'toolSummary', 'toolAction'」は 1.1.13 でも
# 1.1.27 でも出た。あれは版ではなく権限の確認の問題で、cli_bridge.py の
# build_command が `--dangerously-skip-permissions` を常に付けることで避けている
# (agy には道具を名指しで許す口が無い。守りはマウントとネットワークで取る)。
ARG AGY_MIN_VERSION=1.2.14
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh; \
    bash /tmp/agy-install.sh --dir /usr/local/bin; \
    installed="$(/usr/local/bin/agy --version | tr -d '[:space:]')"; \
    echo "agy ${installed} (required >= ${AGY_MIN_VERSION})"; \
    test "$(printf '%s\n%s\n' "${AGY_MIN_VERSION}" "${installed}" | sort -V | head -n1)" = "${AGY_MIN_VERSION}"; \
    rm -f /tmp/agy-install.sh; \
    rm -rf /var/lib/apt/lists/*

# ---- 実行ステージ ----
# 取り出しのステージと同じ Debian 12(bookworm)にする。 claude.exe はそこでビルドされた
# glibc に動的リンクしているので、ディストリビューションを跨ぐと動かなくなる。
FROM debian:bookworm-slim

# python3: ブリッジ本体(FastAPI)。curl: healthcheck 用。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip curl \
    && rm -rf /var/lib/apt/lists/*

# CLI。認証情報は入れない(実行時に、chiezo-app が書き出した写しか環境変数から受け取る)。
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

COPY cli_bridge.py talk_bridge.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Codex の認証ファイルの置き場。中身は entrypoint/server が実行時に書き、コンテナと一緒に捨てる。
ENV CODEX_HOME=/srv/bridge/.codex
# Antigravity は API キー方式を持たず、コンテナ内で 1 回サインインした結果を
# ホーム配下のキャッシュに置く。**ここを書き込み可能なボリュームにバインドすれば
# コンテナを作り直しても消えない**(compose がそうしている)。
ENV HOME=/srv/bridge/home
ENV CHIEZO_BRIDGE_PORT=7013

# どのコミットのイメージが動いているか。**ブリッジは LAN に口を開けない**ので、
# 動いている版を外から確かめる手段がこれしかない —— 版の食い違いに気づけないと、
# 直したはずの不具合を追いかけ続けることになる(実際に 1 往復無駄にした)。
# CI がすべてのイメージへ同じ build-arg を渡している。
ARG BUILD_SHA=""
ARG BUILD_TIME=""
ENV CHIEZO_BRIDGE_BUILD_SHA=$BUILD_SHA \
    CHIEZO_BRIDGE_BUILD_TIME=$BUILD_TIME

# 非 root で動かす。 中で動くのは道具を持ったエージェントなので、抜け出されたときに
# 触れる範囲を狭くしておく。agy の `--dangerously-skip-permissions` も root では
# 拒まれうる(claude が以前 `bypassPermissions` を使っていた頃は、root だと
# `--dangerously-skip-permissions cannot be used with root/sudo privileges` で
# 生成だけが 502 になった。`claude auth status` は通るので気づきにくい)。
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

EXPOSE 7013 7016
CMD ["./entrypoint.sh"]
