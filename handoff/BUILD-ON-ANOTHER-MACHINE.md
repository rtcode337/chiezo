# 別マシンで chiezo の DB を焼く手順

メモリの潤沢なマシン(以下「ビルド機」)で `.db` を作り、それを配信機へコピーするための手順書。
イメージは GHCR(`ghcr.io/rtcode337/chiezo-ingest`)から pull できるので、
このファイルだけ持っていけば完結する(リポジトリのソースは不要)。

chiezo の DB は**自己完結した単一の SQLite ファイル**なので、配布は「ファイルをコピーする」だけ。
export/import も、配信機でのビルドも要らない。SQLite のファイル形式は OS・CPU アーキ非依存なので、
Windows のビルド機で焼いて Linux の配信機で読ませてよい。

## ビルド機に必要なもの

ビルド機で速く焼くための手順書なので、取り込みには **`BUILD_PROFILE=fast`
(速度優先プロファイル)を付ける前提**で書く。下表の必要メモリはそのときの値
(付けなければ既定の `low_memory` になり、どのソースもメモリ 2 GiB で焼ける代わりに
osm が数倍〜10 倍遅い — それなら配信機で直接焼けばよく、この手順書の出番ではない)。

| 項目 | jawiki | osm_japan | geonames |
|---|---|---|---|
| メモリ(Docker が使える量) | 3 GiB 以上 | **12 GiB 以上**(RAM ノード索引) | 3 GiB 以上 |
| ディスク空き | **100 GB**(DB 42GB + VACUUM 中の複製 + ダンプ 11GB) | 15 GB | 40 GB |
| 所要時間の目安 | 4〜6 時間 | 2〜4 時間 | 1〜3 時間 |
| ネットワーク | ダンプを自分で落とすので必要(jawiki は約 11GB) | 約 2.5GB | 約 600MB |

どのソースも `fast` で **12 GiB のマシンがあれば構築できる**。
`geonames` は全世界の地名を 1 ソースで賄うためのソースで、ダンプが約 600MB と軽い
(その代わり店舗・営業時間は持たない。そこは `osm_<国>` の担当)。

取り込みは**開始前にメモリを検査し、足りなければ何もせず中止する**(ダウンロードも始めない)。
足りない旨のメッセージが出たら、下の「メモリが足りないと言われたら」を見ること。

> **Windows の場合**: 裏側が WSL2 になるため、既定では **ホストメモリの 50% しか** Docker から
> 使えない(32GB のマシンでも 16GB)。下の「ビルド機のセットアップ」の S2 を先に済ませること。

## ビルド機のセットアップ(Windows + WSL2 + Docker Engine)

ビルド機が素の Windows の場合。Docker Desktop は入れず、WSL2 の中に Docker Engine を入れる
(軽い・ライセンスを気にしなくてよい・後片付けが簡単)。

### S1. WSL2 を入れる

PowerShell(管理者)で:

```powershell
wsl --install          # 既定で Ubuntu が入る
```

再起動後、Ubuntu が起動して初回ユーザー名/パスワードを聞かれる。

### S2. メモリとディスクを設定する ← **ここが一番ハマる**

**メモリ**: WSL2 が使えるのは既定でホストの **50%**(32GB のマシンなら 16GB)。必要なのは
最大でも `osm_japan` の 12GiB なので、**32GB のマシンなら既定のままで足りる**。

**上限を上げすぎないこと。** `memory=` は予約ではなく上限だが、WSL2 は一度掴んだメモリを
なかなか Windows へ返さない。Windows 側が使うぶんまで WSL に握られるとページファイル行きになり、
マシン全体が重くなる(取り込みが遅くなるだけでなく、まさに避けたいホストのストールを招く)。
明示するなら実メモリの半分程度に留める:

```ini
[wsl2]
memory=16GB
processors=8

[experimental]
autoMemoryReclaim=gradual
```

