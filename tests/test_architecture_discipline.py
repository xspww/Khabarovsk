import ast
import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

HOTSPOT_FILE_LIMITS = {
    "farm.py": 1600,
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
            "runtime/recovery_engine.py": 900,
            "runtime/recovery_evaluator.py": 350,
            "runtime/recovery_network.py": 200,
            "runtime/recovery_owner.py": 350,
            "runtime/recovery_relaunch.py": 350,
            "runtime/recovery_signal_router.py": 350,
            "runtime/account_worker.py": 900,
            "runtime/dispatcher.py": 450,
            "runtime/recovery_support.py": 180,
            "runtime/runtime_scheduler.py": 360,
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

    def test_runtime_orchestrator_is_the_runtime_authority(self):
        orchestrator = ROOT / "runtime" / "runtime_orchestrator.py"
        text = orchestrator.read_text(encoding="utf-8")
        self.assertIn("class RuntimeOrchestrator", text)
        self.assertIn("class RuntimeCommand", text)
        self.assertIn("class RuntimeEvent", text)
        self.assertIn("def handle_runtime_signal", text)
        self.assertIn("def set_recovery_status", text)
        self.assertIn("runtime_generation", text)
        self.assertIn("command_generation", text)

    def test_dashboard_uses_native_module_entrypoint(self):
        index = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        app = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        dashboard = (ROOT / "ui" / "dashboard.js").read_text(encoding="utf-8")
        api = (ROOT / "ui" / "runtime" / "api.js").read_text(encoding="utf-8")
        status = (ROOT / "ui" / "runtime" / "status.js").read_text(encoding="utf-8")
        account_status = (ROOT / "ui" / "runtime" / "accountStatus.js").read_text(encoding="utf-8")
        table = (ROOT / "ui" / "components" / "accountsTable.js").read_text(encoding="utf-8")
        feedback = (ROOT / "ui" / "components" / "feedback.js").read_text(encoding="utf-8")
        bindings = (ROOT / "ui" / "events" / "bindings.js").read_text(encoding="utf-8")
        settings = (ROOT / "ui" / "panels" / "settingsPanels.js").read_text(encoding="utf-8")
        self.assertIn('<script type="module" src="/ui/app.js"></script>', index)
        self.assertIn("import './dashboard.js';", app)
        self.assertIn("from './runtime/status.js'", dashboard)
        self.assertIn("from './runtime/accountStatus.js'", dashboard)
        self.assertIn("from './components/accountsTable.js'", dashboard)
        self.assertIn("from './components/feedback.js'", dashboard)
        self.assertIn("from './events/bindings.js'", dashboard)
        self.assertIn("from './panels/settingsPanels.js'", dashboard)
        self.assertIn("export async function api", api)
        self.assertIn("export function createStatusRuntime", status)
        self.assertIn("export function rowStatusLabel", account_status)
        self.assertIn("export function renderAccountRows", table)
        self.assertIn("export function createFeedback", feedback)
        self.assertIn("export function bindDashboardEvents", bindings)
        self.assertIn("export function renderSettingsPanel", settings)
        self.assertNotIn("setInterval(manualSnapshot,2500)", status)

    def test_status_payload_is_built_by_runtime_view_model(self):
        farm = (ROOT / "farm.py").read_text(encoding="utf-8")
        view_model = (ROOT / "runtime" / "runtime_view_model.py").read_text(encoding="utf-8")
        self.assertIn("return RuntimeViewModelBuilder(self).build_status()", farm)
        self.assertIn("class RuntimeViewModelBuilder", view_model)
        self.assertIn("queue_snapshot", view_model)
        self.assertIn("runtime_health", view_model)

    def test_runtime_command_tracker_is_separate_from_farm_facade(self):
        farm = (ROOT / "farm.py").read_text(encoding="utf-8")
        tracker = (ROOT / "runtime" / "command_tracker.py").read_text(encoding="utf-8")
        self.assertIn("RuntimeCommandTracker", farm)
        self.assertIn("self._command_tracker.begin", farm)
        self.assertIn("self._command_tracker.finish", farm)
        self.assertIn("class RuntimeCommandTracker", tracker)
        self.assertIn("idempotent_replay", tracker)

    def test_farm_lifecycle_is_delegated_out_of_farm_facade(self):
        farm = (ROOT / "farm.py").read_text(encoding="utf-8")
        lifecycle = (ROOT / "runtime" / "farm_lifecycle.py").read_text(encoding="utf-8")
        self.assertIn("FarmLifecycleService", farm)
        self.assertIn("return self._lifecycle.start()", farm)
        self.assertIn("return self._lifecycle.stop()", farm)
        self.assertIn("class FarmLifecycleService", lifecycle)
        self.assertIn("def start", lifecycle)
        self.assertIn("def stop", lifecycle)

    def test_manual_launch_routes_use_account_launch_service_boundary(self):
        route = (ROOT / "api_routes" / "accounts_routes.py").read_text(encoding="utf-8")
        service = (ROOT / "services" / "roblox_launch_service.py").read_text(encoding="utf-8")
        self.assertIn("class AccountLaunchService", service)
        self.assertIn("AccountLaunchService.launch_record", route)
        self.assertIn("AccountLaunchService.kill_duplicate_instances", route)
        self.assertNotIn("HybridLauncher.launch_record", route)
        self.assertNotIn("HybridLauncher.kill_duplicate_instances", route)

    def test_critical_process_side_effects_stay_behind_process_service_boundary(self):
        forbidden = re.compile(
            r"ProcessManager\.(?:"
            r"safe_kill_bound_process|bind_account_process|safe_adopt_visible_process|"
            r"resize_roblox_windows|arrange_roblox_windows|restore_roblox_window_styles|"
            r"kill_all_roblox_clients|evict_pid_cache|cleanup_extra_launch_processes"
            r")"
        )
        scan_roots = ["farm.py", "api_routes", "runtime"]
        offenders = []
        for item in scan_roots:
            path = ROOT / item
            files = [path] if path.is_file() else sorted(path.glob("*.py"))
            for file_path in files:
                rel = file_path.relative_to(ROOT).as_posix()
                text = file_path.read_text(encoding="utf-8-sig", errors="replace")
                for match in forbidden.finditer(text):
                    line_no = text[:match.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line_no}:{match.group(0)}")
        self.assertEqual(
            offenders,
            [],
            "Critical process side effects must route through ProcessService or RuntimeOrchestrator.",
        )

    def test_runtime_scheduler_owns_timer_loops(self):
        scheduler = (ROOT / "runtime" / "runtime_scheduler.py").read_text(encoding="utf-8")
        recovery = (ROOT / "runtime" / "recovery_engine.py").read_text(encoding="utf-8")
        maintenance = (ROOT / "runtime" / "system_maintenance.py").read_text(encoding="utf-8")
        self.assertIn("class RuntimeScheduler", scheduler)
        self.assertIn("class RuntimeScheduledJob", scheduler)
        self.assertIn("def schedule_once", scheduler)
        self.assertIn("def schedule_periodic", scheduler)
        self.assertNotIn("def _scheduler_loop", recovery)
        self.assertNotIn("self._pending", recovery)
        self.assertIn("RuntimeScheduler", recovery)
        self.assertIn("RuntimeScheduler", maintenance)

    def test_recovery_owner_and_signal_routing_are_split_from_engine(self):
        recovery = (ROOT / "runtime" / "recovery_engine.py").read_text(encoding="utf-8")
        owner = (ROOT / "runtime" / "recovery_owner.py").read_text(encoding="utf-8")
        router = (ROOT / "runtime" / "recovery_signal_router.py").read_text(encoding="utf-8")
        evaluator = (ROOT / "runtime" / "recovery_evaluator.py").read_text(encoding="utf-8")
        self.assertIn("RecoveryOwnerRegistry", recovery)
        self.assertIn("RecoverySignalRouter", recovery)
        self.assertIn("RecoveryEvaluator", recovery)
        self.assertNotIn("self._active_recoveries", recovery)
        self.assertNotIn("elif signal_name", recovery)
        self.assertIn("class RecoveryOwnerRegistry", owner)
        self.assertIn("_active_recoveries", owner)
        self.assertIn("def release", owner)
        self.assertIn("class RecoverySignalRouter", router)
        self.assertIn("runtime_signal_dispatch", router)
        self.assertIn("def _dispatch", router)
        self.assertIn("class RecoveryEvaluator", evaluator)

    def test_maintenance_no_longer_wakes_every_worker_unconditionally(self):
        maintenance = (ROOT / "runtime" / "system_maintenance.py").read_text(encoding="utf-8")
        self.assertIn("def _register_periodic_jobs", maintenance)
        self.assertIn("schedule_periodic", maintenance)
        self.assertNotIn("for worker in self._workers.values():\n                worker.wake()", maintenance)


if __name__ == "__main__":
    unittest.main()
