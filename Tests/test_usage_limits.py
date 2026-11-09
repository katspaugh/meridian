import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
import usage_limits as usage


class ClaudeUsageTests(unittest.TestCase):
    def reply(self, data):
        return subprocess.CompletedProcess([], 0, json.dumps({
            "type": "control_response", "response": {
                "subtype": "success", "request_id": "muxteroid-usage", "response": data,
            },
        }) + "\n")

    def test_subscription_windows_and_resets(self):
        reply = self.reply({"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 32.5, "resets_at": "2099-01-01T10:00:00Z"},
            "seven_day": {"utilization": 8, "resets_at": "2099-01-05T10:00:00Z"},
        }})
        with patch.object(usage.subprocess, "run", return_value=reply):
            result = usage.claude_live()
        self.assertEqual([w["percent"] for w in result["windows"]], [32.5, 8])
        self.assertEqual([w["label"] for w in result["windows"]], ["5h", "7d"])
        self.assertTrue(all(isinstance(w["reset"], float) for w in result["windows"]))

    def test_refresh_uses_new_reading_over_cache(self):
        fresh = {"windows": [{"percent": 42}]}
        with patch.object(usage, "claude_live", return_value=fresh), \
             patch.object(usage, "claude_cached") as cached:
            self.assertEqual(usage.claude(Path("/unused")), fresh)
            cached.assert_not_called()

    def test_offline_uses_recent_cache(self):
        cached = {"windows": [{"percent": 42}], "measured": 123}
        with patch.object(usage, "claude_live", side_effect=subprocess.TimeoutExpired("claude", 12)), \
             patch.object(usage, "claude_cached", return_value=cached):
            self.assertEqual(usage.claude(Path("/unused")), cached)

    def test_signed_out_is_actionable(self):
        with patch.object(usage, "claude_live", return_value=None), \
             patch.object(usage, "claude_cached", return_value=None), \
             patch.object(usage, "claude_signed_out", return_value=True):
            result = usage.claude(Path("/unused"))
        self.assertEqual(result["status"], "Sign in")
        self.assertIn("claude auth login", result["hint"])
        self.assertEqual(result["windows"], [])

    def test_account_without_limits_does_not_show_zero_usage(self):
        with patch.object(usage.subprocess, "run", return_value=self.reply({"rate_limits_available": False})):
            result = usage.claude_live()
        self.assertEqual(result["status"], "Unavailable")
        self.assertEqual(result["windows"], [])

    def test_missing_cli_is_unavailable(self):
        with patch.object(usage.subprocess, "run", side_effect=FileNotFoundError()), \
             patch.object(usage, "claude_cached", return_value=None):
            result = usage.claude(Path("/unused"))
        self.assertEqual(result["status"], "Unavailable")


if __name__ == "__main__":
    unittest.main()
