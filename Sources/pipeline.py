"""Model one Superpowers run as phases and plan tasks. Times are epoch seconds.

Every function that takes a run mutates it in place and returns it. The event
rules may instead return a new run, or None when there is no run to report.
schedule and render only read the run and return new dicts.
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
SLOW_FACTOR = 1.5
DEFAULT_TASK = 12 * MIN
IMPLEMENT_SHARE = 0.6

# brainstorm and plan wait on the person, so they carry no estimate.
PHASE_ESTIMATE = {
    "final-review": 10 * MIN,
    "verify": 3 * MIN,
    "pr": 3 * MIN,
    "ci": 15 * MIN,
    "merge": 3 * MIN,
}

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


def average_task_seconds(run):
    spans = [
        task["review"]["endedAt"] - task["implement"]["startedAt"]
        for task in run["tasks"]
        if "startedAt" in task["implement"] and "endedAt" in task["review"]
    ]
    return sum(spans) / len(spans) if spans else DEFAULT_TASK


def _stage_view(stamp, now, budget):
    status = stamp["status"]
    if status == "running":
        started = stamp.get("startedAt")
        return "slow" if started is not None and now - started > budget * SLOW_FACTOR else "running"
    if status == "skipped":
        return "todo"
    return status


def _slot(start, end, estimate, view):
    return {"start": start, "end": end, "estimate": estimate, "view": view}


def _task_slot(n, start, end, estimate, view, implement, review):
    slot = _slot(start, end, estimate, view)
    slot.update({"n": n, "implement": implement, "review": review})
    return slot


# A run without a workspace or any task that has started gives no per-task
# signals, so while it executes nothing is guessed about its tasks.
def _tracks_tasks(run):
    return bool(run.get("workspace")) or any(
        task["implement"]["status"] != "todo" or task["review"]["status"] != "todo" for task in run["tasks"]
    )


def schedule(run, now):
    task_seconds = average_task_seconds(run)
    phases = {}
    tasks = []
    # None once an open-ended step (brainstorm, an unparsed plan, untracked
    # tasks) stands between now and the rest: nothing after it gets a time.
    cursor = now

    for phase in PHASES:
        stamp = run["phases"][phase]
        status = stamp["status"]

        if phase == "execute":
            is_untracked = status == "running" and not _tracks_tasks(run)
            if (not run["tasks"] and status != "done") or is_untracked:
                cursor = None
            previous_end = stamp.get("startedAt")
            has_running = is_untracked or any(
                task["implement"]["status"] == "running" or task["review"]["status"] == "running"
                for task in run["tasks"]
            )
            for task in run["tasks"]:
                implement, review = task["implement"], task["review"]
                if review["status"] == "done":
                    previous_end = review.get("endedAt", previous_end)
                    tasks.append(_task_slot(task["n"], implement.get("startedAt"), review.get("endedAt"),
                                            False, "done", "done", "done"))
                    continue
                if status == "done":
                    tasks.append(_task_slot(task["n"], None, None, False, "done", "done", "done"))
                    continue
                is_active = (implement["status"] == "running" or review["status"] == "running"
                             or (not has_running and status == "running"))
                if is_active:
                    has_running = True
                    start = implement.get("startedAt")
                    if start is None:
                        start = previous_end if previous_end is not None else now
                    end = max(now + MIN, start + task_seconds)
                    implement_view = ("running" if implement["status"] == "todo"
                                      else _stage_view(implement, now, task_seconds * IMPLEMENT_SHARE))
                    review_view = _stage_view(review, now, task_seconds * (1 - IMPLEMENT_SHARE))
                    view = "slow" if now - start > task_seconds * SLOW_FACTOR else "running"
                    tasks.append(_task_slot(task["n"], start, end, True, view, implement_view, review_view))
                    cursor = None if cursor is None else max(cursor, end)
                    continue
                start = cursor
                end = None if cursor is None else cursor + task_seconds
                cursor = end
                tasks.append(_task_slot(task["n"], start, end, True, "todo", "todo", "todo"))
            if status == "done":
                phases[phase] = _slot(stamp.get("startedAt"), stamp.get("endedAt"), False, "done")
            else:
                phases[phase] = _slot(stamp.get("startedAt"), cursor, True, _stage_view(stamp, now, float("inf")))
            continue

        estimate = PHASE_ESTIMATE.get(phase)
        if status in ("done", "failed"):
            phases[phase] = _slot(stamp.get("startedAt"), stamp.get("endedAt"), False, status)
        elif status == "skipped":
            phases[phase] = _slot(None, None, False, "todo")
        elif status == "running" and estimate is None:
            if not (phase == "plan" and run["tasks"]):
                cursor = None
            phases[phase] = _slot(stamp.get("startedAt"), None, True, "running")
        elif status == "running":
            start = stamp.get("startedAt", now)
            end = max(now + MIN, start + estimate)
            cursor = None if cursor is None else max(cursor, end)
            phases[phase] = _slot(start, end, True, _stage_view(stamp, now, estimate))
        elif estimate is None:
            phases[phase] = _slot(cursor, None, True, "todo")
        else:
            end = None if cursor is None else cursor + estimate
            phases[phase] = _slot(cursor, end, True, "todo")
            cursor = end

    last = next((phase for phase in reversed(PHASES) if run["phases"][phase]["status"] != "skipped"), None)
    finish = phases[last]["end"] if last else None
    return {"phases": phases, "tasks": tasks, "finish": finish, "task": task_seconds}


def _when(slot):
    if slot["end"] is not None:
        return slot["end"], slot["estimate"]
    if slot["view"] != "todo":
        return slot["start"], False
    return None, False


def _phase_detail(run, phase):
    tasks = run["tasks"]
    if phase == "brainstorm":
        return slug_name(run["specPath"]) if run.get("specPath") else ""
    if phase == "plan":
        if tasks:
            return "1 task" if len(tasks) == 1 else "{} tasks".format(len(tasks))
        return slug_name(run["planPath"]) if run.get("planPath") else ""
    if phase == "execute" and tasks:
        done = sum(1 for task in tasks if task["review"]["status"] == "done")
        return "{} of {}".format(done, len(tasks))
    if phase == "pr" and run.get("pr") is not None:
        return "#{}".format(run["pr"])
    if phase == "ci":
        return run.get("ciDetail") or ""
    return ""


# A frozen run keeps only the recorded times of finished rows: rows that were in
# progress lose their time along with their mark, and estimates are dropped.
def _freeze(item, marks):
    was_live = False
    for key in marks:
        if item[key] in ("running", "slow"):
            item[key] = "todo"
            was_live = True
    if was_live or item["estimate"]:
        item["time"] = None
        item["estimate"] = False


def render(run, now, waiting=False, active_agents=0, frozen=False):
    plan = schedule(run, now)
    merge = run["phases"]["merge"]
    finished = {"done": "merged", "failed": "closed"}.get(merge["status"])

    phases = []
    for phase in PHASES:
        if run["phases"][phase]["status"] == "skipped":
            continue
        slot = plan["phases"][phase]
        time, estimate = _when(slot)
        item = {
            "id": phase, "label": PHASE_LABEL[phase], "mark": slot["view"], "time": time, "estimate": estimate,
            "detail": _phase_detail(run, phase), "url": run.get("prUrl") if phase == "pr" else None, "tasks": None,
        }
        if phase == "execute":
            item["tasks"] = []
            for task, task_slot in zip(run["tasks"], plan["tasks"]):
                task_time, task_estimate = _when(task_slot)
                item["tasks"].append({
                    "n": task["n"], "title": task["title"], "mark": task_slot["view"],
                    "implement": task_slot["implement"], "review": task_slot["review"],
                    "round": task.get("round"), "time": task_time, "estimate": task_estimate,
                })
        phases.append(item)

    running = [item for item in phases if run["phases"][item["id"]]["status"] == "running"]
    needs_you = (waiting and not frozen and finished is None and active_agents == 0
                 and run["phases"]["ci"]["status"] != "running" and bool(running))
    if needs_you:
        running[-1]["mark"] = "needs"

    finish_at = merge.get("endedAt") if finished == "merged" else plan["finish"]
    if frozen:
        for item in phases:
            _freeze(item, ("mark",))
            for task in item["tasks"] or []:
                _freeze(task, ("mark", "implement", "review"))
        if finished != "merged":
            finish_at = None
        active_agents = 0

    return {
        "name": run["name"], "startedAt": run["launchedAt"], "finishAt": finish_at, "finished": finished,
        "needsYou": needs_you, "polling": bool(run.get("prUrl")) and finished is None and not frozen,
        "frozen": frozen, "activeAgents": active_agents, "phases": phases,
    }
