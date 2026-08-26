"""クライアントへ配信する Claude Code フックの実体を置く。

ここのモジュールは Chiezo の app プロセスからは import されない(実行もしない)。
`app/claude_config.py` がソースをそのまま読み出し、生成時にベース URL を差し替えて
`GET /admin/claude-config.hook.py` として配信する。実ファイルとして置いてあるのは、
文字列テンプレートではなく通常の Python として lint・テストできるようにするため。
"""
