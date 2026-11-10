from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import manager_store as store


class ManagerStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        store.initialize(self.db)
        self.addCleanup(self.db.close)

    def test_rate_guard_allows_thirty_per_hour(self):
        for i in range(30):
            self.assertTrue(store.allow_consult(self.db, "t1", now=1000 + i))
        self.assertFalse(store.allow_consult(self.db, "t1", now=1100))
        self.assertTrue(store.allow_consult(self.db, "t2", now=1100))
        self.assertTrue(store.allow_consult(self.db, "t1", now=1000 + 3601))

    def test_approval_matches_only_the_exact_action_and_expires(self):
        store.approve(self.db, "t1", "Bash", {"command": "npm test"}, now=0)
        self.assertTrue(store.approved(self.db, "t1", "Bash", {"command": "npm test"}, now=599))
        self.assertFalse(store.approved(self.db, "t1", "Bash", {"command": "npm test --watch"}, now=1))
        self.assertFalse(store.approved(self.db, "t2", "Bash", {"command": "npm test"}, now=1))
        self.assertFalse(store.approved(self.db, "t1", "Bash", {"command": "npm test"}, now=601))

    def test_revoke_removes_only_the_matching_approval(self):
        store.approve(self.db, "t1", "Bash", {"command": "npm test"}, now=0)
        store.approve(self.db, "t1", "Bash", {"command": "make"}, now=0)
        store.approve(self.db, "t2", "Bash", {"command": "npm test"}, now=0)
        store.revoke(self.db, "t1", "Bash", {"command": "npm test"})
        self.assertFalse(store.approved(self.db, "t1", "Bash", {"command": "npm test"}, now=1))
        self.assertTrue(store.approved(self.db, "t1", "Bash", {"command": "make"}, now=1))
        self.assertTrue(store.approved(self.db, "t2", "Bash", {"command": "npm test"}, now=1))

    def test_manager_typed_prompt_is_not_the_user(self):
        store.add_nudge(self.db, "t1")
        store.open_ask(self.db, "s1", "t1", "Pick a database", ["Postgres", "SQLite"], "SQLite")
        store.note_tell(self.db, "t1", "Go with SQLite.\n", now=100)
        self.assertFalse(store.note_user_prompt(self.db, "t1", "Go with SQLite.", now=110))
        self.assertEqual(store.nudges(self.db, "t1"), 1)
        self.assertEqual(len(store.state(self.db)["asks"]), 1)
        self.assertTrue(store.note_user_prompt(self.db, "t1", "Use Postgres instead", now=120))
        self.assertEqual(store.nudges(self.db, "t1"), 0)
        self.assertEqual(store.state(self.db)["asks"], [])
        self.assertEqual(store.user_prompt_age(self.db, "t1", now=150), 30)

    def test_stale_tell_does_not_hide_the_user(self):
        store.add_nudge(self.db, "t1")
        store.note_tell(self.db, "t1", "Go with SQLite.", now=100)
        self.assertTrue(store.note_user_prompt(self.db, "t1", "Go with SQLite.", now=220))
        self.assertEqual(store.user_prompt_age(self.db, "t1", now=230), 10)
        self.assertEqual(store.nudges(self.db, "t1"), 0)

    def test_bash_fingerprint_ignores_the_description(self):
        store.approve(self.db, "t1", "Bash", {"command": "make dist", "description": "Build it"}, now=0)
        self.assertTrue(store.approved(self.db, "t1", "Bash", {"command": "make dist", "description": "Package"}, now=1))
        self.assertTrue(store.approved(self.db, "t1", "Bash", {"command": "make dist"}, now=1))
        self.assertFalse(store.approved(self.db, "t1", "Bash", {"command": "make clean", "description": "Build it"}, now=1))
        tool_input = {"file_path": "/x", "description": "a"}
        self.assertNotEqual(store.fingerprint("Write", tool_input), store.fingerprint("Write", {"file_path": "/x"}))
        bash = {"command": "ls", "description": "List"}
        store.fingerprint("Bash", bash)
        self.assertEqual(bash, {"command": "ls", "description": "List"})
        self.assertEqual(store.fingerprint("Bash", "ls"), store.fingerprint("Bash", "ls"))

    def test_terminal_less_asks_do_not_replace_each_other(self):
        first = store.open_ask(self.db, "s1", "", "Merge order?", [], "", now=1)
        second = store.open_ask(self.db, "s2", "", "Which host?", [], "", now=2)
        self.assertEqual([a["id"] for a in store.state(self.db)["asks"]], [first, second])

    def test_dismissing_an_ask_resets_its_terminals_nudges(self):
        for _ in range(store.MAX_NUDGES):
            store.add_nudge(self.db, "t1")
            store.add_nudge(self.db, "t2")
        ask = store.open_ask(self.db, "s1", "t1", "Stuck", [], "")
        loose = store.open_ask(self.db, "s1", "", "Merge order?", [], "")
        store.resolve_asks(self.db, ask_id=ask)
        store.resolve_asks(self.db, ask_id=loose)
        self.assertEqual((store.nudges(self.db, "t1"), store.nudges(self.db, "t2")), (0, store.MAX_NUDGES))
        self.assertEqual(store.state(self.db)["asks"], [])

    def test_state_shape(self):
        store.record_event(self.db, "s1", "t1", "stop", "continue", "Sent it to the next task.",
                           {"message": "Take mx-1.2 next.", "decisions": [{"decision": "Use tabs", "why": "Matches the app", "undo": "Swap the component"}]})
        store.add_chat(self.db, "user", "What is left?")
        state = store.state(self.db)
        self.assertEqual(state["paused"], False)
        self.assertEqual(state["unseen"], 1)
        self.assertEqual(state["events"][0]["message"], "Take mx-1.2 next.")
        self.assertEqual(state["events"][0]["decisions"][0]["undo"], "Swap the component")
        self.assertEqual(state["chat"][0]["role"], "user")
        store.mark_seen(self.db)
        self.assertEqual(store.state(self.db)["unseen"], 0)

    def test_state_defaults_and_ordering(self):
        store.record_event(self.db, "s1", "t1", "permission", "allow", "First.", now=1)
        store.record_event(self.db, "s1", "t1", "permission", "deny", "Second.", now=2)
        store.add_chat(self.db, "user", "Hi", now=1)
        store.add_chat(self.db, "manager", "Hello", now=2)
        store.set_setting(self.db, "briefing", "All moving.")
        store.set_setting(self.db, "briefing_time", "5.5")
        state = store.state(self.db)
        self.assertEqual([e["summary"] for e in state["events"]], ["Second.", "First."])
        self.assertEqual(state["events"][0]["message"], "")
        self.assertEqual(state["events"][0]["decisions"], [])
        self.assertIs(state["events"][0]["seen"], False)
        store.record_event(self.db, None, "t1", "sweep", "tell", "Odd.", {"message": None, "decisions": "x"}, now=3)
        odd = store.state(self.db)["events"][0]
        self.assertEqual((odd["stream"], odd["message"], odd["decisions"]), ("", "", []))
        self.assertEqual([c["text"] for c in state["chat"]], ["Hi", "Hello"])
        self.assertEqual(state["briefing"], "All moving.")
        self.assertEqual(state["briefingTime"], 5.5)
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        store.initialize(empty)
        self.assertEqual(store.state(empty)["briefing"], "")
        self.assertIsNone(store.state(empty)["briefingTime"])

    def test_second_ask_replaces_the_first_for_a_terminal(self):
        store.open_ask(self.db, "s1", "t1", "Pick a database", ["Postgres", "SQLite"], "SQLite", now=1)
        latest = store.open_ask(self.db, "s1", "t1", "Pick a port", ["8080"], None, now=2)
        other = store.open_ask(self.db, "s2", "t2", "Pick a name", [], "Muxteroid", now=3)
        asks = store.state(self.db)["asks"]
        self.assertEqual([a["id"] for a in asks], [latest, other])
        self.assertEqual(asks[0]["options"], ["8080"])
        self.assertEqual(asks[0]["recommendation"], "")
        store.resolve_asks(self.db, ask_id=other)
        self.assertEqual([a["id"] for a in store.state(self.db)["asks"]], [latest])

    def test_paused_follows_setting(self):
        self.assertFalse(store.paused(self.db))
        store.set_setting(self.db, "paused", "1")
        self.assertTrue(store.paused(self.db))
        self.assertTrue(store.state(self.db)["paused"])
        store.set_setting(self.db, "paused", "0")
        self.assertFalse(store.paused(self.db))
        self.assertEqual(store.setting(self.db, "missing", "x"), "x")

    def test_nudges_count_up(self):
        self.assertEqual(store.nudges(self.db, "t1"), 0)
        self.assertEqual(store.add_nudge(self.db, "t1"), 1)
        self.assertEqual(store.add_nudge(self.db, "t1"), 2)
        self.assertEqual(store.nudges(self.db, "t2"), 0)


if __name__ == "__main__":
    unittest.main()
