"""やること(タスク・プロジェクト)のテスト。

書き込みは `app/notes.py` を通すので、FTS と `doc_tags` / `tag_counts` が
本体とずれないことも同時に確かめる。
"""
import pytest

from app import tasks


@pytest.fixture()
def notes_dir(tmp_path, monkeypatch):
    directory = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(directory))
    return directory


@pytest.fixture()
def client(notes_dir, built_data_dir, monkeypatch):
    """notes を読むのに db.query が使えるよう、アプリを起動して mutable に登録させる。"""
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


class TestTasks:
    def test_create_and_read_back(self, client):
        task = tasks.create_task("盤面生成を速くする")
        assert task.title == "盤面生成を速くする"
        assert task.status == tasks.STATUS_TODO
        assert task.flagged is False
        assert task.project is None
        assert task.sort_order == 0

    def test_title_is_required(self, client):
        with pytest.raises(Exception) as e:
            tasks.create_task("   ")
        assert e.value.status_code == 400

    def test_status_is_carried_by_tags(self, client):
        task = tasks.create_task("直す")
        assert tasks.TAG_IN_PROGRESS not in task.tags
        moved = tasks.update_task(task.doc_id, status=tasks.STATUS_IN_PROGRESS)
        assert moved.status == tasks.STATUS_IN_PROGRESS
        assert tasks.TAG_IN_PROGRESS in moved.tags

    def test_no_status_tag_means_not_started(self, client):
        """移植前から todo タグで書かれていたメモが、そのままタスクとして並ぶこと。"""
        client.post("/v1/notes", json={"text": "前から書いてあったメモ", "tags": "todo,環境"})
        listed = tasks.list_tasks()
        assert [t.title for t in listed] == ["前から書いてあったメモ"]
        assert listed[0].status == tasks.STATUS_TODO

    def test_created_at_falls_back_to_updated_at_for_old_memos(self, client):
        created = client.post("/v1/notes", json={"text": "古いメモ", "tags": "todo"}).json()
        task = tasks.require_task(created["doc_id"])
        assert task.created_at == created["updated_at"]

    def test_flag_is_a_separate_axis(self, client):
        task = tasks.create_task("大掛かりな作業")
        flagged = tasks.update_task(task.doc_id, flagged=True)
        assert flagged.flagged is True and flagged.status == tasks.STATUS_TODO

    def test_free_tags_survive_an_update(self, client):
        """メモとして付けたタグを、タスクの操作で落とさないこと。"""
        created = client.post(
            "/v1/notes", json={"text": "調べ物", "tags": "todo,トラブルシュート"}
        ).json()
        updated = tasks.update_task(created["doc_id"], status=tasks.STATUS_DONE)
        assert "トラブルシュート" in updated.tags and tasks.TAG_DONE in updated.tags

    def test_body_keeps_the_title_line(self, client):
        task = tasks.create_task("表題", body="詳しい話")
        assert task.body == "表題\n\n詳しい話"
        renamed = tasks.update_task(task.doc_id, title="別の表題")
        assert renamed.body == "別の表題\n\n詳しい話"

    def test_active_excludes_done(self, client):
        a = tasks.create_task("残る")
        b = tasks.create_task("片付く")
        tasks.update_task(b.doc_id, status=tasks.STATUS_DONE)
        assert [t.doc_id for t in tasks.list_active_tasks()] == [a.doc_id]

    def test_done_is_paged_newest_first(self, client):
        for i in range(3):
            t = tasks.create_task(f"済み {i}")
            tasks.update_task(t.doc_id, status=tasks.STATUS_DONE)
        page = tasks.list_done_tasks(page=0, size=2)
        assert page["total"] == 3 and len(page["items"]) == 2

    def test_unknown_id_is_404(self, client):
        with pytest.raises(Exception) as e:
            tasks.require_task(9999)
        assert e.value.status_code == 404

    def test_a_plain_memo_is_not_a_task(self, client):
        created = client.post("/v1/notes", json={"text": "ただのメモ"}).json()
        with pytest.raises(Exception) as e:
            tasks.require_task(created["doc_id"])
        assert e.value.status_code == 404

    def test_delete_removes_it_from_notes_too(self, client):
        task = tasks.create_task("消す")
        tasks.delete_task(task.doc_id)
        assert client.get("/v1/notes/recall").json()["total"] == 0


