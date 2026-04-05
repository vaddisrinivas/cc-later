import unittest

from tests._loader import load_handler_module


class ConfigValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def test_accepts_minimal_valid_config(self):
        cfg = self.handler.validate_config_dict(
            {
                "paths": {"watch": ["~/projects/my-repo"]},
                "dispatch": {"enabled": True},
            }
        )
        self.assertTrue(cfg.dispatch.enabled)
        self.assertEqual(cfg.paths.watch, ["~/projects/my-repo"])
        self.assertEqual(cfg.window.dispatch_mode, "window_aware")

    def test_rejects_unknown_top_level_key(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {
                    "paths": {"watch": ["~/projects/my-repo"]},
                    "dispatch": {"enabled": True},
                    "unknown": {"foo": "bar"},
                }
            )

    def test_rejects_unknown_nested_key(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {
                    "paths": {"watch": ["~/projects/my-repo"]},
                    "dispatch": {"enabled": True},
                    "window": {"dispatch_mode": "always", "bogus": True},
                }
            )

    def test_rejects_invalid_dispatch_mode(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {"window": {"dispatch_mode": "invalid_mode"}}
            )

    def test_rejects_invalid_model(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {"dispatch": {"model": "gpt-4"}}
            )

    def test_rejects_invalid_mark_completed(self):
        with self.assertRaises(self.handler.ConfigError):
            self.handler.validate_config_dict(
                {"later_md": {"mark_completed": "archive"}}
            )

    def test_empty_config_uses_all_defaults(self):
        cfg = self.handler.validate_config_dict({})
        self.assertFalse(cfg.dispatch.enabled)
        self.assertEqual(cfg.window.dispatch_mode, "window_aware")
        self.assertEqual(cfg.later_md.max_entries_per_dispatch, 3)
        self.assertFalse(cfg.dispatch.allow_file_writes)

    def test_accepts_all_valid_dispatch_modes(self):
        for mode in ("window_aware", "time_based", "always"):
            cfg = self.handler.validate_config_dict(
                {"window": {"dispatch_mode": mode}}
            )
            self.assertEqual(cfg.window.dispatch_mode, mode)

    def test_accepts_both_valid_models(self):
        for model in ("sonnet", "opus"):
            cfg = self.handler.validate_config_dict(
                {"dispatch": {"model": model}}
            )
            self.assertEqual(cfg.dispatch.model, model)

    def test_accepts_auto_resume_section(self):
        cfg = self.handler.validate_config_dict(
            {"auto_resume": {"enabled": True, "min_remaining_minutes": 240}}
        )
        self.assertTrue(cfg.auto_resume.enabled)
        self.assertEqual(cfg.auto_resume.min_remaining_minutes, 240)


if __name__ == "__main__":
    unittest.main()
