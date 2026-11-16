"""Run storage and Claude hook events for the Superpowers pipeline."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
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
import workstream_log as trace


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
        # The hook's guard would hide a tracker that raises; these tests must see it.
        self.real_guarded = tracker.guarded
        passthrough = lambda db, default, function, *args, **kwargs: function(*args, **kwargs)
        for p in [patch.object(streams, 'ROOT', self.root), patch.object(streams, 'TERMINALS', self.root / 'sockets'),
                  patch.object(streams, 'bd', side_effect=lambda *a: json.dumps([self.parent])),
                  patch.object(streams, 'terminals', side_effect=lambda: self.live.copy()),
                  patch.object(streams, 'legacy_sessions', return_value=set()),
                  patch.object(tracker, 'guarded', side_effect=passthrough)]:
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
        folder = self.folder / 'docs/superpowers/plans/dir.md'
        folder.mkdir(parents=True)
        self.post('mx-example', 'Write', {'file_path': str(folder)})
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

    def test_wrongly_typed_runs_count_as_none_and_are_replaced(self):
        good = pipeline.advance_to(pipeline.new_run(5.0), 'execute', 5.0)
        task = {'n': 1, 'title': 'One', 'implement': {'status': 'todo'}, 'review': {'status': 'todo'}}
        spoiled = [dict(good, name=None), dict(good, launchedAt='2026'), dict(good, launchedAt=True),
                   dict(good, launchedAt=float('nan')), dict(good, tasks=[{'n': 1}]),
                   dict(good, tasks=[dict(task, n=True)]), dict(good, tasks=[dict(task, n=10000)]),
                   dict(good, tasks=[dict(task, round=-1)]), dict(good, tasks=[dict(task, title=None)]),
                   dict(good, tasks=[dict(task, review={'status': 'later'})]),
                   dict(good, phases=dict(good['phases'], plan={'status': 'done', 'endedAt': 'noon'})),
                   dict(good, pr='12'), dict(good, prUrl=12), dict(good, ciDetail=['x']), dict(good, currentTask=1.5),
                   dict(good, ledgerMtime='x')]
        with streams.database() as db:
            tracker.update(db, 'mx-example', lambda run: dict(good, tasks=[task], pr=12, currentTask=1, ledgerMtime=3.0))
            self.assertIsNotNone(tracker.load(db, 'mx-example'))
            for run in spoiled:
                db.execute("UPDATE pipeline_runs SET run=? WHERE stream='mx-example'", (json.dumps(run),))
                db.commit()
                self.assertIsNone(tracker.load(db, 'mx-example'), run)
        self.post('mx-example', 'Skill', {'skill': 'superpowers:brainstorming'})
        run = self.run_state()
        self.assertEqual((run['name'], run['phases']['brainstorm']['status']), ('superpowers', 'running'))

    def test_oversized_numbers_in_files_never_reach_the_list(self):
        plan = self.folder / 'docs/superpowers/plans/2026-11-10-big.md'
        plan.parent.mkdir(parents=True)
        plan.write_text('# Big\n### Task 1: One\n### Task 123456789012345678901234567890: Huge\n')
        workspace = self.folder / '.superpowers/sdd/2026-11-10-big'
        workspace.mkdir(parents=True)
        (workspace / 'plan-path').write_text('docs/superpowers/plans/2026-11-10-big.md\n')
        (workspace / 'progress.md').write_text('Task 1: fix round 99999999999999999999\nTask 99999999999999999999: complete\n')
        row = streams.streams()[0]
        tasks = next(p for p in row['pipeline']['phases'] if p['id'] == 'execute')['tasks']
        self.assertEqual([(t['n'], t['round']) for t in tasks], [(1, None)])

    def test_a_completed_workstream_ignores_events_until_it_is_reopened(self):
        import codex_session
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        threads = {'root': [None, 'idle', 't1', '']}
        cache = {}
        with streams.database() as db:
            previous = codex_session.observe(self.codex(threads), db, 'term-extra', None, cache)
        self.parent['status'] = 'closed'
        self.assertTrue(streams.streams()[0]['pipeline']['frozen'])
        before = self.run_state()
        spec = self.folder / 'docs/superpowers/specs/2026-11-10-new-design.md'
        spec.parent.mkdir(parents=True)
        spec.write_text('# New\n')
        workspace = self.folder / '.superpowers/sdd/2026-11-10-demo'
        workspace.mkdir(parents=True)
        self.post('term-extra', 'Skill', {'skill': 'superpowers:brainstorming'})
        self.post('mx-example', 'Write', {'file_path': str(spec)}, {'type': 'create', 'filePath': str(spec)})
        self.post('term-extra', 'Bash', {'command': 'bash x/sdd-workspace p.md'}, {'stdout': str(workspace) + '\n'})
        self.post('mx-example', 'mcp__plugin_github_github__create_pull_request', {'owner': 'o'},
                  'https://github.com/o/r/pull/13')
        threads['root'][2:] = ['t2', 'https://github.com/o/r/pull/7']
        with streams.database() as db:
            previous = codex_session.observe(self.codex(threads), db, 'term-extra', previous, cache)
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_docs').fetchone()[0], 0)
        self.assertEqual(self.run_state(), before)
        self.parent['status'] = 'open'
        streams.streams()
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_closed').fetchone()[0], 0)
        self.post('term-extra', 'Skill', {'skill': 'superpowers:writing-plans'})
        self.assertEqual(self.run_state()['phases']['plan']['status'], 'running')

    def test_a_skill_without_a_run_takes_no_write_lock_unless_it_starts_one(self):
        with streams.database() as db, patch.object(tracker, 'update', side_effect=AssertionError('write lock taken')):
            statements = []
            db.set_trace_callback(statements.append)
            tracker.tool_event(db, 'mx-example', 'Skill', {'skill': 'superpowers:verification-before-completion'}, '', 1.0)
            tracker.tool_event(db, 'mx-example', 'Skill', {'skill': 'commit-commands:commit'}, '', 1.0)
            db.set_trace_callback(None)
            self.assertFalse(db.in_transaction)
        self.assertEqual([s for s in statements if not s.lstrip().upper().startswith('SELECT')], [])
        self.post('mx-example', 'Skill', {'skill': 'brainstorming'})
        self.assertEqual(self.run_state()['phases']['brainstorm']['status'], 'running')

    def test_a_plain_command_in_a_live_run_takes_no_write_lock(self):
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        with streams.database() as db, patch.object(tracker, 'update', side_effect=AssertionError('write lock taken')):
            statements = []
            db.set_trace_callback(statements.append)
            tracker.tool_event(db, 'mx-example', 'Bash', {'command': 'ls'}, {'stdout': 'a\n'}, 1.0)
            tracker.command_event(db, 'term-extra', 'git status', '', 1.0)
            db.set_trace_callback(None)
            self.assertFalse(db.in_transaction)
        self.assertEqual([s for s in statements if not s.lstrip().upper().startswith('SELECT')], [])

    def test_a_symlink_to_a_file_that_is_not_markdown_is_not_a_document(self):
        target = self.folder / 'script.sh'
        target.write_text('# not markdown\n')
        link = self.folder / 'docs/superpowers/specs/2026-11-10-trap-design.md'
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        self.post('mx-example', 'Write', {'file_path': str(link)}, {'type': 'create', 'filePath': str(link)})
        self.assertIsNone(self.run_state())
        self.assertEqual(streams.streams()[0]['documents'], [])
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_docs').fetchone()[0], 0)

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
        with patch.object(tracker, 'guarded', self.real_guarded), patch.object(tracker.trace, 'error') as error, \
                patch.object(tracker, 'tool_event', side_effect=RuntimeError('boom')):
            self.post('mx-example', 'Bash', {'command': 'gh pr create'}, {'stdout': 'https://github.com/o/r/pull/12'})
        error.assert_called_once()
        with streams.database() as db:
            self.assertEqual(db.execute("SELECT detail FROM activity WHERE id='mx-example'").fetchone()[0], 'Claude is continuing.')
            self.assertEqual(db.execute("SELECT count(*) FROM links WHERE stream='mx-example'").fetchone()[0], 1)

    def test_guarded_rolls_back_and_reports_only_names(self):
        def explode(db):
            db.execute("INSERT INTO pipeline_runs VALUES ('mx-example','{}')")
            raise RuntimeError('boom ' + str(self.folder))
        with streams.database() as db, patch.object(tracker.trace, 'error') as error:
            self.assertEqual(self.real_guarded(db, 'fallback', explode, db), 'fallback')
        error.assert_called_once_with('pipeline failed type=RuntimeError in=explode')
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_runs').fetchone()[0], 0)

    def test_commands_without_a_run_take_no_write_lock(self):
        with patch.object(tracker, 'update', side_effect=AssertionError('write lock taken')):
            self.post('mx-example', 'Bash', {'command': 'gh pr create'}, {'stdout': 'https://github.com/o/r/pull/12'})
            self.post('mx-example', 'Bash', {'command': 'bash x/task-brief docs/superpowers/plans/p.md 1'}, {'stdout': 'wrote'})
            with streams.database() as db:
                tracker.command_event(db, 'mx-example', 'gh pr merge 12', '', 1.0)
                self.assertFalse(db.in_transaction)
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_runs').fetchone()[0], 0)

    def test_malformed_tool_input_is_ignored(self):
        with streams.database() as db:
            for name, args in [('Skill', 'superpowers:brainstorming'), ('Skill', None), ('Skill', {'skill': ['superpowers:brainstorming']}),
                               ('Write', ['/x/docs/superpowers/plans/p.md']), ('Write', {'file_path': 7}),
                               ('Edit', {'file_path': {'path': '/x/docs/superpowers/specs/s.md'}}),
                               ('Bash', {'command': ['bash', 'sdd-workspace']}), ('Bash', 'ls')]:
                tracker.tool_event(db, 'mx-example', name, args, '', 1.0)
                self.assertFalse(db.in_transaction)
        self.assertIsNone(self.run_state())

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

    def test_list_rows_carry_pipeline_and_documents(self):
        row = streams.streams()[0]
        self.assertEqual((row['pipeline'], row['documents']), (None, []))
        self.post('mx-example', 'Write', {'file_path': str(self.plan_file())})
        with streams.database() as db:
            db.execute("UPDATE activity SET state='Review' WHERE id IN ('mx-example','term-extra')")
        row = streams.streams()[0]
        self.assertEqual((row['pipeline']['name'], row['pipeline']['needsYou']), ('demo', True))
        self.assertEqual(row['documents'][0]['title'], 'Demo Implementation Plan')
        self.parent['status'] = 'closed'
        self.assertTrue(streams.streams()[0]['pipeline']['frozen'])

    def test_a_document_written_through_a_symlink_has_one_row_and_one_run(self):
        plan = self.plan_file()
        linked = self.root / 'linked worktree'
        linked.symlink_to(self.folder)
        self.post('mx-example', 'Write', {'file_path': str(linked / plan.relative_to(self.folder))})
        with streams.database() as db:
            # Executing, so a second "new" sighting of the plan would start another run.
            launched = tracker.update(db, 'mx-example', lambda run: pipeline.advance_to(run, 'execute', 1.0))['launchedAt']
        row = streams.streams()[0]
        self.assertEqual(([d['path'] for d in row['documents']], row['pipeline']['startedAt']), ([str(plan)], launched))
        with streams.database() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pipeline_docs').fetchone()[0], 1)

    def test_a_failing_snapshot_leaves_the_list_working(self):
        with patch.object(tracker, 'guarded', self.real_guarded), patch.object(tracker.trace, 'error') as error, \
                patch.object(tracker, 'snapshot', side_effect=RuntimeError('boom')):
            row = streams.streams()[0]
        error.assert_called_once()
        self.assertEqual((row['id'], row['pipeline'], row['documents']), ('mx-example', None, []))

    def at_ci(self):
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        self.post('mx-example', 'Bash', {'command': 'gh pr create'}, {'stdout': 'https://github.com/o/r/pull/12'})

    def test_poll_applies_the_pull_request_view(self):
        self.at_ci()
        seen = []
        def view(url):
            seen.append(url)
            # gh runs with the write lock free, so hooks are not kept waiting.
            with contextlib.closing(streams.sqlite3.connect(streams.ROOT / 'activity.sqlite', timeout=0)) as probe:
                probe.execute('BEGIN IMMEDIATE')
                probe.rollback()
            return {'state': 'OPEN', 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
        with patch.object(tracker, 'pr_view', side_effect=view):
            streams.main('pipeline-poll', 'mx-example', 'mx-unknown')
        run = self.run_state()
        self.assertEqual(seen, ['https://github.com/o/r/pull/12'])
        self.assertEqual((run['phases']['ci']['status'], run['phases']['merge']['status'], run['ciDetail']), ('done', 'running', '1 passed'))

    def test_poll_leaves_the_run_alone_when_gh_fails_or_the_run_is_finished(self):
        self.at_ci()
        before = self.run_state()
        with streams.database() as db:
            tracker.poll_pull_requests(db, ['mx-example'], 5.0, view=lambda url: None)
        self.assertEqual(self.run_state(), before)
        with streams.database() as db:
            tracker.update(db, 'mx-example', lambda run: pipeline.complete_phase(run, 'merge', 6.0))
            tracker.poll_pull_requests(db, ['mx-example'], 7.0, view=lambda url: self.fail('polled a finished run'))

    def test_one_failing_workstream_does_not_stop_the_poll(self):
        self.at_ci()
        with streams.database() as db:
            tracker.update(db, 'mx-other', lambda run: pipeline.pull_request_created(
                pipeline.advance_to(pipeline.new_run(1.0), 'pr', 1.0), 'https://github.com/o/r/pull/5', 1.0))
        def view(url):
            if url.endswith('/12'):
                raise RuntimeError('boom')
            return {'state': 'MERGED'}
        with patch.object(tracker, 'guarded', self.real_guarded), patch.object(tracker.trace, 'error') as error:
            with streams.database() as db:
                tracker.poll_pull_requests(db, ['mx-example', 'mx-other'], 5.0, view=view)
                self.assertFalse(db.in_transaction)
                self.assertEqual(tracker.load(db, 'mx-other')['phases']['merge']['status'], 'done')
        error.assert_called_once_with('pipeline failed type=RuntimeError in=poll_pull_request')
        self.assertEqual(self.run_state()['phases']['merge']['status'], 'todo')

    def test_pr_view_survives_a_missing_gh(self):
        with patch.object(tracker.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertIsNone(tracker.pr_view('https://github.com/o/r/pull/12'))

    def codex(self, threads):
        """A fake Codex app-server. threads maps an id to [parent, status type, turn id or None, command output]."""
        folder = str(self.folder)
        def turn(identity):
            _, _, turn_id, output = threads[identity]
            if turn_id is None:
                return {'data': []}
            return {'data': [{'id': turn_id, 'status': 'completed', 'items': [
                {'type': 'commandExecution', 'exitCode': 0, 'command': 'gh pr create', 'aggregatedOutput': output}]}]}
        class Client:
            def call(inner, method, params):
                if method == 'thread/loaded/list':
                    return {'data': list(threads)}
                if method == 'thread/read':
                    parent, state = threads[params['threadId']][:2]
                    return {'thread': {'status': {'type': state}, 'parentThreadId': parent, 'cwd': folder}}
                return turn(params['threadId'])
        return Client()

    def test_codex_root_commands_reach_the_pipeline_and_child_commands_do_not(self):
        import codex_session
        threads = {'root': [None, 'active', None, ''], 'child': ['root', 'idle', None, '']}
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        cache = {}
        with streams.database() as db:
            previous = codex_session.observe(self.codex(threads), db, 'term-extra', None, cache)
            threads['root'][1:] = ['idle', 't1', 'https://github.com/o/r/pull/7']
            threads['child'][1:] = ['idle', 'c1', 'https://github.com/o/r/pull/8']
            codex_session.observe(self.codex(threads), db, 'term-extra', previous, cache)
        self.assertEqual(self.run_state()['pr'], 7)

    def test_codex_replays_nothing_after_a_restart(self):
        import codex_session
        threads = {'root': [None, 'idle', 't1', 'https://github.com/o/r/pull/7']}
        self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
        cache = {}
        with streams.database() as db:
            previous = codex_session.observe(self.codex(threads), db, 'term-extra', None, cache)
            self.assertNotIn('pr', self.run_state())
            previous = codex_session.observe(self.codex(threads), db, 'term-extra', previous, cache)
            self.assertNotIn('pr', self.run_state())
            threads['root'][2] = 't2'
            codex_session.observe(self.codex(threads), db, 'term-extra', previous, cache)
        self.assertEqual(self.run_state()['pr'], 7)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'source repo'
        self.repo.mkdir()
        home = self.root / 'app home'
        home.mkdir()
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.write('docs/superpowers/specs/2026-01-01-old-design.md', '# Old\n')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-m', 'Initial')
        # The list's guard would hide a tracker that raises; these tests must see it.
        passthrough = lambda db, default, function, *args, **kwargs: function(*args, **kwargs)
        for p in [patch.object(streams, 'ROOT', home), patch.object(tracker, 'guarded', side_effect=passthrough)]:
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def snap(self, root=None, now=2_000_000_000.0, reads_only=False, **options):
        """reads_only also asserts the snapshot ran no Git, updated no run and wrote nothing."""
        settings = dict(created=0.0, own_worktree=True, closed=False, waiting=False, active_agents=0)
        settings.update(options)
        statements = []
        with contextlib.ExitStack() as stack:
            if reads_only:
                stack.enter_context(patch.object(tracker, 'update', side_effect=AssertionError('run updated')))
                stack.enter_context(patch.object(tracker.subprocess, 'run', side_effect=AssertionError('git called')))
            db = stack.enter_context(streams.database())
            db.set_trace_callback(statements.append)
            result = tracker.snapshot(db, 'mx-s', str(root or self.repo), now=now, **settings)
            db.set_trace_callback(None)
            if reads_only:
                self.assertFalse(db.in_transaction)
        if reads_only:
            self.assertTrue(statements)
            self.assertEqual([s for s in statements if not s.lstrip().upper().startswith('SELECT')], [])
        return result

    def write(self, relative, text, root=None):
        path = (root or self.repo) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_plain_workstream_has_nothing(self):
        self.assertEqual(self.snap(), (None, []))

    def test_untracked_documents_belong_and_merged_ones_do_not(self):
        spec = self.write('docs/superpowers/specs/2026-11-10-new-design.md', '# New thing\n')
        self.write('docs/superpowers/specs/2026-11-09-theirs-design.md', '# Theirs\n')
        self.git('add', 'docs/superpowers/specs/2026-11-09-theirs-design.md')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-m', 'From main')
        view, documents = self.snap()
        self.assertEqual(documents, [{'kind': 'spec', 'title': 'New thing', 'path': str(spec)}])
        self.assertEqual((view['name'], view['phases'][0]['mark']), ('new', 'running'))
        self.git('add', '.'); self.git('-c', 'commit.gpgsign=false', 'commit', '-m', 'Mine')
        self.assertEqual(self.snap()[1][0]['path'], str(spec))      # stays after it is committed
        spec.unlink()
        self.assertEqual(self.snap()[1], [])

    def test_gitignored_documents_still_belong(self):
        self.write('.gitignore', 'docs/superpowers/\n')
        self.write('docs/superpowers/plans/2026-11-10-a.md', '# A Plan\n')
        self.assertEqual([d['title'] for d in self.snap()[1]], ['A Plan'])

    def test_git_failure_adopts_nothing_and_is_retried_after_a_minute(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        with patch.object(tracker.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 4)):
            self.assertEqual(self.snap(now=1000.0), (None, []))
        self.assertEqual(self.snap(now=1059.0, reads_only=True), (None, []))
        self.assertEqual(len(self.snap(now=1061.0)[1]), 1)

    def test_non_ascii_names_are_adopted(self):
        spec = self.write('docs/superpowers/specs/2026-11-10-café-design.md', '# Café\n')
        self.assertEqual(self.snap()[1], [{'kind': 'spec', 'title': 'Café', 'path': str(spec)}])

    def test_a_run_started_while_the_ledger_is_read_is_left_alone(self):
        self.write('docs/superpowers/plans/2026-11-10-a.md', '# A Plan\n### Task 1: One\n')
        self.snap(now=100.0)
        self.write('.superpowers/sdd/2026-11-10-a/plan-path', 'docs/superpowers/plans/2026-11-10-a.md\n')
        self.write('.superpowers/sdd/2026-11-10-a/progress.md', '# SDD ledger\nTask 1: complete\n')
        real_read = tracker._read
        def read_while_a_hook_starts_a_run(path):
            with streams.database() as other:
                tracker.update(other, 'mx-s', lambda run: pipeline.advance_to(pipeline.new_run(150.0), 'brainstorm', 150.0))
            return real_read(path)
        with patch.object(tracker, '_read', side_effect=read_while_a_hook_starts_a_run):
            self.snap(now=200.0)
        with streams.database() as db:
            run = tracker.load(db, 'mx-s')
        self.assertEqual((run['launchedAt'], run['phases']['execute']['status'], run['tasks']), (150.0, 'todo', []))
        self.assertNotIn('ledgerMtime', run)

    def test_malformed_run_paths_still_list_the_pipeline_and_documents(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.snap()
        def spoil(run):
            run.update(workspace=7, planPath=['not', 'a', 'path'])
            return run
        with streams.database() as db:
            tracker.update(db, 'mx-s', spoil)
        view, documents = self.snap()
        self.assertEqual((view['name'], [d['title'] for d in documents]), ('a', ['A']))

    def test_plan_then_ledger_then_a_second_run(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.snap(now=100.0)
        plan = self.write('docs/superpowers/plans/2026-11-10-a.md', '# A Plan\n### Task 1: One\n### Task 2: Two\n')
        view, documents = self.snap(now=200.0)
        self.assertEqual([d['kind'] for d in documents], ['plan', 'spec'])
        self.assertEqual([p['mark'] for p in view['phases'][:2]], ['done', 'running'])
        self.write('.superpowers/sdd/2026-11-10-a/plan-path', 'docs/superpowers/plans/2026-11-10-a.md\n')
        self.write('.superpowers/sdd/2026-11-10-a/progress.md', '# SDD ledger\nTask 1: complete\n')
        view, _ = self.snap(now=300.0)
        tasks = next(p for p in view['phases'] if p['id'] == 'execute')['tasks']
        self.assertEqual([t['mark'] for t in tasks], ['done', 'running'])
        with streams.database() as db:
            tracker.update(db, 'mx-s', lambda run: pipeline.complete_phase(run, 'merge', 400.0))
        plan.write_text(plan.read_text() + '\n### Task 3: Three\n')            # editing an old plan starts nothing
        self.assertEqual(self.snap(now=500.0)[0]['finished'], 'merged')
        self.write('docs/superpowers/plans/2026-11-12-b.md', '# B Plan\n### Task 1: One\n')
        view, documents = self.snap(now=600.0)
        self.assertEqual((view['name'], view['startedAt'], view['finished'], len(documents)), ('b', 600.0, None, 3))

    def test_shared_folder_takes_only_documents_newer_than_the_workstream(self):
        plain = self.root / 'plain folder'
        old = self.write('docs/superpowers/specs/old-design.md', '# Old\n', root=plain)
        os.utime(old, (1000.0, 1000.0))
        new = self.write('docs/superpowers/specs/new-design.md', '# New\n', root=plain)
        os.utime(new, (3000.0, 3000.0))
        _, documents = self.snap(root=plain, created=2000.0, own_worktree=False)
        self.assertEqual([d['title'] for d in documents], ['New'])

    def test_completed_workstream_is_frozen_and_not_scanned(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.snap(waiting=True)
        self.write('docs/superpowers/plans/2026-11-10-a.md', '# A Plan\n### Task 1: One\n')
        view, documents = self.snap(closed=True, waiting=True)
        self.assertEqual((view['frozen'], view['needsYou'], len(documents)), (True, False, 1))

    def test_unreadable_entries_do_not_break_the_scan(self):
        (self.repo / 'docs/superpowers/plans/dir.md').mkdir(parents=True)
        (self.repo / 'docs/superpowers/plans/bad.md').write_bytes(b'\xff\xfe')
        self.assertEqual(self.snap(), (None, []))

    def test_unchanged_files_cost_no_subprocess(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.snap()
        with patch.object(tracker.subprocess, 'run', side_effect=AssertionError('git called')):
            self.assertEqual(len(self.snap()[1]), 1)

    def test_an_unchanged_refresh_writes_nothing(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.write('docs/superpowers/plans/2026-11-10-a.md', '# A Plan\n### Task 1: One\n### Task 2: Two\n')
        self.write('.superpowers/sdd/2026-11-10-a/plan-path', 'docs/superpowers/plans/2026-11-10-a.md\n')
        self.write('.superpowers/sdd/2026-11-10-a/progress.md', '# SDD ledger\nTask 1: complete\n')
        first = self.snap()
        self.assertEqual(len(next(p for p in first[0]['phases'] if p['id'] == 'execute')['tasks']), 2)
        self.assertEqual(self.snap(reads_only=True), first)

    def test_an_unchanged_refresh_of_a_completed_workstream_writes_nothing(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        self.snap()
        first = self.snap(closed=True)
        self.assertTrue(first[0]['frozen'])
        self.assertEqual(self.snap(closed=True, reads_only=True), first)
        self.snap()
        self.assertEqual(self.snap(reads_only=True)[1], first[1])

    def test_a_run_that_cannot_be_rendered_keeps_its_documents(self):
        self.write('docs/superpowers/specs/2026-11-10-a-design.md', '# A\n')
        with patch.object(tracker.pipeline, 'render', side_effect=KeyError('tasks')), \
                patch.object(tracker.trace, 'error') as error:
            view, documents = self.snap()
        error.assert_called_once_with('pipeline failed type=KeyError in=render')
        self.assertEqual((view, [d['title'] for d in documents]), (None, ['A']))

    def test_timestamp(self):
        self.assertEqual(tracker.timestamp('2026-11-10T05:41:25Z'), 1794289285.0)
        self.assertEqual(tracker.timestamp('2026-11-10T06:41:25+01:00'), 1794289285.0)
        self.assertEqual(tracker.timestamp('2026-11-10T05:41:25.5Z'), 1794289285.5)
        self.assertEqual(tracker.timestamp('not a time'), 0.0)
        self.assertEqual(tracker.timestamp(''), 0.0)


class ErrorLogTests(unittest.TestCase):
    def test_error_is_written_without_a_begun_context(self):
        with tempfile.TemporaryDirectory() as home, patch.object(Path, 'home', return_value=Path(home)), \
                patch.object(trace, '_context', None):
            trace.error('pipeline failed type=RuntimeError in=tool_event')
            logs = list((Path(home) / 'Library/Logs/Muxteroid').glob('workstream-*.log'))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text()
        self.assertRegex(text, r'^\S+Z bridge\[\d+\] - pipeline failed type=RuntimeError in=tool_event\n$')


if __name__ == '__main__':
    unittest.main()
