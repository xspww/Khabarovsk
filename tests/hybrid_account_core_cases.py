from tests.hybrid_account_data_cases import HybridAccountDataCases
from tests.hybrid_account_status_cases import HybridAccountStatusCases
from tests.hybrid_account_settings_cases import HybridAccountSettingsCases
from tests.hybrid_account_cpu_limiter_cases import HybridAccountCpuLimiterCases
from tests.hybrid_account_install_cases import HybridAccountInstallCases
from tests.hybrid_account_startup_cases import HybridAccountStartupCases


class HybridAccountCoreCases(
    HybridAccountDataCases,
    HybridAccountStatusCases,
    HybridAccountSettingsCases,
    HybridAccountCpuLimiterCases,
    HybridAccountInstallCases,
    HybridAccountStartupCases,
):
    """Compatibility facade for core hybrid account regression cases."""
