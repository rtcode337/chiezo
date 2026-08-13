# 使う(AI と話す。既定では無効)

**Chiezo を引ける AI と、ブラウザから話せます**(`/ai/chat`)。1 問 1 答の口(`/v1/ask`)と
会話の口(`/v1/chat`)があり、根拠にした文書は出典として併記します。

**話す相手は AI(使っているモデル)で、Chiezo はその AI が引く知識**です。画面の見出しにも
モデル名が出ます(`AI(Qwen3-8B)と話す`)。Chiezo は AI のための知識ベースで、その AI を
Claude Code の代わりにローカル LLM で立てて同居させたのがこの層、という関係です
(だから既定では無効で、Chiezo 本体は今までどおり外を叩きません)。

**この層は知識ベース本体の機能ではなく、「Chiezo を上手に使う側」をこのリポジトリが用意して
いるものです。** `scripts/gen_claude_config.sh` が Claude Code 用の設定(いつ Chiezo を使うか・
どう引くか)を配るのと同じ考え方で、どう引けば当たるか —— 短い語は前方一致に落ちる、
カテゴリの列挙は `filter?tag=` を使う —— をいちばん知っているのはここなので、道具立てと
プロンプトもここで持ちます。推論そのものは chiezo-api の中では動かさず、
**OpenAI 互換 API を喋る別プロセス**に任せます(配信側 chiezo-api がメモリ数百 MB で
動く前提を壊さないため)。

