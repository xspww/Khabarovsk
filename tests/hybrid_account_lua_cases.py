from tests.hybrid_account_lua_helper_cases import HybridAccountLuaHelperCases
from tests.hybrid_account_lua_event_token_cases import HybridAccountLuaEventTokenCases
from tests.hybrid_account_lua_identity_cases import HybridAccountLuaIdentityCases
from tests.hybrid_account_lua_runtime_signal_cases import HybridAccountLuaRuntimeSignalCases


class HybridAccountLuaCases(
    HybridAccountLuaHelperCases,
    HybridAccountLuaEventTokenCases,
    HybridAccountLuaIdentityCases,
    HybridAccountLuaRuntimeSignalCases,
):
    """Compatibility facade for Lua-related hybrid account regression cases."""
