from vyper import v, Vyper
from loguru import logger

# from vyper.config import Config
import os
from pathlib import Path
from typing import Any, Dict


def load_config() -> Vyper:
    # Initialize Vyper
    v.set_config_name("app")  # Looks for app.yaml/app.yml
    # User-supplied paths checked first so they override the bundled defaults
    v.add_config_path("/etc/directdnsonly")  # system-level mount
    v.add_config_path(".")  # CWD (e.g. /app when run directly)
    v.add_config_path("./config")  # docker-compose volume mount at /app/config
    # Bundled config colocated with this module — last-resort fallback
    v.add_config_path(str(Path(__file__).parent))
    v.set_env_prefix("DADNS")
    v.set_env_key_replacer("_", ".")
    v.automatic_env()
    # Set defaults for all required parameters
    v.set_default("log_level", "info")
    v.set_default("queue_location", "./data/queues")
    v.set_default("timezone", "Pacific/Aucland")

    # Set defaults for app
    v.set_default("app.listen_port", 2222)
    v.set_default("app.proxy_support", True)
    v.set_default("app.proxy_support_base", "http://127.0.0.1")
    v.set_default("app.log_level", "debug")
    v.set_default("app.log_to", "file")
    v.set_default("app.ssl_enable", "false")
    v.set_default("app.listen_port", 2222)
    v.set_default("app.token_valid_for_days", 30)
    v.set_default("app.queue_location", "conf/queues")
    v.set_default("app.auth_username", "directdnsonly")
    v.set_default("app.auth_password", "changeme")
    v.set_default("timezone", "Pacific/Auckland")

    # DNS backend defaults
    v.set_default("dns.backends.bind.enabled", False)
    v.set_default("dns.backends.bind.zones_dir", "/etc/named/zones")
    v.set_default("dns.backends.bind.named_conf", "/etc/named.conf.local")

    v.set_default("dns.backends.nsd.enabled", False)
    v.set_default("dns.backends.nsd.zones_dir", "/etc/nsd/zones")
    v.set_default("dns.backends.nsd.nsd_conf", "/etc/nsd/nsd.conf.d/zones.conf")

    v.set_default("dns.backends.coredns_mysql.enabled", False)
    v.set_default("dns.backends.coredns_mysql.host", "localhost")
    v.set_default("dns.backends.coredns_mysql.port", 3306)
    v.set_default("dns.backends.coredns_mysql.database", "coredns")
    v.set_default("dns.backends.coredns_mysql.username", "coredns")
    v.set_default("dns.backends.coredns_mysql.password", "")
    v.set_default("dns.backends.coredns_mysql.table_name", "records")

    # Set Defaults Datastore
    v.set_default("datastore.type", "sqlite")
    v.set_default("datastore.port", 3306)
    v.set_default("datastore.db_location", "data/directdns.db")

    # Reconciliation poller defaults
    v.set_default("reconciliation.enabled", False)
    v.set_default("reconciliation.dry_run", False)
    v.set_default("reconciliation.interval_minutes", 60)
    v.set_default("reconciliation.verify_ssl", True)

    # Peer sync defaults
    v.set_default("peer_sync.enabled", False)
    v.set_default("peer_sync.interval_minutes", 15)
    v.set_default("peer_sync.auth_username", "peersync")
    v.set_default("peer_sync.auth_password", "changeme")

    # Read configuration
    try:
        if not v.read_in_config():
            logger.warning("No config file found, using defaults")
    except Exception:
        logger.warning("No config file found, using defaults")

    return v


# Global config instance
config = load_config()
