"""Read provider-reported account windows. Timestamps are Unix seconds."""
from datetime import datetime
import contextlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time


def read(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}


def window(label, percent, reset):
    if type(percent) not in (int, float) or not math.isfinite(percent) or percent < 0:
        return None
    if isinstance(reset, str):
        try:
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset = None
    if isinstance(reset, (int, float)) and reset <= time.time():
        return None
    return dict(label=label, percent=percent, reset=reset)


def claude(root):
    snapshots = []
    config_dir = Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home()/".claude")
    config = read(config_dir/".claude.json") or read(Path.home()/".claude.json")
    cached = config.get("cachedUsageUtilization") or {}
    if cached.get("accountUuid") == (config.get("oauthAccount") or {}).get("accountUuid"):
        values = cached.get("utilization") or {}
        windows = []
        for kind, label in (("five_hour","5h"), ("seven_day","7d")):
            value = values.get(kind) or {}
            windows.append(window(label, value.get("utilization"), value.get("resets_at")))
        snapshots.append((cached.get("fetchedAtMs", 0)/1000, windows))
    for path in (root/"views").glob("*/usage.json"):
        payload = read(path)
        if not isinstance(payload, list):
            continue
        labels = {"five_hour":"5h", "seven_day":"7d"}
        windows = [window(labels[v["kind"]], v.get("percentUsed"), v.get("resetsAt"))
                   for v in payload if isinstance(v, dict) and v.get("kind") in labels]
        with contextlib.suppress(OSError):
            snapshots.append((path.stat().st_mtime,windows))
    for stamp, windows in sorted(snapshots, key=lambda s:s[0], reverse=True):
        windows = [w for w in windows if w]
        if windows and time.time()-stamp < 3600:
            return dict(windows=sorted(windows,key=lambda w:w["label"]), measured=stamp)
    return dict(windows=[], hint="Waiting for a Claude usage reading. Run /usage in Claude or use a newly started Muxteroid Claude session.")


def codex():
    # Ask the CLI through its supported stdio interface, including when no
    # workstream is open. The CLI handles account authentication itself.
    with subprocess.Popen(["codex","app-server","--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True) as proc:
        def timeout(*_):
            raise TimeoutError()
        old = signal.signal(signal.SIGALRM, timeout)
        signal.alarm(10)
        try:
            def request(number, method, params):
                proc.stdin.write(json.dumps(dict(id=number,method=method,params=params))+"\n")
                proc.stdin.flush()
                for line in proc.stdout:
                    reply = json.loads(line)
                    if reply.get("id") == number:
                        if "error" in reply:
                            raise ValueError("No account limits available")
                        return reply["result"]
                raise EOFError()
            request(1,"initialize",{"clientInfo":{"name":"muxteroid_usage","version":"1"}})
            proc.stdin.write('{"method":"initialized"}\n')
            result = request(2,"account/rateLimits/read",{})
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
            proc.kill()
    limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits") or {}
    windows = []
    for key in ("primary","secondary"):
        value = limits.get(key) or {}
        minutes = value.get("windowDurationMins")
        label = {300:"5h",10080:"7d"}.get(minutes, f"{minutes}m" if minutes else key.capitalize())
        item = window(label, value.get("usedPercent"), value.get("resetsAt"))
        if item:
            windows.append(item)
    return dict(windows=windows, measured=time.time(), hint="This Codex account did not report usage limits.")


def report(root):
    try:
        data = codex()
    except (OSError, ValueError, EOFError):
        data = dict(windows=[], hint="Codex usage unavailable. Check the CLI sign-in and connection, then refresh.")
    return {"claude":claude(root),"codex":data}