`autoMemoryReclaim` は WSL が抱えたメモリを Windows へ徐々に返す設定(Windows 11 で利用可)。
書いたら PowerShell で `wsl --shutdown` してから WSL を開き直す(これをしないと反映されない)。
`free -g` の `available` が上表の必要メモリを超えているか確認する。

普段使いのマシンを一時的にビルド機にする場合は、**geonames(3GiB)→ jawiki(3GiB)→ osm_japan(12GiB)**
の順に進めるとよい。前 2 つは既定メモリで余裕があり、いちばん重い osm_japan だけ他のアプリを
閉じた状態で回せばよい。

**ディスク**: WSL の仮想ディスクは既定で **C ドライブ**にある。jawiki は 100GB 必要なので、
C の空きが足りないなら先に対処すること:

```powershell
# 空きを確認
wsl --shutdown
# 新しめの WSL なら distro ごと別ドライブへ移せる
wsl --manage Ubuntu --move D:\WSL
```

> **データフォルダを `/mnt/c` や `/mnt/d` に置かないこと。** Windows 側のドライブは WSL からは
> 9p/DrvFs 経由で見えており、**ネイティブの ext4 に比べ桁違いに遅い**。数十 GB の SQLite を
> ランダム書き込みする用途では実用にならない。データは必ず WSL の中(`~/chiezo-data` など)に置く。

### S3. Docker Engine を入れる

WSL(Ubuntu)の中で:

```bash
curl -fsSL https://get.docker.com | sudo sh      # 公式インストールスクリプト
sudo usermod -aG docker $USER                    # sudo 無しで docker を使えるように
exec newgrp docker                               # 今のシェルにグループを反映
sudo service docker start                        # デーモン起動

docker run --rm hello-world                      # 動作確認
```

**(任意)systemd を有効にすると docker を自動起動にできる。** WSL2 の init は既定では
systemd ではないため `systemctl` が使えず、上記のように WSL を開くたび
`sudo service docker start` を打つ必要がある。毎回打つのが面倒なら:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true
EOF
```

PowerShell で `wsl --shutdown` → 開き直してから `sudo systemctl enable --now docker`。
以降は WSL 起動時に docker も上がる。必須ではなく、WSL の起動が少し遅くなるだけの
トレードオフなので、好みで選んでよい(WSL 0.67.6 以降が必要。Windows 11 なら問題ない)。

### S4. 空きを確認してから始める

```bash
free -g            # available が上表の必要メモリを超えているか
df -h ~            # 空きが上表のディスク要件を超えているか
```

## 手順

### 1. イメージを用意する

ビルド機がインターネットに出られるなら GHCR から pull するだけ:

```bash
docker pull ghcr.io/rtcode337/chiezo-ingest:latest
```

**pull は毎回やること。** ローカルに古い `latest` が残っていると、タグは同じなので見た目では
気づけないまま古い版で焼いてしまう。そのイメージが作る DB の `schema_version` は、
取り込みを走らせずに聞ける(配信機の chiezo-api が期待する版より古いと新機能が使えないので、
数時間かける前にここで確かめる):

```bash
docker run --rm ghcr.io/rtcode337/chiezo-ingest:latest \
  python -c "import core; print(core.SCHEMA_VERSION)"
```

出られない場合は、別のマシンで `docker save` した `chiezo-ingest-image.tar.gz` をコピーして読み込む:

```bash
# 転送が壊れていないか確認(任意)
sha256sum -c chiezo-ingest-image.tar.gz.sha256

