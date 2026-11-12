"""Pull request details shown in the inspector's Changes list."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Sources"))
with tempfile.TemporaryDirectory() as home:
    with patch.dict(os.environ, MUXTEROID_HOME=home):
        import streams

links = streams.links
URL = "https://github.com/example/project/pull/12"


def run(status, conclusion=""):
    return {"__typename": "CheckRun", "status": status, "conclusion": conclusion}


def reply(**changes):
    return json.dumps({"title": "Fix the mute button", "state": "OPEN", "isDraft": False,
                       "additions": 34, "deletions": 9, "statusCheckRollup": [], **changes})


class PullDetailsTests(unittest.TestCase):
    def test_running_checks_report_how_many_finished(self):
        checks = [run("COMPLETED", "SUCCESS"), run("COMPLETED", "SKIPPED"), run("IN_PROGRESS"),
                  {"__typename": "StatusContext", "state": "PENDING"},
                  {"__typename": "StatusContext", "state": "SUCCESS"}]
        self.assertEqual(links.summarize(json.loads(reply(statusCheckRollup=checks))),
                         dict(title="Fix the mute button", state="OPEN", additions=34, deletions=9,
                              checks=5, finished=3, failed=0))

    def test_failed_checks_are_counted_from_both_check_kinds(self):
        checks = [run("COMPLETED", "FAILURE"), run("COMPLETED", "NEUTRAL"), run("COMPLETED", "TIMED_OUT"),
                  {"__typename": "StatusContext", "state": "ERROR"}]
        row = links.summarize(json.loads(reply(statusCheckRollup=checks)))
        self.assertEqual((row["checks"], row["finished"], row["failed"]), (4, 4, 3))

    def test_draft_is_reported_only_while_open(self):
        self.assertEqual(links.summarize(json.loads(reply(isDraft=True)))["state"], "DRAFT")
        self.assertEqual(links.summarize(json.loads(reply(isDraft=True, state="CLOSED")))["state"], "CLOSED")

    def test_pull_without_checks_or_with_odd_values_still_decodes(self):
        row = links.summarize(dict(title=None, state="MERGED", additions="many", statusCheckRollup=None))
        self.assertEqual(row, dict(title="", state="MERGED", additions=None, deletions=None,
                                   checks=0, finished=0, failed=0))

    def test_unreachable_and_unrelated_urls_are_left_as_plain_links(self):
        private = "https://github.com/example/private/pull/3"
        asked = []

        def gh(args, cwd, timeout=4):
            asked.append(args[3])
            return reply() if args[3] == URL else ""

        with patch.object(links, "command_output", side_effect=gh):
            found = links.pull_details([URL, private, URL, "https://example.com/pull/1", "--web"])
        self.assertEqual(sorted(asked), sorted([URL, private]))
        self.assertEqual(list(found), [URL])
        self.assertEqual(found[URL]["additions"], 34)

    def test_bridge_prints_details_keyed_by_url(self):
        output = io.StringIO()
        with patch.object(links, "command_output", return_value=reply(state="MERGED")), \
                patch.object(streams.manager, "pull_messages"), \
                contextlib.redirect_stdout(output):
            streams.main("pulls", URL)
        self.assertEqual(json.loads(output.getvalue())[URL]["state"], "MERGED")


if __name__ == "__main__":
    unittest.main()
