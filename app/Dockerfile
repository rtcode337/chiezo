# ---- やること画面(Vue)を組む ----------------------------------------------
#
# 出来た成果物だけを下の段へ持っていくので、node は最終イメージに残らない。
# 画面と本体を同じイメージにしてあるのは、公開する面をコマンドだけで分けるためで、
# 成果物は 200KB 程度なので本体側に積んでも実害が無い(docs/tasks.md)。
FROM node:26-slim AS web
WORKDIR /build
# 依存の取得だけ先にやると、画面のソースを直しても層のキャッシュが効く
COPY tasks-frontend/package.json tasks-frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tasks-frontend/ ./
# ビルド番号(画面のフッターに出る)。渡さなければ nogit と出るだけ
ARG BUILD_SHA=""
ENV GIT_SHA=$BUILD_SHA
RUN npm run build


FROM python:3.12-slim

# healthcheck 用に curl を入れる
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/chiezo

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 画面の成果物。chiezo-tasks だけがここから配る(chiezo-app は口を持たない)
COPY --from=web /build/dist ./tasks-static

# どのコミットが動いているかを画面に出すための情報。 タグは latest で上書きされ、
# デプロイ先が pull し忘れていても外からは見えないので、イメージ自身に持たせる。
# 渡さなければ「不明」と出るだけで、ビルドは通る(手元ビルド用)。
ARG BUILD_SHA=""
ARG BUILD_TIME=""
ENV CHIEZO_BUILD_SHA=$BUILD_SHA \
    CHIEZO_BUILD_TIME=$BUILD_TIME

# root で動かさない。uid はホストに実在しない 10001。 1000 はホストの1人目の
# ユーザーの番号なので、それで動かすと万一コンテナから抜け出されたときにその人の
# ファイルを触れる立場になる。10001 ならホストの誰でもなく、chown で渡した data/ しか
# 触れない。compose の user: で上書きできる(data/ をホストから直接編集したいとき)。
#
# ホームを 1777 で用意するのは、別の uid を指定されたときに /etc/passwd に載らない
# ユーザーになり、HOME が / に落ちて書けなくなるため
RUN useradd --uid 10001 --user-group --home-dir /home/app --create-home chiezo \
    && chmod 1777 /home/app \
    && mkdir -p /data && chown 10001:10001 /data
ENV HOME=/home/app
USER 10001:10001

# 7010 = 知識ベース本体(LAN 内)、7015 = やること(外に出す面)。
# 同じイメージから起動し、compose 側で command を差し替えて使い分ける。
EXPOSE 7010 7015
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7010", "--workers", "2"]
