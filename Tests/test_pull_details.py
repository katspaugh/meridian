"""Pull request details shown in the inspector's Changes list."""
import contextlib
import io
import json
import os
import shlex
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


class GitHubFollowUpTests(unittest.TestCase):
    def test_direct_request_is_personal_and_closed_pulls_clear_attention(self):
        pulls = {URL: reply(reviewRequests=[{'login': 'Someone'}, {'login': 'Sam'}],
                            mergeable='CONFLICTING', reviewDecision='CHANGES_REQUESTED'),
                 URL + '0': reply(reviewRequests=[{'login': 'someone'}]),
                 URL + '1': reply(state='CLOSED', reviewRequests=[{'login': 'SAM'}],
                                  mergeable='CONFLICTING', reviewDecision='CHANGES_REQUESTED')}
        def gh(args, *rest, **kwargs):
            return 'sAm' if args[1] == 'api' else pulls[args[3]]
        with patch.object(links, 'command_output', side_effect=gh):
            rows = links.pull_details(pulls)
        self.assertEqual(rows[URL]['attention'], ['Review requested from you', 'Changes requested', 'Merge conflict'])
        self.assertNotIn('attention', rows[URL + '0'])
        self.assertNotIn('attention', rows[URL + '1'])

    def test_failed_check_keeps_its_own_job_and_log(self):
        job = 'https://github.com/example/project/actions/runs/123/job/456'
        metadata = dict(head_sha='tested-commit', steps=[dict(name='Compile', conclusion='failure')])
        check = dict(run('COMPLETED', 'FAILURE'), name='macOS', detailsUrl=job)
        row = links.summarize(json.loads(reply(statusCheckRollup=[check], headRefOid='pr-head')))
        self.assertEqual(row['failures'], [dict(name='macOS', url=job)])
        with patch.object(links, 'command_output', side_effect=[json.dumps(metadata), 'z' * 13000 + 'error']) as lookup:
            context = links.check_context(URL, 'macOS', job, row['head'])
        self.assertIn('PR head: pr-head', context)
        self.assertIn('Job commit: tested-commit', context)
        self.assertIn('Failed step: Compile', context)
        self.assertTrue(context.endswith('error'))
        self.assertLess(len(context), 12500)
        self.assertIn('456', lookup.call_args.args[0])
        with patch.object(links, 'command_output') as lookup:
            self.assertIn('https://ci.example/job', links.check_context(URL, 'external', 'https://ci.example/job', 'abc'))
            lookup.assert_not_called()

    def test_threads_include_only_unresolved_original_comments_across_pages(self):
        def thread(body, resolved):
            return dict(isResolved=resolved, isOutdated=True, path='app.swift', line=None, originalLine=17,
                        comments=dict(nodes=[dict(url=URL + '#discussion_r17', author=None, body=body)]))
        def page(*threads):
            return dict(data=dict(repository=dict(pullRequest=dict(reviewThreads=dict(nodes=threads)))))
        pages = [page(thread('Done', True)), page(thread('Keep this exact comment.\nSecond line.', False))]
        with patch.object(links, 'command_output', return_value=json.dumps(pages)) as lookup:
            threads = links.review_threads(URL)
        self.assertIn('--paginate', lookup.call_args.args[0])
        self.assertEqual(threads, [dict(url=URL + '#discussion_r17', author=None, path='app.swift', outdated=True,
                                        line=17, body='Keep this exact comment.\nSecond line.')])

    def test_handoff_delivers_context_only_to_the_selected_workstream_agent(self):
        comment = 'The reviewer said\n<untrusted>example</untrusted>.'
        args = ('mx-test', 'term-1', 'thread', URL, 'a.swift:21', comment, URL + '#discussion_r1')
        with patch.object(streams, 'terminal_issue', return_value=({'id': 'mx-other', 'status': 'open'}, 'claude')), \
             patch.object(streams.manager, 'tell') as tell, self.assertRaises(ValueError):
            streams.main('github-handoff', *args)
        tell.assert_not_called()
        with patch.object(streams, 'terminal_issue', return_value=({'id': 'mx-test', 'status': 'open'}, 'codex')), \
             patch.object(streams.manager, 'tell', return_value=True) as tell:
            streams.main('github-handoff', *args)
        _, terminal, prompt = tell.call_args.args
        self.assertEqual(terminal, 'term-1')
        self.assertTrue(tell.call_args.kwargs['user_initiated'])
        self.assertNotIn(comment, prompt)
        file = Path(shlex.split(prompt)[-1])
        self.addCleanup(file.unlink)
        context = file.read_text()
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertIn('a.swift:21', context)
        self.assertIn(URL + '#discussion_r1', context)
        self.assertIn(comment, context)


if __name__ == "__main__":
    unittest.main()
