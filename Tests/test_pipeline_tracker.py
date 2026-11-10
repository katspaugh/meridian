"""Run storage and Claude hook events for the Superpowers pipeline."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
with tempfile.TemporaryDirectory() as home:
    with patch.dict(os.environ, MUXTEROID_HOME=home):
        import streams
import pipeline
import pipeline_tracker as tracker


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'Application Support'
        self.folder = self.root / 'worktree'
        self.folder.mkdir(parents=True)
        self.parent = dict(id='mx-example', title='Shared outcome', status='open', created_at='2020-01-01T00:00:00Z',
                           metadata=dict(folder=str(self.folder), provider='claude'))
        self.live = set()
        for p in [patch.object(streams, 'ROOT', self.root), patch.object(streams, 'TERMINALS', self.root / 'sockets'),
                  patch.object(streams, 'bd', side_effect=lambda *a: json.dumps([self.parent])),
                  patch.object(streams, 'terminals', side_effect=lambda: self.live.copy()),
                  patch.object(streams, 'legacy_sessions', return_value=set())]:
            p.start()
            self.addCleanup(p.stop)
        with streams.database() as db:
            streams.register_original(db, self.parent)
            db.execute("INSERT OR REPLACE INTO workstream_terminals VALUES ('term-extra','mx-example','claude','Claude 2')")
            for key in ('mx-example', 'term-extra'):
                db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?,?,NULL)", (key, 's-' + key, 'Working', ''))

    def post(self, key, name, args, response='', agent=None):
        event = dict(session_id='s-' + key, hook_event_name='PostToolUse', tool_name=name,
                     tool_input=args, tool_response=response, cwd=str(self.folder))
        if agent:
            event['agent_id'] = agent
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(event))):
            streams.hook(key, 's-' + key)

    def run_state(self):
        with streams.database() as db:
            return tracker.load(db, 'mx-example')

    def plan_file(self, name='2026-11-10-demo.md'):
        path = self.folder / 'docs/superpowers/plans' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Demo Implementation Plan\n\n### Task 1: First\n\n### Task 2: Second\n')
        return path

    def test_skill_from_any_terminal_reaches_the_workstream_run(self):
        self.post('term-extra', 'Skill', {'skill': 'superpowers:brainstorming'})
        self.assertEqual(self.run_state()['phases']['brainstorm']['status'], 'running')

    def test_subagent_events_are_ignored(self):
        self.post('mx-example', 'Skill', {'skill': 'superpowers:brainstorming'}, agent='a1')
        self.assertIsNone(self.run_state())

    def test_plan_write_lists_tasks_and_records_the_document(self):
        plan = self.plan_file()
        self.post('mx-example', 'Write', {'file_path': str(plan)})
        run = self.run_state()
        self.assertEqual((run['name'], run['planPath'], len(run['tasks'])), ('demo', str(plan), 2))
        with streams.database() as db:
            rows = db.execute("SELECT kind,title FROM pipeline_docs WHERE stream='mx-example'").fetchall()
        self.assertEqual(rows, [('plan', 'Demo Implementation Plan')])

    def test_missing_and_unreadable_files_are_skipped(self):
        self.post('mx-example', 'Write', {'file_path': str(self.folder / 'docs/superpowers/plans/gone.md')})
        bad = self.folder / 'docs/superpowers/specs/bad.md'
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b'\xff\xfe# x')
        self.post('mx-example', 'Edit', {'file_path': str(bad)})
        self.post('mx-example', 'Write', {'file_path': str(self.folder / 'README.md')})
        self.post('mx-example', 'Write', {})
        self.assertIsNone(self.run_state())

    def test_bash_events_follow_a_task_through_a_path_with_spaces(self):
        plan = self.plan_file()
        workspace = self.folder / '.superpowers/sdd/2026-11-10-demo'
        workspace.mkdir(parents=True)
        (workspace / 'plan-path').write_text('docs/superpowers/plans/2026-11-10-demo.md\n')
        self.post('mx-example', 'Bash', {'command': 'bash x/sdd-workspace docs/superpowers/plans/2026-11-10-demo.md'},
                  {'stdout': str(workspace) + '\n', 'stderr': ''})
        run = self.run_state()
        self.assertEqual((run['workspace'], run['planPath'], run['phases']['execute']['status']),
                         (str(workspace), str(plan), 'running'))
        self.post('mx-example', 'Bash', {'command': 'bash x/task-brief docs/superpowers/plans/2026-11-10-demo.md 1'}, {'stdout': 'wrote'})
        self.assertEqual(self.run_state()['tasks'][0]['implement']['status'], 'running')

    def test_pull_request_from_a_string_response_and_from_github_mcp(self):
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        self.post('mx-example', 'Bash', {'command': 'gh pr create --fill'}, 'https://github.com/o/r/pull/12')
        self.assertEqual(self.run_state()['pr'], 12)
        with streams.database() as db:
            tracker.reset(db, 'mx-example')
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        self.post('mx-example', 'mcp__plugin_github_github__create_pull_request', {'owner': 'o', 'repo': 'r'},
                  [{'type': 'text', 'text': '{"url":"https://github.com/o/r/pull/13"}'}])
        self.assertEqual(self.run_state()['prUrl'], 'https://github.com/o/r/pull/13')
        self.post('mx-example', 'Bash', {'command': 'ls'})          # no tool_response shape problems
        self.post('mx-example', 'Bash', {'command': 'ls'}, None)

    def test_undecodable_run_counts_as_none_and_is_replaced(self):
        with streams.database() as db:
            db.execute("INSERT INTO pipeline_runs VALUES ('mx-example','not json')")
        self.assertIsNone(self.run_state())
        with streams.database() as db:
            db.execute("UPDATE pipeline_runs SET run='{\"name\": 1}' WHERE stream='mx-example'")
        self.assertIsNone(self.run_state())
        self.post('mx-example', 'Skill', {'skill': 'superpowers:brainstorming'})
        self.assertEqual(self.run_state()['phases']['brainstorm']['status'], 'running')

    def test_edit_or_overwrite_lists_a_document_but_never_starts_a_run(self):
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        before = self.run_state()['launchedAt']
        spec = self.folder / 'docs/superpowers/specs/2026-01-01-old-design.md'
        spec.parent.mkdir(parents=True)
        spec.write_text('# Old design\n')
        self.post('mx-example', 'Edit', {'file_path': str(spec)}, {'filePath': str(spec)})
        self.post('mx-example', 'Write', {'file_path': str(spec)}, {'type': 'update', 'filePath': str(spec)})
        run = self.run_state()
        self.assertEqual((run['launchedAt'], run['phases']['execute']['status']), (before, 'running'))
        self.assertNotIn('specPath', run)
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT title FROM pipeline_docs').fetchall(), [('Old design',)])

    def test_a_failing_tracker_does_not_break_the_hook(self):
        with patch.object(tracker, 'tool_event', side_effect=RuntimeError('boom')):
            self.post('mx-example', 'Bash', {'command': 'gh pr create'}, {'stdout': 'https://github.com/o/r/pull/12'})
        with streams.database() as db:
            self.assertEqual(db.execute("SELECT detail FROM activity WHERE id='mx-example'").fetchone()[0], 'Claude is continuing.')
            self.assertEqual(db.execute("SELECT count(*) FROM links WHERE stream='mx-example'").fetchone()[0], 1)

    def test_reset_action_removes_the_run_and_keeps_documents(self):
        self.post('mx-example', 'Write', {'file_path': str(self.plan_file())})
        streams.main('pipeline-reset', 'mx-example')
        self.assertIsNone(self.run_state())
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_docs').fetchone()[0], 1)

    def test_concurrent_updates_keep_every_event(self):
        def bump(run):
            run = run or pipeline.new_run(0.0)
            count = run.get('count', 0)
            time.sleep(0.01)
            run['count'] = count + 1
            return run
        def work():
            with streams.database() as db:
                tracker.update(db, 'mx-example', bump)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(self.run_state()['count'], 8)


if __name__ == '__main__':
    unittest.main()
