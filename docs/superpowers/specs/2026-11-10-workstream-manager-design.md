# Workstream manager: design

Date: 2026-11-10. Status: awaiting approval.

## Why

Agents in Muxteroid stop and wait for the user far more often than they need to. Three cases the user named:

- An agent marks a task blocked, and the blocker turns out to be a small decision.
- An agent waits for information it could have looked up.
- An agent waits when it could have moved to another task.

The user also finds Claude Code's built-in auto-mode classifier too cautious about permissions.

The manager is an intelligence that sits above all workstreams, like a team manager. It keeps work moving without the user, and gives the user a clear picture of what is left, what is blocked, and what needs them.

## What the user decided

These are the user's words or direct decisions. Do not soften them.

- The manager sees all workstreams and all tasks.
- It runs on Claude, model Fable 5.2 at high effort, through the `claude` CLI on the user's subscription. No API key.
- Auto mode's classifier stays the default for permissions. The manager gives a second opinion only when auto mode rejects an action. If that proves technically too difficult, permission approval is dropped from this feature.
- It works from a role, not a rulebook: "like a person who cares for the project to carry on and values the time of its boss."
- It takes product decisions itself, because they are reversible. The user wants to learn of them afterwards.
- It asks the user only about things that are dangerous, irreversible, and have no recommended decision.

One consequence of the last rule, stated so the user can veto it: if something is dangerous and irreversible but the manager has a clear recommendation, it proceeds and reports.

## Goals

1. An agent that stops, asks a question, or hits a permission wall gets an answer from the manager within about a minute, without the user.
2. The user can see, in one place: what is left, what is blocked, what needs them, and every decision the manager made for them.
3. The user can talk to the manager and direct work through it.
4. When the manager does need the user, the question arrives boiled down: the decision, the options, and a recommendation if it has one.

## Non-goals (first version)

- The manager does not edit project files or run project commands. It directs agents.
- It does not create workstreams or start new agents. It may restart a stopped terminal in an existing workstream.
- It does not mark a workstream complete. That stays the user's action.
- It does not answer Codex approval prompts. See "Codex" below.
- No per-workstream on/off switch. One global pause.

## Anti-goals

- **A silent manager.** Every action it takes is recorded with its reason. Nothing it decides is invisible.
- **A manager that breaks the normal flow when it fails.** If it errors, times out, runs out of quota, or returns nonsense, the session behaves exactly as it does today: the prompt appears and the user is asked.
- **A nag.** It must not push an agent in circles or interrupt a conversation the user is having with an agent.
- **A second classifier.** A decision table of fixed outcomes would miss the point. The role prompt describes a person and their priorities.

## How it works

### One brain, several triggers

A single function consults the manager: it assembles context, runs `claude -p` once, and gets back a structured decision. The app carries the decision out. The model never acts directly.

Each consult receives:

- the trigger (what just happened, with the exact request)
- the workstream: title, folder, linked PRs and issues, every task with status and blocker text
- the recent conversation of the terminal involved
- the other terminals in the stream, and a one-line roster of the other workstreams
- what the manager already did in this stream recently
- the manager's notes (below)

Everything written by an agent (requests, blocker text, transcripts) is passed as data, marked as untrusted, and never as instructions.

The manager may investigate before deciding, with read-only tools: reading and searching files, and the web. For Stop reviews, sweeps and chat it also gets a short allowlist of read-only commands (`git` log/diff/status/show, `gh` view/list, `bd` show/list/ready). It has no write tools.

Verified by test call: `claude -p --safe-mode --model fable --effort high --output-format json --json-schema …` works on the subscription login and returned a structured decision in 18 seconds.

### Triggers for Claude sessions (through hooks)

These run inside the hook that Claude Code already calls, so they work even when Muxteroid is closed.

| What happens | Hook | What the manager can return |
|---|---|---|
| Claude shows a permission prompt (in auto mode, after repeated blocks) | `PermissionRequest` | allow; deny, with a message suggesting another route; hand to the user |
| Auto mode's classifier blocks an action | `PermissionDenied` | approve a retry; uphold the block; hand to the user |
| Claude asks a question | `PreToolUse` on `AskUserQuestion` | answer it; hand to the user |
| Claude presents a plan | `PreToolUse` on `ExitPlanMode` | approve; send back with feedback; hand to the user |
| Claude ends its turn | `Stop` | let it stop; tell it to continue, with a message; hand to the user |

The Stop review is where the user's three cases are handled. The manager sees the agent's last message, the open and blocked tasks, and what is ready. It can make the small decision, point the agent at the missing information, or send it to another task. Its message becomes the reason Claude continues.

While the manager is reviewing, the terminal shows as working, with a line such as "The manager is reviewing a permission request for Bash." No notification fires. Only if the manager hands the matter to the user does the terminal move to **Needs you**, with the manager's summary as the text. The existing notification then carries that summary.

Permission and question events from subagents are reviewed too. Subagent stops are not.

**Unproven: overriding the classifier.** The user's sessions run in auto mode. Documentation says a `PermissionDenied` hook can only tell Claude it may retry; it cannot reverse the block. The design is: when the manager approves, record that exact action as pre-approved for a few minutes, and return "retry". A `PreToolUse` hook then allows the retried action when it matches. Whether a `PreToolUse` allow takes effect before the classifier is not documented, and three attempts to provoke a classifier block in a test folder failed (it allowed everything the prompt asked for). The plan must verify this first, by logging a real block in one of the user's own sessions. Do not try to provoke blocks with destructive test prompts: one such attempt during design deleted real cache files.

