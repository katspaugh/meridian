# Superpowers Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a Superpowers run as a timeline in the workstream inspector, with links that open the workstream's specs and plans in a Muxteroid window.

**Architecture:** The Python bridge owns the run. `pipeline.py` is a pure model ported from the reference mod; `pipeline_tracker.py` stores one run per workstream in `activity.sqlite`, feeds it from the existing Claude `PostToolUse` hook, from Codex turn commands and from files, and adds a `pipeline` object and a `documents` list to every stream in `list`. SwiftUI only decodes, formats and lays out.

**Tech Stack:** Python 3.9 standard library (`/usr/bin/python3`), `unittest`; Swift 6.2 toolchain in Swift 5 language mode, SwiftUI, MarkdownUI 2.4.1; `git` and `gh` CLIs.

**Spec:** `docs/superpowers/specs/2026-11-10-superpowers-pipeline-design.md`

**Reference to port:** `~/.claude/mods/pipeline/hooks/model.ts` (and `register.tsx` for the event wiring). It is outside the repository and read-only. Wherever a task says "port", translate that function line for line: milliseconds become seconds, `undefined` becomes an absent key or `None`, and the differences this plan lists win over the mod.

## Global Constraints

- The bridge runs under `/usr/bin/python3` (3.9), standard library only. No `match`, no `X | Y` annotations.
- `list` runs every three seconds holding `bridge.lock`: no network, and no subprocess unless a file changed.
- Hooks run concurrently with `list` and with each other and do not take `bridge.lock`. Every read-modify-write of a run goes through `pipeline_tracker.update`.
- An exception that escapes `list` blanks the whole app, and one that escapes `hook` breaks the agent's turn. Nothing in the pipeline code may raise out of `streams.streams()` or `streams.hook()` because of a missing, unreadable or malformed file, row or payload.
- Events with an `agent_id` (Claude) or from a child thread (Codex) never touch the pipeline.
- No document bodies in `list` output.
- A workstream with no Superpowers activity gets `"pipeline": null` and `"documents": []` and looks exactly as it does today.
- All times cross the bridge as epoch seconds (floats). The app formats them.
- Every new `.py` file is added to the `cp` line in `build.sh` and the `exclude` list in `Package.swift`.
- Run the Python suite with `/usr/bin/python3 -m unittest discover -s Tests`. It must stay green after every task (35 tests pass today, 3 skipped).
- Never run the built app or the bridge against the live data directory. Manual checks set `MUXTEROID_HOME` to a scratch directory.

## Review Focus

1. **Paths with spaces.** Every Muxteroid worktree lives under `~/Library/Application Support/`, so the `sdd-workspace` output and a quoted plan path contain a space. The mod's `\S*` patterns would cut the path. Expected: the full path is recorded. Test in Task 2.
2. **A file that vanishes or cannot be read** between the event and the read (a plan deleted, a directory named `x.md`, invalid UTF-8). Expected: the event or the scan is skipped quietly; `list` still answers. Tests in Tasks 4 and 5.
3. **A stored run that cannot be decoded** (hand-edited, or written by a later version). Expected: treated as no run, and the next event replaces it. Test in Task 4.
4. **A hook payload whose `tool_response` is a plain string or missing** rather than an object with `stdout`. Expected: handled the same. Test in Task 4.
5. **Two terminals of one workstream reporting at the same moment.** Expected: both events land. Test in Task 4.

## File Structure

| File | Responsibility |
|---|---|
| `Sources/pipeline.py` (new) | Pure run model: phases, plan parsing, tasks, ledger, event rules, PR view, schedule, render. No I/O. |
| `Sources/pipeline_tracker.py` (new) | SQLite storage, atomic update, hook and command events, document scan, ledger poll, PR poll, `snapshot` for `list`. |
| `Sources/streams.py` | Wire the tracker into `database`, `hook`, `streams`, `main`. |
| `Sources/codex_session.py` | Feed root-thread commands to the tracker. |
| `Sources/PipelineView.swift` (new) | Decodable types, text formatting, `PipelineSection`. |
| `Sources/DocumentWindow.swift` (new) | Rendered Markdown window for one file. |
| `Sources/WorkstreamInspector.swift`, `Sources/Muxteroid.swift`, `Sources/AttentionNotifications.swift` | Place the section, decode the new fields, add the window scene, poll once a minute. |
| `Tests/test_pipeline_model.py`, `Tests/test_pipeline_tracker.py` (new) | Unit tests. |
| `build.sh`, `Package.swift`, `README.md` | Bundle the new files; document the feature. |

## Shared test fixtures

