"""Model one Superpowers run as phases and plan tasks. Times are epoch seconds.

Every function that takes a run mutates it in place and returns it. The event
rules may instead return a new run, or None when there is no run to report.
"""
import re

PHASES = ("brainstorm", "plan", "execute", "final-review", "verify", "pr", "ci", "merge")

PHASE_LABEL = {
    "brainstorm": "brainstorm",
    "plan": "plan",
    "execute": "tasks",
    "final-review": "final review",
    "verify": "verify",
    "pr": "PR",
    "ci": "CI",
    "merge": "merged",
}

MIN = 60.0

TASK_HEADING = re.compile(r"^#{2,4}\s+Task\s+(\d+)\s*[:.–—-]?\s*(.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")
LEDGER_COMPLETE = re.compile(r"\bTask (\d+): complete\b")
LEDGER_ROUND = re.compile(r"\bTask (\d+): fix round (\d+)")

STARTERS = {
    "brainstorming": "brainstorm",
    "writing-plans": "plan",
    "subagent-driven-development": "execute",
    "executing-plans": "execute",
}

PR_URL = re.compile(r'https?://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)')
SDD_WORKSPACE = re.compile(r"\bsdd-workspace\b")
WORKSPACE_LINE = re.compile(r"^(/.*/\.superpowers/sdd/[^/\s]+)\s*$")
TASK_BRIEF = re.compile(r"""\btask-brief["']?\s+(?:"[^"]*"|'[^']*'|\S+)\s+(\d+)""")
REVIEW_PACKAGE = re.compile(r"\breview-package\b")
PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
PR_MERGE = re.compile(r"\bgh\s+pr\s+merge\b")
PR_MERGE_AUTO = re.compile(r"--auto\b")
PR_MERGE_NUMBER = re.compile(r"\bgh\s+pr\s+merge\s+#?(\d+)")


def new_run(now):
    return {
        "name": "superpowers",
        "launchedAt": now,
        "phases": {phase: {"status": "todo"} for phase in PHASES},
        "tasks": [],
    }


def is_finished(run):
    return run["phases"]["merge"]["status"] in ("done", "failed")


def has_started(run, phase):
    return run["phases"][phase]["status"] not in ("todo", "skipped")


def _stamp(status, started_at=None, ended_at=None):
    stamp = {"status": status}
    if started_at is not None:
        stamp["startedAt"] = started_at
    if ended_at is not None:
        stamp["endedAt"] = ended_at
    return stamp


def _begin(stamp, now):
    if stamp["status"] in ("running", "done"):
        return stamp
    return _stamp("running", now)


# No startedAt is invented for a stamp that was never seen running, so a task
# a resumed ledger reports as complete does not drag the per-task average to 0.
def _finish(stamp, now):
    if stamp["status"] == "done":
        return stamp
    return _stamp("done", stamp.get("startedAt"), now)


def advance_to(run, phase, now):
    at = PHASES.index(phase)
    if any(has_started(run, later) for later in PHASES[at + 1:]):
        return run
    phases = run["phases"]
    for earlier in PHASES[:at]:
        status = phases[earlier]["status"]
        if status in ("running", "failed"):
            phases[earlier] = _finish(phases[earlier], now)
        elif status == "todo":
            phases[earlier] = _stamp("skipped")
    phases[phase] = _begin(phases[phase], now)
    return run


def complete_phase(run, phase, now):
    advance_to(run, phase, now)
    run["phases"][phase] = _finish(run["phases"][phase], now)
    return run


def fail_phase(run, phase, now):
    advance_to(run, phase, now)
    run["phases"][phase] = _stamp("failed", run["phases"][phase].get("startedAt"), now)
    return run


def all_tasks_done(run):
    tasks = run["tasks"]
    return len(tasks) > 0 and all(task["review"]["status"] == "done" for task in tasks)


def parse_plan_tasks(text):
    tasks = []
    seen = set()
    is_in_fence = False
    for line in text.split("\n"):
        if FENCE.match(line):
            is_in_fence = not is_in_fence
        if is_in_fence:
            continue
        match = TASK_HEADING.match(line)
        if not match:
            continue
        n = int(match.group(1))
        if n in seen:
            continue
        seen.add(n)
        title = re.sub(r"[*_`]", "", match.group(2)).strip()
        tasks.append({"n": n, "title": title or "Task {}".format(n)})
    return tasks


def _empty_task(n, title):
    return {"n": n, "title": title, "implement": _stamp("todo"), "review": _stamp("todo")}


def slug_name(path):
    base = path.split("/")[-1]
    base = re.sub(r"\.md$", "", base)
    base = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", base)
    return re.sub(r"-design$", "", base)


def with_plan(run, plan_path, parsed):
    existing = {task["n"]: task for task in run["tasks"]}
    tasks = []
    for item in parsed:
        task = existing.get(item["n"])
        if task is None:
            task = _empty_task(item["n"], item["title"])
        else:
            task["title"] = item["title"]
        tasks.append(task)
    run["planPath"] = plan_path
    run["name"] = slug_name(plan_path)
    if tasks:
        run["tasks"] = tasks
    return run


def with_spec(run, spec_path):
    run["specPath"] = spec_path
    if not run.get("planPath"):
        run["name"] = slug_name(spec_path)
    return run


def start_task(run, n, now):
    advance_to(run, "execute", now)
    if not any(task["n"] == n for task in run["tasks"]):
        run["tasks"].append(_empty_task(n, "Task {}".format(n)))
    for task in run["tasks"]:
        if task["n"] == n and task["review"]["status"] != "done":
            task["implement"] = _begin(task["implement"], now)
    run["tasks"].sort(key=lambda task: task["n"])
    run["currentTask"] = n
    return run


