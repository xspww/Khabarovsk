from tests.hybrid_account_recovery_config_cases import HybridAccountRecoveryConfigCases
from tests.hybrid_account_recovery_popup_cases import HybridAccountRecoveryPopupCases
from tests.hybrid_account_recovery_signal_cases import HybridAccountRecoverySignalCases
from tests.hybrid_account_recovery_visual_cases import HybridAccountRecoveryVisualCases


class HybridAccountRecoveryCases(
    HybridAccountRecoveryConfigCases,
    HybridAccountRecoveryPopupCases,
    HybridAccountRecoverySignalCases,
    HybridAccountRecoveryVisualCases,
):
    """Compatibility facade for recovery-oriented hybrid account regression cases."""
