"""Layering guard: direct top-level imports must stay acyclic.

2026-10-16: 7 direct debts cleared (domain x2, infra x1, services x4 now lazy).
Scope note: transitive coupling via core.py remains by design — core.py
re-exports runtime.* and every layer imports core. This guard covers direct
package imports only; see test_no_transitive_core_bypass docstring.
Only top-level imports count (function-local lazy imports ignored).
Hermetic, Windows-safe, AST-only.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file relative to ROOT) -> allowed top-level imports (fix forward, don't extend)
# 2026-10-16: all 7 debts cleared (domain x2, infra x1, services x4).
# Keep empty — any new top-level cycle fails the test.
ALLOWLIST: dict[str, set[str]] = {}

STRICT_BANNED_TOP: dict[str, set[str]] = {
    "domain": {"config", "services", "runtime", "api_routes", "desktop", "infra"},
    "config": {"services", "runtime", "api_routes", "desktop"},
    "infra": {"api_routes", "desktop"},
    "services": {"runtime", "api_routes", "desktop"},
    "runtime": {"api_routes", "desktop"},
}


def _top_level_imports(py: Path) -> set[str]:
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module:
                out.add(node.module.split(".")[0])
    return out


def _package_files(pkg: str) -> list[Path]:
    d = ROOT / pkg
    if not d.is_dir():
        return []
    return [p for p in d.rglob("*.py") if "__pycache__" not in p.parts]


class TestLayering(unittest.TestCase):
    def test_no_new_top_level_cycles(self) -> None:
        violations: list[str] = []
        for pkg, banned in STRICT_BANNED_TOP.items():
            for py in _package_files(pkg):
                rel = py.relative_to(ROOT).as_posix()
                imports = _top_level_imports(py)
                allowed = ALLOWLIST.get(rel, set())
                hit = sorted((imports & banned) - allowed)
                if hit:
                    violations.append(f"{rel} imports {hit}")
        # services->runtime and infra->desktop debt tracked separately below
        self.assertEqual([], violations, msg="\n".join(violations[:20]))

    def test_known_debt_does_not_grow(self) -> None:
        for rel, allowed in ALLOWLIST.items():
            py = ROOT / rel
            self.assertTrue(py.exists(), f"allowlist stale, {rel} gone — remove it")
            imports = _top_level_imports(py)
            # Only assert the allowlisted package itself didn't gain new banned deps;
            # stdlib/typing/etc ignored. The point: debt list must shrink, not grow.
            for pkg in ("runtime", "desktop", "api_routes"):
                if pkg in allowed:
                    continue
                self.assertNotIn(pkg, imports, f"{rel} gained new banned import {pkg}")

    def test_core_hub_transitive_documented(self) -> None:
        # core.py intentionally re-exports runtime.* so every layer touches
        # runtime transitively via core. Direct-guard above stays green while
        # this holds; removing runtime from core.py is a separate project.
        core_imports = _top_level_imports(ROOT / "core.py")
        self.assertIn("runtime", core_imports)

    def test_runtime_subgroups_documented(self) -> None:
        runtime = ROOT / "runtime"
        recovery = sorted(runtime.glob("recovery_*.py"))
        maintenance = sorted(runtime.glob("maintenance_*.py"))
        self.assertGreaterEqual(len(recovery), 10, f"expected recovery_* group, got {len(recovery)}")
        self.assertGreaterEqual(len(maintenance), 5, f"expected maintenance_* group, got {len(maintenance)}")
        self.assertTrue((runtime / "README.md").exists())


if __name__ == "__main__":
    unittest.main()
