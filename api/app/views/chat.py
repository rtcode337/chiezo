"""会話画面(`/localllm/chat`)。「使う」層のブラウザ側。

答えを組み立てるのは `app/answer.py` / `app/agent.py` で、ここがするのは画面と
その JS を返すことだけ(本文は `/v1/chat` へ POST して SSE で受け取る)。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import agent, answer, notes, websearch
from app.pages import CHAT_PATH, CHAT_STYLE, esc, page_shell
from app.registry import Source

log = logging.getLogger("chiezo.api")

router = APIRouter()

# 会話画面の JS。**この画面だけ JS を使う**(他の画面は従来どおり JS なし)理由は 2 つ:
# 回答まで数十秒かかるので逐次表示しないと無反応に見えること、会話の履歴を持つ主体が
# クライアント側だからこと。EventSource ではなく fetch を使うのは、履歴を送るのに
# POST が要るため(EventSource は GET しか張れない)。
CHAT_JS = """
(function () {
  var log = document.getElementById('log');
  var form = document.getElementById('chat');
  var input = document.getElementById('q');
  var send = document.getElementById('send');
  if (!log || !form || !input || !window.fetch) return;
  var history = [];   // 会話の主体はここ。サーバーは状態を持たない
  var busy = false;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function atBottom() {
    return log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  }
  function toBottom() { log.scrollTop = log.scrollHeight; }

  function turn(who, cls) {
    var t = el('div', 'turn ' + cls);
    var body = el('div', 'text', '');
    t.appendChild(body);
    log.appendChild(t);
    toBottom();
    return {node: t, text: body};
  }
  function addStep(t, s) {
    if (!t.steps) {
      t.steps = el('details', 'steps');
      t.stepSummary = el('summary', null, '調べている…');
      t.stepList = el('ol');
      t.steps.appendChild(t.stepSummary);
      t.steps.appendChild(t.stepList);
      t.node.appendChild(t.steps);
    }
    t.stepList.appendChild(el('li', null,
      s.tool + ' ' + JSON.stringify(s.arguments) + ' → ' + s.summary));
    t.stepSummary.textContent = '調べた手順(' + t.stepList.children.length + ')';
  }
  function addRefs(t, list) {
    if (!list.length) return;
    var box = el('div', 'refs');
    list.forEach(function (r) {
      // タイトルは < や " を含みうるので innerHTML では組み立てない
      var a = el('a', null, (r.source === 'web' ? '🌐 ' : r.source + ' / ') + r.title);
      a.href = r.url;
      a.title = r.title;
      if (r.source === 'web') { a.target = '_blank'; a.rel = 'noreferrer'; }
      box.appendChild(a);
    });
    t.node.appendChild(box);
  }

  function settings() {
    var web = document.getElementById('web'), notes = document.getElementById('notes');
    return {
      source: document.getElementById('source').value || null,
      grounded: document.getElementById('grounded').value === '1',
      mode: document.getElementById('mode').value,
      web: web ? web.checked : null,
      notes: notes ? notes.checked : null
    };
  }

  function send_(text) {
    if (busy || !text) return;
    busy = true;
    if (send) send.disabled = true;
    var empty = document.getElementById('empty');
    if (empty) empty.remove();
    turn('you', 'you').text.textContent = text;
    history.push({role: 'user', content: text});
    var t = turn('bot', 'bot');
    t.text.classList.add('pending');
    t.text.textContent = '考えています…';
    var first = true, answer = '';
    var opts = settings();
    opts.messages = history;
    fetch('/v1/chat?stream=1', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(opts)
    }).then(function (res) {
      if (!res.ok) { throw new Error('HTTP ' + res.status); }
      var reader = res.body.getReader(), decoder = new TextDecoder(), buf = '';
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) return;
          buf += decoder.decode(chunk.value, {stream: true});
          var frames = buf.split('\\n\\n');
          buf = frames.pop();
          var stick = atBottom();
          frames.forEach(function (frame) {
            var ev = /^event: (.*)$/m.exec(frame), da = /^data: (.*)$/m.exec(frame);
            if (!ev || !da) return;
            var data = JSON.parse(da[1]);
            if (ev[1] === 'step') { addStep(t, data); }
            else if (ev[1] === 'references') { addRefs(t, data.references || []); }
            else if (ev[1] === 'delta') {
              if (first) { t.text.textContent = ''; t.text.classList.remove('pending'); first = false; }
              answer += data.text;
              t.text.textContent = answer;
            } else if (ev[1] === 'error') {
              t.text.classList.remove('pending');
              t.text.textContent += '\\n[エラー] ' + (data.error || '');
            }
          });
          if (stick) toBottom();
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      t.text.classList.remove('pending');
      t.text.textContent += '\\n[通信に失敗しました: ' + e.message + ']';
    }).then(function () {
      // 失敗しても履歴には残す(次の発言で文脈が飛ぶのを避ける)
      history.push({role: 'assistant', content: answer || '(応答なし)'});
      busy = false;
      if (send) send.disabled = false;
      input.focus();
    });
  }

  function submit() {
    var text = input.value.trim();
    input.value = '';
    input.style.height = 'auto';
    send_(text);
  }
  form.addEventListener('submit', function (e) { e.preventDefault(); submit(); });
  // Enter で送信・Shift+Enter で改行(日本語入力の変換中は送らない)
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  // 入力に合わせて高さを伸ばす(上限は CSS の max-height)
  input.addEventListener('input', function () {
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  });
  // web 検索と「覚える」は agent モードの道具なので、rag のときは選べないことを見せる
  var mode = document.getElementById('mode');
  var toggles = [
    [document.getElementById('web'), 'web 検索は agent モードの道具です'],
    [document.getElementById('notes'), '「覚える」は agent モードの道具です']
  ];
  function syncToggles() {
    var off = mode.value !== 'agent';
    toggles.forEach(function (pair) {
      var box = pair[0];
      if (!box) return;
      box.disabled = off;
      box.parentNode.classList.toggle('on', box.checked && !off);
      box.parentNode.title = off ? pair[1] : '';
    });
  }
  if (mode) mode.addEventListener('change', syncToggles);
  toggles.forEach(function (pair) {
    if (pair[0]) pair[0].addEventListener('change', syncToggles);
  });
  syncToggles();

  Array.prototype.forEach.call(document.querySelectorAll('.empty button'), function (b) {
    b.addEventListener('click', function () { send_(b.textContent); });
  });
  if (form.dataset.first) { send_(form.dataset.first); }
})();
"""


# 会話の画面。**ローカル LLM を使う側の機能**なので `/localllm/` の下に置く
# (Chiezo 本体の画面 = /admin と /search/… とは並びで区別できるようにする)。
@router.get("/localllm/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    q: str | None = Query(None),
    source: str | None = Query(None),
    nojs: bool = Query(False, description="JS を使わず、1 問 1 答で表示する"),
    grounded: bool | None = Query(None, description="Chiezo で取れたことだけを根拠にする"),
    mode: str | None = Query(None, pattern="^(rag|agent)$", description="rag / agent"),
):
    mode = mode or answer.default_mode()
    grounded = answer.default_grounded() if grounded is None else grounded
    sources: dict[str, Source] = request.app.state.sources
    options = '<option value="">(自動)</option>' + "".join(
        f'<option value="{esc(name)}"{" selected" if name == source else ""}>{esc(name)}</option>'
        for name in sorted(sources)
    )
    # チェックボックスではなく select にしてある。チェックボックスは off のとき何も
    # 送らないので hidden との併用が要り、その場合 grounded=0&grounded=1 の 2 値が
    # 飛んで FastAPI は先頭(=0)を採る。select なら必ず 1 値だけ送られる。
    grounded_options = "".join(
        f'<option value="{value}"{" selected" if (value == "1") == grounded else ""}>{label}</option>'
        for value, label in (("1", "Chiezo で取れたことだけ"), ("0", "モデルの知識で補ってよい"))
    )
    mode_options = "".join(
        f'<option value="{value}"{" selected" if value == mode else ""}>{label}</option>'
        for value, label in (("rag", "1 回検索して答える"), ("agent", "モデルに道具を引かせる"))
    )
    # 設定は**入力欄の下**に並べる(会話中に触るのは稀なので、視線の主役にしない)。
    # web 検索は設定してある環境でだけ出し、**やり取りごとに切れる**トグルにする。
    web_toggle = (
        '<label class="toggle on" id="web-toggle">'
        '<input type="checkbox" id="web" checked>🌐 web 検索</label>'
        if websearch.is_enabled() else ""
    )
    # 「覚えておいて」に応えられるようにする道具(Chiezo で唯一の書き込み)。
    # 何を書いたかは「調べた手順」に出るので、勝手に増えたときも後から分かる。
    notes_toggle = (
        '<label class="toggle on" id="notes-toggle">'
        '<input type="checkbox" id="notes" checked>📝 覚える</label>'
        if notes.is_enabled() else ""
    )
    settings = f"""
