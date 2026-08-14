"""会話画面(`/ai/chat`)。「使う」層のブラウザ側。

答えを組み立てるのは `app/answer.py` / `app/agent.py` で、ここがするのは画面と
その JS を返すことだけ(本文は `/v1/chat` へ POST して SSE で受け取る)。
"""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app import agent, answer, notes, providers, websearch
from app.pages import CHAT_PATH, CHAT_STYLE, esc, page_shell
from app.registry import Source

log = logging.getLogger("chiezo.api")

router = APIRouter()

# 会話画面の JS。**この画面だけ JS を使う**(他の画面は従来どおり JS なし)理由は 2 つ:
# 回答まで数十秒かかるので逐次表示しないと無反応に見えること、会話の履歴を持つ主体が
# クライアント側だからこと。EventSource ではなく fetch を使うのは、履歴を送るのに
# POST が要るため(EventSource は GET しか張れない)。
# AI の返事に混ざる Markdown を、その場で HTML に直すための小さな描画器。
#
# **外部のライブラリを読み込まない。** Chiezo は LAN 内・オフラインで動く前提で、
# 他の画面も JS も CSS も外に出ずに済ませてある —— ここだけ CDN に頼ると、
# 外に出られない環境で装飾だけが消える(しかも原因が分かりにくい)。
#
# **エスケープしてから組み立てる。** 相手はモデルの出力(信用できない文字列)なので、
# 先に & < > " ' を実体参照へ直し、そのうえで Markdown の印を HTML に置き換える ——
# 順番を逆にすると、生成物にタグを書かれた時点で入り込む。
# リンクも **http/https だけ**通す(javascript: を踏ませない)。
#
# 対応するのは LLM が実際に使う範囲: 見出し・箇条書き・番号付き・引用・区切り線・
# コード(インライン/ブロック)・表・強調・打ち消し・リンク・裸の URL。
# **入れ子の箇条書きには対応しない**(1 段だけ)—— 会話の答えで 2 段以上は稀で、
# 対応させるぶんだけ壊れやすくなる。
MARKDOWN_JS = r"""
(function (global) {
  var MARK = '\u0000';   // インラインコードを一時的に隠す目印(本文には出てこない文字)

  function esc(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }

  // 行の中の装飾。**コードを先に抜き取る** —— ** を含むコードを強調にしないため。
  function inline(s) {
    var code = [];
    s = s.replace(/`([^`]+)`/g, function (_, c) {
      code.push(c);
      return MARK + (code.length - 1) + MARK;
    });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // 裸の URL(モデルは出典をこの形で書くことが多い)
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^*])\*([^*]+)\*/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    return s.replace(new RegExp(MARK + '(\\d+)' + MARK, 'g'), function (_, i) {
      return '<code>' + code[i] + '</code>';
    });
  }

  function cells(line) {
    return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(function (c) {
      return c.trim();
    });
  }

  function isDivider(line) {
    return /^\s*\|?[\s:|-]+$/.test(line) && line.indexOf('-') >= 0;
  }

  function render(text) {
    var lines = esc(text || '').split(/\r?\n/);
    var out = [], para = [], list = null, quote = [], i = 0;

    function flushPara() {
      if (para.length) { out.push('<p>' + inline(para.join('<br>')) + '</p>'); para = []; }
    }
    function flushList() {
      if (list) {
        out.push('<' + list.tag + '>' + list.items.join('') + '</' + list.tag + '>');
        list = null;
      }
    }
    function flushQuote() {
      if (quote.length) {
        out.push('<blockquote>' + inline(quote.join('<br>')) + '</blockquote>');
        quote = [];
      }
    }
    function flushAll() { flushPara(); flushList(); flushQuote(); }

    while (i < lines.length) {
      var line = lines[i];

      // コードブロック。**閉じていなくても出す** ——
      // 流している途中の応答では必ず開きっぱなしになる
      if (/^\s*```+\s*[\w+-]*\s*$/.test(line)) {
        flushAll();
        var body = [];
        i++;
        while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { body.push(lines[i]); i++; }
        i++;
        out.push('<pre><code>' + body.join('\n') + '</code></pre>');
        continue;
      }

      // 表(| で区切られ、次の行が区切り線)
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && isDivider(lines[i + 1])) {
        flushAll();
        var head = cells(line).map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('');
        var rows = [];
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          rows.push('<tr>' + cells(lines[i]).map(function (c) {
            return '<td>' + inline(c) + '</td>';
          }).join('') + '</tr>');
          i++;
        }
        out.push('<table><thead><tr>' + head + '</tr></thead><tbody>'
          + rows.join('') + '</tbody></table>');
        continue;
      }

      var heading = /^\s*(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushAll();
        // h1 は画面の見出しに使っているので、本文は h2 から始める(段の差は保つ)
        var level = Math.min(heading[1].length + 1, 6);
        out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
        i++;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushAll();
        out.push('<hr>');
        i++;
        continue;
      }

      var quoted = /^\s*&gt;\s?(.*)$/.exec(line);   // エスケープ済みなので > は &gt;
      if (quoted) {
        flushPara();
        flushList();
        quote.push(quoted[1]);
        i++;
        continue;
      }

      var bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      var numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        flushPara();
        flushQuote();
        var tag = bullet ? 'ul' : 'ol';
        if (!list || list.tag !== tag) { flushList(); list = {tag: tag, items: []}; }
        list.items.push('<li>' + inline((bullet || numbered)[1]) + '</li>');
        i++;
        continue;
      }

      if (!line.trim()) {
        flushAll();
        i++;
        continue;
      }

      flushList();
      flushQuote();
      para.push(line.trim());
      i++;
    }

    flushAll();
    return out.join('');
  }

  global.chiezoMarkdown = render;
})(window);
"""

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
  // 返事は Markdown で来る。描画器があれば装飾つきで、無ければ素のまま出す
  // (描画器を読めなかったときに本文まで消えないようにする)
  function show(node, text) {
    if (window.chiezoMarkdown) {
      node.classList.add('md');
      node.innerHTML = window.chiezoMarkdown(text);
    } else {
      node.textContent = text;
    }
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

  // 隠れているトグルの値は送らない（効かない場面なので、指示だけ飛ぶのを防ぐ）
  function toggleValue(el) {
    if (!el) return null;
    var box = el.closest('label');
    return box && box.hidden ? null : el.checked;
  }

  function settings() {
    var web = document.getElementById('web'), notes = document.getElementById('notes');
    return {
      backend: (document.getElementById('backend') || {}).value || null,
      model: (document.getElementById('model') || {}).value || null,
      effort: (document.getElementById('effort') || {}).value || null,
      source: document.getElementById('source').value || null,
      grounded: document.getElementById('grounded').value === '1',
      mode: document.getElementById('mode').value,
      web: toggleValue(web),
      notes: toggleValue(notes)
    };
  }

  // エラー本文({detail: {error, reason}})から画面に添える一言を取る。
  // 読めない形なら空("HTTP 502" だけが出る)。
  function reasonOf(body) {
    try {
      var d = JSON.parse(body).detail;
      if (typeof d === 'string') return ' ' + d;
      if (d && d.error) return ' ' + d.error + (d.reason ? ' — ' + d.reason : '');
    } catch (e) {}
    return '';
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
      if (!res.ok) {
        // **本文を捨てない。** 理由は JSON の detail に入っていて、捨てると画面に
        // 「HTTP 502」しか出ず何が起きたか追えない(実際にそれで詰まった)。
        return res.text().then(function (body) {
          throw new Error('HTTP ' + res.status + reasonOf(body));
        });
      }
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
              // **毎回まるごと描き直す。** Markdown は行のまとまりで意味が決まるので、
              // 届いた差分だけを足すと表や箇条書きが途中で切れた形のまま残る
              show(t.text, answer);
            } else if (ev[1] === 'error') {
              t.text.classList.remove('pending');
              // 本文とは別の要素に出す(本文は描き直されるので、混ぜると消える)
              t.node.appendChild(el('p', 'stale', '[エラー] ' + (data.error || '')
                + (data.reason ? ' — ' + data.reason : '')));
            }
          });
          if (stick) toBottom();
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      t.text.classList.remove('pending');
      t.node.appendChild(el('p', 'stale', '[通信に失敗しました: ' + e.message + ']'));
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
      // **切れない道具は切れないままにする**（相手が CLI で、こちらから外す口が無い）。
      // ここを素通りさせると、サーバーが付けた disabled を読み込み直後に外してしまう。
      var label = box.parentNode;
      var fixed = label.classList.contains('fixed');
      if (fixed) { box.checked = true; }
      box.disabled = off || fixed;
      label.classList.toggle('on', box.checked && !off);
      label.title = off ? pair[1] : (fixed ? (label.dataset.fixedNote || '') : '');
    });
  }
  // 相手が変わったときも同じ判断を通す（下の出し入れから呼ぶ）
  window.chiezoSyncModeToggles = syncToggles;
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


