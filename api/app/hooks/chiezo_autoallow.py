#!/usr/bin/env python3
"""Claude Code の PreToolUse フック: chiezo だけを読む Bash を自動許可する。

Claude Code の `permissions.allow` はコマンド文字列の**前方一致**で判定する。
そのため `Bash(curl -sG "http://.../:*)` のようなルールは、単発の curl には効くが

    for t in A B C; do curl -sG "http://.../v1/jawiki/doc" ...; done
    curl -sG ... | jq -r '.opening' | head -5

のように curl が先頭に来ない形になった瞬間、1 本もマッチせず毎回プロンプトが出る。
大量取得は必ずループやパイプになるので、いちばん許可したい場面でルールが効かない。

このフックは前方一致ではなく**構造**で判定する。以下を全て満たすときだけ
`permissionDecision: "allow"` を返す:

  1. コマンド中に chiezo の URL が 1 つ以上ある。
  2. コマンド中の `scheme://host` が**全て** chiezo である。
  3. コマンド位置に来る語が、安全なシェルキーワードか読み取り専用コマンドの
     許可リスト(curl/jq/sort など)に入っている。
  4. コマンド位置を隠せる構文(`$(...)` / バッククォート / プロセス置換 /
     ヒアドキュメント / eval・exec・source 等)を含まない。
  5. ディスクへ書く curl フラグ(-o/-O/-K/-T/-D 等)と、/tmp 以外への
     リダイレクトを含まない。

条件を外れたときは**何も出力しない**。フックが黙れば Claude Code は通常の
許可フローに戻るだけなので、最悪でも「今までどおりプロンプトが出る」で済む。
判定に迷ったら黙る(fail closed)方針で書いてある。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from urllib.parse import urlsplit

# gen_claude_config.sh(実体は chiezo の /admin/claude-config.hook.py)が
# 配信時にこの 1 行を実際のベース URL へ差し替える。
CHIEZO_ORIGIN = "http://localhost:9000"

# 読み取り専用で、chiezo の応答を捌くのに使う程度のコマンドだけを許す。
# sed / awk は意図的に外している(sed -i でファイルを書き換えられ、awk は
# `> "file"` と system() でシェルに出られるため)。ネットワークに出るのは curl のみ。
ALLOWED_BINS = {
    "curl", "jq",
    "cat", "head", "tail", "sort", "uniq", "wc", "cut", "tr", "grep", "rg",
    "seq", "nl", "paste", "rev", "column", "tee",
    "echo", "printf", "sleep", "true", "false", "basename", "dirname", "date",
}

# コマンド位置に来てよいシェルキーワード・組み込み。
# `[` / `test` は読み取りだけなので許す(`if [ … ]` のガード用)。
SHELL_OK = {
    "for", "select", "while", "until", "if", "then", "else", "elif", "fi",
    "do", "done", "case", "esac", "in", "time", "!",
    "echo", "printf", "true", "false", "break", "continue",
    "[", "[[", "test",
}

# PATH 上にバイナリが無く shutil.which() では捕まらない組み込み。
# 明示的に拒否しないと素通りしてしまう(特に eval / exec / source)。
DENY_WORDS = {
    "eval", "exec", "source", ".", "trap", "alias", "unalias", "function",
    "sudo", "su", "doas", "command", "builtin", "exit", "return", "kill",
    "wait", "jobs", "disown", "shopt", "set", "unset", "export", "declare",
    "typeset", "local", "readonly", "mapfile", "readarray", "coproc",
    "ulimit", "umask", "cd", "pushd", "popd", "read", "history", "bind",
}

SEPARATORS = {";", "|", "||", "&&", "&", "(", ")", "{", "}", ";;", "|&"}
# これらの直後はまたコマンド位置に戻る。
RESET_TO_CMD = {"do", "then", "else", "elif", "!", "time", "if", "while", "until"}
# これらの直後は「値の並び」で、コマンド位置ではない(`for t in A B C`)。
WORD_LIST_INTRO = {"for", "select", "case"}

# トークナイザから見えないコマンド位置を作れる構文。
FORBIDDEN_SUBSTRINGS = ("$(", "`", "<(", ">(", "<<", "${!")

# ディスクへ書く / 設定ファイルを読む curl フラグ。
CURL_DENIED_FLAGS = {
    "-o", "--output", "-O", "--remote-name", "--remote-name-all",
    "--output-dir", "-K", "--config", "-T", "--upload-file",
    "-D", "--dump-header", "-c", "--cookie-jar", "--create-dirs",
    "--trace", "--trace-ascii", "--stderr", "--etag-save", "--xattr",
}
# 次のトークンを値として食う curl フラグ(値を URL と誤判定しないため)。
CURL_VALUE_FLAGS = {
    "-d", "--data", "--data-raw", "--data-urlencode", "--data-binary",
    "-H", "--header", "-X", "--request", "-w", "--write-out",
    "-A", "--user-agent", "-e", "--referer", "-b", "--cookie",
    "-u", "--user", "-m", "--max-time", "--connect-timeout",
    "--retry", "--retry-delay", "--retry-max-time", "-y", "-Y",
    "--resolve", "--interface", "--limit-rate", "--max-filesize",
}

URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*)://([^/\s'\"]+)")
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REDIRECT_OUT = {">", ">>", ">|"}
REDIRECT_OTHER = {"<", "<<<", "2>", "&>", ">&", "2>&1"}


class Reject(Exception):
    """自動許可しない理由。捕まえて黙るためだけの内部例外。"""


def chiezo_netloc() -> str:
    return urlsplit(CHIEZO_ORIGIN).netloc


def _is_chiezo_target(token: str) -> bool:
    """curl の位置引数(URL)が chiezo を指しているか。スキーム省略も許す。"""
    netloc = chiezo_netloc()
    for prefix in (f"http://{netloc}", f"https://{netloc}", netloc):
        if token == prefix or token.startswith(prefix + "/") or token.startswith(prefix + "?"):
            return True
    return False


def tokenize(command: str) -> list[str]:
    """コマンドをトークン列にする。改行は `;` と同じ区切りとして扱う。

    shlex は改行を単なる空白として捨ててしまい、改行区切りで並べた 2 本目の
    コマンドが 1 本目の引数に見えてしまう。行ごとに分けて間に `;` を挟むことで
    区切りを保つ。行をまたぐクォートはここで ValueError になり、結果として
    自動許可されない(fail closed)。
    """
    tokens: list[str] = []
    for line in command.splitlines():
        if not line.strip():
            continue
        if tokens:
            tokens.append(";")
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens.extend(lexer)
        except ValueError as exc:  # クォートが閉じていない等
            raise Reject(f"tokenize failed: {exc}") from exc
    return tokens


def check_urls(tokens: list[str]) -> None:
    """コマンド中の scheme://host が全て chiezo で、かつ 1 つ以上あること。

    「1 つ以上」を要求するのは、このフックの管轄を chiezo を叩くコマンドだけに
    限るため。chiezo が出てこないコマンドは黙って通常の許可フローへ渡す。
    """
    netloc = chiezo_netloc()
    found = False
    for tok in tokens:
        for scheme, host in URL_RE.findall(tok):
            if scheme.lower() not in ("http", "https"):
                raise Reject(f"non-http scheme: {scheme}")
            if host != netloc:
                raise Reject(f"non-chiezo host: {host}")
            found = True
        # スキームを省いた `curl -s 192.168.0.3:9000/v1/sources` も chiezo とみなす
        if not found and _is_chiezo_target(tok):
            found = True
    if not found:
        raise Reject("no chiezo URL")


def check_command_word(tok: str) -> None:
    if tok in DENY_WORDS:
        raise Reject(f"disallowed builtin: {tok}")
    if tok in SHELL_OK:
        return
    if tok.startswith("$"):
        # `$cmd` の中身は静的に分からない。
        raise Reject(f"variable in command position: {tok}")
    if os.sep in tok or tok.startswith("."):
        if os.path.basename(tok) not in ALLOWED_BINS:
            raise Reject(f"disallowed executable path: {tok}")
        return
    if tok in ALLOWED_BINS:
        return
    if shutil.which(tok) is None:
        # PATH に無い語がコマンド位置に来ている。関数定義やタイポの可能性があり、
        # 何が実行されるか分からないので許可しない。
        raise Reject(f"unknown command: {tok}")
    raise Reject(f"disallowed command: {tok}")


def check_curl_args(args: list[str]) -> None:
    """1 回の curl 呼び出しの引数(`curl` 自身を除く)を検査する。"""
    skip_next = False
    expect_url = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if expect_url:
            expect_url = False
            if not _is_chiezo_target(tok):
                raise Reject(f"--url is not chiezo: {tok}")
            continue
        if tok == "--url":
            expect_url = True
            continue
        head = tok.split("=", 1)[0]
        if tok in CURL_DENIED_FLAGS or head in CURL_DENIED_FLAGS:
            raise Reject(f"curl writes to disk: {tok}")
        if tok in CURL_VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if not _is_chiezo_target(tok):
            raise Reject(f"curl target is not chiezo: {tok}")


def check_structure(tokens: list[str]) -> None:
    """コマンド位置の語を全て検査し、curl の引数だけ追加で見る。"""
    cmd_pos = True
    in_word_list = False
    curl_args: list[str] | None = None

    def flush_curl() -> None:
        nonlocal curl_args
        if curl_args is not None:
            check_curl_args(curl_args)
            curl_args = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1

        if tok in SEPARATORS:
            flush_curl()
            cmd_pos = True
            in_word_list = False
            continue

        if tok in REDIRECT_OUT:
            flush_curl()
            target = tokens[i] if i < len(tokens) else ""
            i += 1
            if not target.startswith("/tmp/"):
                raise Reject(f"redirect outside /tmp: {target}")
            continue
        if tok in REDIRECT_OTHER:
            raise Reject(f"unsupported redirect: {tok}")

        if in_word_list:
            # `for t in A B C; do ...` の A B C は値。`do` で復帰する。
            if tok in RESET_TO_CMD:
                cmd_pos = True
                in_word_list = False
            continue

        if cmd_pos:
            if ASSIGNMENT_RE.match(tok):
                continue  # VAR=value の前置き。まだコマンド位置
            if tok in WORD_LIST_INTRO:
                in_word_list = True
                cmd_pos = False
                continue
            if tok in RESET_TO_CMD:
                continue
            check_command_word(tok)
            if os.path.basename(tok) == "curl":
                curl_args = []
            cmd_pos = False
            continue

        if curl_args is not None:
            curl_args.append(tok)

    flush_curl()


def decide(command: str) -> bool:
    """このコマンドを自動許可してよいか。判定できなければ False。"""
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in command:
                raise Reject(f"contains {bad}")
        tokens = tokenize(command)
        check_urls(tokens)
        check_structure(tokens)
    except Reject:
        return False
    except Exception:  # 想定外は必ず fail closed
        return False
    return True


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    command = (payload.get("tool_input") or {}).get("command")
    if not decide(command):
        return  # 黙る = 通常の許可フローへ
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": f"chiezo read-only lookup ({chiezo_netloc()})",
            },
            "suppressOutput": True,
        },
        sys.stdout,
        ensure_ascii=False,
    )


if __name__ == "__main__":
    main()