Both new test files start like the existing ones:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
```

`Tests/test_pipeline_model.py` uses these throughout (ported from the mod's tests):

```python
import unittest
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
```

---

### Task 1: Run model core

**Files:**
- Create: `Sources/pipeline.py`
- Create: `Tests/test_pipeline_model.py`
- Modify: `build.sh:13` (add `Sources/pipeline.py` to the `cp` line), `Package.swift:15` (add `"pipeline.py"` to `exclude`)

**Interfaces:**
- Produces (all in `pipeline.py`; every function that takes a run mutates it and returns it):
  - `PHASES = ("brainstorm", "plan", "execute", "final-review", "verify", "pr", "ci", "merge")`
  - `PHASE_LABEL = {"brainstorm": "brainstorm", "plan": "plan", "execute": "tasks", "final-review": "final review", "verify": "verify", "pr": "PR", "ci": "CI", "merge": "merged"}`
  - `MIN = 60.0`
  - Run: `{"name": str, "launchedAt": float, "phases": {phase: stamp}, "tasks": [task]}` plus optional `planPath`, `specPath`, `workspace`, `ledgerMtime`, `currentTask`, `pr` (int), `prUrl`, `ciDetail`. Stamp: `{"status": "todo"|"running"|"done"|"failed"|"skipped"}` plus optional `startedAt`, `endedAt`. Task: `{"n": int, "title": str, "implement": stamp, "review": stamp}` plus optional `round`.
  - `new_run(now: float) -> dict` (name `"superpowers"`, every phase `todo`, no tasks)
  - `is_finished(run) -> bool`, `has_started(run, phase) -> bool`, `all_tasks_done(run) -> bool`
  - `advance_to(run, phase, now)`, `complete_phase(run, phase, now)`, `fail_phase(run, phase, now)`
  - `parse_plan_tasks(text: str) -> list` of `{"n": int, "title": str}`
  - `slug_name(path: str) -> str`
  - `with_plan(run, plan_path, parsed)`, `with_spec(run, spec_path)` (stores `specPath`; names the run from the spec only while there is no `planPath`)
  - `start_task(run, n, now)`, `review_task(run, now)`, `apply_ledger(run, text, now)`

- [ ] **Step 1: Write the failing tests** in `Tests/test_pipeline_model.py` (fixtures above), class `RunModelTests`:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `ModuleNotFoundError: No module named 'pipeline'`

- [ ] **Step 3: Implement the functions listed under Produces in `Sources/pipeline.py`**

Port `newRun` (without the agent-id fields and `tokens`), `isFinished`, `hasStarted`, `begin`, `finish`, `advanceTo`, `completePhase`, `failPhase`, `allTasksDone`, `parsePlanTasks`, `slugName`, `withPlan`, `withSpec`, `startTask`, `reviewTask`, `applyLedger` from `model.ts`. The plan heading pattern is exactly `^#{2,4}\s+Task\s+(\d+)\s*[:.–—-]?\s*(.*)$`. A stamp never carries a key whose value would be `None`.

- [ ] **Step 4: Run them and see them pass**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `OK` (7 tests)

- [ ] **Step 5: Register the file and commit**

Add `Sources/pipeline.py` to the `cp` line in `build.sh` and `"pipeline.py"` to `exclude` in `Package.swift`.

```bash
git add Sources/pipeline.py Tests/test_pipeline_model.py build.sh Package.swift
git commit -m "Add the Superpowers run model"
```

---

### Task 2: Event rules

**Files:**
- Modify: `Sources/pipeline.py`
- Modify: `Tests/test_pipeline_model.py`

**Interfaces:**
- Consumes: Task 1.
- Produces (pure; each returns the run, which may be a new one or `None`):
  - `skill_event(run, skill: str, now) -> Optional[dict]`
  - `document_event(run, kind: str, path: str, parsed: list, is_new: bool, now) -> Optional[dict]` (`kind` is `"spec"` or `"plan"`; `parsed` is `parse_plan_tasks` output, `[]` for a spec)
  - `workspace_path(command: str, output: str) -> Optional[str]`
  - `attach_workspace(run, workspace: str, plan_path: Optional[str], parsed: list, now) -> dict`
  - `command_event(run, command: str, output: str, now) -> Optional[dict]`
  - `pull_request_created(run, url: str, now) -> Optional[dict]` (sets `pr` and `prUrl`)
  - `classify_check(check) -> str` (`"pass"`, `"fail"`, `"pending"`), `apply_pr_view(run, view, now) -> dict`
  - `PR_URL = re.compile(r'https?://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)')`

- [ ] **Step 1: Write the failing tests**, class `EventRuleTests`:

```python
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
```

Then port the mod's three `pull request` tests (`pending checks keep CI running with a count`, `a failing check fails CI, and a rerun brings it back to running`, `green checks wait on the merge, and a merged PR finishes the run`) from `~/.claude/mods/pipeline/tests/model.test.ts:128-175` with the same inputs and expected values (`'1 of 2 checks'`, `'1 of 2 failing'`, the rerun stamp `{'status': 'running', 'startedAt': T0 + 70 * MIN}`), and add a fourth, `test_closed_pull_request_fails_the_merge`: `apply_pr_view(run, {'state': 'CLOSED'}, now)` leaves `run['phases']['merge']['status'] == 'failed'` and `is_finished(run)` true.

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `AttributeError: module 'pipeline' has no attribute 'skill_event'` (and the like)

- [ ] **Step 3: Implement the functions**

- `skill_event`: port `onSkill` from `register.tsx`, minus the pane and agent calls. Starters: `brainstorming` → brainstorm, `writing-plans` → plan, `subagent-driven-development` and `executing-plans` → execute, with or without the `superpowers:` prefix. A starter makes a new run when `run` is `None`, finished, or the starter is not execute and execute has started. The other three skills need the prefix and a live run.
- `document_event`, where `own` means `path` equals the run's `specPath` or `planPath`:
  - `is_new`: make a new run when `run` is `None`, finished, or execute has started and not `own`. Then a spec does `with_spec` and `advance_to(…, 'brainstorm')`; a plan does `with_plan` and `advance_to(…, 'plan')`.
  - not `is_new`: if the run is live and `path == run['planPath']`, `with_plan` again; otherwise return `run` untouched.
