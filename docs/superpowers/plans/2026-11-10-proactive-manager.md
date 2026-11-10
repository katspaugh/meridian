# Proactive manager implementation plan

Approved design: ../specs/2026-11-10-proactive-manager-design.md. Build on the existing Python bridge, SQLite store and SwiftUI manager. No new mid-turn model calls.

1. Persist one message per workstream/kind with stable identity, revision, read and Later state. Validate stream/terminal/action references before saving; preserve previous messages on malformed replies. Store recent owner prompts by canonical repository; keep rules in human-editable Markdown under manager/projects with collision-safe names. Track ask read state separately without breaking existing databases.
2. Extend stop and sweep response schemas and context. Stops produce status/next only; sweeps also produce reminders and rule proposals. Include dismissed messages and project rules. Add hourly and return-after-30-minutes sweep eligibility. Record tool descriptions and start times independently of manager pause. Deliver the planned-steps reminder through PostToolUse additionalContext. A live isolated Claude probe on 2026-11-10 returned the injected verification token, confirming delivery on this installed version.
3. Route message actions through the bridge: revalidate tell eligibility at click time, log actions, persist Later, and confirm full rule text before saving. Produce PR-check messages from the existing on-screen poll, once per transition. No additional polling or consult.
4. Add shared message cards, a fixed-height inspector slot with priority/paging, full question details in Manager, unread badge, and a timed Now line. Mark only visible messages read while the app is active. Disable busy-agent tells and keep failed actions visible.
5. Verify migrations, malformed inputs, replacement/suppression, prompt isolation, rules, pause and trigger limits with Python tests; run the full existing suite. Build Swift and render empty/status/multiple/question/long-message inspector states, checking constant slot geometry. Report any checks that the environment cannot run.

Implementation order follows the Beads children: storage, review/activity integration, UI, verification. Keep all tasks directly under the workstream. Do not deploy or merge as part of this local implementation unless requested.

## Verification — 2026-11-10

Implemented all five steps. `python3 -m unittest discover -s Tests -p 'test_*.py'` ran 288 tests successfully (three existing opt-in PTY lifecycle tests skipped). `./build.sh` produced and signed `build/Muxteroid.app`; this is a local bundle, not an installed release.

`python3 Tests/manager_layout.py` compiles the production slot and checks empty, status, multiple-message, question and 100-character states. All retain a 120-point slot and the same footer position; PNGs are written to `/tmp/mux-manager-layout`. Long text fits within two lines with limited font scaling.

An isolated live Claude call executed one harmless printf command and returned the verification token supplied only by its PostToolUse hook. The production reminder therefore uses verified additionalContext delivery, with no extra manager consult. Unit/integration tests cover once-per-owner-prompt gating while paused, current-session isolation, prompt evidence, rule context, stale-click rejection, busy-terminal guards, Later persistence, PR transitions and return/hourly scheduling.

Codex retains its existing state line: the current observer does not fetch running commands while a thread is active. Claude receives live tool descriptions and prompt-history learning through existing hooks, as scoped by the design.