class TestTaskOrdering:
    def test_reorder_numbers_from_one(self, client):
        project = tasks.create_project("arrow-puzzle")
        a = tasks.create_task("あ", project=project.name)
        b = tasks.create_task("い", project=project.name)
        tasks.reorder_tasks(project.name, [b.doc_id, a.doc_id])
        assert [t.doc_id for t in tasks.list_tasks(project.name)] == [b.doc_id, a.doc_id]

    def test_reorder_does_not_bump_updated_at(self, client):
        """並び替えでメモが recall の先頭に浮くと、時系列が乱れて使い物にならない。"""
        project = tasks.create_project("arrow-puzzle")
        a = tasks.create_task("あ", project=project.name)
        b = tasks.create_task("い", project=project.name)
        before = tasks.require_task(a.doc_id).updated_at
        tasks.reorder_tasks(project.name, [b.doc_id, a.doc_id])
        assert tasks.require_task(a.doc_id).updated_at == before

    def test_reorder_rejects_a_foreign_task(self, client):
        one = tasks.create_project("arrow-puzzle")
        other = tasks.create_project("pihole-monitor")
        mine = tasks.create_task("あ", project=one.name)
        theirs = tasks.create_task("い", project=other.name)
        with pytest.raises(Exception) as e:
            tasks.reorder_tasks(one.name, [mine.doc_id, theirs.doc_id])
        assert e.value.status_code == 400

    def test_reorder_rejects_duplicates(self, client):
        task = tasks.create_task("あ")
        with pytest.raises(Exception) as e:
            tasks.reorder_tasks(None, [task.doc_id, task.doc_id])
        assert e.value.status_code == 400

    def test_manual_order_is_ignored_in_the_overall_list(self, client):
        """sort_order はプロジェクト内でしか意味を持たない。"""
        project = tasks.create_project("arrow-puzzle")
        a = tasks.create_task("あ", project=project.name)
        b = tasks.create_task("い", project=project.name)
        tasks.reorder_tasks(project.name, [a.doc_id, b.doc_id])
        # 全体一覧は作成日時の新しい順(b が後に作られている)
        assert [t.doc_id for t in tasks.list_tasks()] == [b.doc_id, a.doc_id]


class TestProjects:
    def test_create_and_list(self, client):
        project = tasks.create_project(
            "arrow-puzzle", description="広告のあのゲーム", repo_urls=["https://example.com/a"]
        )
        assert project.name == "arrow-puzzle"
        assert project.description == "広告のあのゲーム"
        assert project.repo_urls == ["https://example.com/a"]
        assert project.archived is False

    def test_repo_urls_drop_blanks_and_duplicates(self, client):
        project = tasks.create_project(
            "a", repo_urls=["  https://example.com/x  ", "", "https://example.com/x"]
        )
        assert project.repo_urls == ["https://example.com/x"]

    def test_duplicate_name_is_409(self, client):
        tasks.create_project("arrow-puzzle")
        with pytest.raises(Exception) as e:
            tasks.create_project("arrow-puzzle")
        assert e.value.status_code == 409

    @pytest.mark.parametrize("name", ["todo", "完了", "project", "決定", "環境"])
    def test_structural_and_canonical_tags_are_rejected_as_names(self, client, name):
        """名前はそのままタスクに付くタグになるので、種別や状態と読まれる語は使えない。"""
        with pytest.raises(Exception) as e:
            tasks.create_project(name)
        assert e.value.status_code == 400

    def test_a_task_links_once_the_project_appears(self, client):
        """先に書いたメモのタグが、同名のプロジェクトを作った瞬間に紐づくこと。"""
        created = client.post(
            "/v1/notes", json={"text": "前から書いてあった", "tags": "todo,pihole-monitor"}
        ).json()
        assert tasks.require_task(created["doc_id"]).project is None
        tasks.create_project("pihole-monitor")
        assert tasks.require_task(created["doc_id"]).project == "pihole-monitor"

    def test_unlink_removes_the_tag(self, client):
        project = tasks.create_project("arrow-puzzle")
        task = tasks.create_task("あ", project=project.name)
        unlinked = tasks.update_task(task.doc_id, unlink_project=True)
        assert unlinked.project is None and project.name not in unlinked.tags

    def test_rename_retags_its_tasks(self, client):
        project = tasks.create_project("old-name")
        task = tasks.create_task("あ", project=project.name)
        tasks.update_project(project.doc_id, name="new-name")
        assert tasks.require_task(task.doc_id).project == "new-name"

    def test_archive_is_blocked_while_tasks_remain(self, client):
        project = tasks.create_project("arrow-puzzle")
        tasks.create_task("残っている", project=project.name)
        with pytest.raises(Exception) as e:
            tasks.update_project(project.doc_id, archived=True)
        assert e.value.status_code == 400

    def test_archive_passes_once_everything_is_done(self, client):
        project = tasks.create_project("arrow-puzzle")
        task = tasks.create_task("片付ける", project=project.name)
        tasks.update_task(task.doc_id, status=tasks.STATUS_DONE)
        assert tasks.update_project(project.doc_id, archived=True).archived is True

    def test_delete_requires_archive_first(self, client):
        project = tasks.create_project("arrow-puzzle")
        with pytest.raises(Exception) as e:
            tasks.delete_project(project.doc_id)
        assert e.value.status_code == 400

    def test_delete_takes_its_tasks_with_it(self, client):
        project = tasks.create_project("arrow-puzzle")
        task = tasks.create_task("片付ける", project=project.name)
        tasks.update_task(task.doc_id, status=tasks.STATUS_DONE)
        tasks.update_project(project.doc_id, archived=True)
        tasks.delete_project(project.doc_id)
        assert client.get("/v1/notes/recall").json()["total"] == 0

    def test_reorder_requires_every_id(self, client):
        a = tasks.create_project("a")
        tasks.create_project("b")
        with pytest.raises(Exception) as e:
            tasks.reorder_projects([a.doc_id])
        assert e.value.status_code == 400

    def test_reorder_numbers_from_one(self, client):
        a = tasks.create_project("a")
        b = tasks.create_project("b")
        assert [p.doc_id for p in tasks.reorder_projects([b.doc_id, a.doc_id])] == [b.doc_id, a.doc_id]