docker load -i chiezo-ingest-image.tar.gz
docker images ghcr.io/rtcode337/chiezo-ingest   # 読み込めたか確認
```

### 2. 作業用のデータフォルダを用意する

ダンプと DB の置き場。上表のディスク空きが要る。

```bash
mkdir -p /path/to/chiezo-data          # Windows なら D:\chiezo-data など
```

### 3. 取り込みを実行する

`docker compose` は不要(このイメージ単体で動く)。`-v` の左側を 2 で作ったフォルダにする。

**必ず `-d`(デタッチ)で起動すること。** 構築は数時間かかるため、前景(`--rm -it`)で回すと
ターミナルや VSCode を閉じた・スリープした瞬間にビルドごと消える(実際にこれで 2 回、
数時間ぶんが吹き飛んだ)。`-d` ならコンテナは docker デーモンの管理下で動き続ける。

```bash
# geonames(全世界の地名。いちばん軽いので最初の試運転に向く)
# BUILD_PROFILE=fast は速度優先(冒頭の表の前提)。付け忘れても失敗はしないが、
# 既定のメモリ優先(low_memory)になり osm が数倍〜10 倍遅くなる。
docker run -d --name chiezo-build \
  -e SOURCE=geonames \
  -e BUILD_PROFILE=fast \
  -e CHIEZO_DATA_DIR=/data \
  -v ~/chiezo-data:/data \
  ghcr.io/rtcode337/chiezo-ingest:latest

