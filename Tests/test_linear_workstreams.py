"""Exercise the desktop bridge with a private database and an in-memory Linear API."""
import contextlib
import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import linear_workstreams as linear
import manager_store
import workstream_links


class Workspace:
    __file__ = "/test app/streams.py"

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        linear.initialize(self.db)
        manager_store.initialize(self.db)
        workstream_links.initialize(self.db)
        self.db.execute("CREATE TABLE workstream_terminals (id TEXT PRIMARY KEY,stream TEXT)")
        self.db.execute("CREATE TABLE activity (id TEXT PRIMARY KEY,session TEXT,state TEXT,detail TEXT)")
        self.rows = []
        self.manager = Mock(TELLABLE=("Ready", "Review"))

    def database(self):
        return self.db

    def bridge_lock(self):
        return contextlib.nullcontext()

    def bd(self, action, *args):
        if action == "list":
            return json.dumps(self.rows)
        parent = next(row for row in self.rows if row["id"] == args[0])
        if action == "show":
            return json.dumps([parent])
        if action == "reopen":
            parent["status"] = "open"
        elif action == "update":
            parent["external_ref"] = args[2]
            parent["metadata"]["linearIssueID"] = args[4].split("=")[1]
        else:
            raise AssertionError(action)

    def create_stream(self, title, folder, url, provider, identity):
        key = "stream-" + str(len(self.rows) + 1)
        self.rows.append(dict(id=key, title=title, status="open", external_ref=url,
                              metadata=dict(folder=folder, provider=provider, linearIssueID=identity)))
        return key


class LinearFlowTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace()
        self.db = self.workspace.db
        self.addCleanup(self.db.close)
        self.raw = dict(id="issue-uuid", identifier="ENG-7", url="https://linear.app/team/issue/ENG-7/retry",
                        title="Recover uploads", state={"name": "Todo"}, assignee={"name": "Eva"},
                        description="- [ ] Resume pending uploads", comments={"nodes": [
                            dict(body="Keep the retry button", user={"name": "Kim"})]})
        self.comments = {}
        self.attachments = {}
        self.api = Mock(side_effect=self.graphql)

    def graphql(self, query, **params):
        if query == linear.ISSUE_QUERY:
            return {"issue": copy.deepcopy(self.raw)}
        if query == linear.COMMENT:
            return {"comment": self.comments[params["id"]]}
        item = params["input"]
        if "commentCreate" in query:
            comment = item["id"]
            if comment in self.comments:
                raise ValueError("Client UUID already exists.")
            self.comments[comment] = dict(url=self.raw["url"] + "#comment-" + comment,
                                          issue={"id": item["issueId"]}, children={"nodes": []})
            return {"commentCreate": dict(success=True, comment=self.comments[comment])}
        if "attachmentCreate" in query:
            self.attachments[(item["issueId"], item["url"])] = item
            return {"attachmentCreate": {"success": True}}
        raise AssertionError(query)

    def command(self, action, *args):
        return linear.command(self.workspace, "linear-" + action, args, self.api)

    def start(self):
        return self.command("start", "eng-7", "/project", "codex", "", self.raw["id"], "0")

    def test_launch_preview_prompt_and_resume(self):
        preview = self.command("preview", self.raw["url"])
        self.assertIsNone(preview["existing"])
        self.assertEqual(preview["issue"]["assignee"], "Eva")
        key = self.start()
        prompt = linear.initial_prompt(self.db, key)
        self.assertIn("Resume pending uploads", prompt)
        self.assertIn("Kim: Keep the retry button", prompt)
        self.assertIsNone(linear.initial_prompt(self.db, "extra-terminal"))
        self.assertEqual(self.command("preview", "ENG-7")["existing"], key)
        self.assertEqual(self.command("start", "ENG-7", "", "claude", key, self.raw["id"], "0"), key)
        self.assertEqual(len(self.workspace.rows), 1)

    def test_stale_create_and_closed_preview_do_not_start_a_duplicate(self):
        key = self.start()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.start()
        self.workspace.rows[0]["status"] = "closed"
        self.assertTrue(self.command("preview", "ENG-7")["closed"])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.command("start", "ENG-7", "", "codex", key, self.raw["id"], "0")
        self.assertEqual(self.command("start", "ENG-7", "", "codex", key, self.raw["id"], "1"), key)
        self.assertEqual(self.workspace.rows[0]["status"], "open")

    def test_workspace_identity_and_legacy_reference(self):
        self.workspace.rows.append(dict(id="legacy", status="open", external_ref="eng-7", metadata=dict(folder="/project")))
        self.assertEqual(self.command("preview", "ENG-7")["existing"], "legacy")
        self.command("refresh", "legacy")
        self.raw.update(id="other-uuid", url="https://linear.app/other/issue/ENG-7")
        self.assertIsNone(self.command("preview", "ENG-7")["existing"])
        with self.assertRaisesRegex(ValueError, "different Linear workspace"):
            self.command("preview", "https://linear.app/team/issue/ENG-7")
        for bad in ("https://example.com/team/issue/ENG-7", "fix ENG-7 for me", "http://[", "ENG-0"):
            self.assertIsNone(linear.reference(bad))

    def test_pin_existing_stream_and_keep_primary_on_retry(self):
        self.workspace.rows.append(dict(id="legacy", status="open", metadata=dict(folder="/project")))
        self.command("pin", "legacy", self.raw["url"])
        self.assertEqual(self.command("context", "legacy")["issue"]["identifier"], "ENG-7")
        with self.assertRaisesRegex(ValueError, "already has a reference"):
            self.command("pin", "legacy", self.raw["url"])

    def test_changes_persist_offline_and_stale_ack_cannot_hide_them(self):
        key = self.start()
        old = self.command("context", key)["issue"]
        self.raw.update(state={"name": "Started"}, description="Retry after restart")
        self.command("refresh", key)
        self.command("seen", key, old["revision"])
        self.api.side_effect = ValueError("Offline")
        with self.assertRaisesRegex(ValueError, "Offline"):
            self.command("refresh", key)
        current = self.command("context", key)["issue"]
        self.assertIn("Status: Todo → Started", current["changes"])
        self.assertIn("+Retry after restart", current["changes"][-1])
        self.command("seen", key, current["revision"])
        self.assertEqual(self.command("context", key)["issue"]["changes"], [])

    def question(self):
        key = self.start()
        ask = manager_store.open_ask(self.db, key, "terminal", "Keep mobile behavior?", ["Keep", "Change"], "Keep")
        self.db.execute("INSERT INTO activity VALUES ('terminal','session','Needs you','Waiting for decision')")
        self.db.commit()
        return key, ask

    def test_post_retry_recovers_lost_response_and_reads_only_its_thread(self):
        key, ask = self.question()
        def lost_response(query, **params):
            result = self.graphql(query, **params)
            if "commentCreate" in query:
                raise ValueError("Timed out")
            return result
        self.api.side_effect = lost_response
        self.command("ask", key, ask)
        self.command("ask", key, ask)
        self.assertEqual(len(self.comments), 1)
        self.command("poll", key, ask)
        self.assertIsNone(self.command("context", key)["questions"][0]["reply"])
        next(iter(self.comments.values()))["children"]["nodes"] = [dict(body="Keep it", user={"name": "Kim"})]
        self.command("poll", key, ask)
        self.command("poll", key, ask)
        self.assertEqual(self.command("context", key)["questions"][0]["reply"], "Kim: Keep it")
        self.assertEqual(len(manager_store.state(self.db)["events"]), 1)
        self.workspace.manager.tell.assert_not_called()
        self.db.execute("UPDATE activity SET state='Ready'")
        manager_store.set_setting(self.db, "paused", "0")
        self.command("deliver", key, ask)
        self.command("deliver", key, ask)
        self.workspace.manager.tell.assert_called_once()
        self.assertNotIn("Keep it", self.workspace.manager.tell.call_args.args[-1])
        self.assertFalse(self.command("context", key)["questions"][0]["pending"])

    def test_stop_question_wakes_its_idle_terminal_when_the_thread_is_answered(self):
        key, ask = self.question()
        linear.remember_stopped_question(self.db, "terminal", "session", "Waiting for decision")
        manager_store.set_setting(self.db, "paused", "0")
        self.command("ask", key, ask)
        next(iter(self.comments.values()))["children"]["nodes"] = [dict(user={"name": "Kim"}, body="Keep it")]
        self.command("poll", key, ask)
        self.assertEqual(self.db.execute("SELECT state FROM activity").fetchone()[0], "Review")
        self.workspace.manager.tell.assert_called_once()
        self.assertFalse(self.command("context", key)["questions"][0]["pending"])

    def test_late_reply_after_dismissal_is_ignored(self):
        key, ask = self.question()
        self.command("ask", key, ask)
        def dismiss_during_fetch(query, **params):
            manager_store.resolve_asks(self.db, ask_id=ask)
            return {"comment": {"children": {"nodes": [dict(user={"name": "Kim"}, body="Old answer")]}}}
        self.api.side_effect = dismiss_during_fetch
        self.command("poll", key, ask)
        self.assertEqual(self.command("context", key)["questions"], [])
        self.assertEqual(manager_store.state(self.db)["events"], [])

    def test_shared_attachment_survives_new_pr_and_rejects_another_streams_pr(self):
        key = self.start()
        prs = ["https://github.com/test/project/pull/" + str(number) for number in range(1, 4)]
        self.db.execute("INSERT INTO workstream_terminals VALUES ('extra',?)", (key,))
        for terminal, pr in zip((key, "extra", "unrelated"), prs):
            workstream_links.collect(self.db, terminal, pr)
        for pr in prs[:2]:
            self.command("share", key, pr, "Test suite passed", "Design review")
        self.assertEqual(len(self.attachments), 1)
        latest = next(iter(self.attachments.values()))
        self.assertIn(prs[1], latest["metadata"]["messages"][0]["body"])
        with self.assertRaisesRegex(ValueError, "linked"):
            self.command("share", key, prs[2], "Test suite passed", "Design review")
