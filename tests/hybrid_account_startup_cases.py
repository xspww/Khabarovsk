from tests.hybrid_account_fixture import *


class HybridAccountStartupCases:
    def test_console_header_sets_cronus_console_icon(self):
        import inspect
        import desktop_host

        header_source = inspect.getsource(desktop_host._console_header)
        icon_source = inspect.getsource(desktop_host._set_console_window_icon)
        ensure_source = inspect.getsource(desktop_host._ensure_console_icon_file)

        self.assertIn("_set_console_window_icon()", header_source)
        self.assertIn("WM_SETICON", icon_source)
        self.assertIn("SetClassLongPtrW", icon_source)
        self.assertIn("APP_ICON_FILE", ensure_source)
        self.assertIn("cronus_console_icon.ico", ensure_source)

    def test_startup_progress_updates_inline_and_clears_after_window_open(self):
        import inspect
        import desktop_host

        with patch.object(desktop_host, "_console_write_inline") as write_inline:
            desktop_host._console_startup_progress(55, "Starting FastAPI server")

        output = "".join(str(call.args[0]) for call in write_inline.call_args_list)
        self.assertIn("\r", output)
        self.assertIn("55%", output)
        self.assertIn("Starting FastAPI server", output)
        self.assertIn("3/6", output)
        self.assertIn("█", output)
        self.assertIn("░", output)
        self.assertTrue(any(frame in output for frame in desktop_host._STARTUP_SPINNER_FRAMES))
        self.assertIn("[", output)
        self.assertIn("]", output)

        with patch.object(desktop_host, "_console_write_inline") as write_inline, \
             patch.object(desktop_host, "_console_clear_startup_screen") as clear_screen:
            desktop_host._console_startup_progress(100, "Opening desktop window")
            desktop_host._console_finish_startup(clear=True)

        clear_screen.assert_called_once()
        clear_output = "".join(str(call.args[0]) for call in write_inline.call_args_list)
        self.assertIn("\r", clear_output)

        window_source = inspect.getsource(desktop_host._run_desktop_window)
        self.assertLess(window_source.index("window.show()"), window_source.index("_console_finish_after_window_show()"))
        run_source = inspect.getsource(desktop_host.run_desktop)
        self.assertIn("_console_clear_after_window_show(ready)", run_source)
        self.assertIn("_console_finish_startup(clear=ready)", run_source)

    def test_startup_progress_uses_navy_blue_terminal_theme(self):
        import desktop_host

        with patch.object(desktop_host, "_startup_colors_enabled", return_value=True):
            line = desktop_host._startup_progress_line(55, "Starting FastAPI server")

        self.assertIn(desktop_host._COLOR_NAVY_BLUE, line)
        self.assertIn(desktop_host._COLOR_NAVY_TEXT, line)
        self.assertNotIn("139;92;246", line)
        self.assertNotIn("236;72;153", line)

    def test_direct_startup_preflight_reports_requirements_before_progress(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("fastapi>=0.110\nuvicorn[standard]>=0.29\n", encoding="utf-8")

            versions = {"fastapi": "0.111.0", "uvicorn": "0.30.0"}
            with patch.object(main, "_startup_distribution_version", side_effect=lambda name: versions.get(name)), \
                 patch.object(main, "_startup_import_available", return_value=True), \
                 patch.object(main, "_startup_console_write") as write_line:
                ok = main._run_startup_dependency_checks(str(req), exit_on_failure=False)

        self.assertTrue(ok)
        output = "\n".join(str(call.args[0]) for call in write_line.call_args_list)
        self.assertIn("✓ fastapi v0.111.0", output)
        self.assertIn("✓ uvicorn v0.30.0", output)
        self.assertIn("✓ Runtime models: none required", output)
        self.assertNotIn("Installing", output)

    def test_direct_startup_preflight_blocks_missing_requirements(self):
        import main

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "requirements.txt"
            req.write_text("PySide6>=6.6\n", encoding="utf-8")

            with patch.object(main, "_startup_distribution_version", return_value=None), \
                 patch.object(main, "_startup_import_available", return_value=False), \
                 patch.object(main, "_startup_console_write") as write_line:
                ok = main._run_startup_dependency_checks(str(req), exit_on_failure=False)

        self.assertFalse(ok)
        output = "\n".join(str(call.args[0]) for call in write_line.call_args_list)
        self.assertIn("× PySide6 missing", output)
        self.assertIn("python -m pip install -r requirements.txt", output)
