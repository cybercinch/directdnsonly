#!/bin/bash
set -e

# ---------------------------------------------------------------------------
# Detect whether any bind backend is configured and enabled.
# Uses the same config search order as the application itself.
# ---------------------------------------------------------------------------
BIND_ENABLED=$(python3 - <<'EOF'
import yaml, sys, os

config_paths = [
    "/etc/directdnsonly/app.yml",
    "/etc/directdnsonly/app.yaml",
    "/app/app.yml",
    "/app/app.yaml",
    "/app/config/app.yml",
    "/app/config/app.yaml",
]

config = {}
for path in config_paths:
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
        break

backends = config.get("dns", {}).get("backends", {})
for cfg in backends.values():
    if isinstance(cfg, dict) and cfg.get("type") == "bind" and cfg.get("enabled", False):
        print("true")
        sys.exit(0)
print("false")
EOF
)

if [ "$BIND_ENABLED" = "true" ]; then
    echo "[entrypoint] BIND backend enabled — starting named"
    /usr/sbin/named -u bind -f &
else
    echo "[entrypoint] No BIND backend configured — skipping named"
fi

# Start the application
exec python -m directdnsonly
