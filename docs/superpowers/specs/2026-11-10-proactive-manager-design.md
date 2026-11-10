# Proactive manager: design

Date: 2026-11-10. Status: draft for the owner's review.

This builds on the workstream manager (`2026-11-10-workstream-manager-design.md`), which is built and shipped. Read that first. Everything it says still holds unless this document changes it.

## Why

The owner finds the manager underwhelming: "Atm it's just a chat with useful context." What they had in mind is a manager that is present across the app, speaks up on its own, and learns how they work.

Their concrete pain was not knowing what is going on. They opened a workstream and the Tasks pane showed one task in progress and nothing planned, which looked wrong. The terminal said Claude was running something, but not what. They could not tell whether the stream was fine, stuck, or waiting for them.

## What the owner decided

- **Three things the manager should say without being asked:** status updates, what to pick up next, and reminders about things the owner forgot.
- **Bite-sized only. No walls of text.** This is the most important rule in this document.
- **Where it speaks:** a fixed slot at the bottom of the inspector (the right-hand Tasks pane), with action buttons. The owner chose this over a floating card and a title-bar line because it never shifts or covers content. Messages the owner has not acted on also collect under **Manager** in the left sidebar.
- **Learning:** the manager notices instructions the owner repeats in a project and offers to make them standing rules. The owner's example is the definition of done for Muxteroid: push to main, rebuild the app, reinstall it, and relaunch it if needed. The owner keeps asking for this at the end of every task.
- **Planned steps:** agents write their planned steps into Beads before they start, and the manager asks once if an agent has been working with nothing planned. (The owner first asked to show Claude's own to-do list in the Tasks pane. That list turned out to be unused: across 90 Claude sessions in three days, the built-in to-do tool was called zero times, because agents are told to track their steps in Beads.)
- **Not now:** the manager rearranging the interface itself, such as reordering the sidebar or choosing which stream to open. Maybe later.

## Goals

1. Looking at any workstream, the owner can tell within a second what it is doing right now and what comes next.
2. The manager tells the owner, in one short line, the thing worth knowing about each workstream. It offers the next action as a button.
3. A repeated wrap-up instruction becomes a standing rule after one click, and agents follow it without being told again.

## Non-goals

- No change to how the manager reviews permissions, questions, plans, and stops, apart from the rules below.
- No floating cards, pop-ups, or new sounds. The existing macOS notifications stay as they are.
- No interface tailoring (see above).
- No Codex-specific work beyond what already comes for free. Codex terminals have no hooks; see "Now line".

## Anti-goals

- **A wall of text.** Anything in the slot that needs more than one line has failed.
- **Content that jumps.** A message arriving, changing, or going away must not move anything else in the inspector.
- **A nag.** The same message must not come back after the owner said **Later** unless something changed.
- **A manager that does the work.** Buttons that do something send a message to an agent or open something. The manager still never runs commands or changes files itself.

## What the owner sees

### The inspector's bottom section

Today the bottom of the inspector shows the stream state ("● Working"), the folder, the branch, and **Mark complete**. It becomes:

```
┌──────────────────────────────────┐
│ ✦ "Unblock PR CI" is green.      │  ← manager slot: one line
│   Merge it?                      │
│   [Merge] [Open] [Later]         │  ← up to three buttons
├──────────────────────────────────┤
│ ● Working · Running Android      │  ← Now line
│   tests · 3m                     │
│ 📁 via-monorepo  ⎇ fix/…         │
│ [Mark complete]                  │
└──────────────────────────────────┘
```

The slot keeps the same height whether it holds a message or not, so nothing above or below it moves. When the manager has nothing to say about the stream, the slot shows its last status line in grey, or "Nothing from the manager" if it has none.

The slot shows one message at a time, the most important first:

1. An open question for the owner (today these sit above the task list and push it down; they move here).
2. A reminder.
3. A suggestion for what to do next.
4. A status line.

If the stream has more than one message, a small "1 of 3" with arrows lets the owner page through them.

### The Now line

The state line gains what the agent is doing right now, and for how long: "● Working · Running Android tests · 3m". It comes from the hook that fires each time Claude starts a tool. That hook already reaches the app for every tool; today it is used only for questions and plans. For a shell command the line uses the command's own description, which Claude writes for every command. For other tools it uses a short plain phrase: "Editing bottom-sheet.tsx", "Reading 3 files", "Searching the web". The clock restarts with each tool. When Claude ends its turn the line goes back to today's text.

For subagents, the line names the subagent: "Working · subagent: reviewing the plan · 2m".

Codex terminals have no hooks. If the app's existing Codex connection already reports the running command, use it; otherwise Codex terminals keep today's state line.

### Manager in the sidebar

The **Manager** entry's badge today counts open questions. It now counts unread open questions, reminders, next messages, and rule proposals. Status lines never count, so the badge does not sit at the number of active workstreams. A message is read once it has been shown in the slot while Muxteroid is the active app, or once the owner acts on it. (Call.) The Manager view gains a list of every current message across workstreams, newest first, with the same buttons. This is where reminders wait if the owner never opens the stream.

## What the manager says, and when

Messages come from two consults the manager already makes, plus two new sweep triggers. No consult is added per agent turn.

- **Stop review (per turn, Claude).** Each time a Claude agent ends its turn, the manager already reviews it. Its reply gains that workstream's status and next messages. This is the main source, because the existing sweep almost never runs for workstreams with only live Claude terminals: its change check (`manager.signature()`) covers only terminals the hooks cannot see. (Call: reuse the Stop review rather than widen the sweep's trigger, which would mean a full consult over every workstream up to every two minutes. The owner can override this if messages feel stale.)
- **Sweep (all workstreams).** Its reply gains messages too, for every kind and every workstream. It is the only source of reminders and of messages about Codex and stopped terminals.

