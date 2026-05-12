from __future__ import annotations

from .auth import install_api_token_middleware
from .context import ApiContext
from .runtime_routes import register as register_runtime_routes
from .performance_routes import register as register_performance_routes
from .config_routes import register as register_config_routes
from .accounts_routes import register as register_accounts_routes
from .system_routes import register as register_system_routes


def register_api_routes(app, ctx: ApiContext) -> None:
    install_api_token_middleware(app, ctx)
    register_runtime_routes(app, ctx)
    register_performance_routes(app, ctx)
    register_config_routes(app, ctx)
    register_accounts_routes(app, ctx)
    register_system_routes(app, ctx)
