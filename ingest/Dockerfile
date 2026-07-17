FROM python:3.12-slim

# 再開可能ダウンロード用に curl を入れる
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/chiezo-ingest

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py core.py ./
COPY sources ./sources

CMD ["python", "main.py"]
