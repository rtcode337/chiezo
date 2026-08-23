FROM python:3.12-slim

# healthcheck 用に curl を入れる
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/chiezo

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# **どのコミットが動いているかを画面に出すための情報。** タグは latest で上書きされ、
# デプロイ先が pull し忘れていても外からは見えないので、イメージ自身に持たせる。
# 渡さなければ「不明」と出るだけで、ビルドは通る(手元ビルド用)。
ARG BUILD_SHA=""
ARG BUILD_TIME=""
ENV CHIEZO_BUILD_SHA=$BUILD_SHA \
    CHIEZO_BUILD_TIME=$BUILD_TIME

# **root で動かさない。uid はホストに実在しない 10001。** 1000 はホストの1人目の
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

EXPOSE 7010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7010", "--workers", "2"]
