import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import pipeline
from pipeline import MIN

T0 = 1_000_000.0
PLAN_PATH = '/repo/docs/superpowers/plans/2026-10-09-pipeline-pane.md'
PLAN = '\n'.join([
    '# Pipeline pane plan', '',
    '### Task 1: Add the state contract', 'Steps...',
    '```markdown', '### Task 9: [Component Name]', '```',
    '### Task 2: Parse **plan** headings',
    '## Task 3 — Ledger watcher',
])


def planned():
    run = pipeline.advance_to(pipeline.new_run(T0), 'brainstorm', T0)
    run = pipeline.advance_to(run, 'plan', T0 + 5 * MIN)
    return pipeline.with_plan(run, PLAN_PATH, pipeline.parse_plan_tasks(PLAN))


class RunModelTests(unittest.TestCase):
    def test_reads_task_headings_and_skips_fenced_ones(self):
        self.assertEqual(pipeline.parse_plan_tasks(PLAN), [
            {'n': 1, 'title': 'Add the state contract'},
            {'n': 2, 'title': 'Parse plan headings'},
            {'n': 3, 'title': 'Ledger watcher'}])
        self.assertEqual(pipeline.parse_plan_tasks('## Task 4\n## Task 4: again'), [{'n': 4, 'title': 'Task 4'}])

    def test_names_the_run_after_the_plan_file(self):
        self.assertEqual(planned()['name'], 'pipeline-pane')
        self.assertEqual(pipeline.slug_name('/r/docs/superpowers/specs/2026-11-10-foo-design.md'), 'foo')

    def test_later_phase_closes_earlier_ones_and_never_moves_back(self):
        run = planned()
        self.assertEqual(run['phases']['brainstorm']['status'], 'done')
        self.assertEqual(run['phases']['plan']['status'], 'running')
        pipeline.advance_to(run, 'execute', T0 + 10 * MIN)
        pipeline.advance_to(run, 'plan', T0 + 11 * MIN)
        self.assertEqual(run['phases']['plan']['status'], 'done')
        self.assertEqual(run['phases']['execute']['status'], 'running')

    def test_phase_that_never_ran_is_skipped(self):
        run = pipeline.advance_to(pipeline.new_run(T0), 'execute', T0)
        self.assertEqual(run['phases']['brainstorm']['status'], 'skipped')
        self.assertEqual(run['phases']['plan']['status'], 'skipped')

    def test_brief_starts_implementing_and_review_package_hands_over(self):
        run = pipeline.start_task(planned(), 1, T0 + 10 * MIN)
        self.assertEqual(run['tasks'][0]['implement'], {'status': 'running', 'startedAt': T0 + 10 * MIN})
        self.assertEqual(run['currentTask'], 1)
        pipeline.review_task(run, T0 + 17 * MIN)
        self.assertEqual(run['tasks'][0]['implement']['status'], 'done')
        self.assertEqual(run['tasks'][0]['review'], {'status': 'running', 'startedAt': T0 + 17 * MIN})

    def test_ledger_completes_tasks_records_rounds_and_finishes_the_phase(self):
        run = pipeline.start_task(planned(), 1, T0 + 10 * MIN)
        pipeline.apply_ledger(run, '# SDD ledger — plan: x\nTask 1: complete (commits a1b2c3d..d4e5f6a, review clean)\n'
                                   'Task 2: fix round 2/5 (1 addressed, 0 open)\n', T0 + 22 * MIN)
        self.assertEqual(run['tasks'][0]['review']['status'], 'done')
        self.assertEqual(run['tasks'][1]['round'], 2)
        self.assertEqual(run['phases']['execute']['status'], 'running')
        pipeline.apply_ledger(run, 'Task 1: complete\nTask 2: complete\nTask 3: complete\n', T0 + 40 * MIN)
        self.assertEqual(run['phases']['execute']['status'], 'done')

    def test_task_completed_unseen_keeps_no_start_time(self):
        run = pipeline.apply_ledger(pipeline.advance_to(planned(), 'execute', T0), 'Task 2: complete', T0 + 9 * MIN)
        self.assertEqual(run['tasks'][1]['implement'], {'status': 'done', 'endedAt': T0 + 9 * MIN})


def at_ci():
    return pipeline.advance_to(planned(), 'ci', T0 + 60 * MIN)


def check(status, conclusion):
    return {'__typename': 'CheckRun', 'status': status, 'conclusion': conclusion}