<div class="composer-settings">
<select id="source" name="source" title="引くソース">{options}</select>
<select id="mode" name="mode" title="引き方">{mode_options}</select>
<select id="grounded" name="grounded" title="根拠の扱い">{grounded_options}</select>
{web_toggle}
{notes_toggle}
<span class="hint">Enter で送信</span>
</div>
"""
    if not answer.is_enabled():
        # 無効でも入力欄そのものは出す(何をする画面なのかが分からないと、
        # 「壊れている」のか「使っていない機能」なのか見分けが付かない)。
        body = """
<div class="chat-page">
<div class="chat-head"><h1>AI と話す</h1><span class="spacer"></span>
<a href="/admin">管理画面</a></div>
<div class="log">
<p class="stale">「使う」層は無効です。</p>
<p class="muted">推論サーバの OpenAI 互換 URL を <code>CHIEZO_LLM_URL</code> に設定すると
有効になります(compose なら <code>docker compose --profile answer up -d</code>)。</p>
</div>
<div class="composer"><div class="composer-box">
<textarea name="q" rows="3" placeholder="話しかける(いまは無効です)" disabled></textarea>
<button type="button" disabled>↑</button>
</div></div>
</div>
"""
        return HTMLResponse(content=page_shell("AI と話す", body, style=CHAT_STYLE))

    cfg = answer.require_settings()
    # 話す相手は AI(モデル)で、Chiezo はその AI が引く知識。見出しでその関係を出すため、
    # モデル名を名乗らせる(推論サーバに聞く。分からなければ名前なしの「AI」)。
    label = await answer.model_label(cfg)
    heading = f"AI({esc(label)})と話す" if label else "AI と話す"
    if not nojs:
        # 会話は JS(fetch + SSE)が主役。履歴を持つのはブラウザ側で、サーバーは
        # 毎回まるごと受け取る。JS が無い環境には下の 1 問 1 答へ誘導する。
        first = f' data-first="{esc(q)}"' if q else ""
        nojs_url = f"{CHAT_PATH}?nojs=1&mode={mode}&grounded={'1' if grounded else '0'}" + (
            f"&q={quote(q)}" if q else ""
        )
        body = f"""
