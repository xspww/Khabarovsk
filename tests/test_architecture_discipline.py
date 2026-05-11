import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

HOTSPOT_FILE_LIMITS = {
    "farm.py": 4800,
    "main.py": 1700,
    "process_net.py": 2650,
    "core.py": 1700,
}

FORBIDDEN_DUMPING_GROUND_NAMES = {
    "utility.py",
    "utilities.py",
    "helper.py",
    "helpers.py",
    "misc.py",
    "common.py",
}

RUNTIME_OWNER_FILES = {
    "runtime/runtime_state_manager.py",
}

CRITICAL_RUNTIME_FIELDS = {
    "state",
    "desired_state",
    "pid",
    "runtime_generation",
    "recovery_generation",
    "command_generation",
    "current_command_id",
    "current_command",
    "command_inflight_started_at",
    "recovery_inflight",
    "recovery_status",
    "cooldown_until",
    "process_binding_status",
    "bound_process_identity",
    "bound_process_name",
}


def _python_files():
    ignored_parts = {".git", "__pycache__", "build", "data", "dist", "roboguard_rt1_instances"}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if any(part in ignored_parts for part in path.relative_to(ROOT).parts):
            continue
        yield rel, path


def _target_chain(node):
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def _assignment_targets(node):
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    if isinstance(node, ast.AugAssign):
        return [node.target]
    return []


class ArchitectureDisciplineTests(unittest.TestCase):
    def test_hotspot_files_stay_under_architecture_budget(self):
        for rel, max_lines in HOTSPOT_FILE_LIMITS.items():
            with self.subTest(file=rel):
                lines = (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    max_lines,
                    f"{rel} is over budget. Move new logic into a domain module instead of appending.",
                )

    def test_no_new_helper_dumping_ground_modules(self):
        offenders = [
            rel for rel, _path in _python_files()
            if Path(rel).name.lower() in FORBIDDEN_DUMPING_GROUND_NAMES
        ]
        self.assertEqual(offenders, [], "Use domain names, not utility/helper/misc/common dumping grounds.")

    def test_critical_runtime_mutation_stays_in_runtime_owner(self):
        offenders = []
        for rel, path in _python_files():
            if rel in RUNTIME_OWNER_FILES or rel.startswith("tests/"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"), filename=rel)
            for node in ast.walk(tree):
                for target in _assignment_targets(node):
                    if not isinstance(target, ast.Attribute):
                        continue
                    if target.attr not in CRITICAL_RUNTIME_FIELDS:
                        continue
                    chain = _target_chain(target)
                    if not any(part in {"acc", "account"} for part in chain[:-1]):
                        continue
                    offenders.append(f"{rel}:{node.lineno}:{'.'.join(chain)}")
        self.assertEqual(
            offenders,
            [],
            "Critical account runtime fields must be mutated through RuntimeStateManager/AccountRuntimeController.",
        )

    def test_account_runtime_controller_is_the_request_boundary(self):
        controller = ROOT / "runtime" / "account_runtime_controller.py"
        text = controller.read_text(encoding="utf-8")
        self.assertIn("class AccountRuntimeController", text)
        self.assertIn("def request_evaluate", text)
        self.assertIn("def request_rejoin", text)
        self.assertIn("handle_runtime_signal", text)


if __name__ == "__main__":
    unittest.main()