Each message has:

- the workstream (and terminal, if it is about one)
- its kind: status, next, or reminder
- one line of text, at most 100 characters (the app cuts anything longer and logs that it did)
- up to three buttons, each a label of one or two words and an action (below)

What each kind is for:

- **Status:** what moved since the owner last looked. "Tests pass. ~5 min left. Nothing needed from you."
- **Next:** the obvious next step when a stream finishes or goes quiet. "PR is green. Merge it?" "Nothing running here; 2 tasks are ready."
- **Reminder:** something left hanging. A stream untouched for a day with open tasks, a PR waiting on the owner's review, a question an agent asked that nobody answered.

When it speaks:

- **When an agent ends a turn**, through the Stop review.
- **When something hooks cannot see changes**, through the existing sweep trigger.
- **When the owner comes back.** If Muxteroid becomes the active app after 30 minutes or more away, it runs one sweep so the lines are fresh. (Call.)
- **Hourly.** Reminders depend on how long something has waited, which no event reports. While the app is open and any workstream is not Done, a sweep runs at least once an hour, whether or not the change check fired. (Call.)

"PR is green" needs PR check status. The Stop review and sweep may look it up with the read-only `gh` commands they already have; the app does not add PR polling for this. (Call.)

When paused, the manager says nothing new. The Now line and the planned-steps rule still work, since they are app features, not manager consults.

### Buttons

A button does one of these:

- **Tell:** types a message into one of the stream's Claude or Codex terminals, using the existing tell mechanism. **Merge** is a Tell: "The PR is green. Merge it." The agent does the merge, not the manager. Because the owner pressed the button, it is treated like a tell the owner asked for in chat: it may type into the terminal on screen. It still types only into an idle agent (Review or Ready). While the agent is busy the button is disabled, with "Agent is busy" on hover. (Call.)
- **Open:** selects the stream, a terminal, or a link (PR, issue).
- **Later:** hides this message. A later message with different text shows as normal; one with the same text stays hidden. (Call. There is no per-workstream change signature to compare against, and this avoids inventing one.)
- **Save** / **No:** for a proposed rule (below).
- **Details:** opens the Manager view at this message, for open questions whose full text does not fit.

Open questions in the slot show a one-line form: the decision, cut to 100 characters, with **Open** (selects the terminal) and **Details**. Their options and recommendation stay in the Manager view and are not buttons. The terminal behind a question usually has a dialog on screen, and typing into it is unsafe. Muxteroid has no answer flow today and this design does not add one. (Call.)

The manager chooses buttons from this list only. Every button press goes into the activity log.

## Learning standing rules

### Noticing

On each prompt the owner sends to an agent, the app records the prompt's text (first 500 characters) with the project it belongs to. A project is the repository, so all worktrees of one repository count as one. Today only the time of the prompt is kept.