class EventRuleTests(unittest.TestCase):
    def test_starter_skills_open_and_replace_runs(self):
        run = pipeline.skill_event(None, 'superpowers:brainstorming', T0)
        self.assertEqual(run['phases']['brainstorm']['status'], 'running')
        self.assertIs(pipeline.skill_event(run, 'writing-plans', T0 + MIN), run)
        self.assertEqual(run['phases']['plan']['status'], 'running')
        pipeline.skill_event(run, 'superpowers:executing-plans', T0 + 2 * MIN)
        fresh = pipeline.skill_event(run, 'superpowers:brainstorming', T0 + 3 * MIN)
        self.assertIsNot(fresh, run)
        self.assertEqual(fresh['launchedAt'], T0 + 3 * MIN)
        self.assertIsNone(pipeline.skill_event(None, 'superpowers:requesting-code-review', T0))

    def test_review_and_verify_wait_for_tasks_but_finishing_does_not(self):
        run = pipeline.advance_to(planned(), 'execute', T0)
        pipeline.skill_event(run, 'superpowers:requesting-code-review', T0 + MIN)
        pipeline.skill_event(run, 'superpowers:verification-before-completion', T0 + MIN)
        self.assertEqual(run['phases']['final-review']['status'], 'todo')
        self.assertEqual(run['phases']['verify']['status'], 'todo')
        pipeline.skill_event(run, 'finishing-a-development-branch', T0 + MIN)   # no prefix: ignored
        self.assertEqual(run['phases']['pr']['status'], 'todo')
        pipeline.skill_event(run, 'superpowers:finishing-a-development-branch', T0 + 2 * MIN)
        self.assertEqual(run['phases']['pr']['status'], 'running')
        self.assertEqual(run['phases']['execute']['status'], 'done')

    def test_new_documents_start_runs_and_old_ones_do_not(self):
        spec = '/r/docs/superpowers/specs/2026-11-10-foo-design.md'
        run = pipeline.document_event(None, 'spec', spec, [], True, T0)
        self.assertEqual((run['name'], run['phases']['brainstorm']['status']), ('foo', 'running'))
        pipeline.document_event(run, 'plan', PLAN_PATH, pipeline.parse_plan_tasks(PLAN), True, T0 + MIN)
        self.assertEqual((run['name'], run['phases']['plan']['status'], len(run['tasks'])), ('pipeline-pane', 'running', 3))
        pipeline.document_event(run, 'plan', PLAN_PATH, [{'n': 1, 'title': 'Only'}], False, T0 + 2 * MIN)
        self.assertEqual(len(run['tasks']), 1)                       # its own plan is re-read
        pipeline.advance_to(run, 'execute', T0 + 3 * MIN)
        other = '/r/docs/superpowers/plans/2026-11-11-bar.md'
        self.assertIs(pipeline.document_event(run, 'plan', other, [], False, T0 + 4 * MIN), run)   # seen before: ignored
        self.assertEqual(run['planPath'], PLAN_PATH)
        fresh = pipeline.document_event(run, 'plan', other, [], True, T0 + 5 * MIN)                 # new, mid-execution
        self.assertIsNot(fresh, run)
        self.assertEqual(fresh['name'], 'bar')
        merged = pipeline.complete_phase(planned(), 'merge', T0)
        self.assertIs(pipeline.document_event(merged, 'plan', PLAN_PATH, [], False, T0 + MIN), merged)
        self.assertEqual(merged['phases']['merge']['status'], 'done')

    def test_workspace_and_brief_paths_may_contain_spaces(self):
        path = '/Users/a/Library/Application Support/Muxteroid/worktrees/abc/.superpowers/sdd/2026-10-09-pipeline-pane'
        self.assertEqual(pipeline.workspace_path('bash scripts/sdd-workspace docs/p.md', 'note\n' + path + '\n'), path)
        self.assertIsNone(pipeline.workspace_path('ls', path))
        self.assertIsNone(pipeline.workspace_path('sdd-workspace x', 'no such plan file: x'))
        run = pipeline.attach_workspace(None, path, PLAN_PATH, pipeline.parse_plan_tasks(PLAN), T0)
        self.assertEqual((run['workspace'], run['phases']['execute']['status'], len(run['tasks'])), (path, 'running', 3))
        pipeline.command_event(run, 'task-brief "/Users/a/Application Support/p.md" 2', '', T0 + MIN)
        self.assertEqual(run['tasks'][1]['implement']['status'], 'running')
        pipeline.command_event(run, 'x/review-package docs/p.md a b', '', T0 + 2 * MIN)
        self.assertEqual(run['tasks'][1]['review']['status'], 'running')
        pipeline.command_event(run, 'bash "/p q/scripts/task-brief" docs/p.md 3', '', T0 + 3 * MIN)
        self.assertEqual(run['currentTask'], 3)

    def test_pull_request_commands(self):
        run = pipeline.advance_to(planned(), 'pr', T0)
        pipeline.command_event(run, 'gh pr create --fill', 'https://github.com/o/r/pull/12\n', T0 + MIN)
        self.assertEqual((run['pr'], run['prUrl']), (12, 'https://github.com/o/r/pull/12'))
        self.assertEqual((run['phases']['pr']['status'], run['phases']['ci']['status']), ('done', 'running'))
        pipeline.command_event(run, 'gh pr merge 99 --squash', '', T0 + 2 * MIN)
        pipeline.command_event(run, 'gh pr merge --auto', '', T0 + 2 * MIN)
        self.assertFalse(pipeline.is_finished(run))
        pipeline.command_event(run, 'gh pr merge 12 --squash', '', T0 + 3 * MIN)
        self.assertTrue(pipeline.is_finished(run))
        self.assertIsNone(pipeline.command_event(None, 'gh pr create', 'https://github.com/o/r/pull/1', T0))

    def test_pending_checks_keep_ci_running_with_a_count(self):
        run = pipeline.apply_pr_view(
            at_ci(),
            {'state': 'OPEN', 'statusCheckRollup': [check('COMPLETED', 'SUCCESS'), check('IN_PROGRESS', '')]},
            T0 + 61 * MIN)
        self.assertEqual(run['phases']['ci']['status'], 'running')
        self.assertEqual(run['ciDetail'], '1 of 2 checks')

    def test_failing_check_fails_ci_and_a_rerun_brings_it_back_to_running(self):
        failed = pipeline.apply_pr_view(
            at_ci(),
            {'state': 'OPEN', 'statusCheckRollup': [check('COMPLETED', 'FAILURE'), check('COMPLETED', 'SUCCESS')]},
            T0 + 61 * MIN)
        self.assertEqual(failed['phases']['ci']['status'], 'failed')
        self.assertEqual(failed['ciDetail'], '1 of 2 failing')
        rerun = pipeline.apply_pr_view(
            failed,
            {'state': 'OPEN', 'statusCheckRollup': [check('QUEUED', ''), check('COMPLETED', 'SUCCESS')]},
            T0 + 70 * MIN)
        self.assertEqual(rerun['phases']['ci'], {'status': 'running', 'startedAt': T0 + 70 * MIN})

    def test_green_checks_wait_on_the_merge_and_a_merged_pr_finishes_the_run(self):
        green = pipeline.apply_pr_view(
            at_ci(),
            {'state': 'OPEN', 'statusCheckRollup': [check('COMPLETED', 'SUCCESS'), {'state': 'SUCCESS'}]},
            T0 + 75 * MIN)
        self.assertEqual(green['phases']['ci']['status'], 'done')
        self.assertEqual(green['phases']['merge']['status'], 'running')
        merged = pipeline.apply_pr_view(green, {'state': 'MERGED', 'statusCheckRollup': []}, T0 + 80 * MIN)
        self.assertTrue(pipeline.is_finished(merged))

    def test_closed_pull_request_fails_the_merge(self):
        run = pipeline.apply_pr_view(at_ci(), {'state': 'CLOSED'}, T0 + 61 * MIN)
        self.assertEqual(run['phases']['merge']['status'], 'failed')
        self.assertTrue(pipeline.is_finished(run))