# 会話の画面。**Chiezo を「使う」側の機能**なので `/ai/` の下に置く
# (Chiezo 本体の画面 = /admin と /search/… とは並びで区別できるようにする)。
@router.get(CHAT_PATH, response_class=HTMLResponse)
async def chat_page(
    request: Request,
    q: str | None = Query(None),
    source: str | None = Query(None),
    nojs: bool = Query(False, description="JS を使わず、1 問 1 答で表示する"),
    grounded: bool | None = Query(None, description="Chiezo で取れたことだけを根拠にする"),
    mode: str | None = Query(None, pattern="^(rag|agent)$", description="rag / agent"),
    backend: str | None = Query(None, description="どの AI に聞くか(省略時は既定のバックエンド)"),
    model: str | None = Query(None, description="どのモデルを使うか(省略時はその相手の既定)"),
    effort: str | None = Query(None, description="どれだけ考えさせるか(相手が持っていれば)"),
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
    # 話す相手の選択。**相手が 1 つしか無いときは出さない** —— 選べない選択肢を
    # 並べても場所を取るだけで、設定を足せば自然に現れる。
    names = answer.backend_names()
    current_backend = answer.normalize_backend(backend)
    if current_backend not in names:
        current_backend = names[0] if names else ""
    # モデルは**会話のたびに選べる**。候補は相手に聞いた一覧（無ければコードの控え）。
    model_select = ""
    if names:
        model_options = "".join(
            f'<option value="{esc(m)}"{" selected" if m == model else ""}>{esc(m)}</option>'
            for m in await answer.available_models(current_backend)
        )
        if model_options:
            # **相手に任せる選択肢を先頭に置く**（エフォートと同じ扱い）。
            # 指定が要る相手（Gemini など）では、これを選んでも控えの先頭が当たる。
            model_select = (
                '<select id="model" name="model" title="モデル">'
                '<option value="">モデル（既定）</option>'
                f"{model_options}</select>"
            )

    # エフォート（考える量）。**持っている相手のときだけ出す** —— 持たない相手に
    # 出しても送るだけ無駄で、選べたのに効かない、という分かりにくさが残る。
    effort_select = ""
    effort_names = providers.efforts_of(current_backend)
    if effort_names:
        effort_options = '<option value="">考える量（既定）</option>' + "".join(
            f'<option value="{esc(e)}"{" selected" if e == effort else ""}>{esc(e)}</option>'
            for e in effort_names
        )
        effort_select = (
            f'<select id="effort" name="effort" title="考える量">{effort_options}</select>'
        )

    backend_select = ""
    if len(names) > 1:
        backend_options = "".join(
            f'<option value="{esc(name)}"'
            f'{" selected" if name == current_backend else ""}>{esc(answer.backend_label(name))}</option>'
            for name in names
        )
        backend_select = f'<select id="backend" name="backend" title="話す相手">{backend_options}</select>'
    # 設定は**入力欄の下**に並べる(会話中に触るのは稀なので、視線の主役にしない)。
    #
    # **効かない場面ではトグルを出さない。** web 検索も「覚える」も agent モードの
    # 道具でしか働かず、rag モードでは送っても捨てられる。出しっぱなしにすると
    # 「押せるのに何も起きない」状態になる（実際にそうなっていた）。
    is_bridge = bool((spec := providers.get(current_backend)) and spec.bridge)
    current_mode = (mode or answer.default_mode()).strip().lower()
    # CLI ブリッジでは Chiezo の SearXNG ではなく **CLI 自身の web 検索**を開ける。
    # そちらは Chiezo 側の設定と無関係なので、設定していない環境でも出す。
    web_usable = current_mode == "agent" and (websearch.is_enabled() or is_bridge)
    # **「覚える」は CLI ブリッジでは止められない**（MCP をまるごと渡していて、道具を
    # 1 つずつ外す口が無い）。隠すのではなく**入ったまま触れない**状態で見せる ——
    # 使えること自体は伝わったほうがよく、切れるように見せるのだけを避けたい。
    notes_usable = current_mode == "agent" and notes.is_enabled()
    notes_fixed = notes_usable and is_bridge
    # **要素は描いておき、効かない場面では隠す。** モードや相手は JS で切り替わるので、
    # そのたびに作り直すより、出し入れするほうが素直。
    any_bridge = any(bool((p := providers.get(n)) and p.bridge) for n in names)
    web_toggle = (
        f'<label class="toggle on" id="web-toggle"{"" if web_usable else " hidden"}>'
        '<input type="checkbox" id="web" checked>🌐 web 検索</label>'
        if websearch.is_enabled() or any_bridge else ""
    )
    # 「覚えておいて」に応えられるようにする道具(Chiezo で唯一の書き込み)。
    # 何を書いたかは「調べた手順」に出るので、勝手に増えたときも後から分かる。
    fixed_note = "この相手では止められません（CLI が自分で道具を引くため）"
    notes_toggle = (
        f'<label class="toggle on{" fixed" if notes_fixed else ""}" id="notes-toggle"'
        f'{"" if notes_usable else " hidden"}'
        f'{f" title=\"{esc(fixed_note)}\" data-fixed-note=\"{esc(fixed_note)}\"" if notes_fixed else ""}>'
        f'<input type="checkbox" id="notes" checked{" disabled" if notes_fixed else ""}>'
        "📝 覚える</label>"
        if notes.is_enabled() else ""
    )
    settings = f"""
<div class="composer-settings">
{backend_select}
{model_select}
{effort_select}
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

    cfg = answer.require_settings(current_backend, model, effort)
    # 話す相手は AI(モデル)で、Chiezo はその AI が引く知識。見出しでその関係を出すため、
    # モデル名を名乗らせる(推論サーバに聞く。分からなければ名前なしの「AI」)。
    # モデルが決まっていなければ**相手の名前**を出す（`Claude Code` など）。
    # 選べる一覧の先頭を出すと、選んでもいないモデル名が並んで嘘になる。
    label = await answer.model_label(cfg) or answer.backend_label(current_backend)

    def shown_model(name: str) -> str:
        """人に見せるモデル名。**置き字は出さない。**

        モデルを選ばなかったとき、内部では相手に無視される置き字（`chiezo`）が入る。
        そのまま出すと画面に「モデル: chiezo」と並んで、何で答えたのか分からなくなる。
        """
        if name != "chiezo":
            return name
        return label or answer.backend_label(current_backend)
    heading = f"AI({esc(label)})と話す" if label else "AI と話す"
    if not nojs:
        # 会話は JS(fetch + SSE)が主役。履歴を持つのはブラウザ側で、サーバーは
        # 毎回まるごと受け取る。JS が無い環境には下の 1 問 1 答へ誘導する。
        first = f' data-first="{esc(q)}"' if q else ""
        nojs_url = (
            f"{CHAT_PATH}?nojs=1&mode={mode}&grounded={'1' if grounded else '0'}"
            f"&backend={quote(current_backend)}"
        ) + (f"&q={quote(q)}" if q else "")
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
<script>{MARKDOWN_JS}</script>
<script>{CHAT_JS}</script>
<script>
  // モードや相手を変えたら、効かなくなるトグルを隠す(押せるのに何も起きない状態を作らない)。
  (function () {{
    var WEB_READY = {json.dumps(websearch.is_enabled())};   // Chiezo 側の web 検索(SearXNG)
    var NOTES_READY = {json.dumps(notes.is_enabled())};
    var isBridge = {json.dumps(is_bridge)};
    var modeSel = document.getElementById('mode');
    var webBox = document.getElementById('web-toggle');
    var notesBox = document.getElementById('notes-toggle');

    function sync() {{
      var agent = modeSel && modeSel.value === 'agent';
      // CLI ブリッジでは CLI 自身の web 検索を開ける(Chiezo の設定とは無関係)
      if (webBox) {{ webBox.hidden = !(agent && (WEB_READY || isBridge)); }}
      // **「覚える」は CLI ブリッジでは止められない**ので、入ったまま触れなくする
      if (notesBox) {{
        notesBox.hidden = !(agent && NOTES_READY);
        var box = document.getElementById('notes');
        if (box) {{
          box.disabled = isBridge;
          if (isBridge) {{ box.checked = true; }}
        }}
        notesBox.classList.toggle('fixed', isBridge);
        notesBox.dataset.fixedNote = {json.dumps(fixed_note)};
        if (window.chiezoSyncModeToggles) {{ window.chiezoSyncModeToggles(); }}
      }}
    }}
    if (modeSel) {{ modeSel.addEventListener('change', sync); }}
    window.chiezoSyncToggles = function (bridge) {{ isBridge = bridge; sync(); }};
  }})();

  // 相手を変えたらモデルとエフォートの候補も入れ替える(相手ごとに違う)。
  (function () {{
    var b = document.getElementById('backend'), m = document.getElementById('model');
    var ef = document.getElementById('effort'), lastBridge = false;
    if (!b || !m) {{ return; }}
    b.addEventListener('change', function () {{
      m.disabled = true;
      fetch('/ai/models?backend=' + encodeURIComponent(b.value))
        .then(function (r) {{ return r.ok ? r.json() : {{ models: [], efforts: [] }}; }})
        .then(function (d) {{
          lastBridge = !!d.bridge;
          m.innerHTML = '<option value="">モデル（既定）</option>';
          (d.models || []).forEach(function (id) {{
            var o = document.createElement('option');
            o.value = id; o.textContent = id; m.appendChild(o);
          }});
          if (!ef) {{ return; }}
          // **持っていない相手では隠す**(選べても効かない選択肢を残さない)。
          var efforts = d.efforts || [];
          ef.hidden = efforts.length === 0;
          ef.innerHTML = '<option value="">考える量（既定）</option>';
          efforts.forEach(function (id) {{
            var o = document.createElement('option');
            o.value = id; o.textContent = id; ef.appendChild(o);
          }});
        }})
        .then(function () {{
          // 相手が変われば、効くトグルも変わる
          if (window.chiezoSyncToggles) {{ window.chiezoSyncToggles(lastBridge); }}
        }})
        .finally(function () {{ m.disabled = false; }});
    }});
  }})();
</script>
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
{backend_select.replace('id="backend"', 'id="backend-nojs"')}
{model_select.replace('id="model"', 'id="model-nojs"')}
{effort_select.replace('id="effort"', 'id="effort-nojs"')}
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
        footer = f"モデル: {esc(shown_model(result['model']))}(agent モード)"
    else:
        result = await answer.answer(cfg, request, q, source, grounded)
        footer = (
            f"検索に使ったクエリ: {esc(json.dumps(result['queries'], ensure_ascii=False))}"
            f" / モデル: {esc(shown_model(result['model']))}"
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

