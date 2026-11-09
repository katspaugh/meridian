"""Runtime isolation and compatibility checks; never start a real agent."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
with tempfile.TemporaryDirectory() as home:
    with patch.dict(os.environ, MUXTEROID_HOME=home):
        import streams


class WorkstreamTerminalsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "worktree"
        self.folder.mkdir()
        self.parent = dict(id="mx-example", title="Shared outcome", status="open",
                           metadata=dict(folder=str(self.folder), provider="claude"))
        self.child = dict(id="mx-example.1", parent="mx-example", title="Implement change", status="in_progress")
        self.issues = [self.parent, self.child]
        self.live = set()
        self.patches = [
            patch.object(streams, "ROOT", self.root),
            patch.object(streams, "TERMINALS", self.root / "sockets"),
            patch.object(streams, "bd", side_effect=self.bd),
            patch.object(streams, "terminals", side_effect=lambda: self.live.copy()),
            patch.object(streams, "legacy_sessions", return_value=set()),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def bd(self, *args):
        if args[0] == "show":
            self.assertEqual(args[1], self.parent['id'])
            return json.dumps([self.parent])
        if "--parent" in args:
            self.assertEqual(args[args.index("--parent") + 1], self.parent['id'])
            return json.dumps([self.child])
        return json.dumps(self.issues)

    def register(self, key="term-extra", provider="codex", state="Ready", detail="Ready", sid="extra-session"):
        with streams.database() as db:
            streams.register_original(db, self.parent)
            db.execute("INSERT OR REPLACE INTO workstream_terminals VALUES (?,?,?,?)",
                       (key, self.parent['id'], provider, streams.PROVIDERS[provider]))
            db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,NULL)", (key, sid, state, detail))
        return key

    def test_existing_activity_migrates_without_changing_session_or_socket(self):
        with streams.database() as db:
            db.execute("INSERT INTO activity VALUES (?,?,?,?,NULL)", (self.parent['id'], "original-session", "Working", "Original"))
        self.live.add(self.parent['id'])
        row = streams.streams()[0]
        self.assertEqual(len(row['terminals']), 1)
        terminal = row['terminals'][0]
        self.assertEqual(terminal['id'], self.parent['id'])
        self.assertEqual(terminal['session'], "original-session")
        self.assertEqual(terminal['terminalSocket'], str(streams.TERMINALS / self.parent['id']))
        self.assertEqual(row['tasks'][0]['id'], self.child['id'])
        self.assertEqual(streams.streams()[0]['terminals'], row['terminals'])

    def test_extra_agent_receives_shared_parent_context(self):
        key = self.register()
        context = streams.agent_context(key, "SessionStart")
        self.assertIn("workstream mx-example.", context)
        self.assertIn("--parent mx-example", context)
        self.assertIn("mx-example.1 [in_progress]", context)
        self.assertNotIn(key, context)

    def test_all_launchers_use_shared_folder_and_beads_with_distinct_runtime_ids(self):
        for provider in streams.PROVIDERS:
            with self.subTest(provider=provider):
                key = self.register(key="term-" + provider, provider=provider)
                with patch.object(streams.shutil, "which", side_effect=lambda name: "/bin/" + name), \
                     patch.object(streams.shutil, "copyfile"), patch.object(streams, "run") as run:
                    streams.launch(key)
                args = run.call_args.args[0]
                options = run.call_args.kwargs
                self.assertEqual(options['cwd'], self.folder)
                self.assertEqual(options['env']['MUXTEROID_ISSUE'], self.parent['id'])
                self.assertEqual(options['env']['MUXTEROID_TERMINAL'], key)
                self.assertEqual(options['env']['BEADS_DIR'], str(self.root / '.beads'))
                self.assertIn(str(streams.TERMINALS / key), args)
                if provider == 'codex':
                    self.assertEqual(args[-2:], ['codex-run', key])
                if provider == 'claude':
                    self.assertIn(key, args[args.index('--settings') + 1])

    def test_claude_resume_uses_only_its_own_transcript(self):
        key = self.register(provider="claude")
        transcript = self.root / "transcript.jsonl"
        transcript.touch()
        with streams.database() as db:
            db.execute("UPDATE activity SET transcript=? WHERE id=?", (str(transcript), key))
        with patch.object(streams.shutil, "which", return_value="/bin/agent"), \
             patch.object(streams.shutil, "copyfile"), patch.object(streams, "run") as run:
            streams.launch(key)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index('--resume') + 1], 'extra-session')

    def test_live_terminal_is_not_restarted(self):
        key = self.register()
        self.live.add(key)
        with patch.object(streams, "run") as run:
            streams.launch(key)
        run.assert_not_called()

    def test_attention_and_working_take_precedence_over_finished_turn(self):
        self.register(self.parent['id'], "claude", "Review", "Claude reply")
        key = self.register(state="Working", detail="Codex working")
        self.live.update((self.parent['id'], key))
        self.assertEqual(streams.streams()[0]['state'], 'Working')
        with streams.database() as db:
            db.execute("UPDATE activity SET state='Needs you' WHERE id=?", (key,))
        self.assertEqual(streams.streams()[0]['state'], 'Needs you')
        self.live.remove(key)
        row = streams.streams()[0]
        self.assertEqual(row['state'], 'Review')
        self.assertEqual(row['terminals'][1]['state'], 'Offline')

    def test_idle_shell_does_not_hide_finished_agent_reply(self):
        self.register(self.parent['id'], "claude", "Review", "Claude reply")
        key = self.register(provider="shell")
        self.live.update((self.parent['id'], key))
        row = streams.streams()[0]
        self.assertEqual(row['state'], 'Review')
        self.assertEqual(row['detail'], 'Claude reply')

    def test_hook_changes_only_its_terminal_and_rejects_stale_session(self):
        self.register(self.parent['id'], "claude", "Working")
        key = self.register(provider="claude", state="Working")
        event = dict(session_id="extra-session", hook_event_name="Stop", last_assistant_message="Done")
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            streams.hook(key, 'extra-session')
        event['session_id'] = 'stale'
        event['last_assistant_message'] = 'Wrong'
        with patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
            streams.hook(key, 'extra-session')
        with streams.database() as db:
            original = db.execute("SELECT state FROM activity WHERE id=?", (self.parent['id'],)).fetchone()[0]
            extra = db.execute("SELECT state,detail FROM activity WHERE id=?", (key,)).fetchone()
        self.assertEqual(original, 'Working')
        self.assertEqual(extra, ('Review', 'Done'))

    def test_links_and_subagents_remain_associated_with_the_workstream(self):
        key = self.register()
        url = "https://github.com/example/project/pull/12"
        with streams.database() as db:
            streams.links.collect(db, key, url)
            streams.links.collect(db, self.parent['id'], url)
            db.execute("INSERT INTO codex_children VALUES (?,?,?,?,?)", (key, 'child', 'Check behavior', 'Agent', 'Working'))
        row = streams.streams()[0]
        self.assertEqual([link['url'] for link in row['links']], [url])
        self.assertEqual(row['agents'][0]['terminalID'], key)
        self.assertEqual(row['agents'][0]['state'], 'Offline')

    def test_add_persists_unique_ids_and_numbered_names(self):
        with patch.object(streams, 'launch'):
            first = streams.add_terminal(self.parent['id'], 'codex')
            second = streams.add_terminal(self.parent['id'], 'codex')
        self.assertNotEqual(first, second)
        row = streams.streams()[0]
        self.assertEqual([t['title'] for t in row['terminals']], ['Claude', 'Codex', 'Codex 2'])
        self.assertEqual(len(streams.streams()), 1)

    def test_failed_add_rolls_back_and_closed_workstreams_reject_add(self):
        with patch.object(streams, 'launch', side_effect=ValueError('Missing CLI')):
            with self.assertRaisesRegex(ValueError, 'Missing CLI'):
                streams.add_terminal(self.parent['id'], 'codex')
        self.assertEqual(len(streams.streams()[0]['terminals']), 1)
        self.parent['status'] = 'closed'
        with self.assertRaisesRegex(ValueError, 'Reopen'):
            streams.add_terminal(self.parent['id'], 'shell')
        with self.assertRaisesRegex(ValueError, 'Choose'):
            streams.add_terminal(self.parent['id'], 'unknown')

    def test_completed_parent_wins_over_live_sessions(self):
        key = self.register(state="Working")
        self.live.add(key)
        self.parent['status'] = 'closed'
        self.assertEqual(streams.streams()[0]['state'], 'Done')


if __name__ == '__main__':
    unittest.main()