class ScheduleTests(unittest.TestCase):
    def one_task_done_in_twenty_minutes(self):
        run = pipeline.start_task(planned(), 1, T0 + 10 * MIN)
        pipeline.apply_ledger(run, 'Task 1: complete', T0 + 30 * MIN)
        return pipeline.start_task(run, 2, T0 + 30 * MIN)

    def test_projects_remaining_tasks_from_the_measured_average(self):
        plan = pipeline.schedule(self.one_task_done_in_twenty_minutes(), T0 + 35 * MIN)
        self.assertEqual(plan['task'], 20 * MIN)
        self.assertEqual([t['end'] for t in plan['tasks']], [T0 + 30 * MIN, T0 + 50 * MIN, T0 + 70 * MIN])
        self.assertEqual(plan['finish'], T0 + 104 * MIN)

    def test_marks_a_task_slow_past_one_and_a_half_times_the_average(self):
        plan = pipeline.schedule(self.one_task_done_in_twenty_minutes(), T0 + 65 * MIN)
        self.assertEqual((plan['tasks'][1]['view'], plan['tasks'][1]['implement']), ('slow', 'slow'))

    def test_no_finish_while_brainstorming(self):
        run = pipeline.advance_to(pipeline.new_run(T0), 'brainstorm', T0)
        self.assertIsNone(pipeline.schedule(run, T0 + MIN)['finish'])

    def test_run_without_task_signals_guesses_nothing(self):
        run = pipeline.skill_event(planned(), 'superpowers:executing-plans', T0 + 10 * MIN)
        plan = pipeline.schedule(run, T0 + 60 * MIN)
        self.assertEqual([t['view'] for t in plan['tasks']], ['todo', 'todo', 'todo'])
        self.assertEqual([t['end'] for t in plan['tasks']], [None, None, None])
        self.assertEqual(plan['phases']['execute']['view'], 'running')
        self.assertIsNone(plan['finish'])
        pipeline.skill_event(run, 'superpowers:finishing-a-development-branch', T0 + 61 * MIN)
        plan = pipeline.schedule(run, T0 + 62 * MIN)
        self.assertEqual([t['view'] for t in plan['tasks']], ['done', 'done', 'done'])
        self.assertEqual([t['end'] for t in plan['tasks']], [None, None, None])

    def test_render_marks_the_waiting_phase_and_hides_skipped_ones(self):
        view = pipeline.render(planned(), T0 + 6 * MIN, waiting=True)
        self.assertEqual([p['id'] for p in view['phases']], list(pipeline.PHASES))
        plan = view['phases'][1]
        self.assertEqual((plan['label'], plan['mark'], plan['detail']), ('plan', 'needs', '3 tasks'))
        self.assertTrue(view['needsYou'])
        self.assertEqual(view['phases'][0], {'id': 'brainstorm', 'label': 'brainstorm', 'mark': 'done',
            'time': T0 + 5 * MIN, 'estimate': False, 'detail': '', 'url': None, 'tasks': None})
        self.assertEqual(len(view['phases'][2]['tasks']), 3)
        busy = pipeline.render(planned(), T0 + 6 * MIN, waiting=True, active_agents=1)
        self.assertEqual((busy['needsYou'], busy['phases'][1]['mark'], busy['activeAgents']), (False, 'running', 1))
        skipped = pipeline.render(pipeline.advance_to(pipeline.new_run(T0), 'execute', T0), T0 + MIN)
        self.assertEqual(skipped['phases'][0]['id'], 'execute')

    def test_render_pull_request_finish_and_frozen(self):
        run = self.one_task_done_in_twenty_minutes()
        pipeline.pull_request_created(run, 'https://github.com/o/r/pull/12', T0 + 40 * MIN)
        view = pipeline.render(run, T0 + 41 * MIN)
        pr = next(p for p in view['phases'] if p['id'] == 'pr')
        self.assertEqual((pr['detail'], pr['url'], view['polling']), ('#12', 'https://github.com/o/r/pull/12', True))
        tasks = next(p for p in view['phases'] if p['id'] == 'execute')
        self.assertEqual(tasks['detail'], '1 of 3')
        frozen = pipeline.render(run, T0 + 41 * MIN, waiting=True, frozen=True)
        ci = next(p for p in frozen['phases'] if p['id'] == 'ci')
        self.assertEqual((ci['mark'], ci['time'], ci['estimate']), ('todo', None, False))
        self.assertEqual((frozen['frozen'], frozen['polling'], frozen['needsYou'], frozen['finishAt']), (True, False, False, None))
        pipeline.complete_phase(run, 'merge', T0 + 50 * MIN)
        done = pipeline.render(run, T0 + 55 * MIN, waiting=True)
        self.assertEqual((done['finished'], done['finishAt'], done['polling'], done['needsYou']), ('merged', T0 + 50 * MIN, False, False))


if __name__ == '__main__':
    unittest.main()
