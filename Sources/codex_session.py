"""Observe the private Codex app-server that powers the workstream's own TUI."""
import base64
import contextlib
import hashlib
import json
import os
import socket
import signal
import struct
import subprocess
import time

import workstream_log as trace
import workstream_links as links


class Client:
    """Synchronous JSON-RPC over the app-server's local Unix WebSocket."""
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(4)
        self.socket.connect(str(path))
        self.reader = self.socket.makefile("rb")
        self.sequence = 0
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                                 "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                                 f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
            if not self.reader.readline().startswith(b"HTTP/1.1 101 "):
                raise ValueError("Codex refused the local connection.")
            while self.reader.readline() not in (b"\r\n", b""):
                pass
            self.call("initialize", {"clientInfo": {"name": "meridian", "version": "1"}})
            self.send(1, b'{"method":"initialized"}')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.reader.close()
        self.socket.close()

    def send(self, opcode, data):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            size = bytes([128+n])
        elif n < 65536:
            size = b'\xfe'+struct.pack('!H', n)
        else:
            size = b'\xff'+struct.pack('!Q', n)
        self.socket.sendall(bytes([128+opcode])+size+mask+bytes(c ^ mask[i % 4] for i,c in enumerate(data)))

    def call(self, method, params):
        self.sequence += 1
        self.send(1, json.dumps(dict(id=self.sequence, method=method, params=params)).encode())
        message = bytearray()
        while True:
            head = self.reader.read(2)
            if len(head) != 2:
                raise ConnectionError("Codex disconnected.")
            tag, length = head
            opcode = tag & 15
            if length == 126:
                length, = struct.unpack('!H', self.reader.read(2))
            elif length == 127:
                length, = struct.unpack('!Q', self.reader.read(8))
            if length + len(message) > 64 * 1024 * 1024:
                raise ValueError("Codex response too large.")
            data = self.reader.read(length)
            if len(data) != length or opcode == 8:
                raise ConnectionError("Codex disconnected.")
            if opcode == 9:
                self.send(10, data)
            if opcode not in (0, 1):
                continue
            message.extend(data)
            if tag & 128:
                reply = json.loads(message)
                message.clear()
                if reply.get("id") == self.sequence and "method" not in reply:
                    if "error" in reply:
                        raise ValueError(reply["error"]["message"])
                    return reply["result"]


def endpoint(directory, key):
    return directory / ("codex-" + hashlib.sha256(key.encode()).hexdigest()[:16] + ".sock")


def status(thread, turn):
    state = thread["status"]
    if "waitingOnApproval" in state.get("activeFlags", []):
        return "Needs you", "Codex is waiting for approval."
    if "waitingOnUserInput" in state.get("activeFlags", []):
        return "Needs you", "Codex has a question."
    if state["type"] == "systemError" or (turn and turn["status"] == "failed" and state["type"] != "active"):
        return "Needs you", ((turn or {}).get("error") or {}).get("message") or "Codex encountered an error."
    if state["type"] == "active":
        return "Working", "Codex is working on your request."
    if not turn:
        return "Ready", "Codex is ready for a prompt."
    if turn["status"] == "interrupted":
        return "Ready", "Turn interrupted."
    reply = next((i["text"] for i in reversed(turn["items"]) if i["type"] == "agentMessage"), "")
    return "Review", reply or "Codex finished its turn."


