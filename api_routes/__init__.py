from __future__ import annotations

from .accounts_routes import register as register_accounts_routes
from .auth import install_api_token_middleware
from .config_routes import register as register_config_routes
from .context import ApiContext
from .discord_routes import register as register_discord_routes
from .games_routes import register as register_games_routes
from .lua_routes import register as register_lua_routes
from .manual_login_routes import register as register_manual_login_routes
from .performance_routes import register as register_performance_routes
from .runtime_routes import register as register_runtime_routes
from .system_routes import register as register_system_routes


def register_api_routes(app, ctx: ApiContext) -> None:
    install_api_token_middleware(app, ctx)
    register_runtime_routes(app, ctx)
    register_performance_routes(app, ctx)
    register_config_routes(app, ctx)
    register_accounts_routes(app, ctx)
    register_manual_login_routes(app, ctx)
    register_games_routes(app, ctx)
    register_lua_routes(app, ctx)
    register_discord_routes(app, ctx)
    register_system_routes(app, ctx)

