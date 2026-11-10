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


if __name__ == '__main__':
    unittest.main()
