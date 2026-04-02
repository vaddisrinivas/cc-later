"""Tests for the completion verification pipeline."""

import unittest
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.verify import verify_result, passes_threshold
from cc_later.models import LaterEntry, VerifyConfig


def _entry(text: str = "Fix the bug in auth.py") -> LaterEntry:
    return LaterEntry(
        id="t_test", text=text, is_priority=False,
        line_index=0, raw_line=f"- [ ] {text}",
    )


class VerifyResultTests(unittest.TestCase):

    def test_empty_result_gets_none_confidence(self):
        vr = verify_result(
            "t_test", _entry(), "", Path("/repo"), VerifyConfig(), False,
        )
        self.assertEqual(vr.confidence, "none")

    def test_short_result_gets_low_confidence(self):
        vr = verify_result(
            "t_test", _entry(), "Done.", Path("/repo"), VerifyConfig(), False,
        )
        self.assertIn(vr.confidence, ("none", "low"))

    def test_substantive_result_gets_medium_or_high(self):
        result = (
            "I found the bug in auth.py at line 42. The issue is that the "
            "error handling in UserService.delete() silently swallows the "
            "DatabaseException. I modified the function to properly propagate "
            "the exception and added a log statement for debugging."
        )
        vr = verify_result(
            "t_test", _entry(), result, Path("/repo"), VerifyConfig(), False,
        )
        self.assertIn(vr.confidence, ("medium", "high"))

    def test_punt_result_gets_low_confidence(self):
        result = (
            "I was unable to find the file mentioned in the task. "
            "I cannot determine where the bug is located. "
            "This would need additional context from the developer."
        )
        vr = verify_result(
            "t_test", _entry(), result, Path("/repo"), VerifyConfig(), False,
        )
        self.assertIn(vr.confidence, ("none", "low"))

    def test_result_referencing_task_terms_scores_higher(self):
        entry = _entry("Fix the N+1 query in ReportGenerator")
        result_good = (
            "Found the N+1 query in ReportGenerator.generate_report() at line 78. "
            "The issue was loading related records in a loop. Fixed by adding a "
            "prefetch_related call to the queryset."
        )
        result_bad = (
            "I looked at the codebase and made some changes. "
            "Everything should be working now. Please review."
        )
        vr_good = verify_result("t_test", entry, result_good, Path("/repo"), VerifyConfig(), False)
        vr_bad = verify_result("t_test", entry, result_bad, Path("/repo"), VerifyConfig(), False)
        levels = {"none": 0, "low": 1, "medium": 2, "high": 3}
        self.assertGreater(levels[vr_good.confidence], levels[vr_bad.confidence])


class ThresholdTests(unittest.TestCase):

    def test_passes_at_exact_threshold(self):
        from cc_later.verify import VerifyResult
        vr = VerifyResult(task_id="t_1", confidence="medium", reason="", files_changed=[])
        self.assertTrue(passes_threshold(vr, "medium"))
        self.assertTrue(passes_threshold(vr, "low"))
        self.assertFalse(passes_threshold(vr, "high"))

    def test_none_confidence_fails_all_thresholds(self):
        from cc_later.verify import VerifyResult
        vr = VerifyResult(task_id="t_1", confidence="none", reason="", files_changed=[])
        self.assertTrue(passes_threshold(vr, "none"))  # none passes none
        self.assertFalse(passes_threshold(vr, "low"))


if __name__ == "__main__":
    unittest.main()
