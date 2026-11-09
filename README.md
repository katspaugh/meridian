<p align="center"><img src="docs/images/icon.png" width="80" height="80" alt=""></p>
<h1 align="center">Muxteroid</h1>
<p align="center">A native Mac app for running Claude Code and Codex CLI workstreams at once.</p>

Each workstream has persistent terminals, a shared task list the agents maintain in Beads, and links to the pull requests and Linear issues it touches. Terminals can run Claude, Codex, or a plain shell in the same folder and worktree.

![Three workstreams in Muxteroid. Claude Code edits an Android layout in the selected terminal, with blocked and completed tasks on the right and Linear issue links below.](docs/images/workstreams.png)

## Try it on your Mac

You'll need macOS 15 or later, a Swift 6.2 or newer toolchain, Homebrew, and a signed-in [Claude Code](https://code.claude.com/docs/en/quickstart) or [Codex CLI](https://developers.openai.com/codex/cli/) installation. Codex support was tested with CLI 0.162.1 and requires app-server Unix sockets and remote TUI support.

```sh
brew install beads dtach
git clone https://github.com/katspaugh/muxteroid.git
cd muxteroid
./build.sh
open build/Muxteroid.app
```

Press **⌘N**, choose your agent and working folder, and click **Create**. Give the agent a task in the terminal. It names the stream from the goal and updates the title as the work becomes clearer. A Linear reference is optional.

For Git repositories, creating a workstream automatically creates a fresh `muxteroid/<id>` branch and worktree from the selected checkout’s current commit. A selected subfolder is preserved. Uncommitted changes stay in the original checkout. Both agents and any extra terminals share this one worktree. Folders outside Git are used directly; existing workstreams retain their current folders.

## Working across streams

- **Switch away while your agent works.** Each session runs in its own PTY. Switch streams or quit Muxteroid and the agent process keeps running. After a reboot, Muxteroid can resume from the saved conversation.
- **Open another terminal in a stream.** Use the **+** beside the terminal tabs to add a plain terminal, Claude, or Codex. Each terminal has its own session and status; all share the workstream’s folder, worktree, Beads tasks, and links. The app remembers your selected tab. Switching tabs keeps their output in place. After a reboot, use **Start terminal** on the tab to resume an agent or start a fresh shell.
- **Exit an agent or close a tab.** Exiting Claude or Codex with **Ctrl+D** leaves a plain shell in the same tab and worktree. **Ctrl+D** at an empty shell prompt closes that tab. **⌘W** closes the selected terminal and its session directly; you can also choose **Close Terminal** from the tab’s context menu. Closing the last tab keeps the workstream and tasks available, with **+** to open another terminal.
- **Quit the app.** **⌘Q** quits Muxteroid immediately without a confirmation, including while a terminal has focus. Running sessions, worktrees, and Beads tasks remain available when you reopen it. Terminal scrollback is not saved across app restarts.
- **See what's waiting on you.** Permission requests, questions, errors, and workstreams with blocked Beads tasks appear under **Needs you**. Blocked tasks show **Why it’s blocked** and **To unblock** in the inspector and sprint board, including the concrete action needed from you (or what you are waiting for). Older tasks retain their recorded notes and prompt you to ask the agent for the missing next step; these streams return to **Ready for review** once their blockers are cleared if the agent is still awaiting review. Allow notifications at launch to get a macOS alert when a workstream or subagent needs input. Click it to return to the workstream. Alerts run while Muxteroid is open, including in the background. Completed turns appear under **Ready for review**, with the agent’s formatted reply in the inspector. You decide when to mark the workstream complete.

  Terminal tabs identify the session needing input, and notifications select that terminal. The inspector shows the selected terminal’s reply alongside the shared tasks. A stream appears under **Ready for review** once no agent is working or awaiting input; an idle shell does not prevent it. Completion remains a workstream-level action.
- **Inspect delegated work.** Expand a workstream in the sidebar to see its Claude subagents. Select a subagent to switch the embedded terminal to Claude Code’s own agent view. Select the parent to return to the conversation. This requires Claude Code 2.1.300 or later and a workstream terminal started with this version of Muxteroid. Older sessions and agents Claude has unloaded open as transcript snapshots in a terminal pager.

  Muxteroid loads a local Claude mod in each workstream. On first use it creates a Claude Chat shortcut (`ctrl+x ctrl+y` → `command:muxteroid-view`) if no keybindings file exists. If you already have a keybindings file, add that binding to its Chat section or run `/muxteroid-view` after selecting an agent.
- **See the sprint board.** Click **Sprint board** in the sidebar or press **⇧⌘B** to see all workstreams and their Beads tasks grouped by status. Search streams or tasks, hide completed workstreams, and click a task or stream to open its terminal. The board updates as agents maintain their tasks.
- **Follow the plan.** Each agent uses [Beads](https://github.com/gastownhall/beads) to track subtasks, dependencies, and blockers under the workstream. The inspector shows progress. Press **⌥⌘I** to give the terminal the full width.
- **Check account usage.** The bottom bar shows Claude and Codex usage percentages and reset countdowns directly. Hover for window labels or click refresh. Muxteroid queries Claude Code for current limits, falling back to recent measurements if needed. If Claude shows “Sign in,” run `claude auth login` in your terminal, then refresh.
- **Jump to related work.** The bottom bar collects GitHub PRs and Linear issues from agent activity. The links stay with the stream when you reopen it.

## How it runs

Muxteroid uses SwiftUI and an embedded [Ghostty terminal](https://github.com/Lakr233/libghostty-spm). If Ghostty is installed, Muxteroid reads its appearance settings, including its font and colors. The embedded build does not render custom shaders. [dtach](https://github.com/crigler/dtach) keeps the process alive between attachments; scrollback belongs to the current terminal view.

Workstream data lives in `~/Library/Application Support/Muxteroid`, with managed checkouts under `worktrees/`. Each Codex terminal runs its own local app-server behind a private Unix socket. Its terminal and Muxteroid observe the same thread, including approval requests, questions, replies, and loaded subagents. Selecting a subagent selects its owning terminal. Each Claude session receives its parent Beads issue and [hooks](https://code.claude.com/docs/en/hooks) for reporting activity. Plain shells receive `BEADS_DIR` and `MUXTEROID_ISSUE` for the same shared task context. Adding terminals reuses the stream’s worktree. Completing a stream keeps its branch and files available; it does not remove the worktree.

## Support

[![Donate with PayPal](https://img.shields.io/badge/Donate_with_PayPal-003087?style=for-the-badge&logo=paypal&logoColor=white)](https://www.paypal.com/donate/?business=ivan.kalyaev%40gmail.com)

Muxteroid's source is available under the [MIT License](LICENSE).
