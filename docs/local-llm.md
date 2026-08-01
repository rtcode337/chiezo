# 答える(ローカル LLM。既定では無効)

**chiezo を引ける AI と、ブラウザから話せます**(`/localllm/chat`)。1 問 1 答の口(`/v1/ask`)と
会話の口(`/v1/chat`)があり、根拠にした文書は出典として併記します。

**話す相手は AI(使っているモデル)で、chiezo はその AI が引く知識**です。画面の見出しにも
モデル名が出ます(`AI(Qwen3-8B)と話す`)。chiezo は AI のための知識ベースで、その AI を
Claude Code の代わりにローカル LLM で立てて同居させたのがこの層、という関係です
(だから既定では無効で、chiezo 本体は今までどおり外を叩きません)。推論も chiezo-api の中では
動かさず、**OpenAI 互換 API を喋る別プロセス**に任せます(配信側 chiezo-api がメモリ数百 MB で
動く前提を壊さないため)。

有効になるのは `CHIEZO_LLM_URL` を設定したときだけです。設定しなければ `/v1/ask` は 503 を返し、
管理画面にも無効と表示されます。設計の背景は
[設計メモ](design-notes.md#答える層はなぜ-2-段の-rag-か)が正です。

## 使いはじめる

```bash
cp .env.example .env
# .env の CHIEZO_LLM_URL=http://chiezo-llm:8080/v1 の行のコメントを外す

docker compose --profile answer up -d
docker compose logs -f chiezo-llm     # 初回はモデルのダウンロード(約 2.5GB)
```

`--profile answer` を付けたときだけ推論コンテナ(`chiezo-llm` = llama.cpp の
`llama-server`)が起動します。付けなければ従来どおり chiezo-api と chiezo-trigger だけです。
モデルは起動時に Hugging Face から取得して `./models` にキャッシュするので、
2 回目以降のダウンロードはありません。`chiezo-trigger` と同じくホストへポートを公開せず、
chiezo-api からのみ内部ネットワーク経由で到達します。

## 話す(ブラウザ)

**`/localllm/chat`**(管理画面からも辿れます)。見出しにはいま話しているモデルの名前が出ます
(`CHIEZO_LLM_MODEL` が未設定なら推論サーバに問い合わせます)。入力欄は数行ぶんの高さがあり
(Enter で送信・Shift+Enter で改行)、**ソース・引き方・根拠・web 検索の切り替えはその下**に
並びます。会話として続けられるので、「じゃあ京都のほうは?」
「さっきの寺の最寄り駅は?」が通じます。**会話の履歴を持つのはブラウザ側**で、送るたびに
まるごとサーバーへ渡します(サーバーは会話の状態を持ちません。読み取り専用・LAN 内・
複数ワーカーという前提を崩さないため)。回答まで数十秒かかるので、この画面だけは
JavaScript で逐次表示します。JavaScript が無い環境向けに 1 問 1 答の画面もあります。

## 使い方(curl)

```bash
BASE=http://<サーバーIP>:9000

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
- `grounded` — 回答方針。`1` は chiezo で取れたことだけを根拠にし、根拠が無ければ答えません。
  `0` にすると足りない部分をモデル自身の知識で補います(chiezo 由来の部分にだけ出典番号が付きます)。
  **これはモデルの幻覚への対処であって chiezo の制約ではない**ので、用途に応じて選んでください
- `mode` — `rag`(1 回検索して答える)か `agent`(モデルに道具を引かせる。下記)
- `web` — agent モードで web 検索の道具を渡すか。省略時はサーバー設定どおり。`0` にすると**そのやり取りだけ chiezo に閉じます**(設定していない環境では `1` にしても使えません)
- `notes` — agent モードで「覚える・思い出す」の道具を渡すか。同じく省略時はサーバー設定どおりで、`0` で切れます(**chiezo で唯一の書き込み**なので、切れることが要ります)
- `stream=1` — `text/event-stream` で返す。`references`(出典。本文より先に確定するので先に届く)
  → `delta`(本文の差分)× n → `done` の順。途中で推論側が落ちたら `error` イベントが挟まります

`mode` と `grounded` の**既定は環境変数で決められます**(`CHIEZO_ASK_DEFAULT_MODE` /
`CHIEZO_ASK_DEFAULT_GROUNDED`)。素の既定は `rag` + `grounded=1` で、これは小さな機械でも
安全に動く側に倒したものです。GPU で 8B 級を動かしているなら `.env` で
`agent` + `grounded=0` にすると、**普通に会話している感じ**になります(必要なときだけ自分で
chiezo を引き、雑談は雑談として返る)。

`grounded=1` で抜粋が 1 件も取れなかった場合は、**推論を走らせずに**「抜粋からは分かりません」を
返します(小型モデルは抜粋が空でも自分の知識で答えてしまうため、プロンプトに委ねず経路として
断っています。実測は[設計メモ](design-notes.md#答える層はなぜ-2-段の-rag-か)を参照)。

本文中の `[1]` のような番号は `references` の `n` に対応します。**`references` が空のときは
本文の番号に意味がないので無視してください** — 小型モデルは根拠が無くても番号を書くことがあります。

答えの作り方は 2 段です。まず質問文から検索クエリを組み立て(質問文をそのまま全文検索に
入れても当たらないため)、その結果の上位文書の本文を抜粋してから答えさせます。

## agent モード(モデルに道具を引かせる)

既定の `rag` は **`search` を 1 回**引いて終わりなので、chiezo の強い道具に手が届きません。
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
(ブラウザの `/localllm/chat` でも「調べた手順」として出ます)。出典は**本文中の番号ではなく**、
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

agent モードに `web_search` の道具を足せます。**chiezo に無いこと**(取り込んだダンプより
新しい出来事、いま現在の状態)を聞かれたときだけモデルが使います。

```bash
# .env
CHIEZO_WEB_SEARCH_URL=http://searxng:8080/search   # 自分で立てた SearXNG
#CHIEZO_WEB_SEARCH_PROVIDER=searxng                # searxng(既定)/ brave
#CHIEZO_WEB_SEARCH_API_KEY=                        # brave のときだけ
#CHIEZO_WEB_SEARCH_RESULTS=5                       # 1 回に見る件数
```

これは **chiezo 本体ではなく「答える」層(= chiezo を使う側)の機能**です。知識ベースそのものは
引き続き外へ出ません。とはいえ外に出る以上は、次を守っています。

- **どれが web 由来か必ず分かる**。出典の `source` が `web` になり、URL が付きます
- **本文は取りに行かない**。返すのはタイトル・要約・URL だけです(スクレイピングはしません)
- **自分でレート制限をかける**。`User-Agent` はプロジェクト名だけで、連絡先や個人名は載せません
- **chiezo が先**。プロンプトで順番を固定しています(web は足りないぶんだけ)

SearXNG を使う場合は JSON 出力を有効にする必要があります(`settings.yml` の
`search.formats` に `json` を足す)。既定で無効な形式なので、有効にしないと HTML が返り、
chiezo 側は「JSON ではない」というエラーとして扱います。

**設定してあっても、使うかどうかはやり取りごとに選べます。** 会話画面の「🌐 web 検索」を
外すか、API に `web=0` を渡すと、そのやり取りではモデルに道具を渡しません(chiezo だけで
答えさせたいときに使います)。

## 会話の中で覚えてもらう

`CHIEZO_NOTES_DIR` を設定して notes が有効なら、会話画面の「📝 覚える」で
**「これ覚えておいて」に応えられる**ようになります(agent モードのときだけ)。
モデルは `remember` で書き、次の会話では `recall` で思い出します。**chiezo で唯一の
書き込み**なので、次の 2 つで見えるようにしてあります。

- 何を書いたかは「調べた手順」に `remember {...}` として出る
- トグルを外す(API なら `notes=0`)と、そのやり取りでは道具ごと渡らない

## 別の推論サーバに向ける

chiezo が要求するのは OpenAI 互換の `/v1/chat/completions` だけなので、`CHIEZO_LLM_URL` を
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
| GPU あり | 8〜14B 級。下の「GPU で動かす」を参照。agent モードはこちらが前提 |
| メモリ | モデルのファイルサイズ + コンテキスト分(既定 8192 で数百 MB)が目安 |

## GPU で動かす

上書きファイルを重ねて起動します(ホストに nvidia-container-toolkit が要ります)。

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile answer up -d
```

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
[設計メモ](design-notes.md#答える層はなぜ-2-段の-rag-か)にあります。

推論コンテナ側(`chiezo-llm`)は `CHIEZO_LLM_HF_REPO`(モデル)と `CHIEZO_LLM_CTX_SIZE`
(コンテキスト長、既定 8192)で調整します。それ以外の項目は llama-server の
`LLAMA_ARG_*` 環境変数を compose に足せば効きます。

推論コンテナのイメージはタグを固定してあります(`server-b10156`)。dependabot は
`api/` `ingest/` の Dockerfile しか見ていないので、更新するときは compose の
このタグを手で上げてください(タグ一覧は
[GHCR](https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp))。