If the `PreToolUse` allow does not take effect, there is a second route that uses only documented behavior. After three blocks in a row, auto mode shows a normal prompt. `PermissionRequest` fires for it, and the hook allows the pre-approved action. This costs two wasted retries. If neither route works, the retry path is dropped, as the user allowed. Sessions are never moved out of auto mode.

### When the user is working with an agent themselves

Whether to step in is the manager's judgment, not an app rule (builder's call, following the user's wish for a less prescriptive manager). Each consult is told who prompted the agent last and how long ago, and the role prompt says: if the boss is in a conversation with this agent, an answered question is a finished turn, and a question the agent asks is theirs to answer.

One app rule does apply. A sweep never types into the terminal the user currently has selected while Muxteroid is frontmost, so a manager message cannot land in a prompt the user is halfway through writing. Messages the user asks for in chat are typed regardless.

### Triggers that need the app open

- **Sweep.** Some situations fire no hook: a Codex terminal that stopped or asked something, an idle agent whose blocker has since cleared, a stopped terminal with ready work. The app notices when this set changes and runs one consult over all workstreams. The manager returns a list of actions (tell a terminal something, restart a terminal) and a fresh briefing.
- **Chat.** The user writes to the manager. It answers and may act: tell an agent something, record a preference. Chat keeps one continuing conversation.

The manager tells an idle agent something by typing into its terminal. Verified for Claude: a multi-line pasted message followed by Enter arrives as a normal prompt. The app only types when the terminal is idle or working, never when a dialog is open.

### Codex

Codex sessions have no hooks. In the first version the manager sees Codex workstreams, includes them in the briefing, and may nudge an idle Codex terminal by typing. Typing into the Codex terminal is not yet verified; if it proves unreliable, nudging Codex is dropped from this version. Codex approval prompts stay with the user.

### What the user sees

A **Manager** entry at the top of the sidebar, above Sprint board, with a badge counting open questions for the user. Its view has four parts:

1. **Briefing.** A short written summary across all workstreams: what is left, what is blocked, what needs the user. Refreshed by sweeps, and on demand.
2. **Needs you.** Each open question: the decision, the options, the recommendation, and a button to open the workstream.
3. **Decisions and activity.** Everything the manager did, newest first, each with its reason: permissions approved or denied, questions answered, product decisions taken, agents redirected. Unread items are marked. This is how the user learns of decisions after the fact.
4. **Chat.** A box to talk to the manager.

A pause switch sits in the view's header. Paused, every hook behaves as it does today.

In a workstream's inspector, an open question from the manager appears above the task list.

### Memory

The manager keeps a notes file (`manager/notes.md` in Muxteroid's data folder): the user's preferences and standing instructions. It adds a note when the user states a preference in chat. The user can edit the file. Notes are included in every consult.

## Limits built into the app, not left to judgment

- **Fail closed.** Any error, timeout, or malformed reply produces no decision.
- **One-off approvals only.** The manager never grants standing "always allow" rules or changes a session's permission mode.
- **Explicit hook timeouts**: 180 seconds for permission and question reviews, 300 for Stop.
- **Loop guard.** At most three consecutive "continue" verdicts per terminal without a user prompt in between. After that the agent stops and the matter goes to the user.
- **Rate guard.** At most 30 consults per terminal per hour. Beyond that, behavior falls back to today's.
- **No locks held.** A consult never runs while holding the bridge lock or the app's busy flag.

## Costs the user accepts

- Each consult takes roughly 15 to 60 seconds and draws on the same subscription quota the usage bar shows.
- Every time a Claude agent ends a turn, the Stop review adds that delay before the terminal shows as ready for review. The user can keep typing meanwhile.
- Sessions already running keep their old hooks until their terminal is restarted.

## Where things go

- New Python module beside `streams.py` for the consult, its context, storage, and the actuator. `streams.py` routes hooks to it and registers the extra hook events.
- New tables in `activity.sqlite`: the activity log, open questions, pre-approvals, chat, and settings (pause, briefing).
- The role prompt ships as a text file in the app bundle.
- New SwiftUI view and store for the Manager; small additions to the sidebar and inspector.
- Model and effort are configurable in a small config file, defaulting to Fable at high effort.

## Testing

- Python unit tests in the style of the existing ones, with the `claude` call stubbed: each verdict maps to the right hook output; failure of every kind produces no decision; paused means unchanged behavior; pre-approvals match only the exact action; the guards trip.
- A manual script that runs one real consult per trigger against a test folder.
- Build the app and check the Manager view against real workstreams.

## Left to the builder

- Exact table layouts, JSON shapes, and file names.
- The wording of the role prompt, provided it reads as a role and carries the user's decisions above.
- How much conversation to include in a consult, and how the briefing is laid out.
- How to keep the user's own plugins, hooks, and settings out of the manager's `claude` calls. `--safe-mode` mostly does this, but the test call showed one user setting still applied.
- The Manager view's visual design, consistent with Sprint board and the inspector.
