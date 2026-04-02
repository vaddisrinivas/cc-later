"""Tests for complexity estimation and model routing."""

import unittest
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from cc_later.parser import estimate_complexity, route_model
from cc_later.models import LaterEntry


def _entry(text: str, section: str = None, is_priority: bool = False) -> LaterEntry:
    return LaterEntry(
        id="t_test", text=text, is_priority=is_priority,
        line_index=0, raw_line=f"- [ ] {text}", section=section,
    )


class ComplexityTests(unittest.TestCase):

    def test_simple_remove_task_low_complexity(self):
        e = _entry("Remove dead import in utils.py")
        self.assertLessEqual(estimate_complexity(e), 2)

    def test_audit_task_higher_complexity(self):
        e = _entry("Audit error handling in auth.py and middleware/session.py — multiple bare except clauses")
        self.assertGreaterEqual(estimate_complexity(e), 3)

    def test_security_section_bumps_score(self):
        e1 = _entry("Fix SQL injection in filter.py", section="Security")
        e2 = _entry("Fix SQL injection in filter.py", section="Refactor")
        self.assertGreater(estimate_complexity(e1), estimate_complexity(e2))

    def test_priority_flag_bumps_score(self):
        e1 = _entry("Fix the bug", is_priority=True)
        e2 = _entry("Fix the bug", is_priority=False)
        self.assertGreater(estimate_complexity(e1), estimate_complexity(e2))

    def test_multi_file_refs_bump_score(self):
        e1 = _entry("Fix issues in auth.py, session.py, and middleware.py")
        e2 = _entry("Fix issue in auth.py")
        self.assertGreater(estimate_complexity(e1), estimate_complexity(e2))

    def test_complexity_bounded_1_to_5(self):
        for text in ["Check x", "Redesign entire auth system in auth.py, session.py, middleware.py with multi-layer refactoring"]:
            e = _entry(text, section="Security", is_priority=True)
            c = estimate_complexity(e)
            self.assertGreaterEqual(c, 1)
            self.assertLessEqual(c, 5)


class ModelRoutingTests(unittest.TestCase):

    def test_fixed_routing_ignores_complexity(self):
        e = _entry("Audit everything", section="Security", is_priority=True)
        self.assertEqual(route_model(e, "sonnet", "fixed"), "sonnet")

    def test_auto_routing_uses_opus_for_complex(self):
        e = _entry("Audit error handling in auth.py, session.py, middleware.py — bare excepts everywhere",
                    section="Security", is_priority=True)
        model = route_model(e, "sonnet", "auto")
        self.assertEqual(model, "opus")

    def test_auto_routing_uses_haiku_for_simple(self):
        e = _entry("Check import in utils.py")
        model = route_model(e, "sonnet", "auto")
        self.assertEqual(model, "haiku")

    def test_auto_routing_defaults_to_sonnet(self):
        e = _entry("Fix the bug in auth.py")
        model = route_model(e, "sonnet", "auto")
        # Fix + single file = medium complexity = sonnet
        self.assertEqual(model, "sonnet")


if __name__ == "__main__":
    unittest.main()
