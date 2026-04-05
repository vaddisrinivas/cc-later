import unittest

from tests._loader import load_handler_module


class AutoResumeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_module()

    def _cfg(self, enabled: bool = True, mode: str = "window_aware", min_remaining: int = 240):
        return self.handler.validate_config_dict(
            {
                "window": {"dispatch_mode": mode},
                "auto_resume": {
                    "enabled": enabled,
                    "min_remaining_minutes": min_remaining,
                },
            }
        )

    def test_gate_closed_when_disabled(self):
        cfg = self._cfg(enabled=False)
        state = self.handler.AppState()
        watch = ["/repo"]
        self.assertFalse(
            self.handler._is_auto_resume_gate_open(
                cfg=cfg,
                state=state,
                watch_repo_paths=watch,
                window_state=None,
            )
        )

    def test_gate_closed_without_pending_resume(self):
        cfg = self._cfg(enabled=True)
        state = self.handler.AppState()
        watch = ["/repo"]
        window_state = self.handler.WindowState(
            elapsed_minutes=10,
            remaining_minutes=290,
            total_input_tokens=0,
            total_output_tokens=0,
        )
        self.assertFalse(
            self.handler._is_auto_resume_gate_open(
                cfg=cfg,
                state=state,
                watch_repo_paths=watch,
                window_state=window_state,
            )
        )

    def test_gate_open_when_pending_and_new_window_has_capacity(self):
        cfg = self._cfg(enabled=True, mode="window_aware", min_remaining=240)
        state = self.handler.AppState()
        state.repos["/repo"] = self.handler.RepoState(
            resume_entries=[{"id": "t_1", "text": "task"}]
        )
        watch = ["/repo"]
        window_state = self.handler.WindowState(
            elapsed_minutes=20,
            remaining_minutes=280,
            total_input_tokens=0,
            total_output_tokens=0,
        )
        self.assertTrue(
            self.handler._is_auto_resume_gate_open(
                cfg=cfg,
                state=state,
                watch_repo_paths=watch,
                window_state=window_state,
            )
        )

    def test_gate_closed_when_pending_but_window_too_low(self):
        cfg = self._cfg(enabled=True, mode="window_aware", min_remaining=240)
        state = self.handler.AppState()
        state.repos["/repo"] = self.handler.RepoState(
            resume_entries=[{"id": "t_1", "text": "task"}]
        )
        watch = ["/repo"]
        window_state = self.handler.WindowState(
            elapsed_minutes=220,
            remaining_minutes=80,
            total_input_tokens=0,
            total_output_tokens=0,
        )
        self.assertFalse(
            self.handler._is_auto_resume_gate_open(
                cfg=cfg,
                state=state,
                watch_repo_paths=watch,
                window_state=window_state,
            )
        )

    def test_gate_open_in_non_window_aware_modes(self):
        cfg = self._cfg(enabled=True, mode="always")
        state = self.handler.AppState()
        state.repos["/repo"] = self.handler.RepoState(
            resume_entries=[{"id": "t_1", "text": "task"}]
        )
        watch = ["/repo"]
        self.assertTrue(
            self.handler._is_auto_resume_gate_open(
                cfg=cfg,
                state=state,
                watch_repo_paths=watch,
                window_state=None,
            )
        )


if __name__ == "__main__":
    unittest.main()