The sweep's context includes the owner's last 30 prompts for each project with an active stream. When the manager sees the same kind of instruction two or three times, it proposes a rule as a message in the slot:

> You always finish with push → rebuild → reinstall. Make that "done" for Muxteroid? **[Save] [No]**

A proposal carries two texts: the one-line message, and the full rule as it would be written, a few lines at most. **Save** does not save straight away. It opens a small popover beside the slot showing the full rule, with **Save** and **Cancel**. Only text the owner has seen is written. This is the one place the slot opens more than a line, because the rule goes to every agent in the project and can include pushing to main. (Call.)

A proposal shows in one workstream only: the one in that repository where the owner sent a prompt most recently.

**No** means the manager does not propose that rule again. (Call: remembered as a note so the manager sees it.)

### Keeping rules

Saved rules live in one Markdown file per project in the manager's folder, `manager/projects/<repository name>.md`, which the owner can read and edit, like `notes.md`. The first rule kind is the definition of done. The format is plain prose under headings, not a schema, so the owner can write rules by hand too. (Call.)

### Following rules

- **Agents are told.** The context Muxteroid already gives every Claude session at start and on each prompt includes the project's rules: "In this project, done means: …".
- **The manager checks.** When an agent ends its turn and says it is finished, the Stop review gets the project's rules. If a step is missing, the manager sends the agent back to do it, inside the existing limit of three in a row.

Rules can include pushing to main. That is safe only because the owner wrote or saved the rule. The manager never proposes a rule that the owner has not asked for at least twice in their own words.

## Planned steps

- **Instruction.** The instructions Muxteroid gives every agent already say to create Beads steps for implementation work. They change to say: write your planned steps as Beads tasks before your first edit, and keep them current.
- **One reminder.** If a Claude terminal has been working for more than 5 minutes and its stream has at most one unfinished task, the app adds one line to what Claude sees after its next tool: "Write your planned steps as Beads tasks so the owner can see them." This uses the extra context that the after-tool hook can return to Claude. It happens at most once per terminal per prompt from the owner. This is an app rule, not a manager consult, so it costs nothing. (Call: 5 minutes and "at most one".)

The builder must check that the after-tool hook's extra context reaches Claude in the current Claude Code version before relying on it. If it does not, the reminder moves to the Stop review instead.

## Limits

- Fail closed, as before: a failed or malformed sweep produces no messages and leaves the old ones in place.
- At most one message of each kind per workstream at a time. A new message of the same kind replaces the old one.
- Messages are text, not Markdown, in the slot. Markdown stays for the briefing and chat.
- The sweep's message list is checked like its actions: unknown workstreams, terminals, or button actions are dropped.

## Where things go

- `manager.py`: Stop-review and sweep prompts and reply handling for messages, including a short form for asks; the come-back and hourly triggers; project rules in Stop reviews and agent context; the planned-steps reminder.
- `manager_store.py`: a table for messages (with read, later, and dismissed state) and a table for prompt text per project.
- `manager_role.md`: the bite-sized rule, the three kinds of message, and rule proposals. It stays a role, not a rulebook.
- `streams.py`: record what the current tool is for the Now line; add project rules to `agent_context`; change the planned-steps instruction.
- `WorkstreamInspector.swift`: the fixed slot and the Now line in the bottom section; open questions move from the top into the slot.
- `ManagerView.swift` and the sidebar: the message list and the unread count.

## Testing

- Python unit tests, with the `claude` call stubbed: sweep messages are stored, validated, cut to length, and replaced by kind; Later hides until the signature changes; button actions map to the right effect and respect the tell rules; prompts are recorded per repository; project rules reach agent context and Stop reviews; the planned-steps reminder fires once and only under its conditions; paused means no new messages.
- The Now line's phrase for each common tool.
- Build the app and check the slot in the offscreen render harness: empty, one message, three messages, an open question, and a long message cut to one line. Check that the inspector's content does not move between these states.

## Left to the builder

- Table layouts, JSON shapes, file names.
- The exact phrases for each tool in the Now line.
- The slot's visual design, consistent with the inspector and the Pipeline section. It must hold one 100-character line plus a row of buttons at the inspector's normal width without changing height.
- How the prompt history and project rules are trimmed to fit a consult.
- The wording of the role prompt changes.
