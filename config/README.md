# config/ — Local-first configuration

Depends only on `infra/` + `domain/states`. Owns everything under
`%LOCALAPPDATA%/Cronus Launcher/data`.

| File | Does what | Connects to |
| ---- | --------- | ----------- |
| `validation.py` | Pure schema: `CONFIG_SCHEMA_VERSION = 2`, int ranges (`max_retry`, `crash_timeout`, …), `validate_config_payload()`. No I/O. | Imported only by `store.py`. Covered indirectly by `tests/test_settings_state.py`. |
| `store.py` | `ConfigManager` + account/runtime text files (`cronus_rt1_config.json`, `cronus_rt12_accounts.txt`, `cronus_rt12_runtime.txt`). Migrates legacy keys, lazy-imports `core.flog` to break cycles. | Re-exported by `config/__init__.py` and `core.py`. Consumed by `main.py` (`cfg_mgr`), all `api_routes/*`, `services/executor_compatibility.py`, `services/executor_relauncher.py`. |

### Summary

Local-first configuration and account state under `%LOCALAPPDATA%/Cronus Launcher/data`, with schema validation kept separate from the JSON/text store.
