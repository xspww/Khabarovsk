import unittest

from tests.hybrid_account_core_cases import HybridAccountCoreCases
from tests.hybrid_account_dashboard_ui_cases import HybridAccountDashboardUiCases
from tests.hybrid_account_lua_cases import HybridAccountLuaCases
from tests.hybrid_account_api_cases import HybridAccountApiCases
from tests.hybrid_account_recovery_cases import HybridAccountRecoveryCases
from tests.hybrid_account_popup_window_cases import HybridAccountPopupWindowCases
from tests.hybrid_account_launch_cases import HybridAccountLaunchCases
from tests.hybrid_account_private_server_launch_cases import HybridAccountPrivateServerLaunchCases


class HybridAccountTests(
    HybridAccountCoreCases,
    HybridAccountDashboardUiCases,
    HybridAccountLuaCases,
    HybridAccountApiCases,
    HybridAccountRecoveryCases,
    HybridAccountPopupWindowCases,
    HybridAccountLaunchCases,
    HybridAccountPrivateServerLaunchCases,
    unittest.TestCase,
):
    """Compatibility facade for the hybrid account regression suite."""


if __name__ == "__main__":
    unittest.main()