- `workspace_path`: only when the command contains `sdd-workspace` as a word; return the last output line that matches `^(/.*/\.superpowers/sdd/[^/\s]+)\s*$`.
- `attach_workspace`: port the run-selection part of `attachWorkspace` (new run when `None`, finished, or a different plan while execute has started), then `with_plan` if `plan_path`, `advance_to(…, 'execute')`, set `workspace`, drop `ledgerMtime`.
- `command_event`: returns `run` unchanged when it is `None` or finished. Port `onBash`'s remaining branches. The brief pattern is `\btask-brief\s+(?:"[^"]*"|'[^']*'|\S+)\s+(\d+)`. `gh pr create` needs `PR_URL` in the output and calls `pull_request_created`.
- `classify_check`, `apply_pr_view`: port `classifyCheck`, `applyPrView`.

- [ ] **Step 4: Run them and see them pass**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `OK` (16 tests)

- [ ] **Step 5: Commit**

```bash
git add Sources/pipeline.py Tests/test_pipeline_model.py
git commit -m "Add Superpowers event rules to the run model"
```

---

### Task 3: Schedule and render

**Files:**
- Modify: `Sources/pipeline.py`
- Modify: `Tests/test_pipeline_model.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `schedule(run, now) -> dict`: `{"phases": {phase: slot}, "tasks": [task_slot], "finish": Optional[float], "task": float}`. Slot: `{"start": Optional[float], "end": Optional[float], "estimate": bool, "view": "todo"|"running"|"slow"|"done"|"failed"}`. Task slot adds `"n"`, `"implement"`, `"review"` (views).
  - `render(run, now, waiting=False, active_agents=0, frozen=False) -> dict`. This is the JSON the app decodes:

```python
{"name": str, "startedAt": float, "finishAt": Optional[float],
 "finished": None | "merged" | "closed",
 "needsYou": bool, "polling": bool, "frozen": bool, "activeAgents": int,
 "phases": [{"id": str, "label": str, "mark": str, "time": Optional[float], "estimate": bool,
             "detail": str, "url": Optional[str],
             "tasks": None | [{"n": int, "title": str, "mark": str, "implement": str, "review": str,
                               "round": Optional[int], "time": Optional[float], "estimate": bool}]}]}
```

  `mark` is a slot view or `"needs"`. `tasks` is a list only on the `execute` phase.

- [ ] **Step 1: Write the failing tests**, class `ScheduleTests`:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `AttributeError: module 'pipeline' has no attribute 'schedule'`

- [ ] **Step 3: Implement `schedule`**

Port `averageTaskMs`, `stageView` and `schedule` from `model.ts` with `SLOW_FACTOR = 1.5`, a default task of `12 * MIN`, `IMPLEMENT_SHARE = 0.6`, and estimates final-review `10 * MIN`, verify `3 * MIN`, pr `3 * MIN`, ci `15 * MIN`, merge `3 * MIN`. Leave out `slowFinish`. Two differences from the mod, both in the `execute` branch:

- `tracks = bool(run.get('workspace')) or any task whose implement or review status is not 'todo'`. When the execute stamp is **running** and `tracks` is false, every task slot is `todo` with no start or end, no task is inferred active, the cursor becomes `None`, and the execute slot's view is `running` (never `slow`). Before execute starts the mod's projection applies unchanged, so a plan that lists its tasks already gives an estimate.
- When the execute stamp is done, a task whose review is not done gets a slot with view, implement and review all `done`, no start or end, `estimate` false.

- [ ] **Step 4: Implement `render`**

- `plan = schedule(run, now)`. Phases whose status is `skipped` are left out.
- Per phase, `time` is the slot's `end` if set, else its `start` when the view is not `todo`, else `None`; `estimate` is the slot's `estimate` only when `time` came from `end`. Tasks use the same rule.
- `detail`: brainstorm → `slug_name(specPath)` or `''`; plan → `'N tasks'` when there are tasks, else `slug_name(planPath)` or `''`; execute → `'D of N'` when there are tasks; pr → `'#N'`; ci → `ciDetail` or `''`; others `''`. `url` is `prUrl` on the pr phase, else `None`.
- `finished`: `'merged'` when merge is done, `'closed'` when it failed, else `None`. `finishAt`: the merge stamp's `endedAt` when merged, else `plan['finish']`.
- `needsYou`: `waiting and not frozen and finished is None and active_agents == 0 and` ci is not running `and` some phase is running. The last running phase in `PHASES` order then gets mark `'needs'`.
- `polling`: `bool(prUrl) and finished is None and not frozen`.
- `frozen`: marks `running` and `slow` become `todo` (phases, tasks, implement, review); every time that was an estimate becomes `None` with `estimate` false; `finishAt` is `None` unless merged; `activeAgents` is 0.

- [ ] **Step 5: Run them and see them pass**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_model.py'`
Expected: `OK` (22 tests)

- [ ] **Step 6: Commit**

```bash
git add Sources/pipeline.py Tests/test_pipeline_model.py
git commit -m "Schedule and render a Superpowers run"
```

---

### Task 4: Run storage and Claude hook events

**Files:**
- Create: `Sources/pipeline_tracker.py`
- Create: `Tests/test_pipeline_tracker.py`
- Modify: `Sources/streams.py` (`database` at 54-64, `hook` at 279-290, `main` near 566)
- Modify: `build.sh:13`, `Package.swift:15` (add `pipeline_tracker.py`)

