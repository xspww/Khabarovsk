from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from services.process_proof_policy import PROOF_STRONG
from services.process_service import ProcessManager, ProcessService


@dataclass(frozen=True)
class ProcessAdapterResult:
    ok: bool
    reason: str = ""
    pid: int = 0
    proof_level: str = "untrusted"
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "pid": self.pid,
            "proof_level": self.proof_level,
            **dict(self.payload or {}),
        }


class RobloxProcessAdapter:
    """Single process seam for runtime code and tests.

    Status reads can use cached or fake process data. Destructive actions still
    require live validation at the adapter seam so callers cannot kill from a
    stale process snapshot.
    """

    def __init__(
        self,
        *,
        process_service: Any = ProcessService,
        process_manager: Any = ProcessManager,
    ):
        self._process_service = process_service
        self._process_manager = process_manager

    def list_live_game_processes(self, launched_after: Optional[float] = None) -> List[Dict[str, Any]]:
        return list(self._process_manager.list_live_game_processes(launched_after=launched_after) or [])

    def validate_binding(
        self,
        account: Any,
        pid: int,
        *,
        reason: str = "process_adapter_validate",
        expected_identity: str = "",
    ) -> ProcessAdapterResult:
        validation = self._process_service.validate_binding(
            account,
            int(pid or 0),
            reason=reason,
            expected_identity=expected_identity,
        )
        return _adapter_result_from_validation(validation, pid=int(pid or 0))

    def bind_account_process(
        self,
        account: Any,
        pid: int,
        *,
        state_manager: Any = None,
        reason: str = "process_adapter_bind",
    ) -> ProcessAdapterResult:
        result = self._process_service.bind_account_process(
            account,
            int(pid or 0),
            state_manager=state_manager,
            reason=reason,
        )
        return _adapter_result_from_mapping(result, pid=int(pid or 0))

    def safe_kill_bound_process(
        self,
        account: Any,
        *,
        state_manager: Any = None,
        expected_runtime_generation: Optional[int] = None,
        reason: str = "process_adapter_safe_kill",
    ) -> ProcessAdapterResult:
        result = self._process_service.safe_kill_bound_process(
            account,
            state_manager=state_manager,
            expected_runtime_generation=expected_runtime_generation,
            reason=reason,
        )
        return _adapter_result_from_mapping(result, pid=int(getattr(account, "pid", 0) or 0))

    def inspect_disconnect_dialog(self, pid: int, **kwargs: Any) -> Dict[str, Any]:
        return dict(self._process_manager.inspect_disconnect_dialog(int(pid or 0), **kwargs) or {})

    def is_bound_game_alive(self, account: Any, pid: int) -> bool:
        return bool(self._process_manager.is_bound_game_alive(account, int(pid or 0)))


class FakeRobloxProcessAdapter(RobloxProcessAdapter):
    def __init__(self, processes: Optional[Iterable[Mapping[str, Any]]] = None):
        self._processes = {int(item.get("pid") or 0): dict(item) for item in (processes or [])}
        self.killed_pids: List[int] = []
        self.bound_pids: List[int] = []

    def list_live_game_processes(self, launched_after: Optional[float] = None) -> List[Dict[str, Any]]:
        items = list(self._processes.values())
        if launched_after is None:
            return [dict(item) for item in items if item.get("alive", True)]
        return [
            dict(item)
            for item in items
            if item.get("alive", True) and float(item.get("create_time", 0.0) or 0.0) >= float(launched_after)
        ]

    def validate_binding(
        self,
        account: Any,
        pid: int,
        *,
        reason: str = "fake_validate",
        expected_identity: str = "",
    ) -> ProcessAdapterResult:
        item = self._processes.get(int(pid or 0))
        if not item or not item.get("alive", True):
            return ProcessAdapterResult(False, "pid_not_live", int(pid or 0), "untrusted")
        if expected_identity and str(item.get("identity") or "") != str(expected_identity):
            return ProcessAdapterResult(False, "identity_mismatch", int(pid or 0), str(item.get("proof_level") or "weak"))
        proof = str(item.get("proof_level") or "weak")
        return ProcessAdapterResult(True, "validated", int(pid or 0), proof, dict(item))

    def bind_account_process(
        self,
        account: Any,
        pid: int,
        *,
        state_manager: Any = None,
        reason: str = "fake_bind",
    ) -> ProcessAdapterResult:
        validation = self.validate_binding(account, pid, reason=reason)
        if not validation.ok:
            return validation
        self.bound_pids.append(int(pid or 0))
        return validation

    def safe_kill_bound_process(
        self,
        account: Any,
        *,
        state_manager: Any = None,
        expected_runtime_generation: Optional[int] = None,
        reason: str = "fake_safe_kill",
    ) -> ProcessAdapterResult:
        pid = int(getattr(account, "pid", 0) or 0)
        validation = self.validate_binding(account, pid, reason=reason)
        if not validation.ok:
            return validation
        if validation.proof_level != PROOF_STRONG:
            return ProcessAdapterResult(False, "insufficient_process_proof", pid, validation.proof_level)
        self._processes[pid]["alive"] = False
        self.killed_pids.append(pid)
        return ProcessAdapterResult(True, "killed", pid, validation.proof_level)

    def inspect_disconnect_dialog(self, pid: int, **kwargs: Any) -> Dict[str, Any]:
        return dict(self._processes.get(int(pid or 0), {}).get("dialog") or {})

    def is_bound_game_alive(self, account: Any, pid: int) -> bool:
        return bool(self._processes.get(int(pid or 0), {}).get("alive", False))


def _adapter_result_from_validation(validation: Mapping[str, Any], *, pid: int) -> ProcessAdapterResult:
    return ProcessAdapterResult(
        ok=bool(validation.get("ok")),
        reason=str(validation.get("reason") or validation.get("decision") or ""),
        pid=int(validation.get("pid") or pid or 0),
        proof_level=str(validation.get("proof_level") or validation.get("process_proof_level") or "untrusted"),
        payload=dict(validation or {}),
    )


def _adapter_result_from_mapping(result: Mapping[str, Any], *, pid: int) -> ProcessAdapterResult:
    return ProcessAdapterResult(
        ok=bool(result.get("ok")),
        reason=str(result.get("reason") or ""),
        pid=int(result.get("pid") or pid or 0),
        proof_level=str(result.get("proof_level") or result.get("process_proof_level") or "untrusted"),
        payload=dict(result or {}),
    )
