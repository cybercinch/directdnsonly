#!/bin/bash
set -e

# ---------------------------------------------------------------------------
# Detect which DNS backend type(s) are configured and enabled.
# Uses the same config search order as the application itself.
# ---------------------------------------------------------------------------
detect_backend_types() {
python3 - <<'EOF'
import yaml, sys, os

config_paths = [
    "/etc/directdnsonly/app.yml",
    "/etc/directdnsonly/app.yaml",
    "/app/app.yml",
    "/app/app.yaml",
    "/app/config/app.yml",
    "/app/config/app.yaml",
]

# Also honour env-var-only deployments (no config file)
bind_env = os.environ.get("DADNS_DNS_BACKENDS_BIND_ENABLED", "").lower() == "true"
nsd_env  = os.environ.get("DADNS_DNS_BACKENDS_NSD_ENABLED", "").lower()  == "true"

config = {}
for path in config_paths:
    if os.path.exists(path):
        with open(path) as f:
            config = yaml.safe_load(f) or {}
        break

backends = config.get("dns", {}).get("backends", {})
has_bind = bind_env
has_nsd  = nsd_env
for cfg in backends.values():
    if not isinstance(cfg, dict) or not cfg.get("enabled", False):
        continue
    btype = cfg.get("type", "")
    if btype == "bind":
        has_bind = True
    elif btype == "nsd":
        has_nsd = True

types = []
if has_bind:
    types.append("bind")
if has_nsd:
    types.append("nsd")
print(" ".join(types) if types else "none")
EOF
}

BACKEND_TYPES=$(detect_backend_types)
echo "[entrypoint] Detected DNS backend type(s): ${BACKEND_TYPES:-none}"

# ---------------------------------------------------------------------------
# Start BIND if a bind backend is configured
# ---------------------------------------------------------------------------
if echo "$BACKEND_TYPES" | grep -qw "bind"; then
    if command -v named >/dev/null 2>&1; then
        echo "[entrypoint] Starting BIND (named)"
        /usr/sbin/named -u bind -f &
    else
        echo "[entrypoint] WARNING: bind backend configured but 'named' not found — skipping"
    fi
fi

# ---------------------------------------------------------------------------
# Start NSD if an nsd backend is configured
# ---------------------------------------------------------------------------
if echo "$BACKEND_TYPES" | grep -qw "nsd"; then
    if command -v nsd >/dev/null 2>&1; then
        echo "[entrypoint] Starting NSD"
        # Ensure nsd-control keys exist (generated on first run)
        if [ ! -f /etc/nsd/nsd_server.key ]; then
            nsd-control-setup 2>/dev/null || true
        fi
        /usr/sbin/nsd -d -c /etc/nsd/nsd.conf &
    else
        echo "[entrypoint] WARNING: nsd backend configured but 'nsd' not found — skipping"
    fi
fi

if [ "$BACKEND_TYPES" = "none" ] || [ -z "$BACKEND_TYPES" ]; then
    echo "[entrypoint] No local DNS daemon required (CoreDNS MySQL or similar)"
fi

# ---------------------------------------------------------------------------
# Start the directdnsonly application
# ---------------------------------------------------------------------------
exec python -m directdnsonly
