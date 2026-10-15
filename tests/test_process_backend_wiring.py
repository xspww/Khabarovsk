"""The binder seam holds: every bound name must resolve to a callable.

Regression net for services/process_backend.py — a renamed or deleted
fragment used to surface only at runtime on a Windows box. Pure AST plus
one import; no processes, no Win32.
"""
import ast
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "services" / "process_backend.py"
_ALIAS_TO_MODULE = {
    "_processes": "services.roblox_processes",
    "_windows": "services.roblox_windows",
    "_liveness": "services.roblox_liveness",
    "_launch_service": "services.roblox_launch_service",
}


def _bindings():
    tree = ast.parse(_BACKEND.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "ProcessManager":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            call = stmt.value
            if (
                isinstance(target, ast.Name)
                and isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in {"classmethod", "staticmethod"}
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Attribute)
                and isinstance(call.args[0].value, ast.Name)
            ):
                out.append((target.id, call.func.id, call.args[0].value.id, call.args[0].attr))
    return out


class TestBackendWiring(unittest.TestCase):
    def test_every_binding_resolves(self):
        import importlib

        bindings = _bindings()
        self.assertGreater(len(bindings), 30)
        for name, kind, alias, attr in bindings:
            module = importlib.import_module(_ALIAS_TO_MODULE[alias])
            self.assertTrue(
                hasattr(module, attr), f"{alias}.{attr} (bound as {name}) is missing"
            )
            self.assertTrue(callable(getattr(module, attr)), f"{alias}.{attr} is not callable")

    def test_bound_surface_matches_manager(self):
        from services.process_backend import ProcessManager

        for name, _kind, _alias, _attr in _bindings():
            self.assertTrue(hasattr(ProcessManager, name), f"ProcessManager.{name} is missing")
            self.assertTrue(callable(getattr(ProcessManager, name)))

    def test_fragments_take_cls(self):
        import importlib
        import inspect

        for _name, kind, alias, attr in _bindings():
            module = importlib.import_module(_ALIAS_TO_MODULE[alias])
            fn = getattr(module, attr)
            params = list(inspect.signature(fn).parameters)
            if kind == "classmethod":
                self.assertTrue(params and params[0] == "cls", f"{alias}.{attr} must take cls first")


if __name__ == "__main__":
    unittest.main()
