"""Model one Superpowers run as phases and plan tasks. Times are epoch seconds.

Every function that takes a run mutates it in place and returns it.
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
