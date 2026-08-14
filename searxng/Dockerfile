# SearXNG に Chiezo 用の設定を焼き込んだイメージ。
#
# **設定ファイルをマウントしない形にするため**に作っている。素の searxng/searxng は
# `/etc/searxng/settings.yml` を要求するので、compose から `./searxng/settings.yml` を
# bind マウントしていた —— それだと**リポジトリを置けない環境(NAS のコンテナマネージャーに
# YAML を貼るだけの構成)では立てられない**。設定ごとイメージに入れれば、
# 単体定義からも `docker compose up -d` だけで立つ。
#
# 中身は素の SearXNG に settings.yml を 1 つ足しただけ。**上流のタグは固定する**
# (:latest にしない)—— 検索エンジンの実装は頻繁に変わり、黙って壊れると
# 「web 検索だけ効かない」という分かりにくい壊れ方をする。
FROM searxng/searxng:2026.7.31-057a77168

# 既定値を土台に、Chiezo から API として引くために要る分だけ上書きしたもの
# (`use_default_settings: true`。中身の理由は settings.yml のコメント)。
# **読み取り専用で置く。** 上流のエントリポイントはこのディレクトリを searxng ユーザーへ
# chown するが、ファイル自体を書き換える必要は無い。
COPY settings.yml /etc/searxng/settings.yml
