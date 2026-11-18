"""Execute the production inspector completion handler with a fake store."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


class CompletionMenuTests(unittest.TestCase):
    def test_completion_preserves_expansion_and_reopening_reveals_active(self):
        source = (Path(__file__).resolve().parents[1] / 'Sources/Meridian.swift').read_text()
        handler = re.search(
            r'(let reopening = stream.state == "Done".*?)\n            Task \{\n'
            r'(.*?)\n            \}', source, re.S,
        )
        self.assertIsNotNone(handler, 'Could not locate production completion handler')
        harness = r'''
enum Inbox: CaseIterable { case active, attention, review, done }
struct Stream { var state: String; let id = "test" }
final class Store {
    var succeeds = true
    var calls: [[String]] = []
    func perform(_ args: [String]) async -> String? {
        calls.append(args)
        return succeeds ? "ok" : nil
    }
}
final class Dashboard {
    var expanded: Set<Inbox> = []
    let store = Store()
    func complete(_ stream: Stream, keepTasksOpen: Bool) async {
HANDLER
    }
}
@main struct Test {
    static func main() async {
        // Every sidebar expansion combination, including Completed open and closed.
        for mask in 0..<16 {
            let initial = Set(Inbox.allCases.enumerated().compactMap {
                mask & (1 << $0.offset) == 0 ? nil : $0.element
            })
            for state in ["Working", "Done"] {
                for succeeds in [false, true] {
                    for force in [false, true] {
                        let dashboard = Dashboard()
                        dashboard.expanded = initial
                        dashboard.store.succeeds = succeeds
                        await dashboard.complete(Stream(state: state), keepTasksOpen: force)
                        var expected = initial
                        if state == "Done" && succeeds { expected.insert(.active) }
                        precondition(dashboard.expanded == expected,
                                     "Expansion changed unexpectedly: mask=\(mask), state=\(state), success=\(succeeds)")
                        let args = [state == "Done" ? "reopen" : "close", "test"]
                            + (force && state != "Done" ? ["--force"] : [])
                        precondition(dashboard.store.calls == [args])
                    }
                }
            }
        }
        print("PASS: 128 completion/reopening cases")
    }
}
'''.replace('HANDLER', handler[1] + '\n' + handler[2])
        with tempfile.TemporaryDirectory(prefix='completion-menu-') as directory:
            main = Path(directory) / 'Harness.swift'
            binary = Path(directory) / 'test'
            main.write_text(harness)
            subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(main),
                            '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    unittest.main()
