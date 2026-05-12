import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

HOTSPOT_FILE_LIMITS = {
    "farm.py": 3900,
    "main.py": 700,
    "process_net.py": 900,
    "core.py": 1300,
}

API_ROUTE_FILE_LIMIT = 650
SERVICE_DOMAIN_FILE_LIMIT = 650
SERVICE_DOMAIN_FILE_LIMITS = {
    "services/roblox_processes.py": 700,
}
MAINTENANCE_DOMAIN_FILE_LIMIT = 650

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

    def test_api_route_modules_stay_under_architecture_budget(self):
        route_dir = ROOT / "api_routes"
        for path in route_dir.glob("*.py"):
            if path.name in {"__init__.py", "context.py"}:
                continue
            with self.subTest(file=path.relative_to(ROOT).as_posix()):
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    API_ROUTE_FILE_LIMIT,
                    f"{path.name} is over budget. Split route groups by feature instead of appending.",
                )

    def test_service_domain_modules_stay_under_architecture_budget(self):
        service_files = {
            "services/resource_monitor.py",
            "services/ram_service.py",
            "services/cookie_service.py",
            "services/vip_tracker.py",
            "services/network_monitor.py",
            "services/roblox_processes.py",
            "services/roblox_liveness.py",
            "services/roblox_log_evidence.py",
            "services/roblox_windows.py",
            "services/roblox_launch_service.py",
        }
        for rel in service_files:
            with self.subTest(file=rel):
                lines = (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    SERVICE_DOMAIN_FILE_LIMITS.get(rel, SERVICE_DOMAIN_FILE_LIMIT),
                    f"{rel} is over budget. Split the service domain instead of appending.",
                )

    def test_maintenance_domain_modules_stay_under_architecture_budget(self):
        maintenance_files = {
            "runtime/system_maintenance.py": 220,
            "runtime/maintenance_presence.py": MAINTENANCE_DOMAIN_FILE_LIMIT,
            "runtime/maintenance_liveness.py": MAINTENANCE_DOMAIN_FILE_LIMIT,
            "runtime/maintenance_performance.py": MAINTENANCE_DOMAIN_FILE_LIMIT,
            "runtime/maintenance_queue.py": MAINTENANCE_DOMAIN_FILE_LIMIT,
        }
        for rel, max_lines in maintenance_files.items():
            with self.subTest(file=rel):
                lines = (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    max_lines,
                    f"{rel} is over budget. Split maintenance domains instead of appending.",
                )

    def test_farm_runtime_domain_modules_stay_under_architecture_budget(self):
        runtime_files = {
            "runtime/launch_controller.py": 800,
            "runtime/roblox_watchdog.py": 220,
        }
        for rel, max_lines in runtime_files.items():
            with self.subTest(file=rel):
                lines = (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    max_lines,
                    f"{rel} is over budget. Split farm runtime domains instead of appending.",
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
