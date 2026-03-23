"""
Miniflow configuration.

This file contains user-editable configuration such as:
- CRITICAL targets
- ENV_CONSTRAINTS

Do not modify run.py. All project-specific configuration should live here.
"""

# Targets requiring --critical to execute. Format: "layer.name" or "layer.schema.name".
# Edit manually. Never auto-discover.
CRITICAL: set[str] = set()
# CRITICAL = {"core.public.users", "core.public.orders"}

# Environment constraints per target. Maps target → list of allowed MINIFLOW_ENV values.
# Targets with no entry are allowed in any environment (including when MINIFLOW_ENV is unset).
# Edit manually. Never auto-discover.
ENV_CONSTRAINTS: dict[str, list[str]] = {}
# ENV_CONSTRAINTS = {"integrations.cloudflare.deploy": ["prod"], "core.public.test_table": ["dev"]}
