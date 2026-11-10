import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import manager_brain as brain


def reply(output):
    return Mock(returncode=0, stdout=json.dumps({"is_error": False, "structured_output": output}))


class ManagerBrainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        run = patch.object(brain.subprocess, "run")
        which = patch.object(brain.shutil, "which", return_value="/usr/local/bin/claude")
        record = patch.object(brain.workstream_log, "record")
        self.run, self.which, self.record = run.start(), which.start(), record.start()
        for p in (run, which, record):
            self.addCleanup(p.stop)
        self.run.return_value = reply({"verdict": "allow", "summary": "Fine.", "message": ""})

    def args(self):
        return self.run.call_args.args[0]

    def test_returns_structured_output_and_builds_the_command(self):
        environ = {"PATH": "/usr/bin", "CLAUDECODE": "1", "CLAUDE_PID": "7", "CLAUDE_EFFORT": "low",
                   "CLAUDE_CODE_ENTRYPOINT": "cli", "CLAUDE_CODE_SSE_PORT": "1234"}
        with patch.dict(brain.os.environ, environ, clear=True):
            result = brain.consult(self.root, "permission", "CONTEXT")
        self.assertEqual(result["verdict"], "allow")
        args, options = self.args(), self.run.call_args.kwargs
        self.assertEqual(args[:2], ["/usr/local/bin/claude", "-p"])
        self.assertEqual(args[args.index("--model") + 1], "fable")
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), brain.schema("permission"))
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(args[args.index("--allowedTools") + 1:args.index("--permission-mode")], ["Read", "Grep", "Glob"])
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--safe-mode", args)
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("--add-dir", args)
        self.assertNotIn("updatedPermissions", " ".join(args))
        self.assertEqual(options["input"], "CONTEXT")
        self.assertEqual(options["timeout"], 150)
        self.assertEqual(Path(options["cwd"]), self.root / "manager")
        self.assertTrue((self.root / "manager").is_dir())
        self.assertEqual(options["env"], {"PATH": "/usr/bin", "BEADS_DIR": str(self.root / ".beads")})
        self.assertIn("You are the manager", args[args.index("--system-prompt") + 1])

    def test_every_failure_returns_none(self):
        for outcome in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="not json"),
                        Mock(returncode=0, stdout="[]"),
                        Mock(returncode=0, stdout=json.dumps({"is_error": True, "structured_output": {"verdict": "allow", "summary": "x", "message": ""}})),
                        Mock(returncode=0, stdout=json.dumps({"structured_output": {"verdict": "allow"}})),
                        Mock(returncode=0, stdout=json.dumps({"structured_output": "allow"})),
                        subprocess.TimeoutExpired("claude", 150), OSError("gone")):
            with self.subTest(outcome=outcome):
                if isinstance(outcome, BaseException):
                    self.run.side_effect, self.run.return_value = outcome, None
                else:
                    self.run.side_effect, self.run.return_value = None, outcome
                self.assertIsNone(brain.consult(self.root, "permission", "CONTEXT"))
        self.run.side_effect, self.run.return_value = None, reply({"verdict": "allow", "summary": "Fine.", "message": ""})
        self.which.return_value = None
        self.run.reset_mock()
        self.assertIsNone(brain.consult(self.root, "permission", "CONTEXT"))
        self.run.assert_not_called()

    def test_unknown_trigger_returns_none(self):
        self.assertIsNone(brain.consult(self.root, "nonsense", "CONTEXT"))
        self.run.assert_not_called()

    def test_failure_is_traced_without_content(self):
        self.run.return_value = Mock(returncode=0, stdout="SECRET REPLY")
        self.assertIsNone(brain.consult(self.root, "stop", "SECRET CONTEXT", timeout=270))
        self.assertEqual(self.run.call_args.kwargs["timeout"], 270)
        stages = " ".join(call.args[0] for call in self.record.call_args_list)
        self.assertIn("stop", stages)
        self.assertNotIn("SECRET", stages)

    def test_investigate_adds_read_only_lookups(self):
        brain.consult(self.root, "stop", "CONTEXT", investigate=True)
        args = self.args()
        self.assertEqual(args[args.index("--tools") + 1], "Read,Grep,Glob,WebFetch,WebSearch,Bash")
        rules = args[args.index("--allowedTools") + 1:args.index("--permission-mode")]
        self.assertIn("Bash(git log *)", rules)
        self.assertIn("Bash(bd children *)", rules)
        self.assertIn("WebSearch", rules)
        self.assertEqual(len(rules), 19)
        self.assertNotIn("Bash", rules)
        self.assertFalse([r for r in rules if r.startswith("Bash") and not r.endswith(" *)")])

    def test_config_override(self):
        (self.root / "manager").mkdir()
        (self.root / "manager/config.json").write_text(json.dumps({"model": "opus", "effort": "max"}))
        self.assertEqual(brain.config(self.root), {"model": "opus", "effort": "max"})
        brain.consult(self.root, "plan", "CONTEXT")
        args = self.args()
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertEqual(args[args.index("--effort") + 1], "max")

    def test_config_ignores_bad_files(self):
        self.assertEqual(brain.config(self.root), {"model": "fable", "effort": "high"})
        (self.root / "manager").mkdir()
        for text in ("not json", "[1, 2]"):
            (self.root / "manager/config.json").write_text(text)
            self.assertEqual(brain.config(self.root), {"model": "fable", "effort": "high"})

    def test_remember_then_notes_round_trips_into_the_prompt(self):
        self.assertEqual(brain.notes(self.root), "")
        brain.remember(self.root, ["Prefer SQLite.", "", "Ship small PRs."])
        brain.remember(self.root, ["Never touch prod."])
        self.assertEqual(brain.notes(self.root), "- Prefer SQLite.\n- Ship small PRs.\n- Never touch prod.\n")
        brain.consult(self.root, "permission", "CONTEXT")
        prompt = self.args()[self.args().index("--system-prompt") + 1]
        self.assertTrue(prompt.endswith("\n\n# Your notes\n- Prefer SQLite.\n- Ship small PRs.\n- Never touch prod.\n"))

    def test_sessions(self):
        brain.consult(self.root, "chat", "CONTEXT", session="abc", resume=True)
        args = self.args()
        self.assertEqual(args[args.index("--resume") + 1], "abc")
        self.assertNotIn("--session-id", args)
        self.assertNotIn("--no-session-persistence", args)
        brain.consult(self.root, "chat", "CONTEXT", session="abc")
        args = self.args()
        self.assertEqual(args[args.index("--session-id") + 1], "abc")
        self.assertNotIn("--resume", args)
        self.assertNotIn("--no-session-persistence", args)

    def test_add_dir_only_for_existing_folders(self):
        existing = self.root / "project"
        existing.mkdir()
        brain.consult(self.root, "sweep", "CONTEXT", folders=(str(existing), str(self.root / "missing")))
        args = self.args()
        self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == "--add-dir"], [str(existing)])

    def test_schema_verdicts(self):
        expected = {"permission": ["allow", "deny", "ask"], "denied": ["approve", "uphold", "ask"],
                    "question": ["answer", "ask"], "plan": ["approve", "revise", "ask"],
                    "stop": ["stop", "continue", "ask"]}
        for trigger, verdicts in expected.items():
            s = brain.schema(trigger)
            self.assertEqual(s["type"], "object")
            self.assertEqual(s["properties"]["verdict"]["enum"], verdicts)
            self.assertEqual(sorted(s["required"]), ["message", "summary", "verdict"])
            self.assertTrue({"ask", "decisions", "remember"} <= set(s["properties"]))
        self.assertIn("answers", brain.schema("question")["properties"])
        self.assertNotIn("answers", brain.schema("stop")["properties"])

    def test_sweep_and_chat_schemas(self):
        sweep, chat = brain.schema("sweep"), brain.schema("chat")
        self.assertEqual(sorted(sweep["required"]), ["actions", "briefing", "summary"])
        self.assertEqual(sorted(chat["required"]), ["actions", "reply"])
        action = sweep["properties"]["actions"]["items"]
        self.assertEqual(action["properties"]["action"]["enum"], ["tell", "restart"])
        self.assertEqual(chat["properties"]["actions"], sweep["properties"]["actions"])
        self.assertIn("asks", sweep["properties"])
        self.assertIn("remember", chat["properties"])
        self.assertNotIn("verdict", sweep["properties"])

    def test_required_keys_follow_the_trigger(self):
        self.run.return_value = reply({"reply": "Done.", "actions": []})
        self.assertEqual(brain.consult(self.root, "chat", "CONTEXT"), {"reply": "Done.", "actions": []})
        self.assertIsNone(brain.consult(self.root, "sweep", "CONTEXT"))


if __name__ == "__main__":
    unittest.main()