def review_task(run, now):
    current = run.get("currentTask")
    task = next((candidate for candidate in run["tasks"] if candidate["n"] == current), None)
    if task is None or task["review"]["status"] == "done":
        return advance_to(run, "final-review", now) if all_tasks_done(run) else run
    task["implement"] = _finish(task["implement"], now)
    task["review"] = _begin(task["review"], now)
    return run


def apply_ledger(run, text, now):
    complete = {int(match.group(1)) for match in LEDGER_COMPLETE.finditer(text)}
    rounds = {int(match.group(1)): int(match.group(2)) for match in LEDGER_ROUND.finditer(text)}

    known = {task["n"] for task in run["tasks"]}
    for n in sorted((complete | set(rounds)) - known):
        run["tasks"].append(_empty_task(n, "Task {}".format(n)))

    for task in run["tasks"]:
        if task["n"] in rounds:
            task["round"] = rounds[task["n"]]
        if task["n"] in complete and task["review"]["status"] != "done":
            task["implement"] = _finish(task["implement"], now)
            task["review"] = _finish(task["review"], now)
    run["tasks"].sort(key=lambda task: task["n"])

    if all_tasks_done(run) and run["phases"]["execute"]["status"] == "running":
        complete_phase(run, "execute", now)
    return run


def _is_live(run):
    return run is not None and not is_finished(run)


def skill_event(run, skill, now):
    name = re.sub(r"^superpowers:", "", skill)
    starter = STARTERS.get(name)
    if starter:
        if not _is_live(run) or (starter != "execute" and has_started(run, "execute")):
            run = new_run(now)
        return advance_to(run, starter, now)
    if not skill.startswith("superpowers:") or not _is_live(run):
        return run
    if name == "requesting-code-review" and all_tasks_done(run):
        advance_to(run, "final-review", now)
    elif name == "verification-before-completion" and all_tasks_done(run):
        advance_to(run, "verify", now)
    elif name == "finishing-a-development-branch" and has_started(run, "execute"):
        advance_to(run, "pr", now)
    return run


def document_event(run, kind, path, parsed, is_new, now):
    if not is_new:
        if _is_live(run) and path == run.get("planPath"):
            with_plan(run, path, parsed)
        return run
    is_own = run is not None and path in (run.get("specPath"), run.get("planPath"))
    if not _is_live(run) or (has_started(run, "execute") and not is_own):
        run = new_run(now)
    if kind == "spec":
        return advance_to(with_spec(run, path), "brainstorm", now)
    return advance_to(with_plan(run, path, parsed), "plan", now)


def workspace_path(command, output):
    if not SDD_WORKSPACE.search(command):
        return None
    found = None
    for line in output.split("\n"):
        match = WORKSPACE_LINE.match(line)
        if match:
            found = match.group(1)
    return found


def attach_workspace(run, workspace, plan_path, parsed, now):
    current_plan = run.get("planPath") if run is not None else None
    is_other_plan = bool(current_plan) and bool(plan_path) and current_plan != plan_path
    if not _is_live(run) or (is_other_plan and has_started(run, "execute")):
        run = new_run(now)
    if plan_path:
        with_plan(run, plan_path, parsed)
    advance_to(run, "execute", now)
    run["workspace"] = workspace
    run.pop("ledgerMtime", None)
    return run


def pull_request_created(run, url, now):
    match = PR_URL.search(url)
    if not _is_live(run) or match is None:
        return run
    advance_to(complete_phase(run, "pr", now), "ci", now)
    run["pr"] = int(match.group(1))
    run["prUrl"] = match.group(0)
    return run


def command_event(run, command, output, now):
    if not _is_live(run):
        return run
    brief = TASK_BRIEF.search(command)
    if brief:
        return start_task(run, int(brief.group(1)), now)
    if REVIEW_PACKAGE.search(command):
        return review_task(run, now)
    if PR_CREATE.search(command):
        url = PR_URL.search(output)
        return pull_request_created(run, url.group(0), now) if url else run
    if PR_MERGE.search(command) and not PR_MERGE_AUTO.search(command):
        named = PR_MERGE_NUMBER.search(command)
        if named is None or int(named.group(1)) == run.get("pr"):
            complete_phase(run, "merge", now)
    return run


# A JSON null is treated like an absent field.
def classify_check(check):
    if not isinstance(check, dict):
        return "pending"
    status = check.get("status")
    conclusion = check.get("conclusion")
    verdict = conclusion if isinstance(conclusion, str) and conclusion != "" else check.get("state")
    if status is not None and status != "COMPLETED":
        return "pending"
    if verdict in ("SUCCESS", "SKIPPED", "NEUTRAL"):
        return "pass"
    if verdict in ("PENDING", "EXPECTED") or verdict is None:
        return "pending"
    return "fail"


def apply_pr_view(run, view, now):
    if not isinstance(view, dict):
        return run
    state = view.get("state")
    if state == "MERGED":
        return complete_phase(run, "merge", now)
    if state == "CLOSED":
        return fail_phase(run, "merge", now)

    rollup = view.get("statusCheckRollup")
    checks = [classify_check(check) for check in rollup] if isinstance(rollup, list) else []
    failing = checks.count("fail")
    pending = checks.count("pending")
    total = len(checks)
    if failing > 0:
        fail_phase(run, "ci", now)
        run["ciDetail"] = "{} of {} failing".format(failing, total)
        return run
    if pending > 0 or total == 0:
        if run["phases"]["ci"]["status"] == "failed":
            run["phases"]["ci"] = _stamp("todo")
        advance_to(run, "ci", now)
        run["ciDetail"] = "waiting for checks" if total == 0 else "{} of {} checks".format(total - pending, total)
        return run
    advance_to(complete_phase(run, "ci", now), "merge", now)
    run["ciDetail"] = "{} passed".format(total)
    return run