**Interfaces:**
- Consumes: `pipeline.skill_event`, `document_event`, `workspace_path`, `attach_workspace`, `command_event`, `pull_request_created`, `parse_plan_tasks`, `is_finished`, `PR_URL`.
- Produces (in `pipeline_tracker.py`; `db` is the connection from `streams.database()`; `key` is a terminal id, `stream` a workstream id):
  - `initialize(db) -> None`. Tables: `pipeline_runs(stream TEXT PRIMARY KEY, run TEXT NOT NULL)`, `pipeline_docs(stream TEXT, path TEXT, kind TEXT, title TEXT, mtime REAL, PRIMARY KEY(stream,path))`, `pipeline_scans(stream TEXT PRIMARY KEY, signature TEXT NOT NULL)`.
  - `owner(db, key) -> str` (the terminal's workstream from `workstream_terminals`, else `key`)
  - `load(db, stream) -> Optional[dict]`
  - `update(db, stream, change) -> Optional[dict]`: `change(run_or_None) -> run_or_None`; `None` deletes the row.
  - `reset(db, stream) -> None`
  - `record_document(db, stream, kind, path, now) -> None`
  - `tool_event(db, key, name, args, response, now) -> None`
  - `command_event(db, key, command, output, now) -> None`
- Bridge action: `streams.py pipeline-reset STREAM`.

- [ ] **Step 1: Write the failing tests** in `Tests/test_pipeline_tracker.py`

Import `streams` under a temporary `MUXTEROID_HOME` exactly as `Tests/test_workstream_terminals.py:13-16` does, then `import pipeline, pipeline_tracker as tracker`. Class `TrackerTests` with this fixture:

```python
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
```

Tests:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_tracker.py'`
Expected: `ModuleNotFoundError: No module named 'pipeline_tracker'`

- [ ] **Step 3: Implement `Sources/pipeline_tracker.py`**

- `load`: return the decoded run only if it is a dict whose `phases` has a dict with a string `status` for every phase in `pipeline.PHASES` and whose `tasks` is a list; otherwise `None`.
- `update`: if `not db.in_transaction`, `db.execute('BEGIN IMMEDIATE')` (an open Python sqlite3 transaction already holds the write lock); `load`; call `change`; `INSERT OR REPLACE` the JSON or `DELETE` on `None`; `db.commit()`; return the run.
- `record_document`: read the file as UTF-8; on `OSError` or `UnicodeError` return. Title is the first line starting with `# ` (text after it, stripped), else `pipeline.slug_name(path)`. `is_new` is "no `pipeline_docs` row for this stream and path". Upsert the row with the file's `st_mtime`, then `update` with `pipeline.document_event` (`parsed` is `parse_plan_tasks(text)` for a plan, `[]` for a spec).
- `tool_event`: `stream = owner(db, key)`. `Skill` → `update` with `pipeline.skill_event(run, args.get('skill') or '', now)`. `Write` and `Edit` → `path = args.get('file_path') or ''`; kind is `spec` when it matches `/docs/superpowers/specs/[^/]+\.md$`, `plan` for `/docs/superpowers/plans/[^/]+\.md$`; then `record_document`. `Bash` → `command_event(db, key, args.get('command') or '', text, now)`. A tool whose lowercased name contains both `github` and `create_pull_request` → the first `pipeline.PR_URL` match in `text` goes to `pipeline.pull_request_created`. `text` is `response['stdout']` when `response` is a dict with a string `stdout`, `response` itself when it is a string, else `json.dumps(response)`.
- `command_event`: `workspace = pipeline.workspace_path(command, output)`. With a workspace: read `<workspace>/plan-path` (stripped; missing → no plan); the plan path is the marker if absolute, else joined to the workspace path with its trailing `/.superpowers/sdd/<slug>` removed; parse the plan if it can be read; `update` with `pipeline.attach_workspace`. Without one: `update` with `pipeline.command_event`.

- [ ] **Step 4: Wire it into `Sources/streams.py`**

- `import pipeline_tracker as tracker` beside the other module imports, and `tracker.initialize(db)` in `database()` after `subagents.initialize(db)`.
- In `hook`, inside the existing `PostToolUse` block and after `links.tool_result(...)`: when `not event.get('agent_id')`, call `tracker.tool_event(db, key, event.get('tool_name', ''), event.get('tool_input') or {}, event.get('tool_response'), time.time())`.
- In `main`, beside the `link` action (before `bridge.lock`): `pipeline-reset` takes one stream id and calls `tracker.reset` inside `with database() as db`.
- Add `Sources/pipeline_tracker.py` to the `cp` line in `build.sh` and `"pipeline_tracker.py"` to `exclude` in `Package.swift`.

- [ ] **Step 5: Run the whole suite**

Run: `/usr/bin/python3 -m unittest discover -s Tests`
Expected: `OK` with 66 tests (35 existing, 22 model, 9 tracker), 3 skipped.

- [ ] **Step 6: Commit**

```bash
git add Sources/pipeline_tracker.py Sources/streams.py Tests/test_pipeline_tracker.py build.sh Package.swift
git commit -m "Track Superpowers runs from Claude hook events"
```

---

### Task 5: Documents, ledger and the list output

**Files:**
- Modify: `Sources/pipeline_tracker.py`
- Modify: `Sources/streams.py` (`streams()` at 410-438)
- Modify: `Tests/test_pipeline_tracker.py`

**Interfaces:**
- Consumes: Task 4; `pipeline.render`, `apply_ledger`.
- Produces:
  - `timestamp(text: str) -> float` (ISO time such as `2026-11-10T05:41:25Z` to epoch seconds; `0.0` when it cannot be parsed)
  - `snapshot(db, stream, root, *, created, own_worktree, closed, waiting, active_agents, now) -> tuple`: `(pipeline_or_None, documents)`, where `documents` is `[{"kind": "spec"|"plan", "title": str, "path": str}]`, newest first.
  - Every row from `streams.streams()` gains `"pipeline"` and `"documents"`.

- [ ] **Step 1: Write the failing tests**, class `SnapshotTests`, in `Tests/test_pipeline_tracker.py`

Fixture, modelled on `WorktreeCreationTests` (`Tests/test_workstream_terminals.py:281-304`): `self.root` is a resolved temporary directory; `self.repo = self.root / 'source repo'` is a repository (`git init -b main`, `user.name` and `user.email` configured) with `docs/superpowers/specs/2026-01-01-old-design.md` committed; `self.git(*args)` runs git in `self.repo`; `streams.ROOT` is patched to `self.root / 'app home'` (created). Helpers:

```python
def snap(self, root=None, now=2_000_000_000.0, **options):
    settings = dict(created=0.0, own_worktree=True, closed=False, waiting=False, active_agents=0)
    settings.update(options)
    with streams.database() as db:
        return tracker.snapshot(db, 'mx-s', str(root or self.repo), now=now, **settings)

def write(self, relative, text, root=None):
    path = (root or self.repo) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path
```

Tests:

```python
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

def test_timestamp(self):
    self.assertEqual(tracker.timestamp('2026-11-10T05:41:25Z'), 1794289285.0)
    self.assertEqual(tracker.timestamp(''), 0.0)
```

In `TrackerTests` add:

```python
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
```

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_tracker.py'`
Expected: `AttributeError: module 'pipeline_tracker' has no attribute 'snapshot'`

- [ ] **Step 3: Implement the scan, the ledger poll and `snapshot`**

`snapshot` does, in order, catching `OSError` around file access so nothing escapes:

1. Unless `closed`: **scan.** List regular `*.md` files in `<root>/docs/superpowers/specs` and `<root>/docs/superpowers/plans`. The signature is the JSON of their sorted `[path, st_mtime_ns]` pairs; a stream with no stored signature compares as `[]`. If it equals the stored one, skip to step 2. Otherwise:
   - Candidates are listed files with no `pipeline_docs` row. If there are any, ask `git -C <root> ls-files --others --exclude-standard -- docs/superpowers/specs docs/superpowers/plans` (timeout 4 s) and keep the candidates it names; if git exits non-zero or cannot run, keep them all. When `own_worktree` is false, also require `st_mtime > created`.
   - Call `record_document` for each kept candidate and for each known document whose `st_mtime` differs from its row, oldest `st_mtime` first.
   - Store the signature.
2. Unless `closed`: **ledger.** For a live run, the workspace is `run['workspace']`, else `<root>/.superpowers/sdd/<plan file name without .md>` when `run['planPath']` is set and that directory has a `plan-path` file. If `<workspace>/progress.md` exists and its `st_mtime` differs from `run.get('ledgerMtime')`, read it and `update` with a change that does `pipeline.advance_to(run, 'execute', now)`, then `pipeline.apply_ledger`, and sets `workspace` and `ledgerMtime` (leaving a finished or missing run alone). The advance is what moves a files-only run to tasks.
3. Delete `pipeline_docs` rows whose files no longer exist; build `documents` from the rest, ordered by `mtime` descending.
4. `run = load(db, stream)`; the pipeline is `pipeline.render(run, now, waiting, active_agents, frozen=closed)` or `None`.

- [ ] **Step 4: Add the fields in `streams.streams()`**

In the loop that builds `rows`, before the `Done` override changes `state`, call `tracker.snapshot(db, key, meta.get('worktreeRoot') or meta['folder'], created=tracker.timestamp(issue.get('created_at') or ''), own_worktree=bool(meta.get('worktreeRoot')), closed=issue['status'] == 'closed', waiting=state in ('Needs you', 'Review'), active_agents=<count of this stream's agents whose state is 'Working' or 'Needs you'>, now=time.time())` with `meta = issue['metadata']`, and add `pipeline=` and `documents=` to the row. The connection `db` stays usable after its `with` block ends.

- [ ] **Step 5: Run the whole suite**

Run: `/usr/bin/python3 -m unittest discover -s Tests`
Expected: `OK` with 75 tests, 3 skipped.

- [ ] **Step 6: Commit**

```bash
git add Sources/pipeline_tracker.py Sources/streams.py Tests/test_pipeline_tracker.py
git commit -m "List a workstream's specs, plans and pipeline"
```

---

### Task 6: PR polling and Codex commands

**Files:**
- Modify: `Sources/pipeline_tracker.py`, `Sources/streams.py` (`main`), `Sources/codex_session.py:157-161`
- Modify: `Tests/test_pipeline_tracker.py`

**Interfaces:**
- Consumes: Tasks 4–5; `pipeline.apply_pr_view`.
- Produces:
  - `pr_view(url: str) -> Optional[dict]`: runs `gh pr view URL --json state,statusCheckRollup` with a 20 s timeout; `None` on any failure.
  - `poll_pull_requests(db, streams, now, view=None) -> None` (`streams` is an iterable of workstream ids; `view` defaults to the module's `pr_view`, looked up at call time so tests can patch it)
  - Bridge action: `streams.py pipeline-poll STREAM...`, handled before `bridge.lock`.

- [ ] **Step 1: Write the failing tests** in `TrackerTests`

```python
def at_ci(self):
    self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
    self.post('mx-example', 'Bash', {'command': 'gh pr create'}, {'stdout': 'https://github.com/o/r/pull/12'})

def test_poll_applies_the_pull_request_view(self):
    self.at_ci()
    seen = []
    def view(url):
        seen.append(url)
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

def test_pr_view_survives_a_missing_gh(self):
    with patch.object(tracker.subprocess, 'run', side_effect=FileNotFoundError):
        self.assertIsNone(tracker.pr_view('https://github.com/o/r/pull/12'))

def test_codex_root_commands_reach_the_pipeline_and_child_commands_do_not(self):
    import codex_session
    def thread(identity, parent):
        return {'thread': {'status': {'type': 'idle'}, 'parentThreadId': parent, 'cwd': str(self.folder)}}
    def turn(output):
        return {'data': [{'id': 't', 'status': 'completed', 'items': [
            {'type': 'commandExecution', 'exitCode': 0, 'command': 'gh pr create', 'aggregatedOutput': output}]}]}
    class Client:
        def call(inner, method, params):
            if method == 'thread/loaded/list':
                return {'data': ['root', 'child']}
            if method == 'thread/read':
                return thread(params['threadId'], None if params['threadId'] == 'root' else 'root')
            return turn('https://github.com/o/r/pull/7' if params['threadId'] == 'root' else 'https://github.com/o/r/pull/8')
    self.post('mx-example', 'Skill', {'skill': 'superpowers:executing-plans'})
    with streams.database() as db:
        codex_session.observe(Client(), db, 'term-extra', None, {})
    self.assertEqual(self.run_state()['pr'], 7)
```

- [ ] **Step 2: Run them and see them fail**

Run: `/usr/bin/python3 -m unittest discover -s Tests -p 'test_pipeline_tracker.py'`
Expected: failures naming `pr_view`, `poll_pull_requests` and `Unknown action`.

- [ ] **Step 3: Implement**

- `pr_view`: catch `OSError`, `subprocess.TimeoutExpired` and `ValueError`; a non-zero exit is `None`.
- `poll_pull_requests`: for each id, `load`; skip unless the run has `prUrl` and is not finished; call `view(url)` with no transaction open; when it returns a dict, `update` with a change that applies `pipeline.apply_pr_view` only if the run still has that `prUrl` and is not finished.
- `streams.main`: `pipeline-poll` beside `pipeline-reset`, calling `tracker.poll_pull_requests(db, args, time.time())` inside `with database() as db`. It must not take `bridge.lock`.
- `codex_session.observe`: `import pipeline_tracker as tracker`; in the `commandExecution` branch, after `links.tool_result(...)`, when `not parent` call `tracker.command_event(db, key, item["command"], item.get("aggregatedOutput") or "", time.time())`.

- [ ] **Step 4: Run the whole suite**

Run: `/usr/bin/python3 -m unittest discover -s Tests`
Expected: `OK` with 79 tests, 3 skipped.

- [ ] **Step 5: Commit**

```bash
git add Sources/pipeline_tracker.py Sources/streams.py Sources/codex_session.py Tests/test_pipeline_tracker.py
git commit -m "Poll pull requests and follow Codex commands for the pipeline"
```

---

### Task 7: Pipeline section in the inspector

**Files:**
- Create: `Sources/PipelineView.swift`
- Modify: `Sources/Muxteroid.swift:44-54` (`Stream`), `:608-618` (`inspector`)
- Modify: `Sources/WorkstreamInspector.swift:4-64`
- Modify: `Sources/AttentionNotifications.swift:33-39`

**Interfaces:**
- Consumes: the `render` JSON from Task 3, `documents` from Task 5, the `pipeline-reset` and `pipeline-poll` actions.
- Produces (in `PipelineView.swift`):

```swift
struct PipelineTask: Decodable, Identifiable {
    let n: Int
    let title, mark, implement, review: String
    let round: Int?
    let time: Double?
    let estimate: Bool
    var id: Int { n }
}
struct PipelinePhase: Decodable, Identifiable {
    let id, label, mark, detail: String
    let time: Double?
    let estimate: Bool
    let url: URL?
    let tasks: [PipelineTask]?
}
struct Pipeline: Decodable {
    let name: String
    let startedAt: Double
    let finishAt: Double?
    let finished: String?
    let needsYou, polling, frozen: Bool
    let activeAgents: Int
    let phases: [PipelinePhase]
}
struct WorkstreamDocument: Decodable, Identifiable {
    let kind, title, path: String
    var id: String { path }
}
enum PipelineText {
    static func clock(_ time: Double, estimate: Bool) -> String
    static func duration(_ seconds: Double) -> String
    static func summary(_ pipeline: Pipeline, now: Date) -> String?
    static func spoken(mark: String) -> String
}
struct PipelineSection: View {
    let pipeline: Pipeline?
    let documents: [WorkstreamDocument]
    let clear: () -> Void
    var open: (WorkstreamDocument) -> Void = { _ in }
}
```

  `Stream` gains `let pipeline: Pipeline?` and `let documents: [WorkstreamDocument]`. `WorkstreamInspector` gains `let clearPipeline: () -> Void`.

There is no Swift test target; this task is verified by building and by the manual check in Task 8.

- [ ] **Step 1: Add the types, `PipelineText` and the two `Stream` fields**

- `clock`: `Date(timeIntervalSince1970: time).formatted(date: .omitted, time: .shortened)`, prefixed with `≈` when `estimate`.
- `duration`: whole minutes, rounded, never negative; under an hour `"42m"`, otherwise `"1h 10m"`.
- `summary`: `nil` when `frozen` and not merged; `"merged 15:14"` when `finished == "merged"` (time omitted if `finishAt` is nil); `"PR closed without merging"` when `"closed"`; `"about 1h 10m to go → merged ≈ 15:14"` when `finishAt` is set; otherwise `"No estimate yet"`.
- `spoken`: `done` → "done", `running` → "in progress", `slow` → "running late", `failed` → "failed", `needs` → "needs you", anything else → "to come".

- [ ] **Step 2: Build `PipelineSection`**

A `DisclosureGroup` whose expanded state is `@AppStorage("pipelineExpanded")`, default `true`. Follow the mockup in the spec ("What the person sees") for order and content:

- Label: "Pipeline" in `.title3.bold()` to match the Tasks header, and, unless the run is finished or frozen, `"\(duration(now - startedAt)) in"` in secondary caption. The label has `.contextMenu { Button("Clear Pipeline", action: clear) }`, disabled when `pipeline == nil`.
- When `pipeline` is set: the run name; the `summary` line if any; `"N subagent(s) working"` when `activeAgents > 0`; then one row per phase and, under a phase with `tasks`, one indented row per task.
- Phase row: a fixed-width leading time column (`clock`, monospaced digits, secondary, empty when `time` is nil), the mark, the label (semibold while `running`, `slow` or `needs`; secondary when `todo`), then `detail` in secondary, one line, truncating. When `url` is set the detail is a `Link`.
- Task row: mark, `n`, title (one line, truncating), two small squares for `implement` and `review`, `r\(round)` when set, time as in phase rows.
- Marks, as SF Symbols with these colours: `done` `checkmark.circle.fill` green; `running` `circle.lefthalf.filled` blue; `slow` `circle.lefthalf.filled` red; `failed` `xmark.circle.fill` red; `needs` `exclamationmark.circle.fill` orange; `todo` `circle` secondary. Squares: `square.fill` in the mark's colour, `square` secondary for `todo`.
- Each phase and task row is one accessibility element labelled in words, for example "plan, done, 13:20, 6 tasks" and "Task 3, Timeline view, in progress, implement in progress, review to come, about 14:10".
- Documents: below the timeline, one button row per document, "Spec" or "Plan" in secondary caption, the title on one line, and `arrow.up.right`; it calls `open(document)`, has `.help(document.path)` and the accessibility label "Open spec: TITLE" or "Open plan: TITLE".
- The section must read well from 218 to 410 pt wide: nothing wraps into the time column, titles truncate rather than push marks off the edge.

- [ ] **Step 3: Place it in `WorkstreamInspector`**

Move the Tasks header (title, count, progress bar) inside the `ScrollView` so the scroll content is, in order: `PipelineSection` when `stream.pipeline != nil || !stream.documents.isEmpty`, a `Divider` after it, the Tasks header, the task groups, the reply. Keep the existing 16 pt padding. The footer stays outside the scroll view, unchanged. Pass `clearPipeline` to the section's `clear`.

- [ ] **Step 4: Wire the actions**

- `Dashboard.inspector(_:)` passes `clearPipeline: { Task { _ = await store.perform(["pipeline-reset", stream.id]) } }`.
- In `AttentionNotifications.applicationDidFinishLaunching`'s refresh loop, keep a `Date` of the last poll. After a successful refresh, if at least 60 s have passed and `store.rows` has streams whose `pipeline?.polling == true`, record the time and start `Task { _ = try? await Bridge.run(["pipeline-poll"] + ids) }` without awaiting it, so a slow `gh` never delays the refresh.

- [ ] **Step 5: Build**

Run: `swift build 2>&1 | tail -5`
Expected: `Build complete!` with no new warnings from the files touched.

- [ ] **Step 6: Commit**

```bash
git add Sources/PipelineView.swift Sources/Muxteroid.swift Sources/WorkstreamInspector.swift Sources/AttentionNotifications.swift
git commit -m "Show the Superpowers pipeline in the inspector"
```

---

### Task 8: Document window, README and the end-to-end check

**Files:**
- Create: `Sources/DocumentWindow.swift`
- Modify: `Sources/Muxteroid.swift:692-704` (`MuxteroidApp`), `Sources/WorkstreamInspector.swift` (pass `open`)
- Modify: `README.md` ("Working across streams")

**Interfaces:**
- Consumes: `PipelineSection.open`, `WorkstreamDocument`.
- Produces: `struct DocumentWindow: View { let url: URL }` and a scene `WindowGroup("Document", id: "document", for: URL.self)`.

- [ ] **Step 1: Build `DocumentWindow`**

- Loads the file as UTF-8 and shows it with `Markdown(text)` (font size 14, `.textSelection(.enabled)`, content at most 760 pt wide, 24 pt padding) inside a `ScrollView`.
- `.navigationTitle(url.lastPathComponent)`. Toolbar: "Open in Editor" (`NSWorkspace.shared.open(url)`) and "Reveal in Finder" (`NSWorkspace.shared.activateFileViewerSelecting([url])`).
- A `.task(id: url)` loop checks the file's modification date every two seconds and reloads when it changes.
- When the file is missing or unreadable: `ContentUnavailableView("Document not found", systemImage: "doc.questionmark", description: Text(url.path))`. It recovers by itself if the file reappears.
- ⌘W must close this window. The app's "Close Terminal" command owns ⌘W and is disabled without a focused value, so give the window `.focusedSceneValue(\.closeWorkstreamTerminal) { NSApp.keyWindow?.performClose(nil) }`.

- [ ] **Step 2: Add the scene and open documents**

In `MuxteroidApp.body`, after the dashboard `Window`: `WindowGroup("Document", id: "document", for: URL.self) { $url in if let url { DocumentWindow(url: url) } }.defaultSize(width: 760, height: 820).commandsRemoved()`. In `WorkstreamInspector`, read `@Environment(\.openWindow)` and pass `open: { openWindow(id: "document", value: URL(fileURLWithPath: $0.path)) }` to `PipelineSection`.

- [ ] **Step 3: Document it in the README**

Add this bullet to "Working across streams", after "Follow the plan.":

```markdown
- **Follow a Superpowers run.** If you use the [Superpowers](https://github.com/obra/superpowers) plugin, the inspector shows the run above the tasks: brainstorm, plan, each plan task with its implement and review steps, final review, verify, PR, CI, and merge, with the time each finished and an estimate for what is left. The step that is waiting on you is marked. Below the timeline are the specs and plans written in this workstream; click one to read it in its own window, or open it in your editor from there. Muxteroid uses the Superpowers installation you already have. Claude terminals report each step as it happens; Codex terminals are followed through the spec, plan, and task-progress files, so their timeline updates at the end of a turn. PR checks are read with `gh` about once a minute. Right-click **Pipeline** and choose **Clear Pipeline** to forget a run; marking the workstream complete freezes it.
```

In "How it runs", after the sentence that ends "…for reporting activity.", add: `The same hooks drive the Superpowers timeline, which Muxteroid stores per workstream beside its other activity data.`

- [ ] **Step 4: Build the app**

Run: `./build.sh 2>&1 | tail -3 && ls build/Muxteroid.app/Contents/Resources/pipeline.py build/Muxteroid.app/Contents/Resources/pipeline_tracker.py`
Expected: the build completes and both files are listed.

- [ ] **Step 5: Check it by hand against a scratch home**

```bash
export MUXTEROID_HOME="$(mktemp -d)/Application Support/Muxteroid"; mkdir -p "$MUXTEROID_HOME"
demo="$(mktemp -d)/demo repo"; mkdir -p "$demo"; git -C "$demo" init -q -b main
git -C "$demo" -c user.name=T -c user.email=t@example.invalid -c commit.gpgsign=false commit -q --allow-empty -m Initial
bridge=build/Muxteroid.app/Contents/Resources/streams.py
plain=$(/usr/bin/python3 "$bridge" create "Plain" "$demo" "" claude)
run=$(/usr/bin/python3 "$bridge" create "With a run" "$demo" "" claude)
root=$(/usr/bin/python3 "$bridge" list | /usr/bin/python3 -c "import json,sys; print(next(s['folder'] for s in json.load(sys.stdin) if s['title']=='With a run'))")
mkdir -p "$root/docs/superpowers/specs" "$root/docs/superpowers/plans"
cp docs/superpowers/specs/2026-11-10-superpowers-pipeline-design.md "$root/docs/superpowers/specs/"
cp docs/superpowers/plans/2026-11-10-superpowers-pipeline.md "$root/docs/superpowers/plans/"
build/Muxteroid.app/Contents/MacOS/Muxteroid
```

Do not click **Start terminal** (that would start a real agent). Confirm each of these, and say in the report which were seen. If you cannot operate the window, check items 1, 2, 4 and 7 in the output of `/usr/bin/python3 "$bridge" list` instead (use `/usr/bin/python3 "$bridge" pipeline-reset "$run"` for 7) and report items 3, 5, 6 and 8 as not checked, for the owner to run:

1. "Plain" shows no Pipeline section and its inspector looks as before.
2. "With a run" shows Pipeline above Tasks: brainstorm done, plan in progress with "8 tasks", eight task rows to come, a summary of about two hours to go, and a Spec and a Plan row.
3. Clicking the Plan row opens a window with the rendered plan. Appending a line to the copied file shows up within a few seconds. ⌘W closes the window. Deleting the copied file while its window is open shows "Document not found".
4. With `mkdir -p "$root/.superpowers/sdd/2026-11-10-superpowers-pipeline" && cd "$root/.superpowers/sdd/2026-11-10-superpowers-pipeline" && echo docs/superpowers/plans/2026-11-10-superpowers-pipeline.md > plan-path && printf '# SDD ledger\nTask 1: complete\n' > progress.md`, within a few seconds the tasks phase is in progress, task 1 is done and task 2 is in progress.
5. Narrow the inspector to its minimum and widen it to its maximum: rows stay on one line and nothing overlaps.
6. Collapse the section, quit, relaunch with the same `MUXTEROID_HOME`: it is still collapsed.
7. Right-click Pipeline → Clear Pipeline: the timeline goes, the Spec and Plan rows stay.
8. Mark "With a run" complete (after recreating a run by adding another new plan file): no orange or blue marks and no summary line.

Afterwards remove the two scratch worktrees with `git -C "$demo" worktree prune` and delete both temporary directories.

- [ ] **Step 6: Run the Python suite once more and commit**

Run: `/usr/bin/python3 -m unittest discover -s Tests`
Expected: `OK` with 79 tests, 3 skipped.

```bash
git add Sources/DocumentWindow.swift Sources/Muxteroid.swift Sources/WorkstreamInspector.swift README.md
git commit -m "Open specs and plans in a document window"
```
