from tests.hybrid_account_api_misc_cases import HybridAccountApiMiscCases
from tests.hybrid_account_api_account_route_cases import HybridAccountApiAccountRouteCases
from tests.hybrid_account_api_auth_runtime_cases import HybridAccountApiAuthRuntimeCases
from tests.hybrid_account_api_idempotency_cases import HybridAccountApiIdempotencyCases


class HybridAccountApiCases(
    HybridAccountApiMiscCases,
    HybridAccountApiAccountRouteCases,
    HybridAccountApiAuthRuntimeCases,
    HybridAccountApiIdempotencyCases,
):
    """Compatibility facade for API-oriented hybrid account regression cases."""