docker logs -f chiezo-build      # 進捗を見る(Ctrl-C で抜けてもビルドは続く)
docker wait chiezo-build         # 終了まで待つ(終了コードが返る)
docker rm chiezo-build           # 終わったら片付ける(次の SOURCE を回す前に必要)
```

`SOURCE` を `jawiki` / `osm_japan` に変えれば同じ形でそれぞれ構築できる。
`geonames` → `jawiki` → `osm_japan` の順に進めるのが安全(必要メモリが小さい順)。

OSM は日本以外の国も `osm_france` / `osm_thailand` のように国名を変えるだけで焼ける
(Geofabrik にある 195 の国・地域が定義済み。国ごとの必要メモリ・pbf サイズは配信機の
管理画面 `/admin/osm` で確認できる。多くの国は日本より軽く `fast` で 3〜12 GiB、
フランス・ドイツ・カナダ・アメリカ・ロシアだけは `fast` でもディスク索引が既定で
2 GiB・その代わり低速)。

Windows PowerShell から実行する場合、ボリューム指定は `-v D:\chiezo-data:/data` の形。
ただし前述のとおり **Windows 側のドライブを作業領域にすると極端に遅い**ので、WSL 内の
`~/chiezo-data` を使うこと。

途中で中断してよい。運用 DB は `.building` の一時ファイルに作られ、検証を通ってから
初めて本番のファイル名に切り替わるので、壊れかけの DB が残ることはない。再実行すれば最初からやり直す。

### 4. 出来た .db を回収する

```bash
ls -la /path/to/chiezo-data/
# jawiki-20260701.db         ← これが成果物(世代ファイル)
# jawiki.db -> jawiki-...db  ← 同じ中身を指すシンボリックリンク
```

**世代ファイルのほう**(`jawiki-<日付>.db`)をコピーして持ち帰る。42GB あるので USB か LAN 転送で。
`data/dumps/` のダンプは持ち帰らなくてよい(再取得できる)。

コンテナは root で動くため、出来た `.db` の所有者は root になる。WSL から取り出すなら:

```bash
sudo chown $USER: ~/chiezo-data/jawiki-*.db     # 自分のものにしておくと扱いやすい
```

Windows 側からは、エクスプローラで `\\wsl$\Ubuntu\home\<ユーザー名>\chiezo-data\` を開けば
そのまま見える(VSCode を入れているなら Remote-WSL で開いてドラッグしてもよい)。
なお **Windows へコピーする瞬間だけは遅い**が、これは 1 回きりなので気にしなくてよい
(遅くて困るのは、ビルド中ずっと `/mnt/*` を使ってしまう場合)。

### 5. 配信機へ配置する

配信機の data ディレクトリに置き、`<ソース名>.db` として見えるようにする(chiezo-api が数秒以内に自動で読み込む。再起動は不要)。

```bash
cp jawiki-20260701.db /path/to/chiezo/data/
cd /path/to/chiezo/data
ln -sfn jawiki-20260701.db jawiki.db    # シンボリックリンクが使えない環境ならリネームでよい
curl -s http://localhost:9000/v1/sources   # 数秒待って新しい dump_date / schema_version が出れば成功
```

配信側(chiezo-api)は読み取り専用の immutable SQLite を開くだけなので、**メモリ数百 MB の小型機でも動く**。
効いてくるのはメモリではなくディスク(jawiki なら 42GB の空きが要る)。

### 6. ビルド機の後片付け

普段使いのマシンを借りたなど、余計なものを残したくない場合:

```bash
docker rmi ghcr.io/rtcode337/chiezo-ingest:latest
rm -rf /path/to/chiezo-data
```

取り込みが触るのは**公開ダンプ(Wikimedia / Geofabrik)と、指定した data フォルダだけ**。
認証情報や個人ファイルは一切読まないし、外へ送信もしない。残るのは Docker 本体と上記 2 つだけ。

## メモリが足りないと言われたら

```
not enough memory to build osm_japan: 2.0 GiB available < 12.0 GiB required.
```

対処は 3 つ:

1. **メモリの多いマシンで焼く**(本手順書の想定。いちばん速い)
2. **`BUILD_PROFILE=fast` を外す** — 既定のメモリ優先(`low_memory`)なら**どのソースも
   2GiB で焼ける**。構築用 SQLite キャッシュを絞り、osm はノード座標索引をディスクに
   置くため、osm はノード解決がランダム読みになり**数倍〜10 倍遅くなる**
   (実測で日本抽出が数時間 → 十数時間。wikipedia / geonames はほぼ変わらない)。
   大陸単位の OSM(旧 `osm_europe`)は廃止した。全世界の地名は `geonames` が 1 ソースで賄う。
3. **検査を上書きする** — 見積もりが実態と合っていないと分かっている場合のみ。
   `BUILD_MEMORY_GB=<n>` で必要量を変える、`SKIP_MEMORY_CHECK=1` で検査自体を飛ばす。
   足りないまま走らせると数時間かけた末に OOM killer に殺される(共有ホストでは他のプロセスも
   道連れにする)ので、安易に使わないこと。

## 主な環境変数

| 変数 | 説明 |
|---|---|
| `SOURCE` | 取り込むソース名(必須。`jawiki` / `geonames` / `osm_<国>`。例: `osm_japan` `osm_france`) |
| `CHIEZO_DATA_DIR` | データディレクトリ(コンテナ内パス。`-v` の右側と合わせる。既定 `/data`) |
| `DUMP_DATE` | ダンプ日付 `YYYYMMDD` を固定(省略時は最新を自動検出) |
| `DUMP_FILE` | ダウンロードを飛ばして既存ファイルを使う(例: 手で置いたダンプを使う) |
| `BUILD_PROFILE` | 構築プロファイル。既定 `low_memory` = どのソースも 2GiB で焼ける(osm は数倍〜10 倍遅い)。ビルド機では `fast`(速度優先)を明示する |
| `OSM_NODE_INDEX` | osm のノード座標索引の置き場。`sparse_mmap_array`(RAM・速い)/ `sparse_file_array`(ディスク・省メモリ・遅い)。明示指定は `BUILD_PROFILE` より優先 |
| `BUILD_MEMORY_GB` / `SKIP_MEMORY_CHECK` | メモリ検査の上書き / 無効化 |
| `OSM_AREA_ADMIN_LEVEL` | `extra.area` に入れる行政区の admin_level(既定 4 = 都道府県、`0` で省略) |
| `GEONAMES_ALT_LANGS` | geonames で取り込む別名の言語(既定 `ja,en`。`*` で全 400 言語超) |
| `GEONAMES_FEATURE_CLASSES` | geonames で取り込む feature class(既定 `AHLPSTUV` = 道路 `R` 以外) |
