FROM python:3.12-slim

# curl: 再開可能ダウンロード用。libexpat1: pyosmium(osm 系ソース)の実行時依存
RUN apt-get update && apt-get install -y --no-install-recommends curl libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/chiezo-ingest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py core.py ./
COPY sources ./sources

CMD ["python", "main.py"]
