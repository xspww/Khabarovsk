"""Regression: PEP 810 lazy executors must resolve even with Qt loaded.

Python 3.15 declares ThreadPoolExecutor/ProcessPoolExecutor as `lazy from`
names. Once shiboken6 (any PySide6 import) is loaded, touching them returns
a dead `lazy_import` placeholder, which killed QtWebView2Widget construction
and silently dropped the app into the browser fallback. No GUI needed here
-- the shiboken import alone reproduces the poison.
"""
import unittest


class TestStdlibExecutors(unittest.TestCase):
    def test_resolve_after_shiboken(self):
        import concurrent.futures as cf

        import shiboken6  # noqa: F401  (poisons lazy resolution on py3.15)

        from desktop.webview_window import _resolve_stdlib_executors

        _resolve_stdlib_executors()
        self.assertIs(cf.ThreadPoolExecutor, __import__("concurrent.futures.thread", fromlist=["x"]).ThreadPoolExecutor)
        self.assertIs(cf.ProcessPoolExecutor, __import__("concurrent.futures.process", fromlist=["x"]).ProcessPoolExecutor)
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(lambda: 42).result(), 42)


if __name__ == "__main__":
    unittest.main()
