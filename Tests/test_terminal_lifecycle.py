"""Exercise real dtach PTYs without launching agents or using the user's workstreams."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get('MUXTEROID_PTY_TESTS') == '1' and shutil.which('dtach'),
                     'Set MUXTEROID_PTY_TESTS=1 to run real PTY lifecycle tests')
class TerminalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mx-exit-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.sockets = self.root / 'sockets'
        self.sockets.mkdir(mode=0o700)
        self.connections = []
        self.env = dict(os.environ, MUXTEROID_HOME=str(self.root), SHELL='/bin/sh')
        self.helper = self.root / 'session.py'
        source = str(Path(__file__).resolve().parents[1] / 'Sources')
        self.helper.write_text(
            'import sys\n'
            f'sys.path.insert(0, {source!r})\n'
            'import streams\n'
            f'streams.TERMINALS = streams.Path({str(self.sockets)!r})\n'
            'action, key = sys.argv[1:3]\n'
            "issue = dict(id=key, title='Test', status='open', metadata=dict(folder=str(streams.ROOT), provider='codex'))\n"
            "streams.bd = lambda *args: streams.json.dumps([issue])\n"
            "if action == 'close':\n"
            '    streams.close_terminal(key)\n'
            'else:\n'
            '    with streams.database() as db:\n'
            '        streams.register_original(db, issue)\n'
            "        db.execute(\"INSERT OR REPLACE INTO activity VALUES (?,NULL,'Working','Test',NULL)\", (key,))\n"
            "    streams.run_terminal(key, ['/bin/cat'])\n"
        )

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        for path in self.sockets.iterdir():
            result = subprocess.run(['/usr/sbin/lsof', '-t', '-a', '-U', '-c', 'dtach', str(path)],
                                    capture_output=True, text=True)
            for value in result.stdout.split():
                if value.isdigit():
                    try:
                        os.kill(int(value), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        self.temp.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.025)
        self.fail('Timed out waiting for terminal transition')

    def provider(self, key):
        try:
            with sqlite3.connect(self.root / 'activity.sqlite') as db:
                row = db.execute('SELECT provider FROM workstream_terminals WHERE id=?', (key,)).fetchone()
                return row[0] if row else None
        except sqlite3.OperationalError:
            return None

    def closed(self, key):
        with sqlite3.connect(self.root / 'activity.sqlite') as db:
            return db.execute('SELECT 1 FROM closed_terminals WHERE id=?', (key,)).fetchone() is not None

    def start(self, key):
        path = self.sockets / key
        subprocess.run(['dtach', '-n', str(path), '-r', 'winch', sys.executable, str(self.helper), 'run', key],
                       cwd=self.root, env=self.env, stdin=subprocess.DEVNULL, check=True)
        self.wait_for(lambda: self.provider(key) == 'codex' and path.exists())
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2)
        connection.connect(str(path))
        connection.sendall(bytes([1]) + bytes(9))
        self.connections.append(connection)
        return connection

    def send(self, connection, data):
        for offset in range(0, len(data), 8):
            part = data[offset:offset + 8]
            connection.sendall(bytes([0, len(part)]) + part.ljust(8, b'\0'))

    def test_agent_eof_keeps_socket_and_shell_eof_closes_it(self):
        key = 'test-eof'
        connection = self.start(key)
        self.send(connection, b'\x04')
        self.wait_for(lambda: self.provider(key) == 'shell')
        self.assertTrue((self.sockets / key).exists())
        self.assertFalse(self.closed(key))
        self.send(connection, b'echo shell-is-alive\n')
        output = b''
        while b'shell-is-alive\r\n' not in output:
            output += connection.recv(4096)
        self.send(connection, b'\x04')
        self.wait_for(lambda: self.closed(key) and not (self.sockets / key).exists())

    def test_close_terminates_only_the_selected_terminal(self):
        self.start('selected')
        other = self.start('other')
        subprocess.run([sys.executable, str(self.helper), 'close', 'selected'], env=self.env, check=True)
        self.wait_for(lambda: self.closed('selected') and not (self.sockets / 'selected').exists())
        self.assertTrue((self.sockets / 'other').exists())
        self.assertFalse(self.closed('other'))
        self.send(other, b'\x04')
        self.wait_for(lambda: self.provider('other') == 'shell')
        self.send(other, b'\x04')
        self.wait_for(lambda: self.closed('other'))

    def test_detaching_app_client_preserves_running_session(self):
        key = 'test-detach'
        connection = self.start(key)
        connection.close()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.settimeout(2)
        replacement.connect(str(self.sockets / key))
        replacement.sendall(bytes([1]) + bytes(9))
        self.connections.append(replacement)
        self.assertFalse(self.closed(key))
        self.assertEqual(self.provider(key), 'codex')
        self.send(replacement, b'\x04')
        self.wait_for(lambda: self.provider(key) == 'shell')
        self.send(replacement, b'\x04')
        self.wait_for(lambda: self.closed(key))


if __name__ == '__main__':
    unittest.main()
