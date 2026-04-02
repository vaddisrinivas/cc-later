"""Tests for the report generator."""

import tempfile
import unittest
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.models import LaterEntry
from cc_later.reporter import generate_dispatch_report, save_report


def _entry(text: str, task_id: str = "t_test") -> LaterEntry:
    return LaterEntry(
        id=task_id, text=text, is_priority=False,
        line_index=0, raw_line=f"- [ ] {text}",
    )


class ReportGenerationTests(unittest.TestCase):

    def test_report_contains_repo_name(self):
        report = generate_dispatch_report(
            repo_path=Path("/home/user/my-project"),
            entries=[_entry("fix bug", "t_1")],
            results={"t_1": "DONE"},
        )
        self.assertIn("my-project", report)

    def test_report_categorizes_results(self):
        entries = [
            _entry("done task", "t_1"),
            _entry("failed task", "t_2"),
            _entry("skipped task", "t_3"),
        ]
        results = {"t_1": "DONE", "t_2": "FAILED", "t_3": "SKIPPED"}
        report = generate_dispatch_report(
            repo_path=Path("/repo"),
            entries=entries,
            results=results,
        )
        self.assertIn("## Completed", report)
        self.assertIn("## Failed", report)
        self.assertIn("## Skipped", report)
        self.assertIn("done task", report)
        self.assertIn("failed task", report)

    def test_report_includes_summary_line(self):
        entries = [_entry("task", "t_1")]
        report = generate_dispatch_report(
            repo_path=Path("/repo"), entries=entries,
            results={"t_1": "DONE"},
        )
        self.assertIn("1/1 completed", report)

    def test_report_shows_model(self):
        report = generate_dispatch_report(
            repo_path=Path("/repo"),
            entries=[_entry("task", "t_1")],
            results={"t_1": "DONE"},
            model="opus",
        )
        self.assertIn("opus", report)


class SaveReportTests(unittest.TestCase):

    def test_save_creates_report_file(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            report = "# Test Report\nContent here."
            path = save_report(repo, report)
            self.assertTrue(path.exists())
            self.assertIn("later-", path.name)
            content = path.read_text(encoding="utf-8")
            self.assertIn("Test Report", content)

    def test_save_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            save_report(repo, "# First")
            path = save_report(repo, "# Second")
            content = path.read_text(encoding="utf-8")
            self.assertIn("First", content)
            self.assertIn("Second", content)
            self.assertIn("---", content)


if __name__ == "__main__":
    unittest.main()
