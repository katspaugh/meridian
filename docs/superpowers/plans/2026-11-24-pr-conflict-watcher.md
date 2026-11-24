# PR merge-conflict watcher implementation plan

Approved design: ../specs/2026-11-24-pr-conflict-watcher-design.md. The owner pre-approved this plan. Work is done test-first in `Tests/test_manager_comments.py`. Run tests with `/usr/bin/python3 -m unittest discover -s Tests -p 'test_*.py'`.

1. **Detect.** Factor the `gh` call and error classification in `manager_comments` so that GraphQL can reuse them. Add `conflicts()`, which pages through the authored-open-PR search. It returns conflict inbox items carrying a saved follow plan, plus the PR identities whose mergeability is still unknown. Validate the repository, the number and the SHA before building any id or URL.
2. **Poll.** Add `poll_conflicts()` and call it from `poll()`. It inserts new conflict items, drops pending conflict items that are no longer conflicting (keeping unknown ones), clears or sets `comments_error_conflicts`, and records an event once when the error changes. Add that key to `commentErrors` in `manager.state`, de-duplicated.
3. **Deliver.** In `follow_up`, label conflict sources `PR:` instead of `Comment:`. For an unmatched conflict, find a local checkout whose GitHub remote matches the PR's repository, among the workstreams' `repository` and `sourceFolder` values. Then call `bridge.open_github_stream` with that checkout's provider, record a `create` event, refresh the rows and continue through the normal delivery path. If there is no checkout, fall back to the "New workstream needed" question. A creation failure stays pending for retry.
4. **Document.** Describe conflict watching in the README's Comment follow-ups section.
5. **Verify.** Run the full Python suite and `./build.sh`.
