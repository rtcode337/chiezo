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

EXPOSE 7010
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7010", "--workers", "2"]
