import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import agent_navigation


class ClaudeViewSwitchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.views = self.root / "views" / "session"
        self.views.mkdir(parents=True)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE activity (id TEXT, session TEXT)")
        self.db.execute("INSERT INTO activity VALUES ('terminal', 'session')")
        self.socket = Mock(spec=Path)
        self.socket.is_socket.return_value = True

    def switch(self, agent=None):
        identity = "session/" + agent if agent else ""
        return agent_navigation.switch(self.db, self.root, self.socket, "terminal", identity)

    def reply(self, error=""):
        request = json.loads((self.views / "request.json").read_text())
        (self.views / "response.json").write_text(json.dumps({"token": request["token"], "error": error}))

    @patch.object(agent_navigation, "ensure_shortcut")
    def test_parent_focus_skips_shortcut_after_unanswered_legacy_request(self, shortcut):
        (self.views / "request.json").write_text(json.dumps(
            {"session": "session", "agent": None, "token": "unanswered"}))
        (self.views / "response.json").write_text(json.dumps({"token": "older", "error": ""}))
        with patch.object(agent_navigation.subprocess, "run") as send:
            self.assertEqual(self.switch(), "shown")
            self.assertEqual(self.switch(), "shown")
            send.assert_not_called()
        shortcut.assert_not_called()

    @patch.object(agent_navigation, "ensure_shortcut", return_value=False)
    def test_parent_return_after_child_and_archive_fallback(self, shortcut):
        with patch.object(agent_navigation.subprocess, "run") as send:
            send.side_effect = lambda *a, **kw: self.reply()
            self.assertEqual(self.switch("live"), "shown")
            send.side_effect = lambda *a, **kw: self.reply("that agent has ended")
            self.assertEqual(self.switch("finished"), "archive")
            send.side_effect = lambda *a, **kw: self.reply()
            self.assertEqual(self.switch(), "shown")
            self.assertEqual(self.switch(), "shown")
            self.assertEqual(send.call_count, 3)

    @patch.object(agent_navigation, "ensure_shortcut", return_value=False)
    def test_parent_recognizes_a_child_opened_by_the_previous_version(self, shortcut):
        (self.views / "request.json").write_text(json.dumps(
            {"session": "session", "agent": "live", "token": "legacy"}))
        self.reply()
        with patch.object(agent_navigation.subprocess, "run", side_effect=lambda *a, **kw: self.reply()) as send:
            self.assertEqual(self.switch(), "shown")
            send.assert_called_once()
