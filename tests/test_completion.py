import unittest

from tests._loader import load_handler_module


class CompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_parse_result_summary_extracts_ids(self):
        text = """DONE t_aaa111: fix auth bug
SKIPPED (missing context) t_bbb222: update docs
NEEDS_HUMAN (prod-only) t_ccc333: rotate key
"""
        parsed = self.handler.parse_result_summary(text)
        self.assertEqual(parsed["t_aaa111"], "DONE")
        self.assertEqual(parsed["t_bbb222"], "SKIPPED")
        self.assertEqual(parsed["t_ccc333"], "NEEDS_HUMAN")

    def test_apply_completion_marks_only_done_entries(self):
        content = """- [ ] duplicate task
- [ ] duplicate task
- [ ] third task
"""
        entries = self.handler.parse_later_entries(content, priority_marker="[!]")
        done_id = entries[1].id

        updated = self.handler.apply_completion(
            content=content,
            done_ids={done_id},
            dispatched_entries=entries,
            mark_mode="check",
        )

        lines = [line for line in updated.splitlines() if line.startswith("- [")]
        self.assertEqual(lines[0], "- [ ] duplicate task")
        self.assertEqual(lines[1], "- [x] duplicate task")
        self.assertEqual(lines[2], "- [ ] third task")

    def test_apply_completion_delete_mode(self):
        content = """- [ ] task one
- [ ] task two
"""
        entries = self.handler.parse_later_entries(content, priority_marker="[!]")
        done_id = entries[0].id

        updated = self.handler.apply_completion(
            content=content,
            done_ids={done_id},
            dispatched_entries=entries,
            mark_mode="delete",
        )

        self.assertNotIn("- [ ] task one", updated)
        self.assertIn("- [ ] task two", updated)


if __name__ == "__main__":
    unittest.main()
