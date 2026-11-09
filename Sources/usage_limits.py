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


def claude_windows(values):
    windows = []
    for kind, label in (("five_hour", "5h"), ("seven_day", "7d")):
        value = values.get(kind)
        if isinstance(value, dict):
            item = window(label, value.get("utilization"), value.get("resets_at"))
            if item:
                windows.append(item)
    return windows


def claude_live():
    request = {"type": "control_request", "request_id": "muxteroid-usage",
               "request": {"subtype": "get_usage", "skip_behaviors": True}}
    proc = subprocess.run(
        ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose", "--no-session-persistence", "--setting-sources", "user",
         "--settings", '{"disableAllHooks":true}', "--strict-mcp-config"],
        input=json.dumps(request) + "\n", cwd="/", stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, timeout=12, check=True)
    for line in proc.stdout.splitlines():
        message = json.loads(line)
        reply = message.get("response")
        if message.get("type") != "control_response" or not isinstance(reply, dict):
            continue
        if reply.get("request_id") != request["request_id"] or reply.get("subtype") != "success":
            continue
        data = reply.get("response") or {}
        windows = claude_windows(data.get("rate_limits") or {})
        if windows:
            return dict(windows=windows, measured=time.time())
    return None


def claude_cached(root):
    snapshots = []
    config_dir = Path(os.getenv("CLAUDE_CONFIG_DIR") or Path.home()/".claude")
    config = read(config_dir/".claude.json") or read(Path.home()/".claude.json")
    cached = config.get("cachedUsageUtilization") or {}
    if cached.get("accountUuid") == (config.get("oauthAccount") or {}).get("accountUuid"):
        values = cached.get("utilization") or {}
        snapshots.append((cached.get("fetchedAtMs", 0)/1000, claude_windows(values)))
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
    return None


def claude(root):
    # A separate CLI process may have a different account context from the
    # interactive workstream. Prefer recent readings from Claude itself.
    cached = claude_cached(root)
    if cached:
        return cached
    try:
        live = claude_live()
        if live:
            return live
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return dict(windows=[], hint="No recent usage figures from Claude Code.")


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
