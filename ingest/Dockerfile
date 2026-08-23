FROM python:3.12-slim

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

EXPOSE 8080
CMD ["python", "main.py"]
