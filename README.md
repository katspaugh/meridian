<p align="center"><img src="docs/images/icon.png" width="80" height="80" alt=""></p>
<h1 align="center">Muxteroid</h1>
<p align="center">A native Mac app for running several Claude Code workstreams at once.</p>

Each workstream has a persistent terminal, a task list Claude maintains in Beads, and links to the pull requests and Linear issues it touches. When one session asks for permission or finishes a turn, it appears under **Needs you** or **Ready for review** in the sidebar.

![Three workstreams in Muxteroid. Claude Code edits an Android layout in the selected terminal, with blocked and completed tasks on the right and Linear issue links below.](docs/images/workstreams.png)

## Try it on your Mac

You'll need macOS 15 or later, a Swift 6.2 or newer toolchain, Homebrew, and a signed-in [Claude Code](https://code.claude.com/docs/en/quickstart) installation. Run `claude` once to sign in.

```sh
brew install beads dtach
git clone https://github.com/katspaugh/muxteroid.git
cd muxteroid
./build.sh
open build/Muxteroid.app
```

Press **⌘N**, choose a working folder, and click **Create**. Give Claude a task in the terminal. Claude names the stream from the goal and updates the title as the work becomes clearer. A Linear reference is optional.

## Working across streams

- **Switch away while Claude works.** Each session runs in its own PTY. Switch streams or quit Muxteroid and the Claude process keeps running. After a reboot, Muxteroid can resume from Claude's saved conversation.
- **See what's waiting on you.** Permission requests, questions, and errors appear under **Needs you**. Allow notifications at launch to get a macOS alert when a workstream or subagent needs input. Click it to return to the workstream. Alerts run while Muxteroid is open, including in the background. Completed turns appear under **Ready for review**, with Claude's formatted reply in the inspector. You decide when to mark the workstream complete.
- **Inspect delegated work.** Expand a workstream in the sidebar to see its Claude subagents. Select one to read its assignment, tool activity, and responses, then select the parent to return to its terminal.
- **Follow the plan.** Claude uses [Beads](https://github.com/gastownhall/beads) to track subtasks, dependencies, and blockers under the workstream. The inspector shows progress. Press **⌥⌘I** to give the terminal the full width.
- **Jump to related work.** The bottom bar collects GitHub PRs and Linear issues from Claude's activity. The links stay with the stream when you reopen it.

## How it runs

Muxteroid uses SwiftUI and an embedded [Ghostty terminal](https://github.com/Lakr233/libghostty-spm). If Ghostty is installed, Muxteroid reads its appearance settings, including its font and colors. The embedded build does not render custom shaders. [dtach](https://github.com/crigler/dtach) keeps the process alive between attachments; scrollback belongs to the current terminal view.

Workstream data lives in `~/Library/Application Support/Muxteroid`. Each Claude session receives its parent Beads issue and [hooks](https://code.claude.com/docs/en/hooks) for reporting activity. Muxteroid uses the working folder you choose. For parallel changes to the same repository, choose a separate Git worktree for each stream.

## Support

[![Donate with PayPal](https://img.shields.io/badge/Donate_with_PayPal-003087?style=for-the-badge&logo=paypal&logoColor=white)](https://www.paypal.com/donate/?business=ivan.kalyaev%40gmail.com)

Muxteroid's source is available under the [MIT License](LICENSE).
