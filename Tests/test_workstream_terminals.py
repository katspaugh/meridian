"""Runtime isolation and compatibility checks; never start a real agent."""
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
with tempfile.TemporaryDirectory() as home:
    with patch.dict(os.environ, MUXTEROID_HOME=home):
        import streams
REVIEW = streams.manager.review


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
            patch.object(streams.manager, "review", return_value=streams.manager.NOTHING),
            # begin() writes to the real log folder and turns on tracing for the whole process.
            patch.object(streams.trace, "begin"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.review = streams.manager.review
        self.begin = streams.trace.begin
        # hook() installs handlers in this process; put the runner's own back afterwards.
        for number in (streams.signal.SIGTERM, streams.signal.SIGINT, streams.signal.SIGHUP):
            self.addCleanup(streams.signal.signal, number, streams.signal.getsignal(number))

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

    def test_blocker_details_reach_app_and_update_when_resolved(self):
        self.child.update(status="blocked", notes="Older progress notes", metadata={
            "blocked_reason": "No mixed-provider workstream",
            "unblock_action": "Add Claude beside Codex, then ask for verification",
        })
        task = streams.streams()[0]['tasks'][0]
        self.assertEqual(task['blockedReason'], "No mixed-provider workstream")
        self.assertEqual(task['unblockAction'], "Add Claude beside Codex, then ask for verification")
        self.child.update(status="in_progress", metadata={})
        task = streams.streams()[0]['tasks'][0]
        self.assertEqual(task['status'], "in_progress")
        self.assertIsNone(task['blockedReason'])
        self.assertIsNone(task['unblockAction'])

    def test_legacy_blocker_keeps_notes_without_inventing_an_action(self):
        self.child.update(status="blocked", notes="Waiting for a live example")
        task = streams.streams()[0]['tasks'][0]
        self.assertEqual(task['notes'], "Waiting for a live example")
        self.assertIsNone(task['blockedReason'])
        self.assertIsNone(task['unblockAction'])

    def test_agent_context_requires_actionable_blockers(self):
        context = streams.agent_context(self.parent['id'], "SessionStart")
        self.assertIn('--set-metadata blocked_reason=', context)
        self.assertIn('--set-metadata unblock_action=', context)
        self.assertIn('--unset-metadata unblock_action', context)

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

    def run_hook(self, key, sid, **event):
        out = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(dict(dict(session_id=sid), **event)))), \
             contextlib.redirect_stdout(out):
            streams.hook(key, sid)
        return out.getvalue()

    def activity(self, key):
        with streams.database() as db:
            return db.execute("SELECT state,detail FROM activity WHERE id=?", (key,)).fetchone()

    def subagent_state(self, key, agent):
        with streams.database() as db:
            return db.execute("SELECT state FROM agents WHERE stream=? AND agent=?", (key, agent)).fetchone()[0]

    def test_plain_tool_call_does_not_ask_for_attention(self):
        key = self.register(provider="claude", state="Working", detail="Claude is continuing.", sid="sid")
        self.run_hook(key, "sid", hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(self.activity(key), ("Working", "Claude is continuing."))

    def test_question_still_needs_you_when_the_manager_does_nothing(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.assertEqual(self.run_hook(key, "sid", hook_event_name="PreToolUse", tool_name="AskUserQuestion",
                                       tool_input={}), "")
        self.assertEqual(self.activity(key), ("Needs you", "Claude is waiting for AskUserQuestion."))

    def test_unreviewed_permission_request_still_needs_you(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.review.return_value = streams.manager.NOTHING
        self.run_hook(key, "sid", hook_event_name="PermissionRequest", tool_name="Bash", tool_input={})
        self.assertEqual(self.activity(key)[0], "Needs you")

    def test_manager_decision_is_printed_and_sets_the_state(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.review.return_value = streams.manager.Outcome({"decision": "block", "reason": "Carry on."}, "Working", "Sent it on.")
        out = self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done?")
        self.assertEqual(json.loads(out), {"decision": "block", "reason": "Carry on."})
        self.assertEqual(self.activity(key), ("Working", "Sent it on."))
        bridge, terminal, sid, event = self.review.call_args.args
        self.assertIs(bridge, streams)
        self.assertEqual((terminal, sid, event["hook_event_name"]), (key, "sid", "Stop"))

    def test_manager_ask_sets_needs_you_without_output(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.review.return_value = streams.manager.Outcome(None, "Needs you", "Pick one")
        self.assertEqual(self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done?"), "")
        self.assertEqual(self.activity(key), ("Needs you", "Pick one"))

    def test_failed_review_keeps_todays_behavior(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.review.side_effect = RuntimeError("boom")
        self.assertEqual(self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done?"), "")
        self.assertEqual(self.activity(key), ("Review", "Done?"))

    def test_approved_tool_call_is_printed_without_changing_the_state(self):
        key = self.register(provider="claude", state="Working", detail="Claude is continuing.", sid="sid")
        allow = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
        self.review.return_value = streams.manager.Outcome(allow, None, None)
        out = self.run_hook(key, "sid", hook_event_name="PreToolUse", tool_name="Bash", tool_input={})
        self.assertEqual(json.loads(out), allow)
        self.assertEqual(self.activity(key), ("Working", "Claude is continuing."))

    def test_unreviewed_denial_clears_the_reviewing_line(self):
        key = self.register(provider="claude", state="Working",
                            detail="The manager is reviewing a permission request for Bash.", sid="sid")
        self.review.side_effect = RuntimeError("boom")
        self.run_hook(key, "sid", hook_event_name="PermissionDenied", tool_name="Bash", tool_input={}, reason="[Scope]")
        self.assertEqual(self.activity(key), ("Working", "Claude is continuing after auto mode blocked an action."))

    def test_a_killed_review_still_writes_the_default_state(self):
        for number in (streams.signal.SIGTERM, streams.signal.SIGINT, streams.signal.SIGHUP):
            key = self.register(provider="claude", state="Working",
                                detail="The manager is reviewing where this work stands.", sid="sid")
            handlers, seen = {}, []

            def review(*args, number=number):
                seen.append(set(handlers))
                handlers[number](number, None)  # As if the signal arrived during the consult.
            self.review.side_effect = review
            with patch.object(streams.signal, "signal", side_effect=lambda n, h: handlers.__setitem__(n, h)):
                self.assertEqual(self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done?"), "")
            self.assertEqual(seen, [{streams.signal.SIGTERM, streams.signal.SIGINT, streams.signal.SIGHUP}])
            self.assertEqual(self.activity(key), ("Review", "Done?"))
            with self.assertRaises(Exception):
                handlers[number](number, None)

    def test_no_needs_you_notification_while_the_manager_reviews(self):
        reviewing = "The manager is reviewing a permission request for Bash."
        key = self.register(provider="claude", state="Working", detail=reviewing, sid="sid")
        self.run_hook(key, "sid", hook_event_name="Notification", notification_type="permission_prompt",
                      message="Claude needs your permission to use Bash")
        self.assertEqual(self.activity(key), ("Working", reviewing))
        self.register(key, provider="claude", state="Working", detail="Claude is continuing.", sid="sid")
        self.run_hook(key, "sid", hook_event_name="Notification", notification_type="permission_prompt",
                      message="Claude needs your permission to use Bash")
        self.assertEqual(self.activity(key), ("Needs you", "Claude needs your permission to use Bash"))

    def test_prompt_is_reviewed_and_still_receives_agent_context(self):
        key = self.register(provider="claude", state="Ready", sid="sid")
        out = json.loads(self.run_hook(key, "sid", hook_event_name="UserPromptSubmit", prompt="Go"))
        self.assertIn("Beads", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.review.call_args.args[3]["prompt"], "Go")
        self.assertEqual(self.activity(key)[0], "Working")

    def test_stale_session_is_not_reviewed(self):
        key = self.register(provider="claude", sid="sid")
        self.run_hook(key, "sid", session_id="stale", hook_event_name="Stop")
        self.review.assert_not_called()

    def test_subagent_tool_call_does_not_ask_for_attention(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.run_hook(key, "sid", hook_event_name="SubagentStart", agent_id="a1", agent_type="Explore")
        self.run_hook(key, "sid", hook_event_name="PreToolUse", agent_id="a1", tool_name="Bash", tool_input={})
        self.assertEqual(self.subagent_state(key, "a1"), "Working")
        self.run_hook(key, "sid", hook_event_name="PreToolUse", agent_id="a1", tool_name="AskUserQuestion", tool_input={})
        self.assertEqual(self.subagent_state(key, "a1"), "Needs you")
        self.assertEqual(self.activity(key)[0], "Working")

    def test_subagent_decided_by_the_manager_is_not_marked_needs_you(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        allow = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
        self.run_hook(key, "sid", hook_event_name="SubagentStart", agent_id="a1", agent_type="Explore")
        self.review.return_value = streams.manager.Outcome(allow, None, None)
        out =self.run_hook(key, "sid", hook_event_name="PermissionRequest", agent_id="a1", tool_name="Bash", tool_input={})
        self.assertEqual(json.loads(out), allow)
        self.assertEqual(self.subagent_state(key, "a1"), "Working")

    def test_manager_reviews_through_the_real_bridge(self):
        key = self.register(key=self.parent['id'], provider="claude", state="Working", detail="Claude is continuing.", sid="sid")
        self.review.side_effect = REVIEW
        call = dict(tool_name="Bash", tool_input={"command": "make"})
        with streams.database() as db:
            streams.manager_store.set_setting(db, "paused", "0")
            streams.manager_store.approve(db, key, "Bash", call["tool_input"])
        with patch.object(streams.manager.brain, "consult", return_value=None) as consult:
            out = self.run_hook(key, "sid", hook_event_name="PreToolUse", **call)
            self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "allow")
            self.run_hook(key, "sid", hook_event_name="PostToolUse", tool_response="", **call)
            self.assertEqual(self.run_hook(key, "sid", hook_event_name="PreToolUse", **call), "")
            self.assertEqual(self.activity(key), ("Working", "Claude is continuing."))
            self.assertEqual(self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done?"), "")
        consult.assert_called_once()
        self.begin.assert_called_once_with("manager-review", key)
        self.assertIn("Folder, title, links and tasks", consult.call_args.args[2])
        self.assertEqual(consult.call_args.kwargs["folders"], [str(self.folder)])
        self.assertEqual(self.activity(key), ("Review", "Done?"))
        with streams.database() as db:
            events = db.execute("SELECT stream, terminal, trigger, verdict FROM manager_events ORDER BY id").fetchall()
        self.assertEqual(events, [(self.parent["id"], key, "denied", "retry-allowed"),
                                  (self.parent["id"], key, "denied", "ran")])

    def test_settings_register_the_manager_hooks(self):
        hooks = json.loads(streams.settings("k", "s"))["hooks"]
        self.assertIn("PermissionDenied", hooks)
        self.assertNotIn("matcher", hooks["PreToolUse"][0])
        self.assertEqual(hooks["PermissionRequest"][0]["hooks"][0]["timeout"], 180)
        self.assertEqual(hooks["PermissionDenied"][0]["hooks"][0]["timeout"], 180)
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["timeout"], 180)
        self.assertEqual(hooks["Stop"][0]["hooks"][0]["timeout"], 300)
        self.assertNotIn("timeout", hooks["PostToolUse"][0]["hooks"][0])
        self.assertEqual(hooks["Notification"][0]["matcher"], "permission_prompt|elicitation_dialog")

    def test_database_has_the_manager_tables(self):
        with streams.database() as db:
            db.execute("SELECT count(*) FROM manager_events").fetchone()

    def test_bridge_lock_is_exclusive_and_released(self):
        def try_lock():
            with (self.root / "bridge.lock").open("a") as other:
                try:
                    streams.fcntl.flock(other, streams.fcntl.LOCK_EX | streams.fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False
        with streams.bridge_lock():
            self.assertFalse(try_lock())
        self.assertTrue(try_lock())

    def test_links_and_subagents_remain_associated_with_the_workstream(self):
        key = self.register()
        self.live.add(key)
        url = "https://github.com/example/project/pull/12"
        with streams.database() as db:
            streams.links.collect(db, key, url)
            streams.links.collect(db, self.parent['id'], url)
            db.execute("INSERT INTO codex_children VALUES (?,?,?,?,?)", (key, 'child', 'Check behavior', 'Agent', 'Working'))
        row = streams.streams()[0]
        self.assertEqual([link['url'] for link in row['links']], [url])
        self.assertEqual(row['agents'][0]['terminalID'], key)
        self.assertEqual(row['agents'][0]['state'], 'Working')

    def test_only_active_subagents_are_listed_for_each_provider(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                key = self.register(key='term-' + provider, provider=provider)
                self.live.add(key)
                states = ('Working', 'Needs you', 'Finished', 'Offline', 'Ready', 'Recorded')
                with streams.database() as db:
                    for state in states:
                        if provider == 'codex':
                            db.execute('INSERT INTO codex_children VALUES (?,?,?,?,?)',
                                       (key, state, state, 'Agent', state))
                        else:
                            db.execute('INSERT INTO agents VALUES (?,?,?,?,?,?,?)',
                                       (key, 'extra-session', state, 'Agent', state, None, None))
                    if provider == 'claude':
                        db.execute('INSERT INTO agents VALUES (?,?,?,?,?,?,?)',
                                   (key, 'old-session', 'stale', 'Agent', 'Working', None, None))
                row = streams.streams()[0]
                terminal = next(t for t in row['terminals'] if t['id'] == key)
                self.assertCountEqual([a['state'] for a in terminal['agents']], ['Working', 'Needs you'])
                self.assertEqual([a for a in row['agents'] if a['terminalID'] == key], terminal['agents'])
                self.live.remove(key)
                self.assertFalse([a for a in streams.streams()[0]['agents'] if a['terminalID'] == key])

    def test_finished_claude_agent_disappears_but_retains_its_response(self):
        key = self.register(provider='claude')
        self.live.add(key)
        with streams.database() as db:
            streams.subagents.record(db, key, 'extra-session',
                                     dict(agent_id='child', hook_event_name='SubagentStart'))
        self.assertEqual(len(streams.streams()[0]['agents']), 1)
        with streams.database() as db:
            streams.subagents.record(db, key, 'extra-session',
                                     dict(agent_id='child', hook_event_name='SubagentStop',
                                          last_assistant_message='Done'))
        self.assertEqual(streams.streams()[0]['agents'], [])
        with streams.database() as db:
            self.assertEqual(streams.subagents.messages(db, key, 'extra-session/child'),
                             [dict(label='Response', text='Done')])

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

    def test_agent_exit_runs_shell_before_closing_tab(self):
        key = self.register(provider='claude')
        commands = []
        def execute(command, **kwargs):
            commands.append(command)
            if len(commands) == 2:
                with streams.database() as db:
                    self.assertEqual(db.execute('SELECT provider FROM workstream_terminals WHERE id=?', (key,)).fetchone()[0], 'shell')
                    self.assertIsNone(db.execute('SELECT 1 FROM closed_terminals WHERE id=?', (key,)).fetchone())
        with patch.object(streams.signal, 'signal'), patch.object(streams.os, 'isatty', return_value=False), \
             patch.object(streams.subprocess, 'run', side_effect=execute):
            streams.run_terminal(key, ['claude'])
        self.assertEqual(commands[0], ['claude'])
        self.assertEqual(commands[1][-1], '-l')
        self.assertNotIn(key, [t['id'] for t in streams.streams()[0]['terminals']])

    def test_shell_exit_closes_tab_without_starting_another_shell(self):
        key = self.register(provider='shell')
        with patch.object(streams.signal, 'signal'), patch.object(streams.os, 'isatty', return_value=False), \
             patch.object(streams.subprocess, 'run') as execute:
            streams.run_terminal(key, ['/bin/sh', '-l'])
        execute.assert_called_once_with(['/bin/sh', '-l'], check=False)
        self.assertNotIn(key, [t['id'] for t in streams.streams()[0]['terminals']])

    def test_last_tab_stays_closed_and_workstream_can_add_again(self):
        streams.streams()
        streams.close_terminal(self.parent['id'])
        row = streams.streams()[0]
        self.assertEqual(row['terminals'], [])
        self.assertEqual(row['state'], 'Ready')
        self.assertEqual(row['tasks'][0]['id'], self.child['id'])
        with patch.object(streams, 'launch'):
            key = streams.add_terminal(self.parent['id'], 'shell')
        self.assertEqual([t['id'] for t in streams.streams()[0]['terminals']], [key])
        with self.assertRaisesRegex(ValueError, 'closed'):
            streams.launch(self.parent['id'])

    def test_close_signals_only_the_selected_socket_owner(self):
        key = self.register()
        self.live.add(key)
        result = subprocess.CompletedProcess([], 0, '12345\n', '')
        with patch.object(streams.subprocess, 'run', return_value=result) as lookup, patch.object(streams.os, 'kill') as kill:
            streams.close_terminal(key)
        self.assertEqual(lookup.call_args.args[0][-1], str(streams.TERMINALS / key))
        kill.assert_called_once_with(12345, streams.signal.SIGTERM)
        self.assertFalse(streams.use_shell(key))

    def test_legacy_exit_falls_back_only_after_agent_has_stopped(self):
        key = self.register()
        self.live.add(key)
        with patch.object(streams, 'launch') as launch:
            streams.terminal_exited(key)
            launch.assert_not_called()
            self.live.remove(key)
            streams.terminal_exited(key)
            launch.assert_called_once_with(key)
        self.assertEqual(streams.terminal_issue(key)[1], 'shell')
        streams.terminal_exited(key)
        self.assertNotIn(key, [t['id'] for t in streams.streams()[0]['terminals']])

    def test_now_line_tracks_tools_and_clears_at_turn_end(self):
        key = self.register(provider="claude", state="Working", sid="sid")
        self.live.add(key)
        self.run_hook(key, "sid", hook_event_name="PreToolUse", tool_name="Bash",
                      tool_input={"description": "Running tests", "command": "make test"})
        terminal = next(t for t in streams.streams()[0]['terminals'] if t['id'] == key)
        self.assertEqual(terminal['nowText'], 'Running tests')
        self.assertIsInstance(terminal['nowStarted'], float)
        self.run_hook(key, "sid", hook_event_name="Stop", last_assistant_message="Done")
        terminal = next(t for t in streams.streams()[0]['terminals'] if t['id'] == key)
        self.assertEqual(terminal['nowText'], '')
        self.assertIsNone(terminal['nowStarted'])

    def test_saved_rules_reach_session_and_prompt_context(self):
        key = self.register(provider="claude", sid="sid")
        streams.streams()
        project = streams.manager_store.project_key(self.parent['metadata'])
        streams.manager_store.save_rule(self.root, project, 'Run tests before finishing.')
        for event in ('SessionStart', 'UserPromptSubmit'):
            self.assertIn('Run tests before finishing.', streams.agent_context(key, event))

    def test_return_after_thirty_minutes_requests_one_sweep(self):
        import time
        with streams.database() as db:
            streams.manager_store.set_setting(db, 'sweep_time', '1234')
            streams.manager_store.set_setting(db, 'inactive_since', str(time.time() - 1801))
        streams.main('manager-active', '1')
        with streams.database() as db:
            self.assertEqual(streams.manager_store.setting(db, 'sweep_time'), '0')
            self.assertEqual(streams.manager_store.setting(db, 'inactive_since'), '')
            streams.manager_store.set_setting(db, 'sweep_time', '5678')
        streams.main('manager-active', '1')
        with streams.database() as db:
            self.assertEqual(streams.manager_store.setting(db, 'sweep_time'), '5678')

    def test_post_tool_reminder_reaches_hook_output_while_paused(self):
        import time
        key = self.register(provider="claude", state="Working", sid="sid")
        self.live.add(key)
        with streams.database() as db:
            streams.manager_store.note_user_prompt(db, key, 'Work', now=time.time() - 301)
        with patch.object(streams.manager, 'review', side_effect=REVIEW):
            out = json.loads(self.run_hook(key, 'sid', hook_event_name='PostToolUse', tool_name='Read', tool_input={}))
        self.assertIn('planned steps', out['hookSpecificOutput']['additionalContext'])
        self.assertEqual(out['hookSpecificOutput']['hookEventName'], 'PostToolUse')


class WorktreeCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'source repo'
        self.repo.mkdir()
        self.home = self.root / 'app home'
        self.home.mkdir()
        self.created = []
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'app').mkdir()
        (self.repo / 'app' / 'file.txt').write_text('committed\n')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-m', 'Initial')
        for p in [patch.object(streams, 'ROOT', self.home), patch.object(streams, 'bd', side_effect=self.bd)]:
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-C', str(cwd or self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def bd(self, *args):
        if args[0] == 'create':
            metadata = json.loads(args[args.index('--metadata') + 1])
            key = 'mx-created-' + str(len(self.created))
            self.created.append(dict(id=key, title=args[args.index('--title') + 1], status='open', metadata=metadata))
            return key
        if args[0] == 'show':
            return json.dumps([next(i for i in self.created if i['id'] == args[1])])
        return json.dumps(self.created)

    def test_each_provider_gets_a_distinct_worktree_and_branch(self):
        (self.repo / 'app' / 'file.txt').write_text('uncommitted\n')
        (self.repo / 'untracked.txt').write_text('local only\n')
        for provider in ('claude', 'codex'):
            streams.create_stream('', str(self.repo), '', provider)
        folders = [Path(i['metadata']['folder']) for i in self.created]
        self.assertNotEqual(*folders)
        for folder, issue in zip(folders, self.created):
            self.assertEqual(issue['title'], 'source repo')
            self.assertEqual((folder / 'app' / 'file.txt').read_text(), 'committed\n')
            self.assertFalse((folder / 'untracked.txt').exists())
            self.assertEqual(self.git('rev-parse', 'HEAD', cwd=folder), self.git('rev-parse', 'HEAD'))
            self.assertEqual(self.git('branch', '--show-current', cwd=folder), issue['metadata']['worktreeBranch'])
            self.assertEqual(issue['metadata']['sourceFolder'], str(self.repo))
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual((self.repo / 'app' / 'file.txt').read_text(), 'uncommitted\n')

    def test_subdirectory_and_selected_checkout_commit_are_preserved(self):
        source = self.root / 'existing checkout'
        self.git('worktree', 'add', '-b', 'feature/source', str(source))
        (source / 'app' / 'file.txt').write_text('feature\n')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-am', 'Feature', cwd=source)
        streams.create_stream('Test', str(source / 'app'), '', 'codex')
        meta = self.created[0]['metadata']
        folder = Path(meta['folder'])
        self.assertEqual(folder.name, 'app')
        self.assertEqual((folder / 'file.txt').read_text(), 'feature\n')
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=folder), self.git('rev-parse', 'HEAD', cwd=source))

    def test_extra_terminals_reuse_the_created_worktree(self):
        key = streams.create_stream('Test', str(self.repo), '', 'codex')
        before = self.git('worktree', 'list', '--porcelain')
        with patch.object(streams, 'terminals', return_value=set()), \
             patch.object(streams, 'legacy_sessions', return_value=set()), \
             patch.object(streams.shutil, 'which', return_value='/bin/agent'), \
             patch.object(streams.shutil, 'copyfile'), patch.object(streams, 'run') as launch:
            streams.add_terminal(key, 'shell')
            self.assertEqual(launch.call_args.kwargs['cwd'], Path(self.created[0]['metadata']['folder']))
            streams.add_terminal(key, 'claude')
            self.assertEqual(launch.call_args.kwargs['cwd'], Path(self.created[0]['metadata']['folder']))
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)

    def test_plain_folder_remains_usable(self):
        folder = self.root / 'plain folder'
        folder.mkdir()
        streams.create_stream('Test', str(folder), '', 'claude')
        self.assertEqual(self.created[0]['metadata']['folder'], str(folder))
        self.assertNotIn('worktreeRoot', self.created[0]['metadata'])

    def test_initial_names_use_reference_and_preserve_explicit_titles(self):
        folder = self.root / 'plain folder'
        folder.mkdir()
        cases = [
            ('', '  ', 'plain folder'),
            ('', ' TEAM-123 ', 'TEAM-123'),
            ('', 'https://linear.app/team/issue/eng-42/fix-startup', 'ENG-42'),
            ('', 'https://github.com/owner/project/pull/123', 'project#123'),
            ('', 'https://github.com/owner/project/pull/123/files?view=split#diff', 'project#123'),
            ('', 'https://example.com/docs/Getting%20started/?view=full#intro', 'Getting started'),
            ('', 'https://example.com/', 'example.com'),
            ('  Custom name  ', 'https://example.com/', 'Custom name'),
        ]
        for title, reference, expected in cases:
            with self.subTest(reference=reference, title=title):
                streams.create_stream(title, str(folder), reference)
                self.assertEqual(self.created[-1]['title'], expected)

    def test_unborn_repository_and_uncommitted_subfolder_fail_before_creating_issue(self):
        unborn = self.root / 'unborn'
        unborn.mkdir()
        self.git('init', cwd=unborn)
        with self.assertRaisesRegex(ValueError, 'initial commit'):
            streams.create_stream('Test', str(unborn), '', 'codex')
        folder = self.repo / 'untracked'
        folder.mkdir()
        with self.assertRaisesRegex(ValueError, 'subfolder is not committed'):
            streams.create_stream('Test', str(folder), '', 'claude')
        self.assertEqual(self.created, [])

    def test_issue_creation_failure_cleans_up_only_the_new_checkout(self):
        before = self.git('worktree', 'list', '--porcelain')
        with patch.object(streams, 'bd', side_effect=ValueError('Tracker unavailable')):
            with self.assertRaisesRegex(ValueError, 'Tracker unavailable'):
                streams.create_stream('Test', str(self.repo), '', 'codex')
        self.assertEqual(self.git('worktree', 'list', '--porcelain'), before)
        self.assertEqual(self.git('branch', '--list', 'muxteroid/*'), '')
        self.assertEqual(list((self.home / 'worktrees').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
