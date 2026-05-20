import ast
import re
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]

HOTSPOT_FILE_LIMITS = {
    "farm.py": 1150,
    "main.py": 700,
    "process_net.py": 900,
    "core.py": 1000,
    "roblox_hybrid.py": 1150,
    "desktop_host.py": 820,
    "services/process_service.py": 900,
    "runtime/runtime_state_manager.py": 800,
}

HYBRID_ACCOUNT_TEST_FILE_LIMIT = 900
HYBRID_ACCOUNT_TEST_FACADE_LIMIT = 120
RUNTIME_HARDENING_TEST_FILE_LIMIT = 650
RUNTIME_HARDENING_TEST_FACADE_LIMIT = 90
DASHBOARD_STYLE_MODULE_LIMIT = 800

API_ROUTE_FILE_LIMIT = 650
SERVICE_DOMAIN_FILE_LIMIT = 650
SERVICE_DOMAIN_FILE_LIMITS = {
    "services/process_account_runtime.py": 320,
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
    ignored_parts = {".git", "__pycache__", "build", "data", "dist", "cronus_rt1_instances"}
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

    def test_hybrid_account_regression_suite_stays_split(self):
        facade = ROOT / "tests" / "test_hybrid_account.py"
        facade_lines = facade.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        self.assertLessEqual(
            len(facade_lines),
            HYBRID_ACCOUNT_TEST_FACADE_LIMIT,
            "tests/test_hybrid_account.py should stay a small compatibility facade.",
        )
        self.assertIn("class HybridAccountTests", facade.read_text(encoding="utf-8"))

        case_files = sorted((ROOT / "tests").glob("hybrid_account_*_cases.py"))
        self.assertGreaterEqual(len(case_files), 5)
        for path in case_files:
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(file=rel):
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    HYBRID_ACCOUNT_TEST_FILE_LIMIT,
                    f"{rel} is over budget. Split by behavior domain instead of appending.",
                )

    def test_runtime_hardening_regression_suite_stays_split(self):
        facade = ROOT / "tests" / "test_runtime_hardening.py"
        facade_text = facade.read_text(encoding="utf-8-sig", errors="replace")
        facade_lines = facade_text.splitlines()
        self.assertLessEqual(
            len(facade_lines),
            RUNTIME_HARDENING_TEST_FACADE_LIMIT,
            "tests/test_runtime_hardening.py should stay a small compatibility facade.",
        )
        self.assertIn("class RuntimeHardeningTests", facade_text)

        case_files = sorted((ROOT / "tests").glob("runtime_hardening_*_cases.py"))
        self.assertGreaterEqual(len(case_files), 4)
        for path in case_files:
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(file=rel):
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    RUNTIME_HARDENING_TEST_FILE_LIMIT,
                    f"{rel} is over budget. Split by runtime hardening domain instead of appending.",
                )

    def test_dashboard_stylesheet_stays_split(self):
        manifest = ROOT / "ui" / "dashboard.css"
        manifest_text = manifest.read_text(encoding="utf-8-sig", errors="replace")
        manifest_lines = manifest_text.splitlines()
        imports = re.findall(r'@import\s+url\("\./styles/([^"?]+)\?v=main-view-animation"\);', manifest_text)
        self.assertEqual(len(manifest_lines), len(imports))
        self.assertGreaterEqual(len(imports), 5)

        for name in imports:
            path = ROOT / "ui" / "styles" / name
            self.assertTrue(path.exists(), f"Missing imported stylesheet: {name}")

        for path in sorted((ROOT / "ui" / "styles").glob("*.css")):
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(file=rel):
                lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                self.assertLessEqual(
                    len(lines),
                    DASHBOARD_STYLE_MODULE_LIMIT,
                    f"{rel} is over budget. Split styles by UI surface instead of appending.",
                )

    def test_roblox_private_server_helpers_are_split_from_launcher_facade(self):
        hybrid = (ROOT / "roblox_hybrid.py").read_text(encoding="utf-8-sig", errors="replace")
        helpers = (ROOT / "domain" / "roblox_private_servers.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("ensure_owned_private_server as _ensure_owned_private_server", hybrid)
        self.assertIn("def ensure_owned_private_server", hybrid)
        self.assertIn("class HybridLauncher", hybrid)
        for helper_name in (
            "parse_vip_link",
            "parse_vip_components",
            "build_place_launcher_url",
            "build_roblox_player_uri",
            "parse_launch_destination_from_cmdline",
        ):
            self.assertIn(f"def {helper_name}", helpers)
            self.assertNotIn(f"def {helper_name}", hybrid)

    def test_desktop_single_instance_helpers_are_split_from_host_facade(self):
        host = (ROOT / "desktop_host.py").read_text(encoding="utf-8-sig", errors="replace")
        guard = (ROOT / "desktop" / "instance_guard.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("from desktop.instance_guard import", host)
        self.assertIn("def _run_desktop_window", host)
        for helper_name in (
            "_cmdline_targets_this_app",
            "_stop_previous_instance",
            "_stop_same_app_processes",
            "prepare_backend_single_instance",
        ):
            self.assertIn(f"def {helper_name}", guard)
            self.assertNotIn(f"def {helper_name}", host)

    def test_desktop_console_icon_helpers_are_split_from_host_facade(self):
        host = (ROOT / "desktop_host.py").read_text(encoding="utf-8-sig", errors="replace")
        icon = (ROOT / "desktop" / "console_icon.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("from desktop.console_icon import", host)
        self.assertIn("def ensure_console_icon_file", icon)
        self.assertIn("def set_console_window_icon", icon)
        self.assertIn("WM_SETICON", icon)
        self.assertNotIn("def _ensure_console_icon_file", host)
        self.assertNotIn("def _set_console_window_icon", host)

    def test_core_logging_is_split_from_runtime_model_facade(self):
        core = (ROOT / "core.py").read_text(encoding="utf-8-sig", errors="replace")
        logging_module = (ROOT / "core_logging.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("from core_logging import", core)
        self.assertIn("class Account", core)
        self.assertIn("class StateManager", core)
        for helper_name in ("_redact_value", "flog_struct", "flog", "_kv_value", "flog_kv"):
            self.assertIn(f"def {helper_name}", logging_module)
            self.assertNotIn(f"def {helper_name}", core)

    def test_smart_queue_is_split_from_core_model_facade(self):
        core = (ROOT / "core.py").read_text(encoding="utf-8-sig", errors="replace")
        smart_queue = (ROOT / "runtime" / "smart_queue.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("from runtime.smart_queue import SmartQueue", core)
        self.assertIn("class SmartQueue", smart_queue)
        self.assertNotIn("class SmartQueue", core)

    def test_farm_health_surface_is_split_from_controller_facade(self):
        farm = (ROOT / "farm.py").read_text(encoding="utf-8-sig", errors="replace")
        health = (ROOT / "runtime" / "farm_health.py").read_text(encoding="utf-8-sig", errors="replace")

        self.assertIn("from runtime.farm_health import", farm)
        self.assertIn("def build_farm_health_snapshot", health)
        self.assertIn("def get_runtime_diagnostics", health)
        self.assertNotIn("def _farm_health_snapshot", farm)
        self.assertNotIn("def _farm_health_account_rows", farm)

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
            "services/process_window_ops.py",
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
            "runtime/recovery_engine.py": 820,
            "runtime/recovery_evaluator.py": 350,
            "runtime/recovery_network.py": 200,
            "runtime/recovery_owner.py": 350,
            "runtime/recovery_relaunch.py": 350,
            "runtime/recovery_scheduling.py": 260,
            "runtime/recovery_signal_router.py": 350,
            "runtime/account_worker.py": 900,
            "runtime/dispatcher.py": 455,
            "runtime/recovery_support.py": 180,
            "runtime/runtime_scheduler.py": 360,
            "runtime/runtime_state_observability.py": 260,
            "runtime/runtime_transactions.py": 260,
            "runtime/smart_queue.py": 320,
            "runtime/farm_health.py": 320,
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

    def test_smart_queue_has_single_method_definitions(self):
        path = ROOT / "runtime" / "smart_queue.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"), filename=path.as_posix())
        smart_queue = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SmartQueue"
        )
        methods = [
            node.name for node in smart_queue.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        duplicates = sorted({name for name in methods if methods.count(name) > 1})
        self.assertEqual(
            duplicates,
            [],
            "SmartQueue must not define duplicate methods; Python silently overrides earlier bodies.",
        )

    def test_legacy_roblox_watchdog_thread_is_removed(self):
        legacy_path = ROOT / "runtime" / "roblox_watchdog.py"
        self.assertFalse(
            legacy_path.exists(),
            "Use runtime/maintenance_liveness.py for watchdog behavior; the old per-account thread is legacy.",
        )
        for rel in ("farm.py", "runtime/account_worker.py"):
            text = (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
            self.assertNotIn("runtime.roblox_watchdog", text)
            self.assertNotIn("RobloxWatchdog", text)

    def test_farm_config_snapshot_uses_public_update_boundaries(self):
        tree = ast.parse((ROOT / "farm.py").read_text(encoding="utf-8-sig", errors="replace"), filename="farm.py")
        farm_controller = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "FarmController"
        )
        method = next(
            node for node in farm_controller.body
            if isinstance(node, ast.FunctionDef) and node.name == "apply_config_snapshot"
        )
        offenders = []
        for node in ast.walk(method):
            for target in _assignment_targets(node):
                if not isinstance(target, ast.Attribute):
                    continue
                chain = _target_chain(target)
                if len(chain) >= 3 and chain[0] == "self" and chain[2].startswith("_"):
                    offenders.append(f"{node.lineno}:{'.'.join(chain)}")
                elif len(chain) >= 2 and chain[0] in {"worker", "launcher", "limiter"}:
                    offenders.append(f"{node.lineno}:{'.'.join(chain)}")
        self.assertEqual(
            offenders,
            [],
            "FarmController.apply_config_snapshot must call public update methods instead of mutating collaborator internals.",
        )

    def test_process_window_ops_are_split_from_process_service(self):
        process_service = (ROOT / "services" / "process_service.py").read_text(encoding="utf-8")
        window_ops = (ROOT / "services" / "process_window_ops.py").read_text(encoding="utf-8")
        self.assertIn("def resize_roblox_windows", window_ops)
        self.assertIn("def arrange_roblox_windows", window_ops)
        self.assertIn("def restore_roblox_window_styles", window_ops)
        self.assertIn("resize_roblox_windows = staticmethod(_resize_roblox_windows)", process_service)
        self.assertIn("arrange_roblox_windows = staticmethod(_arrange_roblox_windows)", process_service)
        self.assertIn("restore_roblox_window_styles = staticmethod(_restore_roblox_window_styles)", process_service)

    def test_process_account_runtime_helpers_are_split_from_process_service(self):
        process_service = (ROOT / "services" / "process_service.py").read_text(encoding="utf-8")
        account_runtime = (ROOT / "services" / "process_account_runtime.py").read_text(encoding="utf-8")

        self.assertIn("from services.process_account_runtime import", process_service)
        self.assertIn("def runtime_generation_matches", account_runtime)
        self.assertIn("def set_process_diagnostics", account_runtime)
        self.assertIn("def set_adopt_diagnostics", account_runtime)
        self.assertNotIn("def _runtime_generation_matches", process_service)
        self.assertNotIn("def _set_process_diagnostics", process_service)
        self.assertNotIn("def _set_adopt_diagnostics", process_service)

    def test_runtime_transactions_are_split_from_state_manager(self):
        state_manager = (ROOT / "runtime" / "runtime_state_manager.py").read_text(encoding="utf-8")
        transactions = (ROOT / "runtime" / "runtime_transactions.py").read_text(encoding="utf-8")
        self.assertIn("def begin_rejoin_transaction", transactions)
        self.assertIn("def update_rejoin_transaction", transactions)
        self.assertIn("def finish_rejoin_transaction", transactions)
        self.assertIn("_begin_rejoin_transaction", state_manager)
        self.assertIn("_update_rejoin_transaction", state_manager)
        self.assertIn("_finish_rejoin_transaction", state_manager)

    def test_runtime_state_observability_is_split_from_mutation_owner(self):
        state_manager = (ROOT / "runtime" / "runtime_state_manager.py").read_text(encoding="utf-8")
        observability = (ROOT / "runtime" / "runtime_state_observability.py").read_text(encoding="utf-8")

        self.assertIn("from runtime.runtime_state_observability import", state_manager)
        self.assertIn("def runtime_log_fields", observability)
        self.assertIn("def emit_invariant_violations", observability)
        self.assertIn("def snapshot_account_runtime", observability)
        self.assertNotIn("hard_codes = {", state_manager)
        self.assertNotIn("def _transition_invariant_blockers", state_manager)

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
        self.assertIn('<script type="module" src="/ui/app.js?v=main-view-animation"></script>', index)
        self.assertIn("import './dashboard.js?v=main-view-animation';", app)
        self.assertIn("from './runtime/status.js'", dashboard)
        self.assertIn("from './runtime/accountStatus.js'", dashboard)
        self.assertIn("from './components/accountsTable.js'", dashboard)
        self.assertIn("from './components/feedback.js'", dashboard)
        self.assertIn("from './events/bindings.js'", dashboard)
        self.assertIn("from './panels/settingsPanels.js'", dashboard)
        self.assertIn("export async function api", api)
        self.assertIn("opt.headers['X-Cronus-Token']=token", api)
        self.assertNotIn("toUpperCase()!=='GET'", api)
        self.assertIn("export function createStatusRuntime", status)
        self.assertIn("export function rowStatusLabel", account_status)
        self.assertIn("export function renderAccountRows", table)
        self.assertIn("export function createFeedback", feedback)
        self.assertIn("export function bindDashboardEvents", bindings)
        self.assertIn("export function renderSettingsPanel", settings)
        self.assertNotIn("setInterval(manualSnapshot,2500)", status)

    def test_status_payload_is_built_by_runtime_view_model(self):
        farm = (ROOT / "farm.py").read_text(encoding="utf-8")
        health = (ROOT / "runtime" / "farm_health.py").read_text(encoding="utf-8")
        view_model = (ROOT / "runtime" / "runtime_view_model.py").read_text(encoding="utf-8")
        self.assertIn("return build_farm_status(self)", farm)
        self.assertIn("return RuntimeViewModelBuilder(farm).build_status()", health)
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

    def test_recovery_scheduling_is_split_from_engine(self):
        recovery = (ROOT / "runtime" / "recovery_engine.py").read_text(encoding="utf-8")
        scheduling = (ROOT / "runtime" / "recovery_scheduling.py").read_text(encoding="utf-8")

        self.assertIn("from runtime.recovery_scheduling import", recovery)
        self.assertIn("def schedule_cooldown", scheduling)
        self.assertIn("def queue_account", scheduling)
        self.assertIn("def run_scheduled_recovery", scheduling)
        self.assertNotIn("self._scheduler.schedule_once(", recovery)
        self.assertNotIn("self._queue.push(", recovery)

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

    def test_pytest_isolates_appdata_from_operator_runtime(self):
        conftest = ROOT / "conftest.py"
        self.assertTrue(conftest.exists(), "pytest must set an isolated CRONUS_USER_ROOT before app imports.")
        text = conftest.read_text(encoding="utf-8")
        self.assertIn("CRONUS_USER_ROOT", text)
        self.assertIn("tempfile.mkdtemp", text)
        self.assertIn("shutil.rmtree", text)

        unittest_bootstrap = ROOT / "tests" / "env_bootstrap.py"
        self.assertTrue(unittest_bootstrap.exists(), "unittest discovery must also isolate CRONUS_USER_ROOT before app imports.")
        text = unittest_bootstrap.read_text(encoding="utf-8")
        self.assertIn("CRONUS_USER_ROOT", text)
        self.assertIn("tempfile.mkdtemp", text)
        self.assertIn("shutil.rmtree", text)
        for rel, first_app_import in {
            "tests/test_runtime_state_machine.py": "from core import",
            "tests/test_recovery_storm.py": "from core import",
            "tests/test_machine_supervisor.py": "from core import",
            "tests/test_config_sections.py": "from config_sections import",
        }.items():
            test_text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("ensure_test_user_root()", test_text)
            self.assertLess(
                test_text.index("ensure_test_user_root()"),
                test_text.index(first_app_import),
                f"{rel} must set CRONUS_USER_ROOT before importing app modules.",
            )


if __name__ == "__main__":
    unittest.main()
