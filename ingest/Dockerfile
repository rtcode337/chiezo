FROM python:3.14-slim

# curl: 再開可能ダウンロード用。libexpat1: pyosmium(osm 系ソース)の実行時依存
RUN apt-get update && apt-get install -y --no-install-recommends curl libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/chiezo-ingest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py core.py server.py lookup.py ./
COPY sources ./sources

# 既定は one-shot 取り込み(chiezo-ingest プロファイル)。
# chiezo-trigger サービスは docker-compose.yml の command で server:app 起動に上書きする。
EXPOSE 8080
CMD ["python", "main.py"]