class TestTagCountsStayConsistent:
    def test_counts_follow_task_edits(self, client):
        task = tasks.create_task("あ")
        tasks.update_task(task.doc_id, status=tasks.STATUS_DONE)
        counts = {t["tag"]: t["docs"] for t in client.get("/v1/notes/tags").json()["tags"]}
        assert counts == {tasks.TAG_TASK: 1, tasks.TAG_DONE: 1}


class TestRules:
    def test_create_and_list_in_order(self, client):
        a = tasks.create_rule("日本語で書く", "- 応答も説明も日本語")
        b = tasks.create_rule("ドキュメントを追従させる", "- 同じコミットで直す")
        assert [r.doc_id for r in tasks.list_rules()] == [a.doc_id, b.doc_id]
        assert a.enabled is True

    def test_body_has_no_title_line(self, client):
        """本文が主役なので混ぜない。混ぜると combined() で見出しが二重になる。"""
        rule = tasks.create_rule("日本語で書く", "- 応答も説明も日本語")
        assert rule.body == "- 応答も説明も日本語"

    def test_title_and_body_are_required(self, client):
        for title, body in [("", "本文"), ("見出し", "  ")]:
            with pytest.raises(Exception) as e:
                tasks.create_rule(title, body)
            assert e.value.status_code == 400

    def test_disabled_is_carried_by_a_tag(self, client):
        rule = tasks.create_rule("あ", "本文")
        off = tasks.update_rule(rule.doc_id, enabled=False)
        assert off.enabled is False
        assert tasks.require_rule(rule.doc_id).enabled is False

    def test_combined_skips_disabled_rules(self, client):
        tasks.create_rule("残る", "本文 A")
        off = tasks.create_rule("消える", "本文 B")
        tasks.update_rule(off.doc_id, enabled=False)
        combined = tasks.combined()
        assert "## 残る" in combined and "## 消える" not in combined

    def test_combined_is_empty_when_nothing_is_enabled(self, client):
        assert tasks.combined() == ""
        rule = tasks.create_rule("あ", "本文")
        tasks.update_rule(rule.doc_id, enabled=False)
        assert tasks.combined() == ""

    def test_combined_carries_the_preamble_and_repo_rule(self, client):
        tasks.create_rule("あ", "本文")
        combined = tasks.combined()
        assert combined.startswith(tasks.COMBINED_PREAMBLE)
        assert tasks.AUTO_ADDED_TITLE in combined

    def test_reorder_changes_the_combined_order(self, client):
        a = tasks.create_rule("あ", "本文 A")
        b = tasks.create_rule("い", "本文 B")
        tasks.reorder_rules([b.doc_id, a.doc_id])
        assert tasks.combined().index("## い") < tasks.combined().index("## あ")

    def test_delete(self, client):
        rule = tasks.create_rule("あ", "本文")
        tasks.delete_rule(rule.doc_id)
        assert tasks.list_rules() == []

    def test_a_task_is_not_a_rule(self, client):
        task = tasks.create_task("あ")
        with pytest.raises(Exception) as e:
            tasks.require_rule(task.doc_id)
        assert e.value.status_code == 404