def observe(client, db, key, previous, cache):
    ids = []
    cursor = None
    while True:
        page = client.call("thread/loaded/list", {"cursor": cursor})
        ids.extend(page["data"])
        cursor = page.get("nextCursor")
        if not cursor:
            break
    for identity in ids:
        thread = client.call("thread/read", {"threadId": identity})["thread"]
        parent = thread.get("parentThreadId")
        if not parent and previous and identity != previous:
            continue
        latest = None
        new_turn = False
        if thread["status"]["type"] == "active":
            cache.pop(identity, None)
        else:
            params = {"threadId": identity, "limit": 1}
            try:
                page = client.call("thread/turns/list", dict(params, itemsView="notLoaded"))
            except ValueError as error:
                if "not materialized" not in str(error):
                    raise
                # A brand new, empty thread cannot be resumed after exit.
                if not parent:
                    db.execute("UPDATE activity SET state='Ready',detail='Codex is ready for a prompt.' WHERE id=?", (key,))
                    db.commit()
                continue
            summary = next(iter(page["data"]), {})
            stamp = (summary.get("id"), summary.get("status"))
            if identity not in cache or cache[identity][0] != stamp:
                result = client.call("thread/turns/list", dict(params, itemsView="full"))
                latest = next(iter(result["data"]), None)
                cache[identity] = (stamp, latest)
                new_turn = True
            latest = cache[identity][1]
        state, detail = status(thread, latest)
        if parent:
            title = thread.get("agentNickname") or thread.get("name") or thread.get("preview") or "Codex subagent"
            db.execute("INSERT OR REPLACE INTO codex_children VALUES (?,?,?,?,?)",
                       (key,identity,title,thread.get("agentRole") or "Subagent","Finished" if state=="Review" else state))
        else:
            previous = identity
            db.execute("UPDATE activity SET session=?,state=?,detail=? WHERE id=?", (identity,state,detail,key))
        if new_turn and latest:
            for item in latest["items"]:
                if item["type"] == "agentMessage":
                    links.collect(db,key,item["text"])
                elif item["type"] == "commandExecution" and item.get("exitCode") == 0:
                    links.tool_result(db,key,"Bash",{"command":item["command"]},item.get("aggregatedOutput") or "",thread["cwd"])
        db.commit()
    for child, in db.execute("SELECT id FROM codex_children WHERE stream=? AND state IN ('Working','Needs you')", (key,)).fetchall():
        if child not in ids:
            db.execute("UPDATE codex_children SET state='Offline' WHERE stream=? AND id=?", (key,child))
    db.commit()
    return previous


def run(key, db, directory, beads, guide):
    path = endpoint(directory,key)
    path.unlink(missing_ok=True)
    previous = db.execute("SELECT session FROM activity WHERE id=?", (key,)).fetchone()[0]
    def shutdown(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    trace.record("starting Codex app-server")
    server = subprocess.Popen(["codex", "-c", "developer_instructions="+json.dumps(guide),
                               "-c", "sandbox_workspace_write.writable_roots="+json.dumps([str(beads)]), "app-server",
                               "--listen", "unix://"+str(path)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        trace.record(f"waiting for Codex socket server_pid={server.pid}")
        for _ in range(200):
            if path.exists():
                break
            if server.poll() is not None:
                raise ValueError("Codex app-server failed to start. Check your Codex CLI installation.")
            time.sleep(.05)
        else:
            raise ValueError("Timed out starting the Codex app-server.")
        args = ["codex", "--remote", "unix://"+str(path)]
        if previous:
            args += ["resume",previous]
        trace.record("Codex socket ready, starting TUI")
        tui = subprocess.Popen(args)
        trace.record(f"Codex TUI started pid={tui.pid}")
        try:
            cache = {}
            while tui.poll() is None and server.poll() is None:
                try:
                    with contextlib.closing(Client(path)) as client:
                        while tui.poll() is None:
                            previous = observe(client,db,key,previous,cache)
                            time.sleep(1)
                except (OSError, ValueError, EOFError, struct.error) as error:
                    db.execute("UPDATE activity SET detail=? WHERE id=? AND state='Ready'",
                               ("Waiting for Codex session: "+str(error),key))
                    db.commit()
                    time.sleep(.5)
        finally:
            if tui.poll() is None:
                tui.terminate()
            tui.wait()
    finally:
        server.terminate()
        server.wait()
        path.unlink(missing_ok=True)
