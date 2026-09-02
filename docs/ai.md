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
プロンプトもここで持ちます。推論そのものは chiezo-app の中では動かさず、
**OpenAI 互換 API を喋る別プロセス**に任せます(配信側 chiezo-app がメモリ数百 MB で
動く前提を壊さないため)。

有効になるのは `CHIEZO_LLM_URL` を設定したときだけです。設定しなければ `/v1/ask` は 503 を返し、
管理画面にも無効と表示されます。設計の背景は
[設計メモ](design-notes.md#使う層はなぜ-2-段の-rag-か)が正です。

## 使いはじめる

推論サーバと検索エンジンの**コンテナは `docker-compose.llm.yml` に外出し**してあります
(chiezo-app 側の設定は本体の `docker-compose.yml` にあります)。重ねなければコンテナは
立たず、Chiezo は検索 API・MCP として動きます。

```bash
cp .env.example .env
# .env の CHIEZO_LLM_URL=http://chiezo-llm:7011/v1 の行のコメントを外す

docker compose -f docker-compose.yml -f docker-compose.llm.yml --profile llm up -d
docker compose logs -f chiezo-llm     # 初回はモデルのダウンロード(約 2.5GB)
```

毎回 `-f` を並べるのが煩わしければ、`.env` に
`COMPOSE_FILE=docker-compose.yml:docker-compose.llm.yml` と書けば以後は
`docker compose --profile llm up -d` で済みます。

起動するのは推論サーバ(`chiezo-llm` = llama.cpp の `llama-server`)と
検索エンジン(`searxng`)の 2 つです。**推論を LAN の別マシン(Ollama 等)に任せるなら
このファイルは要りません** —— `CHIEZO_LLM_URL` にその URL を書くだけで、本体だけで動きます。

モデルは起動時に Hugging Face から取得して `./models` にキャッシュするので、
2 回目以降のダウンロードはありません。`chiezo-trigger` と同じくホストへポートを公開せず、
chiezo-app からのみ内部ネットワーク経由で到達します(別ホストの chiezo-app から使うなら
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

## 何を頼めるのか(`GET /v1/capabilities`)

chiezo 経由で AI に頼めることは **7 つに分類**してあります。管理画面の「AI の相手」の
いちばん上と、この口が同じものを返します。

| 分類 | いま | 頼み方 |
|---|---|---|
| **会話** | ✅ | `/v1/ask`・`/v1/chat`・`/ai/chat`・MCP の道具 |
| **読み上げ** | ✅ | `speech_generate` / `POST /v1/media/speech` |
| **文字起こし** | ✅ | `transcribe` / `POST /v1/media/transcribe` |
| **画像** | ✅ | `image_generate` / `POST /v1/media/image` |
| **動画** | ✅ | `video_generate` / `POST /v1/media/video` |
| **音楽** | ✅ | `audio_generate(sound="music")` |
| **SE** | ✅ | `audio_generate(sound="sfx")` |

**「未対応」と「相手がいない」は別物です。** 前者は実装が無い（作れば直る）、後者は
実装はあるが使える相手がいない（鍵を登録する・GPU にモデルを置けば直る）—— 次にすることが
違うので、同じ言葉にしていません。いまは全部 ✅ ですが、**この区別は残してあります**
（消すと、次に分類が増えたときにまた同じ言葉に潰れます）。

**読み上げと文字起こしを分けてあるのも同じ理由**です。同じ「声」でも仕事の向きが逆で、
相手もモデルも別物 —— まとめると「読み上げはできるが文字起こしはできない」相手を
「声が使える」と言うことになります。

**音楽と SE を分けてあるのは、相手もモデルも別物だから**です（job の `kind` はどちらも
`audio`）。Lyria は曲しか作れず、Stable Audio Open は効果音向き、ACE-Step は曲向き。
「音が作れます」とだけ言うと、効果音を頼んで 30 秒の曲が返ることになります。

## 絵を描かせる(MCP の `image_*`)

**知識を引くのとは別の仕事**ですが、口は Chiezo にまとめてあります —— MCP の登録先を
増やさないためです。ゲーム素材や図版を、**自分の GPU(ComfyUI)か外部(Gemini /
OpenAI / ElevenLabs / CLI のサブスク枠)を選んで**作れます。

```
image_backends()                        … 頼める相手・モデル・サイズ(使えない相手は理由つき)
image_generate(prompt, backend?, model?, size?, seed?, count?, negative?, group?)
                                        … job_id を返す(**待たない**)
image_status(job_id)                    … 仕上がり。files に保存先のパスと URL
```

- **画像そのものは返しません。** 1 枚 1〜2MB あり、道具の結果はまるごと呼び出し側の
  コンテキストに載ります。返すのは**パスと URL**(`GET /media/<日付>/<名前>`)で、
  要るときだけ取りに来てもらいます
- **待ちません。** 生成は数秒〜数分かかり、待たせると呼び出し側が先に切れます。
  進み具合は `image_status` で引きます(state は queued / running / done /
  **partial**(一部だけ描けた)/ failed)
- **中断された生成は failed になります。** MCP の接続が切れるとタスクごと畳まれることがあり、
  そのままだと job は running のまま残ります(相手は描き上がっているのに、こちらは待ち続ける)。
  中断を捕まえて記録し、それでも取り残されたもの(ワーカーごと落ちた場合など)は
  読み出すときに畳みます —— 生成 1 枚ぶんの上限 + 60 秒だけ無音なら「もう誰も見ていない」
- **形式は相手によって違います。** ComfyUI と OpenAI は PNG、**Gemini は JPEG のみ**
  (png を頼むと 400)。保存するファイルの拡張子も中身に合わせるので、
  透過の要る素材は自前の GPU 側で作ります
- **seed を指定すると同じ絵を作り直せます**(ComfyUI のみ)。ゲーム素材は「同じキャラの
  別ポーズ」を作るので、再現性が要ります。指定しなければ毎回振り直します
- 出来た画像は `CHIEZO_MEDIA_DIR`(既定は `CHIEZO_STATE_DIR/media`)に日付ごとに置かれ、
  `CHIEZO_MEDIA_KEEP_DAYS`(既定 14)より古いものは自動で消えます

### 相手を選ぶ

| id | 実体 | 鍵 | 課金 |
|---|---|---|---|
| `comfyui` | 自前の GPU の ComfyUI | 不要 | 電気代だけ |
| `codex` | **Codex CLI の内蔵ツール**(gpt-image-2) | Codex の行(ChatGPT のログイン) | **ChatGPT のサブスク枠** |
| `antigravity` | **Antigravity CLI の内蔵ツール** | Antigravity の行(Google アカウントのサインイン) | **Google AI のサブスク枠** |
| `gemini` | Gemini の画像生成(`gemini-3.1-flash-image` ほか) | Gemini の行 | 無料枠 |
| `openai` | gpt-image(`gpt-image-2`) | OpenAI の行 | 従量課金 |
| `elevenlabs` | **他社モデルの預かり**(gpt-image / Gemini / Seedream) | **この相手専用** | **API は Pro 以上** |

**相手を指定しなければ、表の上の相手から順に選ばれます**(`image_backends` の並びと同じ)。
既定は **`codex`** —— **かつては自前の GPU(`comfyui`)を既定にしていました**が、
外へ出さず枠も食わない代わりに出来が落ちるので、名指ししない呼び出しが良い相手へ行くように
入れ替えてあります。**枠を使いたくないときは `backend="comfyui"` と名指ししてください。**
順位は `app/media_providers.py` の `PREFERENCE`(種類ごとの表)にあります。

**`elevenlabs` に自社の絵のモデルはありません。** 他社のモデルを 1 つの口から選べる
だけなので、同じモデルを OpenAI や Gemini に直接頼めるならそちらのほうが枠の要件が
緩くなります —— ここを選ぶ利点は「1 つの鍵で絵も動画も声も音もそろう」ことだけです。

**`codex` と `antigravity` はサブスクの枠**（API キーが要りません）。**画像を作れるのは
内蔵ツールを持つ CLI だけ**で、Claude Code CLI は持ちません。どちらも
「エージェントにファイルを書かせる」形なので、ブリッジ側の段取りは共通です。

**`codex` と `openai` は同じ gpt-image-2 ですが、課金の出どころが違います。**
`codex` は chatgpt.com 側(サブスクリプション)で追加の API キーが要らず、`openai` は
platform.openai.com 側(従量課金)でクレジットが要ります —— 残高は行き来しません。
ただし**画像のターンは文字だけのターンより 3〜5 倍の速さでサブスクの枠を食う**ので、
枚数を出すなら自前の GPU が向いています。

**鍵と on/off は相手ごとに 1 つです**(同じ鍵を 2 か所に入れさせないため)。
つまり**「有効にする」を押していない相手には絵も描かせません** —— 鍵を持っている相手を
止めたのに片方だけ動き続けるのは、止めたつもりの人にとって事故になるためです。
**「答える」層を停止すると全部止まり、MCP の道具も出なくなります。**

管理画面の**「AI の相手」**の表に、話す相手と一緒に並びます。**相手ごとに 1 行**で、
「できること」の欄に**その相手でいま何ができるか（話す・絵・音）**が出ます
—— 鍵も on/off も相手ごとに 1 つなので、無効にすると全部止まります
(表を分けていた頃は同じ相手が 2 か所に出ていて、どちらが効くのか読めませんでした)。
**既定は無効**なので、立ち上げたら一度「有効にする」を押してください。

「接続を試す」は**繋がるかどうかに加えて、チェックポイントが置いてあるかまで見ます**
—— ComfyUI は立っていてもモデルが無ければ 1 枚も描けません。**絵と音は別のファイルが
要る**ので、どちらが何件あるかも返します。

`codex` を使うには、`chiezo-bridge-codex` を立てて表の Codex CLI を有効にしてください
(画像はブリッジの `/v1/images/generations` を通り、中で `codex exec` が内蔵ツールを回します。
**会話の口と違い、この 1 回のための作業ディレクトリにだけ書き込みを許します** ——
画像はファイルとして書き出されるためです)。

**画像は 1 回ずつ直列に走ります。** 内蔵ツールは指示した作業ディレクトリではなく
共有の保存先(`$CODEX_HOME/generated_images`)へ置くことがあり、そこは使い回されるので、
同時に走らせると「どれがこの実行のものか」を時刻では決められません
(実際に 4 件同時に頼んで、4 件とも同じ絵が返りました)。走らせる前にあったファイルを
覚えて除き、かつブリッジ側で直列化しています。1 枚 1 分以上かかる相手なので、
並べても速くはなりません。

gpt-image が **403** を返したら、OpenAI の開発者コンソールで**組織の本人確認**を求められて
いることがあります —— API の話なので、ChatGPT や Codex のサブスクで使えているかは関係しません。

サイズは相手ごとに受け取り方が違うので、こちらは常に `幅x高さ` で頼み、変換は中で行います
—— Gemini は比率(`3:2` など)、OpenAI は決まった組み合わせ(`1536x1024` など)へ
**近いものに寄せます**。画素どおりに出るのは ComfyUI だけです。

**画素どおりに出るぶん、ComfyUI は一覧以外のサイズを受け付けません**(`exact_sizes`)。
頼まれた画素でそのまま潜在空間を作るので、モデルの学習解像度(SDXL なら 1024)を
下回ると絵が崩壊します —— しかも生成は成功として返るため、受け取った側は絵を見るまで
気づけません。アイコンのような小さい素材が要るときは、一覧のサイズで描いてから縮小します。
512 が native のモデル(SD 1.5 系)を置くなら、`media_providers.py` の comfyui の
`sizes` にそのサイズを足してください。

自分の GPU で回すには、GPU のあるホストで:

```bash
docker compose -f docker-compose.yml -f docker-compose.comfyui.yml --profile comfyui up -d
```

**モデル(チェックポイント)は自分で置いてください** —— 数 GB あり、ライセンスも
配布条件もモデルごとに違うので、同梱も自動取得もしていません。
`./models/comfyui/ComfyUI/models/checkpoints/` に `.safetensors` を置くと `image_backends` に
出ます（**この上書きは `./models/comfyui` をコンテナの `/root` に繋ぐ**ので、ComfyUI 本体が
展開される 1 段下がモデルの置き場になります）。

待ち受けは **7014**(7010 = API・7011 = 推論・7012 = 検索・7013 = CLI ブリッジの並び)。
ComfyUI の既定は 8188 ですが、番号が食い違うと URL を書くたびに迷うので寄せてあります。

**GPU が別のマシンにあるなら、そちらで ComfyUI を動かして URL を渡すだけです**
(`CHIEZO_IMAGE_URL=http://<GPUマシン>:7014`。素の ComfyUI を自分で動かしているなら
その待ち受けポート、既定なら 8188)。推論サーバと同じ逃げ道です。

## 音を作らせる(MCP の `audio_*`)

効果音と BGM も同じ口から作れます。**作りは絵とまったく同じ**(頼む → job → 仕上がりを
引く)で、違うのは頼むときの語彙だけ —— 絵はサイズ、音は**種類(効果音 / 曲)と長さ**です。

```
audio_backends()                        … 頼める相手・モデル・種類と長さの上限
audio_generate(prompt, sound?, backend?, model?, seconds?, seed?, count?, lyrics?, loop?, group?)
                                        … job_id を返す(**待たない**)
audio_status(job_id)                    … 仕上がり。files に保存先のパスと URL
```

- **`sound` は `sfx`(効果音)か `music`(曲)。** 同じ「音」でもモデルが別物で、
  相手によっては口そのものが分かれています。**既定は `sfx`**
- **頼める種類は相手によって違います。** Lyria は曲のモデルなので効果音を作れません
  —— 頼まれたら断ります(短い衝突音を頼んで 30 秒の曲が返るほうが分かりにくいため)
- **長さ(`seconds`)は上限を超えると断ります。黙って短くはしません** ——
  丸めて返すと、呼んだ側は「頼んだ尺で出来た」と思ったまま短い素材を受け取ります。
  **長さを指定できない相手もいます**(Lyria は尺がモデルで決まっていて、渡す口が無い)
- **`lyrics` を空にすると器楽として作ります。** ゲームの BGM に歌が乗ると台詞と喧嘩するので、
  こちらを既定にしてあります(歌詞を渡したときだけ歌ものになります)
- **`loop` は繋いで鳴らせる素材にしたいとき。** 効くのは ElevenLabs の効果音だけです
- 出来たものの置き場・掃除・中断の後始末・配信は**絵とまったく同じ**
  (`CHIEZO_MEDIA_DIR`、`CHIEZO_MEDIA_KEEP_DAYS`)。ジョブの表も共通です

### 相手を選ぶ

| id | 実体 | 頼めるもの | 鍵 | 課金 |
|---|---|---|---|---|
| `comfyui` | 自前の GPU(Stable Audio Open / ACE-Step) | 効果音 〜47 秒・曲 〜240 秒 | 不要 | 電気代だけ |
| `gemini` | Lyria 3(`lyria-3-clip-preview` ほか) | **曲のみ・長さは指定できない** | Gemini の行 | 無料枠 |
| `elevenlabs` | ElevenLabs(`/sound-generation`・`/music`) | 効果音 〜30 秒・曲 〜600 秒 | **この相手専用**(下記) | 無料枠あり |

**相手を指定しなければ既定は `elevenlabs`**(絵と同じで「頼む順」の先頭。
枠を使いたくないときは `backend="comfyui"`)。

**ElevenLabs だけ鍵を自分の行に登録します。** 会話の口が「先にエージェントを作って
`agent_id` で話す」形で、`app/providers.py` の「OpenAI 互換に 1 往復投げる」枠に
入らないため、鍵を借りる先がありません。他の外部サービスは絵と同じで、
同じ行の鍵と on/off をそのまま使います。

自前の GPU で作るには、**絵とは別のモデル**を `./models/comfyui/ComfyUI/models/checkpoints/` に
置きます（絵と同じ置き場です）。

| 何を作るか | 置くファイル | 補足 |
|---|---|---|
| 効果音・短い素材 | `stable-audio-open-1.0.safetensors` | **`../text_encoders/t5-base.safetensors` も要る**。**`Comfy-Org/stable-audio-open-1.0_repackaged` から落とす**（本家 `stabilityai/…` は gated で、同意とアクセストークンが要る）。ライセンスは Stability AI Community License —— **配布物に「Powered by Stability AI」の表示が要る** |
| 曲 | `ace_step_v1_3.5b.safetensors` | all-in-one（text encoder は不要）。7.7GB、認証なしで落とせる。Apache-2.0 なので表示の縛りが無い |

**どちらの系統かは名前で見分けています。** ComfyUI に「このチェックポイントは何用か」を
聞く口は無く(読み込んで初めて分かる)、GPU にモデルを載せてから間違いに気づくのは高くつきます。
名前を変えて置いたときは `model` で名指ししてください。名前に `audio` か `ace` を含むものだけを
音のチェックポイントとして扱うので、**絵のモデルと同じ置き場でも混ざりません**。


## 何案か作って選んでもらう(見比べ)

**どれがいいかを人に決めてほしいときは、`group` に同じ名前を付けて何案か頼みます。**
同じ名前の依頼はやること画面の「見比べ」(`/media`)に 1 組として並び、依頼文つきで
見比べ・聴き比べできます。**音は AI 自身が聴けない**ので、聴いてもらう手段はここだけです。

```
image_generate(..., group="ホームランの音")   … 同じ名前の依頼が 1 組になる
audio_generate(..., group="ホームランの音")
media_picks(limit?, group?)                  … 人が選んだ案(と、添えられた一言)
```

REST では `POST /v1/media/image|audio` の本体に `group` を、採用の読み出しは
`GET /v1/media/picks`(やること層は `GET /api/media/picks`)を使います。

- **会話で「何番がいい?」と聞かないこと。** 選ぶ人と頼んだ AI が別のやり取りにいると
  拾えません。印は画面で付けてもらい、`media_picks` を見に来ます
- **1 組から選ばれるのは 1 つ。** 選び直されたら前の印は外れています
- 名前を付けなかった依頼も、画面では 1 件 1 組として並びます ——
  消すと「作られなかった」と読めるため
- 画面から生成は頼めません(課金が走る口は外に出す面に載せていないため)。
  あの画面が持つのは「作られたものを読む」と「印を付ける」だけです

## 動画を作らせる(MCP の `video_*`)

```
video_backends()                        … 頼める相手・モデル・サイズ・**受け付ける尺の一覧**
video_generate(prompt, backend?, model?, size?, seconds?, seed?, count?, negative?, audio?)
                                        … job_id を返す(**待たない**)
video_status(job_id)                    … 仕上がり。files に保存先のパスと URL
```

**作りは絵と同じ**(頼む → job → 仕上がりを引く)ですが、動画には絵や音に無い事情が
3 つあります。どれも**間違えても生成そのものは成功して返る**ので、頼む前に断っています。

- **待ち時間の桁が違う。** 絵が数十秒なのに対して、動画は 8 秒ぶんで数分〜十数分です。
  上限は `CHIEZO_VIDEO_TIMEOUT`(既定 1200 秒)。job を「もう誰も面倒を見ていない」と
  判断して畳む猶予も、動画だけ別に取ってあります —— 絵と同じ基準で畳むと、
  **まだ相手の中で作っている最中のものを「中断された」と書いてしまい**、
  出来上がった動画を取りに行けなくなります
- **尺は一覧にある値しか頼めません。** 相手ごとに飛び飛びで(Sora は 4/8/12 秒、
  Veo は 4/6/8 秒)、**近い値には丸めません** —— 寄せると「6 秒で頼んだのに 8 秒が返る」に
  なり、数分と数十 MB を使ってから気づくことになります。**尺を指定できない相手**
  (Gemini Omni Flash)に秒数を渡した場合も断ります
- **1 本が重い。** 一度に頼める本数は絵の 4 本に対して **2 本まで**です。置き場の掃除は
  絵と同じ(既定 14 日)なので、要るものは早めに引き取ってください

### 相手を選ぶ

| id | 実体 | 尺 | 鍵 | 課金 |
|---|---|---|---|---|
| `comfyui` | 自前の GPU(**Wan 系のみ**) | 2 / 3 / 5 秒 | 不要 | 電気代だけ |
| `gemini` | Gemini Omni Flash・Veo 3.1 | omni は**指定不可**、Veo は 4/6/8 秒 | Gemini の行 | 無料枠 |
| `openai` | Sora 2 / Sora 2 Pro | 4 / 8 / 12 秒 | OpenAI の行 | 従量課金 |
| `elevenlabs` | Veo・Seedance(他社モデルの預かり) | 4〜10 秒 | **この相手専用** | **API は Pro 以上** |

**Gemini だけ口が 2 通りに分かれます。** Omni Flash は絵や曲と同じ `interactions` に
1 往復で返りますが、Veo は `:predictLongRunning` に投げてオペレーションを待ち、
最後に出来た動画を**鍵つきで**取りに行きます(署名済み URL ではありません)。
どちらかは**モデル名で見分けています** —— 秒数を渡す口があるのは Veo だけなので、
ここを取り違えると「秒数を渡したのに無視された」が黙って起きます。

自前の GPU で作るには、**絵や音とは置き場が違います**。Wan は UNet 単体で配られるので、

| 何を置くか | 置き場 |
|---|---|
| `wan2.2_t2v_*.safetensors` | `models/diffusion_models/` |
| `umt5_xxl_*.safetensors` | `models/text_encoders/` |
| `wan_2.1_vae.safetensors` | `models/vae/` |

の 3 つが要ります。**どれか 1 つでも欠けるとグラフごと通らない**ので、GPU を回す前に
揃っているかを確かめて断ります(ComfyUI 側のエラーは「名前が不正」としか出ず、
何を置けばよいのか分かりません)。対応しているのは **Wan 系だけ**です ——
HunyuanVideo や LTX-Video は text encoder が 2 本要るなどグラフの形が違い、
1 つのテンプレでは賄えません。

## 声を扱う(MCP の `speech_*` / `transcribe`)

```
voice_backends(kind?)                   … 相手・モデル・**選べる声**(kind は speech / transcribe)
speech_generate(text, backend?, model?, voice?, speed?, language?, instructions?)
                                        … job_id を返す(**待たない**)
speech_status(job_id)                   … 仕上がり。files に保存先のパスと URL
transcribe(path?, url?, backend?, model?, language?)
                                        … **その場で文字が返る**(job にならない)
```

**向きが逆の 2 つを同じ節にまとめてあります。** 読み上げは絵や音と同じ job ですが、
**文字起こしだけは job になりません** —— 返るのが文字(数 KB)なので、置き場も掃除も
配信も要らず、その場で返すほうが呼ぶ側の手数が少なくて済みます。

- **`text` は読み上げる文章そのもの**です。絵や音のような「こういうものを作って」という
  指示ではありません(job の表では同じ `prompt` 列に載りますが、性格が違います)
- **声は名前で頼めます。** ElevenLabs の id を控えている人はいないので、`Rachel` のような
  名前を id に直してから投げます。**一覧に無い名前はそのまま id として渡します** ——
  こちらが一覧を取り損ねただけ、という場合に頼みごと自体を潰さないためです
- **自前の GPU に読み上げは頼めません。** ComfyUI 本体に TTS のノードが無く、あるのは
  外部の拡張(TTS Audio Suite・F5-TTS 等)だけです —— 何を入れているかでノード名も引数も
  変わるので、こちらからグラフを組み立てられません
- **生の PCM には WAV の殻をかぶせます。** Gemini は 24kHz・16bit・モノラルの生 PCM を
  返すことがあり、そのまま保存すると**拡張子も中身も再生できないファイル**になります
  (受け取った側は開くまで気づけません)
- 文字起こしに渡せるのは **chiezo が作ったもの(`/media/…`)か、サーバーから届く URL** だけ
  です。**頼んだ人の手元のファイルは渡せません** —— chiezo はコンテナの中で動いていて、
  そちらのディスクが見えないためです。自分のファイルは
  `POST /v1/media/transcribe` に multipart で送ります

### 相手を選ぶ

| id | 読み上げ | 文字起こし | 鍵 |
|---|---|---|---|
| `gemini` | `gemini-3.1-flash-tts-preview` ほか(決め打ちの 30 声) | ふつうの Gemini に音を渡す | Gemini の行 |
| `openai` | `gpt-4o-mini-tts` ほか(決め打ちの 11 声) | `gpt-4o-transcribe` / `whisper-1` | OpenAI の行 |
| `elevenlabs` | `eleven_v3` ほか(**声は相手に聞く**) | Scribe | **この相手専用** |

**Gemini に文字起こし専用のモデルはありません。** ふつうの Gemini に音を入力として渡す形
なので、「一字一句そのまま書き起こす」と言い切って頼んでいます —— 言わないと要約や
感想が混じります。**OpenAI は 25MB までしか受け取りません**ので、長い音は切り分けるか
他の相手を選んでください。

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
CHIEZO_WEB_SEARCH_URL=http://searxng:7012/search   # 同居(本体の compose で立つ)
#CHIEZO_WEB_SEARCH_URL=http://<立てたホストのIP>:7012/search  # LAN の別ホストのもの
#CHIEZO_WEB_SEARCH_PROVIDER=searxng                # searxng(既定)/ brave
#CHIEZO_WEB_SEARCH_API_KEY=                        # brave のときだけ
#CHIEZO_WEB_SEARCH_RESULTS=5                       # 1 回に見る件数
```

ポートは**内も外も 7012**です(7010 = API・7011 = 推論の隣に揃えてあります)。ホストへは
公開せず、`chiezo-app` からサービス名で引きます —— SearXNG は本体の compose にあるので、
`chiezo-app` を立てるマシンには必ず一緒に立てられます(検索の中継なので軽い)。
別ホストのものを指す構成は用意していません(`docker-compose.lan.yml` が開けるのは
GPU の要るものだけ)。

これは **Chiezo 本体ではなく「使う」層(= Chiezo を使う側)の機能**です。知識ベースそのものは
引き続き外へ出ません。とはいえ外に出る以上は、次を守っています。

- **どれが web 由来か必ず分かる**。出典の `source` が `web` になり、URL が付きます
- **本文は取りに行かない**。返すのはタイトル・要約・URL だけです(スクレイピングはしません)
- **自分でレート制限をかける**。`User-Agent` はプロジェクト名だけで、連絡先や個人名は載せません
- **Chiezo が先**。プロンプトで順番を固定しています(web は足りないぶんだけ)

### 検索エンジン(SearXNG)

**立てる手順はありません。本体(`docker-compose.yml`)を上げれば一緒に立ちます。**
web 検索を使うかどうかは、上の `CHIEZO_WEB_SEARCH_URL` を書くかどうかだけで決まります ——
立っているだけでは外へ検索を投げません。

**推論サーバとは独立しています。** 話す相手が Gemini や Claude Code でも web 検索は要るのに、
以前は `--profile llm` に入れていたせいで、検索を使いたいだけで数 GB の推論サーバまで
立ち上げることになっていました。要らない環境では
`docker compose up -d chiezo-app chiezo-trigger` のようにサービスを選んで起動します。

使うのは **Chiezo が agent ループを回す相手だけ**です(ローカルの推論サーバ・Gemini・
OpenRouter・OpenAI)。CLI ブリッジ(Claude Code / Codex / Antigravity)は
**CLI 自身の web 検索**を引くので、SearXNG は通りません。`rag` モードも道具を渡さないので
使いません。

設定は**素の SearXNG に compose の `configs` で流し込みます**(独自イメージもマウントも
作りません)。**焼き込みでもマウントでもいけません**: 上流のイメージは `/etc/searxng` を
`VOLUME` 宣言しているので、焼き込んだ設定は匿名ボリュームに隠れます(実際に 0 バイトの
`settings.yml` に隠され、既定の継承ごと失われて `KeyError: 'default_doi_resolver'` で
起動しなくなっていました)。マウントはリポジトリを置けない環境(単体定義)で使えません。
`configs` はボリュームより深いパスへ置かれるので、どちらの問題も起きません。
設定をいじるときは `docker-compose.yml` 末尾の `configs` を直してください。

**SearXNG の既定は HTML しか返さない**ので、設定で `search.formats` に `json` を
足してあります。無いと Chiezo 側は「JSON ではない」というエラーとして扱います。

```bash
# 動いているか確かめる(ホストには公開していないのでコンテナの中から叩く)
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

## 相手を選ぶ（管理画面から）

**相手は管理画面（`/admin` の「AI の相手」）から on/off します。** `.env` に書くことはありません。

節の先頭に**「答える」層そのものの元栓**があります。止めると、相手をいくつ有効にしてあっても
`/v1/ask`・`/ai/chat` は 503 になります（相手を 1 つずつ切って回らずに機能ごと止めたいとき用）。
相手の設定はそのまま残るので、再開すれば元どおりです。

| 相手 | 使えるようにするには |
|---|---|
| 推論サーバ（同梱の llama.cpp） | `--profile llm` で立ち上げる → on |
| Gemini | API キーを登録 → on |
| OpenRouter | API キーを登録 → on |
| Claude Code | ブリッジのコメントを外して起動 → 認証情報を登録 → on |
| Codex CLI | 同上 |
| Antigravity CLI | ブリッジのコメントを外して起動 → **コンテナ内で1回サインイン** → on |

**CLI の認証情報も管理画面から登録します。** ブリッジのコンテナが設定 DB（`/state`）を
**読み取り専用でマウント**して、要求のたびに読むためです。chiezo-app に「トークンを返す口」を
開けずに済むのが要点で、認証なしの LAN サービスにそんな口は足したくありません。
**登録し直してもブリッジの再起動は要りません。**

**同居の推論サーバも外部のサービスも CLI も、扱いは全部同じです。** 特別扱いする相手はありません。
相手の URL は 1 つに決まっている（Gemini の OpenAI 互換の口は 1 つだけ、コンテナ名は compose で
決まっている）ので、`app/providers.py` に決め打ちしてあります。決まっているものを設定にすると、
書き間違いの余地を増やすだけなので置いていません。

**入れるのは 3 つだけです。**

| 決めること | どこで | 備考 |
|---|---|---|
| 使うかどうか（on/off） | 管理画面 | 条件を満たすまで on にできない（下記） |
| API キー | 管理画面 | 値は二度と表示しない（登録の有無と日時だけ） |
| どのモデルを使うか | **会話のたびに** `/ai/chat` のセレクト | 候補は相手に聞き、聞けなければコードの控え |

保存先は `/state`（compose がマウント済み。`CHIEZO_STATE_DIR`）。ここが無い環境では
どの相手も有効にできません。

### 推論サーバを別マシンで動かしている場合

**`CHIEZO_LLM_URL` にその URL を書きます。** これが唯一の例外で、コンテナ名で辿り着けない
相手のための逃げ道です（IP は環境ごとに違うので決め打ちにできない）。
「相手を増やす設定」ではないので、他の相手には同種の変数はありません。

```bash
# .env
CHIEZO_LLM_URL=http://192.0.2.10:11434/v1   # 別マシンの Ollama 等
```

認証を掛けているなら、API キーは管理画面から入れます（推論サーバは鍵が「任意」の扱い）。

### まず「接続を試す」

**「接続を試す」が一度でも通るまで、その相手は on にできません。**

**会話は 1 往復もせず**、`/models` を引くだけ（CLI ブリッジは `claude auth status` 等を
CLI に聞かせる）ので、サブスクの枠を食いません。

登録の有無だけでは、打ち間違えた認証情報や期限切れは分かりません —— 会話して初めて
失敗し、原因が追いにくくなります（実際に本番で 502 として出ました）。到達できることと
話せることも別です（認証情報が間違っていても到達はする）。

**認証情報を入れ替えると、確認済みの印は消えます。** 新しい情報はまだ確かめていないためです。
一度通ったあとに失敗したときも消えるので、壊れた相手が有効なまま残りません。

### on にできる条件

- **認証情報の要る相手（Gemini / OpenRouter / Claude Code / Codex CLI）** … 未登録なら
  on にできません。消すと同時に無効になります（認証情報の無い相手を有効のまま残すと、
  会話のたびに失敗するだけなので）
- **すべての相手** … 「接続を試す」が通っていなければ on にできません

管理画面は**描画のときに相手へ問い合わせません**（記録された確認結果だけを見ます）。
以前は毎回ブリッジの到達確認をしていましたが、立っていない相手の数だけ表示が遅れるうえ、
「到達できる」は「話せる」の保証になっていませんでした。

> ⚠️ Chiezo は認証なし・LAN 内前提です。ここに入れた API キーは、**管理画面を開ける人なら
> 誰でも差し替えられます**（値の表示はしませんが、書き換えは防げません）。

## 使用量を見る（枠の残りと、Chiezo が使ったぶん）

管理画面の「使用量」節（`/admin#ai-usage`）と `GET /v1/ai/usage` で、相手ごとの
使用量が見られます。**数が 2 つあるのは、測っているものが違うから**です。

| | 何の数か | 分かること | 相手 |
|---|---|---|---|
| 相手が言う枠 | 相手の勘定 | **残り**（使用率と、いつ戻るか） | 下の表の 4 つだけ |
| Chiezo が使ったぶん | Chiezo の勘定 | 回数とトークン。**残りは分からない** | 全部 |

「Chiezo が使ったぶん」には **Chiezo を通していない利用が入りません**（手元の端末で
回した Claude Code など）。逆に「相手が言う枠」はそれも含んだ、相手の側の勘定です。

### 枠を聞ける相手と、その聞き方

| 相手 | 聞き方 | 備考 |
|---|---|---|
| Codex CLI | ブリッジの `/usage` → `codex app-server` の `account/rateLimits/read` | **CLI に聞く**ので、期限切れになる access_token の更新はあちらがやる |
| Antigravity CLI | ブリッジの `/usage` → `agy -p /usage` | **クレジット残高(`/credits`)ではなくモデルの枠**。窓はグループ × 週/5 時間 |
| OpenRouter | `GET /api/v1/key` | クレジットの使用額と残高 |
| ElevenLabs | `GET /v1/user/subscription` | 鍵だけで引ける。**声・効果音・曲・絵・動画が同じ 1 つの残量**を食う |

**絵と音だけの相手も同じ表に並びます。** ElevenLabs は枠を聞けます（この 1 つの残量から
声・効果音・曲・絵・動画のすべてが引かれるので、見えないと「作れなくなった理由」が
画面から分かりません）。相手は文字数（`character_count` / `character_limit`）と呼びますが、
料金表とダッシュボードの呼び名はクレジットなので画面ではそちらに合わせています。
自前の GPU（ComfyUI）には枠がありません（電気代だけ）。

**Gemini・OpenAI・推論サーバには聞く口がありません。** Gemini の残量は Google Cloud の
Quotas API 側にあり、OpenAI の使用量は Admin キー（`sk-admin-…`）が要るので、
どちらもここに入れる鍵では引けません。画面には「この相手は枠を出さない」と出ます
（空欄にすると「使っていない」と読めてしまうため）。

**Claude Code CLI もこちら側です。** CLI に出口が無く（サブコマンドが無く、`/usage` は
対話画面の中だけ）、CLI 自身が叩いている口は `user:profile` を要求するのに対し、
Chiezo が預かるのは `claude setup-token` の長期トークン —— 推論だけに絞られていて
このスコープを持ちません。**取れないものをエラーとして出し続けても打つ手が無い**ので、
「枠を出さない相手」に倒してあります（取り直しのボタンも出しません）。
取れるようにする道はあります（下記）。

**どの聞き方もモデルを呼びません。** 確かめるたびにサブスクの枠を食っては本末転倒なので、
「接続を試す」と同じ方針です。

### 開いたときには聞きに行かない

管理画面は**描画のときに相手へ問い合わせません**（「接続を試す」と同じ流儀）。控えてある
値と「いつ取ったか」を出し、取り直しはボタンで —— 落ちている相手がいると、
その数だけ画面が遅れるためです。**取れなかったときも、直前まで見えていた数字は消しません**
（一時的に繋がらないだけのことがあるので、値と失敗の理由を並べて出します）。

表の上に**まとめて取り直すボタン**もあります（`POST /admin/ai/usage/all`）。**並行に聞く**ので
待つのは一番遅い相手のぶんだけで、**1 つ落ちていても残りは取り直します** —— 結果は
「N 件を取り直しました（取れなかった相手: …）」と、**取れた数と落ちた相手の名前**で返します
（「全部取り直しました」とだけ書くと、落ちている相手に気づけないため）。
**鍵が未登録の相手には行きません**（手前で理由になる）。

聞くのは**「使う」にしてある相手だけ**です。呼ばない相手の残りを見ても仕方がないうえ、
鍵を外したまま残している相手が毎回「取れませんでした」に数えられるためです。
対象が 1 つも無いときはボタンを出しません（押しても何も起きないボタンは、壊れているのか
設定が足りないのか読めない）。**1 件ずつの「取り直す」は off の相手でも押せます** ——
あちらは名指しの操作なので。

```bash
# 控えてある値（外へは問い合わせない）
curl -s "$BASE/v1/ai/usage" | jq .

# 取り直す（相手に聞きに行く。1 相手だけなら backend も指定する）
curl -s "$BASE/v1/ai/usage?refresh=1&backend=codex" | jq '.backends[0].quota'

# 全部取り直す（backend を付けなければ、枠を聞ける相手に並行で聞く）
curl -s "$BASE/v1/ai/usage?refresh=1" | jq '.backends[] | {id, quota}'
```

```json
{
  "supported": true,
  "fetched_at": "2026-08-24T00:02:11+00:00",
  "error": "",
  "windows": [
    {"id": "five_hour", "label": "セッション(5 時間)", "used_percent": 42.5,
     "remaining_percent": 57.5, "resets_at": "2026-08-24T06:00:00+00:00",
     "used": null, "limit": null, "unit": ""}
  ]
}
```

`spent` のほうは窓ごと（`5h` / `24h` / `7d`）に回数とトークン数が入ります。
**トークン数の `unknown_tokens` は「相手が言わなかった回数」**で、0 とは別物です ——
CLI ブリッジ経由の相手はトークン数を返さないので、回数だけが確かな数になります。
**絵・音・動画・読み上げの呼び出しも同じ表に入ります**（同じサブスクの枠を食うため）。

記録は `state/usage.db` に置き、既定で 30 日で消えます（`CHIEZO_USAGE_KEEP_DAYS`）。
`CHIEZO_STATE_DIR` が無い環境では記録も表示もされません。

### どこまで確かめてあるか

- **Codex** …… サインイン済みの応答まで実測しました（2026-08）。未サインインの
  `CODEX_HOME` では
  `{"error":{"code":-32600,"message":"codex account authentication required to read rate limits"}}`
  が返ります（= メソッドは実在し、手順も合っている）。サインイン済みだと、こう返ります:

  ```json
  {"rateLimits": {"limitId": "codex",
                  "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1788135754},
                  "secondary": null, "credits": {...}, "planType": "plus"},
   "rateLimitsByLimitId": {"codex": { … 上とまったく同じもの … }},
   "rateLimitResetCredits": {...}}
  ```

  読み取りで効いてくるのが 2 点あります。**同じ枠が `rateLimits` と
  `rateLimitsByLimitId.<id>` の両方に入っている**ので、入れ子のどこからでも拾う書き方の
  ままだと同じ窓が 2 行並びます（`_dedupe` で中身の同じものを 1 つにまとめています）。
  **鍵が camelCase**（`windowDurationMins` / `resetsAt`）なので、snake_case だけを見ると
  窓の長さと明ける時刻を落とし、名前が相手の内部名（`primary`）のまま画面に出ます。
  どちらも実際にそうなっていたので直しました。`secondary` は plus では null（週の窓 1 本だけ）。
- **Antigravity** …… サインイン済みの応答まで実測しました（2026-08）。**聞くのは
  `/usage`（モデルの枠）で、`/credits` ではありません。** あちらは有償プランの
  クレジット残高という別の勘定で、**「残り 0」でも推論はできます**（実測。CLI の
  changelog にも「空のクレジット応答を残高 0 と読む」不具合の記述があります）——
  かつて `/credits` を引いていたときは、枠を使い切ったように見える 0 が画面に出ていました。

  `/usage` はこう返します:

  ```json
  {"response": "Gemini Models\tWeekly Limit Remaining\t99%\t2026-08-30T01:45:06Z\n…",
   "command": {"name": "usage", "data": {"groups": [
     {"name": "Gemini Models", "buckets": [
       {"id": "gemini-weekly", "name": "Weekly Limit Remaining", "window": "weekly",
        "remaining_fraction": 0.986, "reset_time": "2026-08-30T01:45:06Z"}, …]}, …]}}}
  ```

  窓は **モデルのグループ 2 つ（Gemini / Claude と GPT）× 週・5 時間 の 4 つ**。
  読み方で 2 点あります。**グループ名は窓の兄弟ではなく親に付いている**ので、
  入れ子のどこからでも拾う書き方では名前を作れません（同じ "Weekly Limit Remaining" が
  2 つ並ぶ）—— この形だけは専用に読みます。相手が言うのは **残り**
  （`remaining_fraction`）なので、画面の使用率にそろえるためこちらで裏返します。
  `command.data` の形が変わったときは、同じ内容がタブ区切りで入っている `response` を読みます。
  コマンド自体は `CHIEZO_BRIDGE_USAGE_CMD`（カンマ区切り。既定
  `agy,-p,/usage,--output-format,json`）で差し替えられます。
- **Claude** …… **`claude setup-token` の長期トークンでは枠を取れません**(実測)。
  `/api/oauth/usage` は `user:profile` スコープを要求しますが、長期トークンは安全のため
  推論だけに絞られているためです:

  ```
  HTTP 403 {"type":"error","error":{"type":"permission_error",
   "message":"OAuth token does not meet scope requirement user:profile"}}
  ```

  **会話・要約・絵の生成には影響しません**(そちらは `user:inference` で足ります)。
  取れないものをエラーとして出し続けても打つ手が無いので、**いまは経路を持たず
  「この相手は枠を出さない」と書きます**。Claude の枠は CLI の `/usage`(対話画面)で
  見てください。`/api/oauth/usage` 自体は CLI の中から見つけたもので、公開ドキュメントには
  ありません。

  **取れるようにする道はあります。** `claude auth login`(通常のサインイン)の資格情報は
  スコープに `user:profile` を含み、同じ口が実測で 200 を返します:

  ```
  five_hour → utilization 41.0%（4:29 に戻る） / seven_day → utilization 8.0%
  ```

  ただしあれは**アクセストークンが数時間で切れ、CLI が自分で更新するもの**なので、
  設定 DB に貼り付ける形には向きません。やるなら codex・antigravity と同じく
  **コンテナの中で 1 回サインインする**形になります —— `chiezo-bridge-claude` に
  書き込めるホーム（名前付きボリューム）を渡し、`docker compose exec
  chiezo-bridge-claude claude auth login` を 1 回実行し、ブリッジ側に `/usage` を足して
  `$HOME/.claude/.credentials.json` の accessToken で引く（古ければ先に
  `claude auth status` を走らせれば、モデルを呼ばずに更新されます）。
  そこまでやる価値があると判断したときに入れること。

**数字にできなかったときは、CLI や相手が返した文言をそのまま画面に出します**
—— 推測で数字は作りません。

## CLI の AI と話す（ブリッジ）

これらは HTTP ではなく CLI なので、そのままでは指せません。サブスクの枠で使うには
CLI を通すしかない（API キー経路は従量課金になる）ため、**OpenAI 互換の口に見せる
コンテナ**を挟みます（`bridge/`）。

**イメージは 1 つで、`CHIEZO_BRIDGE_CLI` で役割を決めます。** イメージは 1 回 pull すれば
ディスクは 1 つぶんで、コンテナを何個立てても増えるのは書き込み層（数 KB）だけなので、
CLI ごとに分けず 1 枚にまとめてあります。

**MCP は任意です**（`CHIEZO_BRIDGE_MCP_URL` を空にすると繋ぎません）。Chiezo 専用の部品では
なく、**「CLI を OpenAI 互換の口に見せるサービス」として他のアプリからも使えます** ——
postgres を別コンテナで立てて複数のアプリが繋ぐのと同じ形です。

ブリッジは **Chiezo の MCP（`/mcp`）を CLI に繋ぎます**。つまり検索して答える段取りは
ブリッジ側で組まず、道具は CLI 自身が引きます —— Claude Code も Codex も、道具を自分で
回すのが本業だからです。Chiezo 側から見ると「1 回聞いたら答えが返る」ので、
`rag` / `agent` の区別は関係なくなります。

安全のために、**CLI の組み込みの道具（シェル・ファイルの読み書き・web 取得）は塞いで**
あります（`--disallowed-tools`）。使えるのは Chiezo の MCP だけです。

**`--tools ""`（組み込みを全部切る指定）は使えません** —— これは MCP の道具まで消します。
つまり「Chiezo の知識を引かせる」というブリッジの目的が黙って働かず、CLI が自分の記憶
だけで答える状態になります（本番でそうなっていました）。**`ToolSearch` も塞げません**
——MCP の道具はそこから読み込まれるためです。

**塞ぐ一覧は取りこぼしうる**点に注意してください。組み込みの道具は CLI の版が上がるたび
増え、**新しいものは既定で使えてしまいます**。`CHIEZO_BRIDGE_DISALLOWED_TOOLS` で
差し替えられるので、CLI を上げたら見直してください。

### compose のファイルが無い環境で立てる

NAS のコンテナマネージャーのように**リポジトリを置けない環境**でも、ブリッジは立てられます。
守るのは 2 つだけです。

- **コンテナ名を `chiezo-bridge-<CLI 名>` にする**（`chiezo-bridge-claude` など）。
  Chiezo はこの名前で呼びに行きます（URL は `app/providers.py` に決め打ち）
- **chiezo-app と同じ Docker ネットワークに繋ぐ**（名前で解決できないと届きません）

```bash
# Claude Code CLI / Codex CLI —— 認証情報は設定 DB から読むので、読み取り専用で渡す
docker run -d --name chiezo-bridge-claude --network <chiezo と同じネットワーク> \
  -v <state のパス>:/state:ro \
  -e CHIEZO_BRIDGE_CLI=claude -e CHIEZO_BRIDGE_MCP_URL=http://chiezo-app:7010/mcp \
  --restart unless-stopped ghcr.io/rtcode337/chiezo-bridge:latest
```

**Antigravity CLI だけ手順が違います。API キーでは動きません** —— 鍵を渡しても
Google アカウントのサインインを求められます（`GEMINI_API_KEY` を入れて実行すると、
鍵を見ずに OAuth の URL を出して待ちに入るのを実測）。なので**書き込めるホームをバインドして、
コンテナの中で 1 回サインイン**します。

```bash
docker run -d --name chiezo-bridge-antigravity --network <chiezo と同じネットワーク> \
  -v chiezo-antigravity-home:/srv/bridge/home \
  -e CHIEZO_BRIDGE_CLI=antigravity -e CHIEZO_BRIDGE_MCP_URL=http://chiezo-app:7010/mcp \
  --restart unless-stopped ghcr.io/rtcode337/chiezo-bridge:latest

# サインイン（1 回だけ。表示された URL を手元のブラウザで開き、出たコードを貼り戻す）
docker exec -it chiezo-bridge-antigravity agy
```

**ホームは名前付きボリュームにしてください。** ホストのディレクトリをバインドすると、
こうなります:

```
failed to complete onboarding: failed to save settings:
open /srv/bridge/home/.gemini/antigravity-cli/settings.json.<...>.tmp: permission denied
```

ブリッジは**非 root（uid 10001）で動く**のに、**バインド先が存在しないと Docker が
root 所有で作る**ためです。名前付きボリュームなら**イメージ側の所有者（uid 10001）が
そのまま入る**ので、この問題が起きません。どうしてもホストのパスに置きたいときは、
先に作って `sudo chown -R 10001:10001 <ディレクトリ>` してからバインドしてください
（既に踏んでしまった場合も、同じ chown で直ります）。

**uid を 1000 から 10001 へ変えた前後で立てていた場合は、1 回だけ付け替えが要ります。**
既にある名前付きボリュームの中身は uid 1000 のままなので、新しいイメージ（10001）は
そこへ書けません（症状はサインインの保存に失敗し、`ERROR: logging before google.Init`
などが出る）。**サインインし直す必要はなく、所有者を付け替えるだけで戻ります。**

**動いているコンテナに root で入って直すのが一番手数が少ない**（止めなくてよい）。
コンテナ管理画面のコンソールを root で開ける環境なら、そこから 1 行:

```bash
docker exec -u 0 chiezo-bridge-antigravity chown -R 10001:10001 /srv/bridge/home
```

ホストで `docker` を打てる環境なら、止めてからボリュームを直してもよい:

```bash
docker stop chiezo-bridge-antigravity
docker run --rm -v <ボリューム名>:/h alpine:3.24 chown -R 10001:10001 /h
docker start chiezo-bridge-antigravity
```

ボリューム名は `docker volume ls | grep antigravity` で確かめてください
（スタック名が前に付くことがあります）。どちらの手順でも、実際に付け替えたあとに
コンテナから書けるようになり、保存済みのサインインもそのまま残ることを確認しています。

**ターミナルを開けないコンテナ管理画面**では、その画面のコンソール機能から
同じコマンドを実行してください。

立てたら管理画面（`/admin` の「AI の相手」）で「接続を試す」→「有効にする」の順に押します。

### 🌐 web 検索 / 📝 覚える は agent モードだけ

会話画面のこの 2 つは **agent モードの道具**で、rag モードでは送っても捨てられます。
押せるのに何も起きない状態を作らないよう、**効かない場面ではトグルを出しません**
（モードや相手を変えると出し入れされます）。

CLI ブリッジ相手のときは、次のように振る舞いが変わります。

| | ブリッジ相手 |
|---|---|
| 🌐 web 検索 | 出る。**ただし引く先は SearXNG ではなく CLI 提供元の検索**（`WebSearch` / `WebFetch` を要求ごとに開ける）。Chiezo 側で web 検索を設定していなくても使える |
| 📝 覚える | **入ったまま触れない**（常に使える）。ブリッジには MCP をまるごと渡していて、`remember` だけ外す口が無いため。使えること自体は見せ、切れるように見せるのだけを避ける |

### 1 回ごとの上限（往復と待ち時間）

| 送るもの | 効く CLI | 意味 |
|---|---|---|
| `chiezo_max_turns` | Claude Code（`--max-turns`） | 道具を引く往復の上限。**ここが総コストの上限**になる（対象が増えてもここから先には伸びない） |
| `chiezo_timeout` | すべて | この 1 回の上限秒数。`CHIEZO_BRIDGE_TIMEOUT` より短くも長くもできる |

同じブリッジを、数十秒で返ってほしい会話と、数分かかる調査の両方で使うためです。
Codex CLI と Antigravity CLI には往復の上限にあたる指定がありません（`--help` で確認）。

### Chiezo 以外のアプリから使う(素の問い合わせ)

**自分のプロンプトと材料を持っているアプリ**は、`/v1/ai/complete` に投げれば
Chiezo に登録した相手をそのまま使えます。**知識ベースは引きません** ——
`/v1/chat` は必ず抽出を混ぜるので、材料が手元にあるアプリには向きません。
鍵は Chiezo が握ったままなので、呼ぶ側は認証情報を持たずに済みます。

```bash
# 話せる相手(管理画面で on にしたもの)と、選べるモデル・エフォート
curl -s "$BASE/v1/ai/backends" | jq .

# 渡したメッセージをそのまま 1 往復投げる(system も使える)
curl -s "$BASE/v1/ai/complete" -H 'Content-Type: application/json' -d '{
  "backend": "gemini",
  "model": "gemini-2.5-flash",
  "messages": [
    {"role": "system", "content": "あなたは技術情報のダイジェストを書く編集者。"},
    {"role": "user", "content": "材料A / 材料B"}
  ]
}' | jq -r .content
```

相手を知らなければ 404(選べる相手の一覧つき)、「答える」層が無効なら 503 です。

### ブリッジを直接使う(認証情報の置き場を共有する)

ブリッジは Chiezo 専用の部品ではありません。**認証情報の置き場を読み取り専用で共有**
すれば、他のアプリからも同じ形で使えます。

**表の形はこちらに合わせてもらいます**（`settings.db` の `provider_settings`）。
問い合わせを相手ごとに変えられるようにすると、繋ぐ先のアプリの数だけ設定が増えるためで、
ディレクトリを共有する取り込みのトリガー（`chiezo-trigger`）と同じ流儀です。

```yaml
volumes:
  - ./state:/state:ro        # アプリ側が settings.db を書くディレクトリ
environment:
  - CHIEZO_BRIDGE_MCP_URL=   # Chiezo の知識は引かない場合
```

```sql
-- アプリ側が用意する表（Chiezo と同じ形）
CREATE TABLE provider_settings (provider TEXT PRIMARY KEY, credential TEXT, ...);
INSERT INTO provider_settings (provider, credential) VALUES ('claude', '<トークン>');
```

**共有する DB を WAL にしないこと** —— WAL の読み手は `-shm` への書き込みを要求するので、
読み取り専用のマウントでは `unable to open database file` になります。

アプリ側が画面からトークンを入れ替えても、**ブリッジは要求のたびに読み直す**ので
再起動は要りません。

### Codex は MCP を引けない(上流の不具合・2026-08 時点)

**`codex exec`(非対話)では MCP の呼び出しが必ずキャンセルされます** ——
Codex が途中でユーザーへの確認を求める経路に入り、非対話ではそれに答えられないため
自動的に取り消されます(`user cancelled MCP tool call`。openai/codex#16685、未修正)。
ブリッジは非対話で回すので、**Codex 相手のときは Chiezo の知識を引けません**
(答え自体は返りますが、モデルの知識だけで書かれます)。

**そのため Codex では agent モードを選べません**(画面のセレクトに出ません)。
代わりに **rag モード**で動きます —— Chiezo 側が検索して抜粋をプロンプトに載せるので、
道具が無くても根拠つきで答えられます。**画像生成は MCP を使わないので影響しません。**

道具を自分で回させたい(「カテゴリ○○の記事は何件?」のような数え上げ)ときは、
**Claude Code か推論サーバを選んでください**。

### agent モードはブリッジ側に任せる

CLI ブリッジ相手のときは、**Chiezo は agent のループを回しません**（1 回聞いて 1 回
受け取ります）。道具を引く段取りは CLI が自分で持っているためで、こちらから渡す `tools`
は受け取ってもらえません。出典は CLI の本文の中に書かれるので、画面の「調べた手順」と
参照リストは空になります。

**コンテナは非 root（uid 10001）で動きます。** claude は権限確認を飛ばす指定を root では
受け付けません（`--dangerously-skip-permissions cannot be used with root/sudo privileges`）。
非対話で動かす以上その指定は外せないので、root だと生成が必ず失敗します —— しかも
`claude auth status` は root でも通るため、**「接続を試す」は成功したまま会話だけが 502**
という分かりにくい壊れ方をします（実際に踏みました）。

そのため **Antigravity のホームにバインドするディレクトリは uid 10001 が書けること**が要ります
（名前付きボリュームにすれば不要です。上の「compose のファイルが無い環境で立てる」を参照）。
書けないとサインインが残りません。

```bash
# 1) docker-compose.yml の chiezo-bridge-* のコメントを外して立ち上げる
#    （profile も追加の -f も要らない。イメージは GHCR から pull される）
docker compose up -d

# 2) 認証情報を手元で作る
claude setup-token                 # → 出てきたトークン
codex login --device-auth          # → ~/.codex/auth.json の中身

# 3) 管理画面（/admin）の「AI の相手」でそれを登録し、on にする
```

環境変数（`CLAUDE_CODE_OAUTH_TOKEN` / `CODEX_AUTH_JSON`）でも渡せます —— 設定 DB を
マウントできない環境向けの逃げ道で、DB に無ければそちらへ落ちます。

ブリッジのイメージ（`ghcr.io/rtcode337/chiezo-bridge`）には両方の CLI が入っていて、
`CHIEZO_BRIDGE_CLI` で役割を決めます。

| 環境変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_BRIDGE_CLI` | `claude` | 包む CLI（`claude` / `codex` / `antigravity`） |
| `CHIEZO_BRIDGE_MCP_URL` | `http://chiezo-app:7010/mcp` | CLI に繋ぐ Chiezo の MCP。**空にすると繋がない** |
| `CHIEZO_BRIDGE_STATE_DB` | `/state/settings.db` | 認証情報を読む Chiezo の設定 DB（読み取り専用でマウント） |
| `CHIEZO_BRIDGE_MODEL` | （CLI の既定） | 何も選ばれなかったときのモデル。会話画面で選んだものが優先される |
| `CHIEZO_BRIDGE_MODELS` | （下記） | 会話画面に出すモデルの候補（カンマ区切り） |
| `CHIEZO_BRIDGE_EFFORTS` | （下記） | 会話画面に出す「考える量」の候補（カンマ区切り） |
| `CHIEZO_BRIDGE_ALLOWED_TOOLS` | `mcp__chiezo` | CLI に許す道具。書き込み（`remember`）まで止めるならここを絞る |
| `CHIEZO_BRIDGE_DISALLOWED_TOOLS` | （組み込み一式） | 塞ぐ組み込みの道具。**`ToolSearch` を入れないこと**（MCP が引けなくなる）。`WebSearch` / `WebFetch` は要求ごとに開く |
| `CHIEZO_BRIDGE_TIMEOUT` | `300` | 1 回の上限（秒）。CLI は道具を何度も引くので推論サーバより長い |
| `CHIEZO_BRIDGE_USAGE_CMD` | `agy,-p,/credits,--output-format,json` | 使用量を聞くコマンド（Antigravity のみ。カンマ区切り）。**実地で確かめられていない**ので差し替えられるようにしてある |
| `CHIEZO_BRIDGE_USAGE_TIMEOUT` | `60` | 使用量を聞くときの上限（秒） |

### モデルは会話ごとに選べる

会話画面のモデル選択はブリッジにも効きます（`/v1/models` が名乗ったものが並ぶ）。
**一覧に無い名前も通ります** —— CLI は正式名（`claude-fable-5` など）も受け付けるためで、
間違っていれば CLI のエラーがそのまま画面に出ます。`-` で始まる名前だけは拒みます
（引数として渡すので、CLI のフラグとして解釈されてしまうため）。

| CLI | 候補 | 備考 |
|---|---|---|
| Claude Code | `sonnet` / `fable` / `opus` / `haiku` | `claude --help` のエイリアス（4 つとも実測） |
| Codex CLI | （なし） | 一覧を出す口が無い |
| Antigravity CLI | （なし） | `agy models` はあるが、サインイン済みでないと何も返さない |

**確かめていない ID は並べません** —— 画面には出るのに選ぶと必ず失敗する選択肢に
なるためです。入れたい場合は `CHIEZO_BRIDGE_MODELS` で渡してください。

先頭に**「モデル（既定）」**があり、これを選ぶと**何も渡しません**（相手が自分で決めます）。
Claude Code の既定は `claude-sonnet-5` です。ただし **Gemini や OpenRouter のように
モデルの指定が要る相手では、「既定」を選んでも控えの先頭が当たります**（モデル無しでは
通らないため）。

一覧の先頭は「何も選ばなかったときに使われるもの」に揃えてあります（画面の見出しは
一覧の先頭を名乗るので、ずらすと使っていないモデル名が出ます）。

### 考える量（エフォート）も選べる

会話画面の「考える量」は、持っている CLI のときだけ出ます。

| CLI | 段階 | 由来 |
|---|---|---|
| Claude Code | `low` / `medium` / `high` / `xhigh` / `max` | `claude --help`（5 つとも実測） |
| Antigravity CLI | `low` / `medium` / `high` | `agy --help`（`xhigh` / `max` は無い） |
| Codex CLI | （なし） | `codex exec --help` に無い |

**モデル名と違い、一覧に無い値は 400 で拒みます。** claude は `--effort bogus` を
エラーにも警告にもせず**黙って既定で動く**（実測）ので、通すと「選んだのに効いていない」
ことに誰も気づけません。選ばなければ何も渡さず、CLI の既定に任せます。

Chiezo からは OpenAI 互換の `reasoning_effort` として送られます
（`/v1/chat` の `effort`、`/v1/ask` の `?effort=`）。

**いまは応答を待ち切ってから流します**（差分では流れません）。CLI ごとに
`--output-format stream-json` / `--json` を解析すれば差分にできますが、2 つ分の解析を
抱えるだけの値打ちがまだ無いと判断しています。

## 別の推論サーバに向ける

Chiezo が要求するのは OpenAI 互換の `/v1/chat/completions` だけなので、`CHIEZO_LLM_URL` を
差し替えれば Ollama・LM Studio・GPU 付きの別マシンなど何にでも向けられます
(その場合 `--profile llm` は不要です)。

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
docker compose -f docker-compose.yml -f docker-compose.llm.cuda.yml --profile llm up -d
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

**配信機に同居させないでください。** chiezo-app 自体は従来どおり数百 MB で動きますが、
推論はモデルサイズぶんのメモリを持っていきます。小型の配信機で使うなら、推論は
LAN 上の別マシンに置いて `CHIEZO_LLM_URL` で指すのが素直です。

## 環境変数

chiezo-app 側:

| 変数 | 既定 | 説明 |
|---|---|---|
| `CHIEZO_STATE_DIR` | (未設定 = 管理画面から相手を増やせない) | 相手の設定(on/off・API キー・モデル)の置き場 |
| `CHIEZO_LLM_URL` | (未設定) | **環境変数でしか指せない相手**の OpenAI 互換ベース URL。パスを持たない URL にだけ `/v1` を補う(Gemini のように既にパスがある相手には足さない) |
| `CHIEZO_LLM_LABEL` | `推論サーバ` | その相手を選ぶセレクトに出す名前 |
| `CHIEZO_LLM_MODEL` | `chiezo` | リクエストに載せるモデル名。llama-server は 1 プロセス 1 モデルなので何でもよい |
| `CHIEZO_LLM_API_KEY` | (なし) | 設定すると `Authorization: Bearer` を送る |
| `CHIEZO_ANSWER_TIMEOUT` | 相手による | 相手を待つ時間(秒)。DB クエリの 5 秒とは別枠。**CLI ブリッジ経由は 900、API で直に叩く相手と推論サーバは 120**。ブリッジは道具を何度も引くので桁が違ううえ、ブリッジ自身の上限(`CHIEZO_BRIDGE_TIMEOUT`。既定 300 / compose 600)より**待つ側が長くないと**、向こうの判断が見えないまま ReadTimeout になる |
| `CHIEZO_ANSWER_DOCS` | `4` | 根拠として本文を取ってくる文書数 |
| `CHIEZO_ANSWER_MAX_CHARS` | `6000` | 抜粋の合計文字数の上限 |
| `CHIEZO_ASK_DEFAULT_MODE` | `rag` | `mode` を省いたときの既定 |
| `CHIEZO_ASK_DEFAULT_GROUNDED` | `1` | `grounded` を省いたときの既定 |
| `CHIEZO_USAGE_KEEP_DAYS` | `30` | 使用量の記録を残す日数（`state/usage.db`）。集計の窓は最長 7 日 |

**CPU 推論では `CHIEZO_ANSWER_MAX_CHARS` を下げてください。** 所要時間は抜粋の長さ
(プロンプト処理)がほぼ支配します。実測は
[設計メモ](design-notes.md#使う層はなぜ-2-段の-rag-か)にあります。

推論コンテナ側(`chiezo-llm`)は `CHIEZO_LLM_HF_REPO`(モデル)と `CHIEZO_LLM_CTX_SIZE`
(コンテキスト長、既定 8192)で調整します。それ以外の項目は llama-server の
`LLAMA_ARG_*` 環境変数を compose に足せば効きます。

推論コンテナのイメージはタグを固定してあります(`server-b10156`)。dependabot は
`app/` `ingest/` の Dockerfile しか見ていないので、更新するときは compose の
このタグを手で上げてください(タグ一覧は
[GHCR](https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp))。
