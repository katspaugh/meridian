# Superpowers pipeline and documents in the inspector

Date: 2026-11-10

## Intent

People who drive a workstream with the [Superpowers](https://github.com/obra/superpowers) plugin go through a fixed sequence: brainstorm a design, write a plan, execute the plan task by task, review, verify, open a PR, wait for CI, merge. Today Muxteroid shows none of that. The inspector lists Beads tasks and the agent's last reply, and the spec and plan the agent wrote are files you have to go and find in the worktree.

This feature makes the run visible where the workstream already lives: the right sidebar shows a timeline of the run and links to the spec and plan written in that workstream.

The behavioural reference is a personal Claude Code mod, `~/.claude/mods/pipeline` (`hooks/model.ts`, `hooks/register.tsx`, `hooks/view.ts`, and its tests). It draws the same timeline as a pane inside Claude Code. Muxteroid does not use that mod, but its rules are the ones to port. They are restated below so this document stands alone.

## What was decided, and by whom

Decided by the owner:

- The pipeline is a **timeline for the selected workstream**, not an overview across workstreams.
- The sidebar carries **links to specs and plans**. It does not render them inline.
- Specs and plans appear in the **right sidebar** (the inspector).

Everything else in this document is the author's call (Claude), accepted by the owner in outline. Each is marked "Call" where it first appears.

## Goals

1. The inspector shows a Pipeline section for a workstream that has Superpowers activity: phases in order, per-task progress, clock times, estimates for what is still to come, and a marker when the run is waiting on the person.
2. The section links to the specs and plans written in that workstream. One click opens the document, readable, without leaving Muxteroid.
3. It works with whatever Superpowers installation the person already has, in Claude Code and, more coarsely, in Codex.

## Non-goals

- No view across workstreams: nothing new in the left sidebar or the sprint board.
- Muxteroid does not install, bundle, load or configure Superpowers.
- No token or cost accounting. (The mod shows tokens; Claude's command hooks do not report usage.)
- No editing of specs or plans in the app.
- No dependence on the personal `pipeline` mod or its store file (`~/.claude/plugins/store/pipeline_inline-*.json`). Muxteroid is a public app and that mod is not shipped with it.

## Anti-goals

- A workstream that never uses Superpowers must look exactly as it does today. No empty Pipeline section, no hint, no prompt to install anything.
- The three-second refresh must not get noticeably slower, and must never wait on the network.
- Old specs and plans that were already in the repository when the workstream started must not show up as this workstream's documents.

## What the person sees

```
┌ Pipeline ──────────────── 42m in ┐
│ about 1h 10m to go → ≈ 15:14     │
│                                  │
│ 13:02   ● brainstorm             │
│ 13:20   ● plan        6 tasks    │
│         ◐ tasks       2 of 6     │
│ 13:41     ● 1 Bridge module  ■■  │
│ 13:58     ● 2 Hook events    ■■  │
│ ≈14:10    ◐ 3 Timeline view  ■□  │
│           ○ 4 …                  │
│ ≈14:50  ○ final review           │
│ ≈14:53  ○ verify                 │
│ ≈14:56  ○ PR                     │
│ ≈15:11  ○ CI                     │
│ ≈15:14  ○ merged                 │
│                                  │
│ Spec  Superpowers pipeline  ↗    │
│ Plan  Superpowers pipeline  ↗    │
├ Tasks ────────────── 3 of 5 done ┤
│ (Beads tasks and reply, as now)  │
```

- **Placement (Call).** The Pipeline section sits at the top of the inspector, above Tasks, and can be collapsed; the collapsed state is remembered. Its header has a context menu with Clear Pipeline (see "When a run ends or goes quiet"). The footer (state, folder, branch, Mark complete) stays fixed as it is now. The mockup fixes the order; whether the Tasks header scrolls with the content is left to the builder.
- **Header.** Run name (derived from the plan or spec file name), time since the run started, and one summary line: "about N to go → merged ≈ HH:MM" while there is an estimate, "No estimate yet" when there is none, "merged HH:MM" or "PR closed without merging" at the end. When subagents are working, say how many.
- **Phase rows.** One per phase that was not skipped: status mark, label, the time it finished (or `≈` an estimated finish), and a short detail: the task count for plan, `#123` for PR (a link to the PR), the check summary for CI ("5 of 12 checks", "2 of 12 failing", "12 passed").
- **Task rows** under the tasks phase: number, title, two boxes for implement and review, the fix round (`r2`) while a task is in a fix loop, and its time.
- **Marks:** done, running, slow (running well past its estimate), failed, to come, and needs you. Use the app's existing state colours (`stateColor`) where they fit; "needs you" is orange like elsewhere in the app.
- **Needs you.** The active phase is marked "needs you" when the workstream is waiting on the person (its state is Needs you or Review), no subagent is working, CI is not what is running, and the run is not finished.
- **Documents.** Below the timeline, one row per spec or plan that belongs to this workstream, newest first: kind (Spec or Plan), title (the file's first `# ` heading, else its name without date and `.md`), and a control that opens it.
- **Opening a document (Call).** It opens in its own Muxteroid window, rendered as Markdown with the MarkdownUI package the app already uses, with buttons for Open in Editor (the system default for the file) and Reveal in Finder. The window picks up changes to the file while it is open. A missing file says so instead of showing an empty window.
- **Times** follow the Mac's 12/24-hour setting.
- The section and its rows have accessibility labels in words ("Plan, done at 13:20, 6 tasks"), as other controls in the app do.

## How the run is tracked

**Call: the bridge owns the state.** A new Python module (`Sources/pipeline.py`) holds the run model, applies events, scans files, and computes the schedule. The Swift side only formats and lays out. This follows the existing split and keeps the logic under the existing Python tests.

One run per **workstream** (the Beads parent), not per terminal: every terminal in a stream shares the worktree, so their events feed the same run. A run stays on screen until a new one replaces it.

### Phases

In order: `brainstorm`, `plan`, `execute` (shown as "tasks"), `final-review`, `verify`, `pr`, `ci`, `merge` (shown as "merged"). Each has a status (`todo`, `running`, `done`, `failed`, `skipped`) and start and end times.

Advancing to a phase: do nothing if any later phase has already started; otherwise earlier phases that were running or failed become done, earlier phases still to do become skipped, and the phase becomes running. Skipped phases are not drawn.

### Events from Claude

Muxteroid already registers a `PostToolUse` command hook with no matcher for every Claude terminal (`streams.settings`, handled in `streams.hook`), so successful Skill, Bash, Write and Edit calls already arrive there. Events that carry an `agent_id` come from subagents and are ignored for the pipeline, as the mod does. The payload shapes were captured from Claude Code on 2026-11-10 and are recorded in the plan.

- **Skill** (`tool_input.skill`, with or without the `superpowers:` prefix for the first three):
  - `brainstorming` → brainstorm. `writing-plans` → plan. `subagent-driven-development` or `executing-plans` → execute.
  - These start a **new run** when there is none, when the current one is finished (merged or closed), or when the skill is brainstorming or writing-plans and the current run has already started executing.
  - `superpowers:requesting-code-review` → final-review, only once every task is done.
  - `superpowers:verification-before-completion` → verify, only once every task is done.
  - `superpowers:finishing-a-development-branch` → pr, only once execute has started.
- **Write / Edit** (`tool_input.file_path`):
  - `…/docs/superpowers/specs/*.md` → the file is this workstream's document, and the run's spec; the run is named after it until there is a plan.
  - `…/docs/superpowers/plans/*.md` → the file is this workstream's document, and the run's plan: parse its tasks, advance to plan, name the run after the plan.
  - Whether such a write starts a new run follows the rule under "Events from files". Only a Write that creates the file counts as a new document. An Edit, or a Write over an existing file, lists the document but never starts a run, so editing a spec that was already on the branch cannot replace a live run.
- **Bash** (`tool_input.command`, output from `tool_response`):
  - `sdd-workspace` in the command and a `…/.superpowers/sdd/<slug>` path in the output → that is the run's workspace. Read its `plan-path` marker (repo-relative or absolute), parse that plan, advance to execute. If it names a different plan than the current run and that run has started executing, start a new run.
  - `task-brief <plan> N` → task N starts (implement running) and becomes the current task.
  - `review-package` → the current task's implement is done and its review is running. If there is no current unfinished task and every task is done, advance to final-review.
  - `gh pr create` with a `/pull/N` URL in the output → pr done, ci running, remember the PR (URL and number).
  - `gh pr merge` without `--auto`, naming this PR or no number → merge done.
- **Call:** a successful GitHub MCP `create_pull_request` counts the same as `gh pr create` when its result contains a PR URL.

Plan tasks are headings outside code fences that match the mod's pattern `^#{2,4}\s+Task\s+(\d+)\s*[:.–—-]?\s*(.*)$`: two to four `#`, then `Task N`, then an optional colon, full stop, hyphen or dash, then the title. `*`, `_` and backticks are stripped from the title, an empty title becomes "Task N", and a repeated number is ignored.

### Events from files

These run during the regular `list` refresh and must stay at the cost of a few `stat` calls unless something changed.

- **Ledger.** `<workspace>/progress.md`, where the workspace is the one recorded on the run or `<root>/.superpowers/sdd/<plan file name without .md>` if its `plan-path` marker exists. Re-read only when its modification time changes. `Task N: complete` marks the task done; `Task N: fix round R` records the round. When every task is done and execute is running, execute is done.
- **Documents.** See "Which documents belong to a workstream". A spec or plan found by the scan is applied to the run by the same rule as a hook-observed write (Call):
  - A document **not seen before** in this workstream starts a new run when there is no run, when the current run is finished, or when the current run has started executing and the document is not its own spec or plan. It is then applied: a spec gives a running brainstorm, a plan moves the run to plan with its tasks.
  - A document **seen before** is applied only if it is the current run's plan (its tasks are re-read). Editing an older document never starts a run.
  - When a scan finds several new documents, they are applied oldest first.

  This is what gives a Codex-only workstream a timeline from files alone, and a second timeline after the first run is merged.

### Events from Codex

Codex does not report skill use. `codex_session.observe` already hands each finished turn's successful shell commands to `links.tool_result`; hand the **root thread's** commands to the pipeline's Bash handling too. Commands from child threads are subagents' and are ignored, as for Claude. Combined with the file scan this gives Codex the documents, the task progress, and PR detection, but phase changes arrive at the end of a turn and review/verify may be skipped over. That coarseness is accepted.

### PR and CI

**Call:** a separate bridge action (for example `pipeline-poll`) asks `gh pr view <PR URL> --json state,statusCheckRollup` for each unfinished run that has a PR, and the app calls it about once a minute. It must not take `bridge.lock` and must not run inside `list`. If `gh` is missing, not signed in, or fails, the run is left unchanged and nothing is shown to the person.

- `MERGED` → merge done. `CLOSED` → merge failed.
- Otherwise classify the checks: any failing → ci failed, "k of n failing". Any pending, or no checks yet → ci running, "x of n checks" or "waiting for checks" (a previously failed ci starts over). All passed → ci done, merge running, "n passed".
- A check counts as passed when its conclusion or state is `SUCCESS`, `SKIPPED` or `NEUTRAL`; pending when its status is not `COMPLETED` or its verdict is `PENDING`, `EXPECTED` or absent; failed otherwise.

### Estimates

- A task is estimated at the average of the tasks finished so far in this run (implement start to review end), or 12 minutes before any has finished. Implement is taken as 60% of a task for judging slowness.
- Fixed estimates: final review 10 min, verify 3, PR 3, CI 15, merge 3.
- Brainstorm and plan wait on the person and carry no estimate. Until the plan lists tasks there is no finish estimate.
- A running step is "slow" once it has run more than 1.5 times its estimate.
- Remaining steps are laid end to end from now; a running step ends no earlier than a minute from now.

### Runs without per-task signals

Per-task progress comes only from the subagent-driven flow's `sdd-workspace`, `task-brief` and `review-package` commands and its `progress.md` ledger (Superpowers 7). Inline `executing-plans`, older versions, and Codex without a ledger do not produce them. For those runs (Call):

- A task is inferred to be the active one only when the run has a workspace or some task has already started. Otherwise every task row shows as to come, the tasks phase shows as running and is never slow, and there is no finish estimate; the summary line reads "No estimate yet".
- The run still moves on: `finishing-a-development-branch`, `gh pr create` and the PR checks advance it without waiting for tasks. Final review and verify stay gated on every task being done, so they are skipped in these runs.
- Once the tasks phase is done, tasks that were never individually completed are drawn as done, without times.

### When a run ends or goes quiet

**Call:** the mod treats a run older than 24 hours as stale and ignores further events. Muxteroid does not: a workstream can run for days, and the run belongs to the workstream, so it stays live until it is merged, closed, replaced, or cleared.

A run can stop short of merged: a branch finished without a PR, an abandoned brainstorm, a PR left open. Two things bound that (Call):

- **Mark complete freezes the run.** For a completed workstream the timeline shows what happened and nothing else: no needs-you or slow marks, no estimates, and its PR is no longer polled. Completed workstreams are not scanned for documents and never get a new run. Reopening the workstream brings the run back to life.
- **Clear Pipeline.** A context-menu item on the section header removes the workstream's run (the app's version of the mod's `/pipeline reset`). The documents stay listed; the timeline returns when a new run starts.

## Which documents belong to a workstream

**Call:** only documents created in this workstream. A file under `docs/superpowers/specs` or `docs/superpowers/plans` belongs if either holds:

1. A Write or Edit to it was observed from one of the workstream's Claude terminals (any path, also outside the scanned directories' root).
2. A scan found it **untracked by Git** (ignored files count; so does any file when a directly used folder is not a Git repository), and, for a workstream that uses a folder directly instead of its own worktree, modified after the workstream was created. If Git cannot answer (a timeout, a missing `git`), the scan adopts nothing and tries again on the next refresh.

Once a document belongs it stays listed for as long as the file exists, including after it is committed.

Untracked is the test on purpose. Comparing against the commit the branch started from would also pick up other people's specs after a merge or rebase from the main branch, and a pull in a shared checkout would do the same to a modification-time test; neither produces untracked files. The cost is accepted: a document that a Codex terminal both creates and commits while Muxteroid is not running is missed. Workstreams that share one plain folder also share its new documents.

Scan from the worktree root (`worktreeRoot` in the stream's metadata), not from `folder`, because a workstream can live in a subfolder; fall back to `folder` when there is no worktree. Rescan only when the listing or modification times of the two directories' Markdown files change, and drop documents whose files are gone.

## Data handed to the app

Each stream in the `list` output gains two fields: the pipeline (absent when there is no run) and the documents. The exact shape is the builder's, within these rules:

- The bridge sends the computed schedule: per phase and per task, a mark, a time and whether that time is an estimate, plus the header facts (name, start, estimated finish, slow-case finish if used, PR, check summary, needs-you, active subagents).
- Times are sent as timestamps; the app formats them.
- Documents are sent as kind, title and absolute path. **No document bodies in `list`.** The document window reads the file itself.

## Constraints

- The bridge runs under `/usr/bin/python3`, currently 3.9, standard library only. No `match`, no `X | Y` annotations evaluated at runtime.
- `list` runs every three seconds while holding `bridge.lock`. No network and no subprocess per refresh unless something changed.
- Hooks run concurrently with `list` and with each other, and do not take `bridge.lock`. Every read-modify-write of a run must be atomic in SQLite (for example `BEGIN IMMEDIATE`), or events will be lost.
- A failure anywhere in the pipeline code must not take existing behaviour with it: the terminal's state update and link collection in the hook, and the whole `list` answer, survive any pipeline error. The pipeline calls are isolated and their errors logged.
- State lives in `activity.sqlite` beside the existing tables, created on demand like the others, so existing installations upgrade without a migration step.
- Terminals that were started before this version keep working. Claude terminals call the bridge script by path on every hook, so they pick up the new behaviour as soon as the bundle is rebuilt in place. A running Codex terminal is a long-lived bridge process with the old code loaded: until it is restarted it feeds no commands to the pipeline, and only the file scan reaches it.
- A new Python file must be added to both the copy list in `build.sh` and the `exclude` list in `Package.swift`.
- The README documents every feature in its "Working across streams" list; add this one, in the same voice.

## Testing

- Python unit tests in `Tests/` in the existing style (`MUXTEROID_HOME` pointed at a temporary directory, `bd` and terminals patched). Cover: phase transitions and the new-run rules; plan parsing; Skill, Write, Bash events with realistic payloads; subagent events ignored; the ledger; the PR view classification; estimates, including a run without task signals; document ownership for a worktree stream (tracked files from a merge excluded) and a plain folder; a files-only (Codex) run and its second run; a completed workstream; clearing a run; concurrent updates not losing an event. The mod's `tests/model.test.ts` is a good source of cases.
- `swift build` passes and the app is checked by hand against a scratch `MUXTEROID_HOME`, not the live data directory: no section for a plain workstream; a section that fills in as a run proceeds; Clear Pipeline; a document window that opens and follows edits.

## Left to the builder

- Module and table layout, the JSON field names, and how the app schedules the once-a-minute poll.
- The exact visual treatment of marks, spacing and truncation in a 218–410 pt wide inspector.
- Whether the timeline rows for brainstorm and plan also act as links to their documents, in addition to the document rows.
- How the document window notices file changes.