有効になるのは `CHIEZO_LLM_URL` を設定したときだけです。設定しなければ `/v1/ask` は 503 を返し、
管理画面にも無効と表示されます。設計の背景は
[設計メモ](design-notes.md#使う層はなぜ-2-段の-rag-か)が正です。

## 使いはじめる

推論サーバと検索エンジンの**コンテナは `docker-compose.answer.yml` に外出し**してあります
(chiezo-api 側の設定は本体の `docker-compose.yml` にあります)。重ねなければコンテナは
立たず、Chiezo は検索 API・MCP として動きます。

```bash
cp .env.example .env
# .env の CHIEZO_LLM_URL=http://chiezo-llm:7011/v1 の行のコメントを外す

docker compose -f docker-compose.yml -f docker-compose.answer.yml --profile answer up -d
docker compose logs -f chiezo-llm     # 初回はモデルのダウンロード(約 2.5GB)
```

毎回 `-f` を並べるのが煩わしければ、`.env` に
`COMPOSE_FILE=docker-compose.yml:docker-compose.answer.yml` と書けば以後は
`docker compose --profile answer up -d` で済みます。

起動するのは推論サーバ(`chiezo-llm` = llama.cpp の `llama-server`)と
検索エンジン(`searxng`)の 2 つです。**推論を LAN の別マシン(Ollama 等)に任せるなら
このファイルは要りません** —— `CHIEZO_LLM_URL` にその URL を書くだけで、本体だけで動きます。

モデルは起動時に Hugging Face から取得して `./models` にキャッシュするので、
2 回目以降のダウンロードはありません。`chiezo-trigger` と同じくホストへポートを公開せず、
chiezo-api からのみ内部ネットワーク経由で到達します(別ホストの chiezo-api から使うなら
`docker-compose.lan.yml` を重ねます)。

## 話す(ブラウザ)

**`/ai/chat`**(管理画面からも辿れます)。見出しにはいま話しているモデルの名前が出ます
(`CHIEZO_LLM_MODEL` が未設定なら推論サーバに問い合わせます)。入力欄は数行ぶんの高さがあり
(Enter で送信・Shift+Enter で改行)、**ソース・引き方・根拠・web 検索の切り替えはその下**に
並びます。会話として続けられるので、「じゃあ京都のほうは?」
「さっきの寺の最寄り駅は?」が通じます。**会話の履歴を持つのはブラウザ側**で、送るたびに
まるごとサーバーへ渡します(サーバーは会話の状態を持ちません。読み取り専用・LAN 内・
複数ワーカーという前提を崩さないため)。回答まで数十秒かかるので、この画面だけは
JavaScript で逐次表示します。JavaScript が無い環境向けに 1 問 1 答の画面もあります。

## 使い方(curl)

```bash
BASE=http://<サーバーIP>:7010

# 1 問 1 答
curl -sG "$BASE/v1/ask" --data-urlencode "q=浅草寺はどこにある?" | jq .
curl -sG "$BASE/v1/ask" --data-urlencode "q=浅草寺の歴史は?" -d source=jawiki   # ソースを固定
curl -sG "$BASE/v1/ask" --data-urlencode "q=浅草寺はどこにある?" -d stream=1     # SSE で流す

# 会話(履歴を毎回まるごと送る。末尾が今回の発言)
curl -s "$BASE/v1/chat" -H 'Content-Type: application/json' -d '{
  "messages": [
    {"role": "user", "content": "浅草寺について教えて"},
    {"role": "assistant", "content": "東京都台東区の寺院です。"},
    {"role": "user", "content": "じゃあ京都のほうは?"}
  ],
  "mode": "agent", "grounded": false
}' | jq .
```

```json
{
  "question": "浅草寺はどこにある?",
  "answer": "浅草寺は東京都台東区浅草にある寺院です [1]。",
  "references": [
    {"n": 1, "source": "jawiki", "title": "浅草寺", "doc_id": 3, "url": "/search/jawiki/doc/3"}
  ],
  "queries": [{"source": "jawiki", "q": "浅草寺"}],
  "model": "chiezo"
}
```

- `q` — 質問文(自然文でよい)。`/v1/chat` では `messages` の末尾が今回の発言、それより前が履歴
- `source` — 引くソースを固定する(省略時はどのソースを引くか LLM が選ぶ)
- `grounded` — 回答方針。`1` は Chiezo で取れたことだけを根拠にし、根拠が無ければ答えません。
  `0` にすると足りない部分をモデル自身の知識で補います(Chiezo 由来の部分にだけ出典番号が付きます)。
  **これはモデルの幻覚への対処であって Chiezo の制約ではない**ので、用途に応じて選んでください
- `mode` — `rag`(1 回検索して答える)か `agent`(モデルに道具を引かせる。下記)
- `web` — agent モードで web 検索の道具を渡すか。省略時はサーバー設定どおり。`0` にすると**そのやり取りだけ Chiezo に閉じます**(設定していない環境では `1` にしても使えません)
- `notes` — agent モードで「覚える・思い出す」の道具を渡すか。同じく省略時はサーバー設定どおりで、`0` で切れます(**Chiezo で唯一の書き込み**なので、切れることが要ります)
- `stream=1` — `text/event-stream` で返す。`references`(出典。本文より先に確定するので先に届く)
  → `delta`(本文の差分)× n → `done` の順。途中で推論側が落ちたら `error` イベントが挟まります

`mode` と `grounded` の**既定は環境変数で決められます**(`CHIEZO_ASK_DEFAULT_MODE` /
`CHIEZO_ASK_DEFAULT_GROUNDED`)。素の既定は `rag` + `grounded=1` で、これは小さな機械でも
安全に動く側に倒したものです。GPU で 8B 級を動かしているなら `.env` で
`agent` + `grounded=0` にすると、**普通に会話している感じ**になります(必要なときだけ自分で
Chiezo を引き、雑談は雑談として返る)。

`grounded=1` で抜粋が 1 件も取れなかった場合は、**推論を走らせずに**「抜粋からは分かりません」を
返します(小型モデルは抜粋が空でも自分の知識で答えてしまうため、プロンプトに委ねず経路として
断っています。実測は[設計メモ](design-notes.md#使う層はなぜ-2-段の-rag-か)を参照)。

本文中の `[1]` のような番号は `references` の `n` に対応します。**`references` が空のときは
本文の番号に意味がないので無視してください** — 小型モデルは根拠が無くても番号を書くことがあります。

答えの作り方は 2 段です。まず質問文から検索クエリを組み立て(質問文をそのまま全文検索に
入れても当たらないため)、その結果の上位文書の本文を抜粋してから答えさせます。

## agent モード(モデルに道具を引かせる)

既定の `rag` は **`search` を 1 回**引いて終わりなので、Chiezo の強い道具に手が届きません。
`mode=agent` を付けると、`search` / `doc` / `filter` / `tags` / `titles` / `links` を
**モデル自身に**引かせます(道具の定義も実行も MCP と同じものを使うので、Claude Code から
使うときと同じ道具立てです)。notes が有効なら **`remember` / `recall`**(覚える・思い出す)、
web 検索を設定してあれば **`web_search`** も加わります。

```bash
curl -sG "$BASE/v1/ask" -d mode=agent --data-urlencode "q=カテゴリ「東京都の寺」の記事は何件ある?" | jq .
```

rag では原理的に答えられなかった問いに届きます:

| 質問 | agent が使う道具 |
|---|---|
| カテゴリ「○○」の記事は何件ある? | `tags` で正式な名前 → `filter` の `total` |
| 京都府の博物館を挙げて | `filter?feature=tourism=museum&area=京都府` |
| 浅草寺の最寄り駅は? | jawiki の `doc` で座標 → osm の `filter?bbox=…&feature=railway=station` |

応答には `queries` の代わりに `steps`(どの道具を何の引数で呼び、何が返ったか)が入ります。
`stream=1` なら道具を呼ぶたびに `step` イベントが流れ、`references` → `delta` → `done` と続きます
(ブラウザの `/ai/chat` でも「調べた手順」として出ます)。出典は**本文中の番号ではなく**、
道具の応答に出てきた文書の一覧です(生の応答に番号を振る先が無いため)。

**ツール呼び出しが安定するモデル(8B 級以上)と GPU が実質の前提です。** 4B 未満は
引数を間違える・同じ検索を繰り返すが普通に起き、CPU では 1 問が分単位になります。
既定が `rag` のままなのはそのためで、`mode=agent` は明示的に選んだときだけ使われます。

| 変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_AGENT_MAX_STEPS` | `6` | 道具を呼べる回数。使い切ったら道具なしでもう 1 回だけ聞いて答えさせる |
| `CHIEZO_AGENT_TOOL_CHARS` | `3000` | 1 回の道具の結果をモデルに返す上限文字数 |
| `CHIEZO_AGENT_TIMEOUT` | `180` | ループ全体の締め切り(秒) |

必要なコンテキスト長の目安は **`MAX_STEPS` × `TOOL_CHARS`** です(既定なら 16k 以上を推奨)。
どこまで解けて何が解けないかの実測は
[設計メモ](design-notes.md#agent-モード-道具をモデルに引かせる)にあります。

## web 検索で足りないぶんを補う(既定では無効)

agent モードに `web_search` の道具を足せます。**Chiezo に無いこと**(取り込んだダンプより
新しい出来事、いま現在の状態)を聞かれたときだけモデルが使います。

```bash
# .env
CHIEZO_WEB_SEARCH_URL=http://searxng:7012/search   # 同居(--profile answer で立つ)
#CHIEZO_WEB_SEARCH_URL=http://<立てたホストのIP>:7012/search  # LAN の別ホストのもの
#CHIEZO_WEB_SEARCH_PROVIDER=searxng                # searxng(既定)/ brave
#CHIEZO_WEB_SEARCH_API_KEY=                        # brave のときだけ
#CHIEZO_WEB_SEARCH_RESULTS=5                       # 1 回に見る件数
```

ポートは**内も外も 7012**です(7010 = API・7011 = 推論の隣に揃えてあります)。同居なら
サービス名で、別ホストなら `docker-compose.lan.yml` が公開する 7012 を指します。

これは **Chiezo 本体ではなく「使う」層(= Chiezo を使う側)の機能**です。知識ベースそのものは
引き続き外へ出ません。とはいえ外に出る以上は、次を守っています。

- **どれが web 由来か必ず分かる**。出典の `source` が `web` になり、URL が付きます
- **本文は取りに行かない**。返すのはタイトル・要約・URL だけです(スクレイピングはしません)
- **自分でレート制限をかける**。`User-Agent` はプロジェクト名だけで、連絡先や個人名は載せません
- **Chiezo が先**。プロンプトで順番を固定しています(web は足りないぶんだけ)

### 検索エンジン(SearXNG)

**立てる手順はありません。`--profile answer` を付けた時点で推論サーバと一緒に立ちます**
(`docker-compose.answer.yml`)。web 検索を使うかどうかは、上の `CHIEZO_WEB_SEARCH_URL` を
書くかどうかだけで決まります —— 道具を足すたびに起動コマンドが増えるほうが混乱するため、
起動は一本にして、使うかは設定で切り分けています。

設定は `searxng/settings.yml` に入っています(リポジトリに同梱)。**SearXNG の既定は
HTML しか返さない**ので、そこで `search.formats` に `json` を足してあります。無いと
Chiezo 側は「JSON ではない」というエラーとして扱います。

```bash
# 動いているか確かめる(コンテナの外から見るなら docker-compose.lan.yml を重ねて 7012)
docker compose exec searxng wget -qO- "http://localhost:7012/search?q=test&format=json" | head -c 200
```

`settings.yml` の `secret_key` は**秘密の値ではありません**(プリファレンスの Cookie と
画像プロキシの署名用で、Chiezo は JSON の検索しか叩きません)。気にするなら `.env` の
`CHIEZO_WEB_SEARCH_SECRET` で置き換えてください。`limiter: false` は API として叩くための
設定で、**LAN 内前提**です。SearXNG 以外(Brave)を使うなら
`CHIEZO_WEB_SEARCH_PROVIDER=brave` と API キーを設定すれば、この節は要りません。

**設定してあっても、使うかどうかはやり取りごとに選べます。** 会話画面の「🌐 web 検索」を
外すか、API に `web=0` を渡すと、そのやり取りではモデルに道具を渡しません(Chiezo だけで
答えさせたいときに使います)。

## 会話の中で覚えてもらう

`CHIEZO_NOTES_DIR` を設定して notes が有効なら、会話画面の「📝 覚える」で
**「これ覚えておいて」に応えられる**ようになります(agent モードのときだけ)。
モデルは `remember` で書き、次の会話では `recall` で思い出します。**Chiezo で唯一の
書き込み**なので、次の 2 つで見えるようにしてあります。

- 何を書いたかは「調べた手順」に `remember {...}` として出る
- トグルを外す(API なら `notes=0`)と、そのやり取りでは道具ごと渡らない

## 話す相手を増やす

`CHIEZO_LLM_URL` は「名前なし = 既定の相手」です。`CHIEZO_LLM_<名前>_URL` を足すと相手が増え、
`/ai/chat` のセレクトと `/v1/ask?backend=<名前>` で選べるようになります。

Chiezo が要求するのは **OpenAI 互換の `/chat/completions` だけ**なので、相手の素性は問いません。

```bash
# .env
CHIEZO_LLM_URL=http://chiezo-llm:7011/v1          # 既定(従来どおり)

CHIEZO_LLM_GEMINI_URL=https://generativelanguage.googleapis.com/v1beta/openai
CHIEZO_LLM_GEMINI_MODEL=gemini-2.5-flash
CHIEZO_LLM_GEMINI_API_KEY=...
CHIEZO_LLM_GEMINI_LABEL=Gemini                    # 画面に出す名前(省略可)
```

設定する項目は `URL`(必須)・`MODEL`・`API_KEY`・`LABEL` の 4 つです。待ち時間や抜粋の量
(`CHIEZO_ANSWER_*`)は相手ごとには分けません —— 相手が変わっても「どれだけ根拠を積むか」は
Chiezo 側の都合だからです。

> **Gemini の URL は末尾に `/v1` を付けません。** この URL の直下が `chat/completions` です。
> Chiezo は「パスを持たない相手」にだけ `/v1` を補うので、上のとおり書けば正しく組み立てられます。

**compose は環境変数を 1 つずつ渡す作り**なので、独自の名前を使うときは
`docker-compose.yml` の `chiezo-api` の `environment:` にも 4 行足してください
(`.env` を丸ごと流し込まないのは、CLI ブリッジ用の認証情報まで `chiezo-api` に
入ってしまうためです)。

## Claude Code / Codex CLI と話す(CLI ブリッジ)

この 2 つは HTTP ではなく CLI なので、そのままでは指せません。サブスクの枠で使うには
CLI を通すしかない(API キー経路は従量課金になる)ため、**OpenAI 互換の口に見せる小さな
コンテナ**を挟みます(`bridge/`)。

ブリッジは **Chiezo の MCP(`/mcp`)を CLI に繋ぎます**。つまり検索して答える段取りは
ブリッジ側で組まず、道具は CLI 自身が引きます —— Claude Code も Codex も、道具を自分で
回すのが本業だからです。Chiezo 側から見ると「1 回聞いたら答えが返る」ので、
`rag` / `agent` の区別は関係なくなります。

安全のために、**CLI の組み込みの道具(シェル・ファイルの読み書き)は全部切って**あります。
渡すのは Chiezo の MCP だけです。

```bash
# 1) 認証情報を手元で作る
claude setup-token                 # → 出てきたトークンを CLAUDE_CODE_OAUTH_TOKEN へ
codex login --device-auth          # → ~/.codex/auth.json の中身を CODEX_AUTH_JSON へ

# 2) docker-compose.answer.yml の chiezo-bridge-* のコメントを外す

# 3) .env に認証情報と、Chiezo から見た URL を書く
#    CLAUDE_CODE_OAUTH_TOKEN=...
#    CHIEZO_LLM_CLAUDE_URL=http://chiezo-bridge-claude:7013/v1
#    CHIEZO_LLM_CLAUDE_LABEL=Claude Code

# 4) 立ち上げる(イメージは GHCR から pull される)
docker compose -f docker-compose.yml -f docker-compose.answer.yml --profile bridge up -d
```

`--profile bridge` を `answer` と分けてあるのは、推論サーバとブリッジが排他ではなく
**併用するもの**だからです。相手は何本でも並べられます。

ブリッジのイメージ(`ghcr.io/rtcode337/chiezo-bridge`)には両方の CLI が入っていて、
`CHIEZO_BRIDGE_CLI` で役割を決めます。

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_BRIDGE_CLI` | `claude` | 包む CLI(`claude` / `codex`) |
| `CHIEZO_BRIDGE_MCP_URL` | `http://chiezo-api:7010/mcp` | CLI に繋ぐ Chiezo の MCP |
| `CHIEZO_BRIDGE_MODEL` | (CLI の既定) | 使うモデル。サブスク枠を食うので明示を推奨 |
| `CHIEZO_BRIDGE_MODEL_LABEL` | CLI 名かモデル名 | 画面の見出しに出す名前 |
| `CHIEZO_BRIDGE_ALLOWED_TOOLS` | `mcp__chiezo` | CLI に許す道具。書き込み(`remember`)まで止めるならここを絞る |
| `CHIEZO_BRIDGE_TIMEOUT` | `300` | 1 回の上限(秒)。CLI は道具を何度も引くので推論サーバより長い |

**いまは応答を待ち切ってから流します**(差分では流れません)。CLI ごとに
`--output-format stream-json` / `--json` を解析すれば差分にできますが、2 つ分の解析を
抱えるだけの値打ちがまだ無いと判断しています。

## 別の推論サーバに向ける

Chiezo が要求するのは OpenAI 互換の `/v1/chat/completions` だけなので、`CHIEZO_LLM_URL` を
差し替えれば Ollama・LM Studio・GPU 付きの別マシンなど何にでも向けられます
(その場合 `--profile answer` は不要です)。

```bash
CHIEZO_LLM_URL=http://<推論サーバのIP>:11434/v1   # Ollama
CHIEZO_LLM_MODEL=qwen3:8b                          # 複数モデルを持つ相手では実在名が要る
```

## モデルとメモリ

既定は `ggml-org/gemma-3-4b-it-GGUF:Q4_K_M`(約 2.5GB)です。`CHIEZO_LLM_HF_REPO` に
Hugging Face の GGUF リポジトリを `<user>/<repo>:<quant>` の形で指定すると差し替わります。

| | 目安 |
|---|---|
| CPU のみ | 4B 級・Q4_K_M まで。1 回の回答に数十秒。agent モードは実用外 |
| GPU あり | 8〜14B 級。下の「GPU で動かす(NVIDIA)」を参照。agent モードはこちらが前提 |
| メモリ | モデルのファイルサイズ + コンテキスト分(既定 8192 で数百 MB)が目安 |

## GPU で動かす(NVIDIA)

上書きファイルを重ねて起動します(ホストに nvidia-container-toolkit が要ります)。

```bash
docker compose -f docker-compose.yml -f docker-compose.cuda.yml --profile answer up -d
```

**この上書きは NVIDIA 専用**です。イメージが CUDA ビルドで、`gpus: all` も NVIDIA
Container Toolkit の経路のため、AMD や Intel の GPU では動きません。llama.cpp は同じ
ビルド番号で `server-rocm-*`(AMD)・`server-vulkan-*`(ベンダー非依存)・`server-intel-*`
のイメージも公開していますが、デバイスの渡し方が違う(ROCm は `/dev/kfd` と `/dev/dri`、
Vulkan は `/dev/dri`)ので、使うなら別の上書きファイルが要ります。

既定は **Qwen3-8B Q4_K_M(約 5GB)・コンテキスト 16k・全層 GPU・思考オフ**で、VRAM 12GB 級を
想定しています。CUDA 13 のイメージを使うので、ドライバが古い場合はタグを
`server-cuda12-<同じビルド番号>` に落としてください(対応 CUDA は `nvidia-smi` の右上に出ます)。

VRAM 12GB の GPU での実測は **プロンプト処理 3,300〜3,800 tok/s・生成 72〜78 tok/s**、1 問あたり
rag で 2.5 秒・agent で 2〜8 秒です。

**コンテキスト長は KV キャッシュとして VRAM を食います。** 同じ 12GB での実測
(画面描画に使われる 2GB 弱を含む):

| | VRAM 使用 | 空き |
|---|---|---|
| `CHIEZO_LLM_CTX_SIZE=32768` | 11.3GB | 0.9GB |
| `CHIEZO_LLM_CTX_SIZE=16384`(既定) | 8.8GB | 3.4GB |

**画面も同じ GPU が描いているなら、空きは 2GB 以上残してください。** 尽きるとホスト OS が
GPU メモリをシステムメモリへ退避し、ホストごとページングで固まります(実測で作業機が
フリーズしました → [設計メモ](design-notes.md#vram-を使い切るとホストごと止まるwsl2--windows))。

思考(reasoning)を既定で切っているのは、道具を何度も呼ぶ agent モードでは 1 ステップごとの
思考が待ち時間としてそのまま積み上がるためです。品質を優先するなら
`CHIEZO_LLM_THINK_BUDGET=-1` で戻せます。

**配信機に同居させないでください。** chiezo-api 自体は従来どおり数百 MB で動きますが、
推論はモデルサイズぶんのメモリを持っていきます。小型の配信機で使うなら、推論は
LAN 上の別マシンに置いて `CHIEZO_LLM_URL` で指すのが素直です。

## 環境変数

chiezo-api 側:

| 変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_LLM_URL` | (未設定 = 無効) | 推論サーバの OpenAI 互換ベース URL。`/v1` は省略しても補われる |
| `CHIEZO_LLM_MODEL` | `chiezo` | リクエストに載せるモデル名。llama-server は 1 プロセス 1 モデルなので何でもよい |
| `CHIEZO_LLM_API_KEY` | (なし) | 設定すると `Authorization: Bearer` を送る |
| `CHIEZO_ANSWER_TIMEOUT` | `120` | 推論の待ち時間(秒)。DB クエリの 5 秒とは別枠 |
| `CHIEZO_ANSWER_DOCS` | `4` | 根拠として本文を取ってくる文書数 |
| `CHIEZO_ANSWER_MAX_CHARS` | `6000` | 抜粋の合計文字数の上限 |
| `CHIEZO_ASK_DEFAULT_MODE` | `rag` | `mode` を省いたときの既定 |
| `CHIEZO_ASK_DEFAULT_GROUNDED` | `1` | `grounded` を省いたときの既定 |

**CPU 推論では `CHIEZO_ANSWER_MAX_CHARS` を下げてください。** 所要時間は抜粋の長さ
(プロンプト処理)がほぼ支配します。実測は
[設計メモ](design-notes.md#使う層はなぜ-2-段の-rag-か)にあります。

推論コンテナ側(`chiezo-llm`)は `CHIEZO_LLM_HF_REPO`(モデル)と `CHIEZO_LLM_CTX_SIZE`
(コンテキスト長、既定 8192)で調整します。それ以外の項目は llama-server の
`LLAMA_ARG_*` 環境変数を compose に足せば効きます。

推論コンテナのイメージはタグを固定してあります(`server-b10156`)。dependabot は
`api/` `ingest/` の Dockerfile しか見ていないので、更新するときは compose の
このタグを手で上げてください(タグ一覧は
[GHCR](https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp))。