<div class="chat-page">
<div class="chat-head"><h1>{heading}</h1><span class="spacer"></span>
<a href="/admin">管理画面</a></div>
<div class="log" id="log">
<div class="empty" id="empty">
<p>Chiezo にためた知識(登録済みソース)を引ける AI です。<br>
根拠にした文書は発言のあとに並びます。</p>
<div class="examples">
<button type="button">浅草寺について教えて</button>
<button type="button">カテゴリ「東京都の寺」の記事は何件ある?</button>
<button type="button">京都府にある博物館を挙げて</button>
</div>
</div>
</div>
<form class="composer" id="chat"{first}>
<div class="composer-box">
<textarea id="q" name="q" rows="3" placeholder="話しかける(自然文でよい)" autofocus></textarea>
<button type="submit" id="send" title="送信">↑</button>
</div>
{settings}
</form>
<noscript><p class="stale">JavaScript が無効です。
<a href="{esc(nojs_url)}">1 問 1 答の画面</a>を使ってください(会話の継続はできません)。</p></noscript>
<script>{CHAT_JS}</script>
</div>
"""
        return HTMLResponse(content=page_shell(heading, body, style=CHAT_STYLE))

    # ---- JS なしの 1 問 1 答(会話は続かないが、これだけで用が足りることも多い)
    form = f"""
<nav><a href="/admin">管理画面</a></nav>
<h1>{heading}(JS なし・1 問 1 答)</h1>
<form method="get" action="{CHAT_PATH}">
<input type="hidden" name="nojs" value="1">
<input type="text" name="q" value="{esc(q or '')}" placeholder="質問を書く(自然文でよい)">
<select name="source">{options}</select>
<select name="grounded">{grounded_options}</select>
<select name="mode">{mode_options}</select>
<button type="submit">質問する</button>
</form>
<p class="muted"><a href="{CHAT_PATH}">会話できる画面へ戻る</a></p>
"""
    if not q:
        return HTMLResponse(content=page_shell(heading, form))

    steps_block = ""
    if mode == "agent":
        result = await agent.answer_question(cfg, request, q, source, grounded)
        trace = "\n".join(
            f'<li>{s["step"]}. {esc(s["tool"])} '
            f'{esc(json.dumps(s["arguments"], ensure_ascii=False))} → {esc(s["summary"])}</li>'
            for s in result["steps"]
        ) or "<li>(道具を使わずに答えた)</li>"
        steps_block = f"<h2>調べた手順</h2>\n<ul>\n{trace}\n</ul>\n"
        footer = f"モデル: {esc(result['model'])}(agent モード)"
    else:
        result = await answer.answer(cfg, request, q, source, grounded)
        footer = (
            f"検索に使ったクエリ: {esc(json.dumps(result['queries'], ensure_ascii=False))}"
            f" / モデル: {esc(result['model'])}"
        )
    refs = "\n".join(
        f'<li>[{r["n"]}] <a href="{esc(r["url"])}">{esc(r["source"])} / {esc(r["title"])}</a></li>'
        for r in result["references"]
    ) or "<li>(なし)</li>"
    body = form + f"""
{steps_block}<h2>回答</h2>
<pre class="answer">{esc(result['answer'])}</pre>
<h2>出典</h2>
<ul>
{refs}
</ul>
<p class="muted">{footer}</p>
"""
    return HTMLResponse(content=page_shell(heading, body))

