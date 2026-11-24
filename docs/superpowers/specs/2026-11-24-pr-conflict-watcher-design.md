# PR merge-conflict watcher — design

Approved by the owner on 2026-11-24.

## Intent

The owner wants Meridian to keep every open pull request they authored mergeable. When GitHub reports that one conflicts with its base branch, an agent in the right workstream resolves the conflict without the owner having to notice it.

## Decisions

- A merge conflict is treated like another GitHub comment that needs follow-up. It goes through the comment inbox (`manager_comments`), so the same rules apply: it is gated by **Watch Linear and GitHub comments** and by the soul's pause, it is delivered only to an idle agent that is off screen, a completed stream is reopened, and an offline agent is restarted. There is no separate setting.
- Discovery uses one paginated `gh api graphql` search, `is:pr is:open author:@me`, which reads `mergeable` and the head commit. `UNKNOWN` (GitHub has not computed mergeability yet) is retried on the next poll and is never treated as clean.
- Each conflict is stored as `github:conflict:<owner/repo>#<number>:<head sha>` with a saved `follow` plan, so no model classification runs. A new head commit replaces the older pending entry. A pending entry whose PR is no longer conflicting, closed or merged is dropped, so a stale conflict is never delivered.
- The agent receives fixed text plus a URL rebuilt from validated parts. Titles and branch names from GitHub never become terminal input. The text tells it to merge the base branch into the PR branch, resolve the conflicts, run the tests, and push to the PR's head branch. Rebasing and force-pushing are not allowed.
- **When no workstream matches**, Meridian opens one automatically, the same way that pasting the PR URL into New workstream does (`open_github_stream`). It uses a local checkout of the repository that an existing workstream already uses, together with that workstream's agent provider. The stream's brief is the PR URL plus the fixed conflict text, never the PR body. The new stream's agent is started, and the conflict is delivered on a later poll, once the agent has had time to start. It is never typed in the same watch pass that created the stream. If no checkout of the repository is known, the owner gets a "New workstream needed" question with the PR link, as unmatched comments do now.
- Provider failures are reported in the existing monitoring-error list with the same GitHub error classification. Raw CLI output is never stored.

## Non-goals

- Meridian itself never runs git merges and never pushes or posts to GitHub. Agents do the work.
- PRs authored by other people are not watched.
- There is no new UI and no Swift change.

## Approaches considered

1. **Comment inbox (chosen).** This reuses dedupe, routing, reopening, restarting, focus and pause guards. The owner chose it explicitly.
2. A separate watcher with its own table, toggle and delivery. It would duplicate the delivery path, and the owner did not want a separate setting.
3. Reuse the `Merge conflict` attention flag from `pull_details`. That only covers linked PRs while the app is polling them, not all open PRs.
