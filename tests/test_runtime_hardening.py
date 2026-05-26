import unittest

from tests.runtime_hardening_core_cases import RuntimeHardeningCoreCases
from tests.runtime_hardening_recovery_policy_cases import RuntimeHardeningRecoveryPolicyCases
from tests.runtime_hardening_recovery_flow_cases import RuntimeHardeningRecoveryFlowCases
from tests.runtime_hardening_observability_cases import RuntimeHardeningObservabilityCases
from tests.runtime_hardening_scheduler_network_cases import RuntimeHardeningSchedulerNetworkCases
from tests.runtime_hardening_migration_cases import RuntimeHardeningMigrationCases
from tests.runtime_hardening_shared import RuntimeHardeningBase


class RuntimeHardeningTests(
    RuntimeHardeningBase,
    RuntimeHardeningCoreCases,
    RuntimeHardeningRecoveryPolicyCases,
    RuntimeHardeningRecoveryFlowCases,
    RuntimeHardeningObservabilityCases,
    RuntimeHardeningSchedulerNetworkCases,
    RuntimeHardeningMigrationCases,
    unittest.TestCase,
):
    """Compatibility facade for the runtime hardening regression suite."""


if __name__ == "__main__":
    unittest.main()