class TestRuleMarkdown:
    def test_round_trip(self, client):
        """連結して貼り付け直したら同じ並びに戻ること(復旧の経路そのもの)。"""
        tasks.create_rule("日本語で書く", "- 応答も説明も日本語")
        tasks.create_rule("ドキュメントを追従させる", "- 同じコミットで直す")
        markdown = tasks.combined()
        result = tasks.import_rules(markdown, replace=True)
        assert result["titles"] == ["日本語で書く", "ドキュメントを追従させる"]
        assert [r.title for r in tasks.list_rules()] == ["日本語で書く", "ドキュメントを追従させる"]
        assert tasks.combined() == markdown

    def test_the_auto_added_rule_is_not_imported_again(self, client):
        """規約リポジトリの扱いは連結時に自動で付くので、取り込むと貼り替えのたびに増える。"""
        tasks.create_rule("あ", "本文")
        tasks.import_rules(tasks.combined(), replace=True)
        assert [r.title for r in tasks.list_rules()] == ["あ"]

    def test_the_preamble_is_dropped(self, client):
        parsed = tasks.parse_rule_markdown(
            tasks.COMBINED_PREAMBLE + "\n## 実ルール\n\n本文\n"
        )
        assert parsed == [("実ルール", "本文")]

    def test_headings_inside_a_code_fence_are_not_split_points(self, client):
        """ルール本文にはシェルの例が入る。フェンス内の ## で分断してはいけない。"""
        markdown = "## 手順\n\n```bash\n## これはコメント\necho hi\n```\n\n続き\n"
        parsed = tasks.parse_rule_markdown(markdown)
        assert len(parsed) == 1
        assert parsed[0][0] == "手順" and "## これはコメント" in parsed[0][1]

    def test_tilde_fences_work_too(self, client):
        markdown = "## 手順\n\n~~~\n## コメント\n~~~\n"
        assert len(tasks.parse_rule_markdown(markdown)) == 1

    def test_third_level_headings_stay_in_the_body(self, client):
        parsed = tasks.parse_rule_markdown("## 親\n\n### 子\n\n本文\n")
        assert len(parsed) == 1 and "### 子" in parsed[0][1]

    def test_headings_without_a_body_are_dropped(self, client):
        assert tasks.parse_rule_markdown("## 見出しだけ\n\n## 次\n\n本文\n") == [("次", "本文")]

    def test_import_appends_when_not_replacing(self, client):
        tasks.create_rule("既存", "本文")
        tasks.import_rules("## 追加\n\n本文\n")
        assert [r.title for r in tasks.list_rules()] == ["既存", "追加"]

    def test_dry_run_does_not_write(self, client):
        result = tasks.import_rules("## 追加\n\n本文\n", dry_run=True)
        assert result["titles"] == ["追加"] and tasks.list_rules() == []

    def test_nothing_to_import_is_400(self, client):
        with pytest.raises(Exception) as e:
            tasks.import_rules("見出しの無い文章")
        assert e.value.status_code == 400


class TestRulesRepoUrl:
    @pytest.fixture()
    def state_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))

    def test_unset_is_none(self, client, state_dir):
        assert tasks.rules_repo_url() is None

    def test_set_and_read_back(self, client, state_dir):
        assert tasks.set_rules_repo_url("  https://example.com/kiyaku  ") == "https://example.com/kiyaku"
        assert tasks.rules_repo_url() == "https://example.com/kiyaku"

    def test_blank_clears_it(self, client, state_dir):
        tasks.set_rules_repo_url("https://example.com/kiyaku")
        assert tasks.set_rules_repo_url("   ") is None
