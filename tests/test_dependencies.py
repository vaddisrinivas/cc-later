"""Tests for task dependencies — parsing, DAG filtering, dispatch ordering."""

import unittest
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.parser import parse_later_entries, select_entries


class DependencyParsingTests(unittest.TestCase):

    def test_dependency_extracted_from_entry(self):
        content = "- [ ] Fix the bug (after: t_abc123)\n"
        entries = parse_later_entries(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].depends_on, "t_abc123")
        self.assertEqual(entries[0].text, "Fix the bug")

    def test_no_dependency_when_absent(self):
        content = "- [ ] Fix the bug\n"
        entries = parse_later_entries(content)
        self.assertIsNone(entries[0].depends_on)

    def test_dependency_stripped_from_text(self):
        content = "- [ ] Add tests for auth fix (after: t_def456)\n"
        entries = parse_later_entries(content)
        self.assertEqual(entries[0].text, "Add tests for auth fix")
        self.assertNotIn("after:", entries[0].text)


class DependencySelectionTests(unittest.TestCase):

    def test_dependent_task_filtered_when_dependency_not_done(self):
        content = (
            "- [ ] Audit auth flow\n"
            "- [ ] Fix auth bypass (after: t_abc123)\n"
        )
        entries = parse_later_entries(content)
        selected = select_entries(entries, max_entries=10, completed_ids=set())
        # Only the first task should be selected (second depends on t_abc123 which isn't done)
        texts = [e.text for e in selected]
        self.assertIn("Audit auth flow", texts)
        self.assertNotIn("Fix auth bypass", texts)

    def test_dependent_task_included_when_dependency_done(self):
        content = (
            "- [ ] Audit auth flow\n"
            "- [ ] Fix auth bypass (after: t_abc123)\n"
        )
        entries = parse_later_entries(content)
        selected = select_entries(entries, max_entries=10, completed_ids={"t_abc123"})
        texts = [e.text for e in selected]
        self.assertIn("Audit auth flow", texts)
        self.assertIn("Fix auth bypass", texts)

    def test_tasks_without_dependencies_always_eligible(self):
        content = (
            "- [ ] Independent task\n"
            "- [ ] Dependent task (after: t_xyz)\n"
        )
        entries = parse_later_entries(content)
        selected = select_entries(entries, max_entries=10, completed_ids=set())
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].text, "Independent task")


if __name__ == "__main__":
    unittest.main()
