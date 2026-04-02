"""Tests for retry logic — backoff, escalation, metadata tracking."""

import unittest
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.parser import (
    apply_retry_metadata,
    parse_later_entries,
    META_PATTERN,
)
from cc_later.models import LaterEntry


class RetryMetadataTests(unittest.TestCase):

    def test_first_failure_adds_metadata_comment(self):
        content = "- [ ] fix the bug\n"
        entries = parse_later_entries(content)
        result = apply_retry_metadata(
            content=content,
            failed_ids={entries[0].id: "FAILED"},
            dispatched_entries=entries,
            max_attempts=3,
            escalate_to_priority=True,
            now_iso="2026-04-01T12:00:00Z",
        )
        self.assertIn("<!-- cc-later: attempts=1", result)
        self.assertIn("last=2026-04-01T12:00:00Z", result)

    def test_second_failure_updates_metadata(self):
        content = (
            "- [ ] fix the bug\n"
            "  <!-- cc-later: attempts=1, last=2026-04-01T10:00:00Z -->\n"
        )
        entries = parse_later_entries(content)
        self.assertEqual(entries[0].attempts, 1)

        result = apply_retry_metadata(
            content=content,
            failed_ids={entries[0].id: "FAILED"},
            dispatched_entries=entries,
            max_attempts=3,
            escalate_to_priority=True,
            now_iso="2026-04-01T14:00:00Z",
        )
        self.assertIn("attempts=2", result)
        self.assertIn("last=2026-04-01T14:00:00Z", result)
        self.assertNotIn("attempts=1", result)

    def test_max_attempts_escalates_to_needs_human(self):
        content = (
            "- [ ] fix the bug\n"
            "  <!-- cc-later: attempts=2, last=2026-04-01T10:00:00Z -->\n"
        )
        entries = parse_later_entries(content)
        result = apply_retry_metadata(
            content=content,
            failed_ids={entries[0].id: "FAILED"},
            dispatched_entries=entries,
            max_attempts=3,
            escalate_to_priority=True,
            now_iso="2026-04-01T22:00:00Z",
        )
        self.assertIn("[?]", result)
        # Metadata comment should be removed after escalation
        self.assertFalse(META_PATTERN.search(result))

    def test_no_escalation_when_disabled(self):
        content = (
            "- [ ] fix the bug\n"
            "  <!-- cc-later: attempts=2, last=2026-04-01T10:00:00Z -->\n"
        )
        entries = parse_later_entries(content)
        result = apply_retry_metadata(
            content=content,
            failed_ids={entries[0].id: "FAILED"},
            dispatched_entries=entries,
            max_attempts=3,
            escalate_to_priority=False,
            now_iso="2026-04-01T22:00:00Z",
        )
        # When escalation is disabled, entry stays as [ ] but metadata is removed
        self.assertNotIn("[?]", result)
        self.assertIn("[ ]", result)
        self.assertFalse(META_PATTERN.search(result))

    def test_retry_metadata_preserved_through_rotation(self):
        from cc_later.parser import extract_pending_for_rotation
        content = (
            "# LATER\n\n"
            "- [ ] fix the bug\n"
            "  <!-- cc-later: attempts=1, last=2026-04-01T10:00:00Z -->\n"
            "- [x] done task\n"
        )
        fresh = extract_pending_for_rotation(content)
        self.assertIn("fix the bug", fresh)
        self.assertIn("<!-- cc-later:", fresh)
        self.assertNotIn("done task", fresh)


class RetryParsingTests(unittest.TestCase):

    def test_parse_entries_reads_attempt_count(self):
        content = (
            "- [ ] fix the bug\n"
            "  <!-- cc-later: attempts=2, last=2026-04-01T10:00:00Z -->\n"
        )
        entries = parse_later_entries(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].attempts, 2)
        self.assertEqual(entries[0].last_attempt, "2026-04-01T10:00:00Z")

    def test_parse_entries_without_metadata(self):
        content = "- [ ] normal task\n"
        entries = parse_later_entries(content)
        self.assertEqual(entries[0].attempts, 0)
        self.assertIsNone(entries[0].last_attempt)

    def test_needs_human_entries_skipped(self):
        content = "- [?] needs human intervention\n- [ ] normal task\n"
        entries = parse_later_entries(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].text, "normal task")


if __name__ == "__main__":
    unittest.main()
