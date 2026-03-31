import unittest

from tests._loader import load_handler_module


class LaterEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_priority_entries_are_selected_first(self):
        content = """# LATER
- [ ] normal one
- [!] urgent one
- [ ] normal two
- [x] done item
"""
        entries = self.handler.parse_later_entries(content, priority_marker="[!]")
        selected = self.handler.select_entries(entries, max_entries=3)

        self.assertEqual([e.text for e in selected], ["urgent one", "normal one", "normal two"])

    def test_ids_are_deterministic_and_unique_for_duplicates(self):
        content = """- [ ] duplicate task
- [ ] duplicate task
"""
        entries_first = self.handler.parse_later_entries(content, priority_marker="[!]")
        entries_second = self.handler.parse_later_entries(content, priority_marker="[!]")

        ids_first = [e.id for e in entries_first]
        ids_second = [e.id for e in entries_second]

        self.assertEqual(ids_first, ids_second)
        self.assertEqual(len(set(ids_first)), 2)

    def test_empty_content_returns_no_entries(self):
        self.assertEqual(self.handler.parse_later_entries(""), [])
        self.assertEqual(self.handler.parse_later_entries("\n\n"), [])

    def test_all_completed_items_returns_no_pending(self):
        content = "- [x] done one\n- [x] done two\n"
        entries = self.handler.parse_later_entries(content)
        self.assertEqual(entries, [])

    def test_malformed_lines_are_ignored(self):
        content = "# LATER\n\nSome prose text.\n- [ ] valid task\n- invalid\n[ ] no dash\n"
        entries = self.handler.parse_later_entries(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].text, "valid task")

    def test_custom_priority_marker(self):
        content = "- [ ] normal\n- [*] starred priority\n"
        entries = self.handler.parse_later_entries(content, priority_marker="[*]")
        self.assertEqual(len(entries), 2)
        priority = [e for e in entries if e.is_priority]
        self.assertEqual(len(priority), 1)
        self.assertEqual(priority[0].text, "starred priority")

    def test_select_entries_respects_max(self):
        content = "\n".join(f"- [ ] task {i}" for i in range(10))
        entries = self.handler.parse_later_entries(content)
        selected = self.handler.select_entries(entries, max_entries=3)
        self.assertEqual(len(selected), 3)

    def test_select_entries_zero_max_returns_empty(self):
        content = "- [ ] some task\n"
        entries = self.handler.parse_later_entries(content)
        self.assertEqual(self.handler.select_entries(entries, max_entries=0), [])

    def test_is_priority_flag_set_correctly(self):
        content = "- [!] urgent\n- [ ] normal\n"
        entries = self.handler.parse_later_entries(content)
        by_text = {e.text: e for e in entries}
        self.assertTrue(by_text["urgent"].is_priority)
        self.assertFalse(by_text["normal"].is_priority)

    def test_section_assigned_to_entries_under_header(self):
        content = """# LATER

## Tests
- [ ] Add integration tests

## Reports
- [ ] Generate perf report
"""
        entries = self.handler.parse_later_entries(content)
        self.assertEqual(len(entries), 2)
        by_text = {e.text: e for e in entries}
        self.assertEqual(by_text["Add integration tests"].section, "Tests")
        self.assertEqual(by_text["Generate perf report"].section, "Reports")

    def test_entries_before_any_section_have_none_section(self):
        content = "# LATER\n\n- [ ] no section task\n\n## Tests\n- [ ] sectioned task\n"
        entries = self.handler.parse_later_entries(content)
        by_text = {e.text: e for e in entries}
        self.assertIsNone(by_text["no section task"].section)
        self.assertEqual(by_text["sectioned task"].section, "Tests")

    def test_section_not_confused_with_task_lines(self):
        content = "## Security\n- [!] Fix injection\n- [ ] normal\n"
        entries = self.handler.parse_later_entries(content)
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertEqual(entry.section, "Security")

    def test_priority_ordering_preserved_across_sections(self):
        content = """## Reports
- [ ] report task

## Security
- [!] urgent fix
"""
        entries = self.handler.parse_later_entries(content)
        selected = self.handler.select_entries(entries, max_entries=2)
        self.assertEqual(selected[0].text, "urgent fix")
        self.assertEqual(selected[1].text, "report task")


if __name__ == "__main__":
    unittest.main()
