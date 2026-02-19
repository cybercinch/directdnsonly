# DirectDNSOnly - DNS Management System

## Deployment Topologies

Three reference topologies are documented below. Choose the one that matches your infrastructure.

---

### Topology A — Dual NSD/BIND Instances (High-Availability / Multi-Server)

Two independent DirectDNSOnly containers, each running a bundled DNS daemon (NSD by default, or BIND9). Both are registered as Extra DNS servers in the same DirectAdmin Multi-Server environment, so DA pushes every zone change to both simultaneously.

```
DirectAdmin Multi-Server
        │
        ├─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-1  (container, BIND backend)
        │                                     │
        │                               Persistent Queue
        │                               ├─ writes zone file
        │                               ├─ reloads named
        │                               └─ retry on failure (exp. backoff)
        │                               (serves authoritative DNS on :53)
        │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-2  (container, BIND backend)
                                               │
                                         Persistent Queue
                                         ├─ writes zone file
                                         ├─ reloads named
                                         └─ retry on failure (exp. backoff)
                                         (serves authoritative DNS on :53)
```

**Each instance is completely independent** — no shared state, no cross-talk. Redundancy comes from DA pushing to both. If one container goes down, DA continues to push to the other.

#### Failure behaviour

| Scenario | What happens |
|---|---|
| One container down during DA push | DA cannot deliver; that instance misses the update. The retry queue inside that instance cannot help — the push never arrived. When the container recovers, it will serve stale zone data until DA re-pushes (next zone change triggers a new push). |
| BIND crashes but container stays up | The zone write lands in the persistent queue. The retry worker replays it with exponential backoff (30 s → 2 m → 5 m → 15 m → 30 m, up to 5 attempts). |
| Zone deleted from DA while instance was down | The reconciliation poller detects the orphan on the next pass and queues a delete, keeping the BIND instance clean without manual intervention. |
| Two instances diverge | No automatic cross-instance sync. Drift persists until DA re-pushes the affected zone (i.e. the next time that domain is touched in DA). |

> **DNS consistency note:** DirectAdmin pushes to each Extra DNS server sequentially, not atomically. If one instance is offline when a zone is changed, that instance will serve stale data until the next DA push for that zone. For workloads where split-brain DNS is unacceptable, use Topology B (single write path → multiple MySQL backends) instead.

#### `config/app.yml` — instance 1

```yaml
app:
  auth_username: directdnsonly
  auth_password: your-secret

dns:
  default_backend: bind
  backends:
    bind:
      type: bind
      enabled: true
      zones_dir: /etc/named/zones
      named_conf: /etc/bind/named.conf.local
```

#### `docker-compose.yml` sketch — instance 1

```yaml
services:
  directdnsonly-1:
    image: guisea/directdnsonly:2.5.0
    ports:
      - "2222:2222"   # DA pushes here
      - "53:53/udp"   # authoritative DNS
    volumes:
      - ./config:/app/config
      - ./data:/app/data
```

Register both containers as separate Extra DNS entries in DA → DNS Administration → Extra DNS Servers, with the same credentials configured in each `config/app.yml`.

---

### Topology B — Single Instance, Multiple CoreDNS MySQL Backends (Multi-DC)

One DirectDNSOnly instance receives zone pushes from DirectAdmin and fans out to two (or more) CoreDNS MySQL databases in parallel. CoreDNS servers in each data centre read from their local database. The directdnsonly instance is the sole write path — it does **not** serve DNS itself.

```
DirectAdmin
        │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly  (single container)
                                                │
                                     Persistent Queue (survives restarts)
                                     zone_data stored to SQLite after each write
                                                │
                                     ThreadPoolExecutor (one thread per backend)
                                         │               │
                                         ▼               ▼
                               coredns_mysql_dc1   coredns_mysql_dc2
                               (MySQL 10.0.0.80)   (MySQL 10.0.1.29)
                                         │               │
                                    [success]       [failure → retry queue]
                                         │               │
                                         ▼       30s/2m/5m/15m/30m backoff
                                  CoreDNS (DC1)        retry → coredns_mysql_dc2
                               serves :53 from DB
                                                │
                             Reconciliation poller (every N minutes)
                             ├─ orphan detection (zones removed from DA)
                             └─ healing pass: zone_exists() per backend
                                → re-queue any backend missing a zone
                                  using stored zone_data (no DA re-push needed)
```

Both MySQL backends are written **concurrently** within the same zone update. A slow or unreachable secondary does not block the primary write. Failed backends enter the retry queue automatically. The reconciliation healing pass provides a further safety net for prolonged outages.

#### Failure behaviour

| Scenario | What happens |
|---|---|
| One MySQL backend unreachable | Other backend(s) succeed immediately. Failed backend queued for retry with exponential backoff (30 s → 2 m → 5 m → 15 m → 30 m, up to 5 attempts). |
| MySQL backend down for hours | Retry queue exhausts. On recovery, the reconciliation healing pass detects the backend is missing zones and re-pushes all of them using stored `zone_data` — no DA intervention required. |
| directdnsonly container restarts | Persistent queue survives. In-flight zone updates replay on startup. |
| directdnsonly container down during DA push | DA cannot deliver. Persistent queue on disk is intact; when the container comes back, it resumes processing any previously queued items. New pushes during downtime are lost at the DA level (DA does not retry). |
| Zone deleted from DA | Reconciliation poller detects orphan and queues delete across all backends. |

#### `config/app.yml`

```yaml
app:
  auth_username: directdnsonly
  auth_password: your-secret

dns:
  default_backend: coredns_mysql_dc1
  backends:
    coredns_mysql_dc1:
      type: coredns_mysql
      enabled: true
      host: 10.0.0.80
      port: 3306
      database: coredns
      username: coredns
      password: your-db-password

    coredns_mysql_dc2:
      type: coredns_mysql
      enabled: true
      host: 10.0.1.29
      port: 3306
      database: coredns
      username: coredns
      password: your-db-password
```

Adding a third data centre is a single stanza in the config — no code changes required.

---

### Topology C — Multi-Instance with Peer Sync (Most Robust)

Multiple independent DirectDNSOnly containers, each with a single local DNS backend (NSD or CoreDNS MySQL), registered as separate Extra DNS servers in DirectAdmin Multi-Server. Peer sync provides eventual consistency — if one instance misses a DA push while it is offline, it recovers the missing zone data from a peer on the next sync interval.

```
DirectAdmin Multi-Server
        │
        ├─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-syd  (NSD or CoreDNS MySQL)
        │                                     │
        │                            Persistent Queue + zone_data store
        │                            ├─ writes zone file / MySQL
        │                            ├─ reloads daemon
        │                            └─ retry on failure
        │                                     │
        │                             ◀──── peer sync ────▶
        │                                     │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-mlb  (NSD or CoreDNS MySQL)
                                               │
                                        Persistent Queue + zone_data store
                                        ├─ writes zone file / MySQL
                                        ├─ reloads daemon
                                        └─ retry on failure
```

**Why this is the most robust topology:**

- DA pushes to each instance independently — no single point of failure
- No load balancer in the write path — a dead LB cannot silence both instances
- Each instance serves DNS immediately from its own daemon
- If SYD misses a push while offline, it pulls the newer zone from MLB on the next peer sync (default 15 minutes)
- Peer sync is best-effort eventual consistency — deliberately simple, no consensus protocol

#### Failure behaviour

| Scenario | What happens |
|---|---|
| One instance down during DA push | Other instance(s) receive and serve the update. When the downed instance recovers, peer sync detects the stale/missing `zone_updated_at` and pulls the newer zone data from a peer. |
| Both instances down during DA push | Both miss the push. When they recover, they sync from each other — the most recently updated peer wins per zone. No DA re-push needed. |
| Peer offline | Peer sync silently skips unreachable peers. Syncs resume automatically when the peer recovers. |
| Zone deleted from DA | Reconciliation poller detects the orphan and queues the delete on each instance independently. |

#### `config/app.yml` — instance syd

```yaml
app:
  auth_username: directdnsonly
  auth_password: your-secret

dns:
  default_backend: nsd
  backends:
    nsd:
      type: nsd
      enabled: true
      zones_dir: /etc/nsd/zones
      nsd_conf: /etc/nsd/nsd.conf.d/zones.conf

peer_sync:
  enabled: true
  interval_minutes: 15
  peers:
    - url: http://directdnsonly-mlb:2222
      username: directdnsonly
      password: your-secret

reconciliation:
  enabled: true
  interval_minutes: 60
  directadmin_servers:
    - hostname: da.syd.example.com
      port: 2222
      username: admin
      password: da-secret
      ssl: true
```

Register each container as a separate Extra DNS server entry in DA → DNS Administration → Extra DNS Servers with the same credentials.

---

### Topology Comparison

| | Topology A — Dual NSD/BIND | Topology B — CoreDNS MySQL | Topology C — Multi-Instance + Peer Sync |
|---|---|---|---|
| **DNS server** | NSD or BIND9 (bundled) | CoreDNS (separate, reads MySQL) | NSD or CoreDNS MySQL (per instance) |
| **Write path** | DA → each instance independently | DA → single instance → all backends | DA → each instance independently |
| **Zone storage** | Zone files on container disk | MySQL database rows | Zone files or MySQL + SQLite zone_data store |
| **DA registration** | Two Extra DNS server entries | One Extra DNS server entry | One entry per instance |
| **Redundancy model** | Independent app+DNS units | One app, N database backends | Independent instances + peer sync |
| **Transient backend failure** | Retry queue (exp. backoff, 5 attempts) | Retry queue (exp. backoff, 5 attempts) | Retry queue (exp. backoff, 5 attempts) |
| **Prolonged backend outage** | No auto-recovery — waits for next DA push | Reconciler healing pass re-pushes all missing zones | Peer sync pulls missed zones from a healthy peer |
| **Container down during push** | Zone missed entirely | Zone missed at DA level | Zone missed at DA level; recovered via peer sync |
| **Cross-node consistency** | No sync between instances | All backends share same write path | Peer sync provides eventual consistency |
| **Orphan detection** | Yes — reconciler | Yes — reconciler | Yes — reconciler (per instance) |
| **External DB required** | No | Yes (MySQL per CoreDNS node) | No (NSD) or Yes (CoreDNS MySQL) |
| **Horizontal scaling** | Add DA Extra DNS entries + containers | Add backend stanzas in config | Add DA Extra DNS entries + containers + peer list |
| **Best for** | Simple HA, no external DB | Multi-DC, stronger consistency | Most robust HA — survives extended outages without DA re-push |

---

## DNS Server Resource and Scale Guide

### BIND9 vs CoreDNS MySQL — resource profile

| | BIND9 (bundled) | CoreDNS + MySQL |
|---|---|---|
| **Base memory** | ~13–15 MB | ~20–30 MB (CoreDNS binary) + MySQL process |
| **Per-zone overhead** | ~300 bytes per resource record in memory | Schema rows in MySQL; CoreDNS itself holds no zone state |
| **100-zone deployment** | ~30–60 MB total | ~80–150 MB (CoreDNS + MySQL combined) |
| **500-zone deployment** | ~100–300 MB total | ~100–200 MB (zone data lives in MySQL, not CoreDNS) |
| **Zone reload** | `rndc reload <zone>` — per-zone is fast; full reload blocks queries for seconds at large counts | No reload needed — CoreDNS queries MySQL at resolution time |
| **Zone update latency** | File write + `rndc reload` — typically <100 ms for a single zone | Write to MySQL — immediately visible to CoreDNS on next query |
| **CPU on reload** | Spikes on full `rndc reload`; grows linearly with zone count | No reload CPU spike; MySQL write is the only cost |
| **Query throughput** | High — zones loaded into memory | Slightly lower — each query hits MySQL (mitigated by MySQL query cache / connection pooling) |
| **Scale ceiling** | Degrades past ~1 000 zones: memory climbs, full reloads take 120 s+ | Scales with MySQL — thousands of zones with no DNS-process impact |

**Rule of thumb:** Below ~300 zones BIND9 and CoreDNS MySQL are broadly comparable. Above ~500 zones, CoreDNS MySQL has a significant advantage because zone data lives entirely in the database — adding a new zone costs one MySQL INSERT, not a daemon reload.

---

### Bundled DNS daemons — NSD and BIND9

The container image ships with **both NSD and BIND9** installed. The entrypoint reads your config and starts only the daemon that matches the configured backend type. CoreDNS MySQL deployments start neither.

**NSD (Name Server Daemon)** from NLnet Labs is the default recommendation:

| | BIND9 | NSD | Knot DNS |
|---|---|---|---|
| **Design focus** | Everything (authoritative + recursive + DNSSEC + ...) | Authoritative only | Authoritative only |
| **Base memory** | ~13–15 MB | ~5–10 MB | ~10–15 MB |
| **500-zone memory** | ~100–300 MB | <100 MB (estimated) | ~100–200 MB (3× zone text size) |
| **Zone update** | `rndc reload <zone>` | `nsd-control reload` | `knotc zone-reload` (atomic via RCU — zero query interruption) |
| **Config format** | `named.conf` / zone files | `nsd.conf` / zone files (nearly identical format) | `knot.conf` / zone files |
| **Docker image** | ~150–200 MB | ~30–50 MB Alpine | ~40–60 MB Alpine |
| **Recursive queries** | Yes (if configured) | No | No |
| **Throughput** | Baseline | ~2–5× BIND9 | ~5–10× BIND9 (2.2 Mqps at 32 cores) |
| **Production use** | Wide adoption | TLD servers (`.nl`, `.se`), major registries | CZ.NIC, Cloudflare internal testing |

**NSD** would slot almost directly into the existing BIND backend implementation — zone files have the same RFC 1035 format, and `nsd-control reload` is the equivalent of `rndc reload`. The main implementation difference is the daemon config file (`nsd.conf` vs `named.conf`) and the absence of `named.conf.local`-style zone includes (NSD uses pattern-based config).

**Knot DNS** is worth considering if seamless zone updates matter: its RCU (Read-Copy-Update) mechanism serves the old zone to in-flight queries while atomically swapping in the new one — there is no window where queries see a partially-loaded zone. It is meaningfully heavier than NSD at moderate zone counts but the best performer at high scale.

**Summary recommendation:**

- **Up to ~300 zones, no external DB:** Use the NSD backend (bundled) — lighter, faster, authoritative-only, same zone file format as BIND.
- **300–1 000+ zones:** CoreDNS MySQL wins — zone data in MySQL means no daemon reload at all.
- **Need zero-interruption zone swaps:** Knot DNS.
- **Need an HTTP API for zone management (no file I/O):** PowerDNS Authoritative with its native HTTP API and file/SQLite backend.

---

## CoreDNS MySQL Backend — Required Fork

The `coredns_mysql` backend writes zones to a MySQL database that CoreDNS reads
at query time. **Vanilla CoreDNS with a stock MySQL plugin is not sufficient** —
out of the box it does not act as a fully authoritative server, does not return
NS records in the additional section, does not set the AA flag, and does not
handle wildcard records.

This project is designed to work with a patched fork that resolves all of those
issues:

**[cybercinch/coredns_mysql_extend](https://github.com/cybercinch/coredns_mysql_extend)**

Key differences from the upstream plugin:

- Fully authoritative responses — correct AA flag and NXDOMAIN on misses
- Wildcard record support (`*` entries served correctly)
- NS records returned in the additional section

Use the BIND backend if you want a zero-dependency setup with no custom CoreDNS
build required.

---

## Features
- Multi-backend DNS management (NSD, BIND, CoreDNS MySQL)
- Parallel backend dispatch — all enabled backends updated simultaneously
- Persistent queue — zone updates survive restarts
- Automatic record-count verification and drift reconciliation
- Peer sync — eventual consistency between directdnsonly instances
- Thread-safe operations
- Loguru-based logging

## Installation
```bash
    poetry install
    poetry run dadns
```

## Concurrent Multi-Backend Processing

DirectDNSOnly propagates every zone update to all enabled backends in parallel using a
queue-based worker architecture.

### Architecture

```
DirectAdmin zone push
        │
        ▼
  Persistent Queue  (persist-queue, survives restarts)
        │
        ▼
  save_queue_worker  (single daemon thread, sequential dequeue)
        │
        ├─ 1 backend enabled ──▶  direct call  (no thread overhead)
        │
        └─ N backends enabled ──▶  ThreadPoolExecutor(max_workers=N)
                                         │
                                   ┌─────┴─────┐
                                   ▼           ▼
                                 bind     coredns_dc1  ...
                                (concurrent, as_completed)
```

### How it works

1. **Queue consumer** — A single background thread drains the persistent save
   queue. Items are processed one zone at a time, in order.

2. **Single-backend path** — When only one backend is enabled, the zone is
   written directly with no extra thread spawning.

3. **Parallel-backend path** — When two or more backends are enabled, a
   `ThreadPoolExecutor` with one thread per backend dispatches all writes
   simultaneously. Results are collected with `as_completed`, so a slow or
   failing backend does not block the others.

4. **Record verification** — After each successful write, the backend's stored
   record count is compared against the authoritative count parsed from the
   source zone file (the DirectAdmin zone). Mismatches trigger automatic
   reconciliation: extra records are removed and the count is re-verified.

5. **Batch telemetry** — The worker tracks batch start time and emits a summary
   log on queue drain, including zones processed, failures, elapsed time, and
   throughput (zones/sec).

### Log output (example)

```
INFO  | 📥 Batch started — 12 zone(s) queued for processing
DEBUG | Processing example.com across 2 backends concurrently: bind, coredns_dc1
DEBUG | Parallel processing of example.com across 2 backends completed in 43ms
SUCCESS | 📦 Batch complete — 12/12 zone(s) processed successfully in 1.8s (6.7 zones/sec)
```

### Adding backends

Enable additional backends in `config/app.yml`. Each enabled backend is
automatically included in the parallel dispatch — no code changes required.

```yaml
dns:
  backends:
    bind:
      enabled: true
    coredns_dc1:
      enabled: true
      host: "mysql-dc1"
    coredns_dc2:
      enabled: true        # adds a third parallel worker automatically
      host: "mysql-dc2"
```

## Configuration

DirectDNSOnly uses [Vyper](https://github.com/sn3d/vyper-py) for configuration. Settings are resolved in this priority order (highest wins):

1. **Environment variables** — `DADNS_` prefix, dots replaced with underscores (e.g. `DADNS_APP_AUTH_PASSWORD`)
2. **Config file** — `app.yml` searched in `/etc/directdnsonly`, `.`, `./config`, then the bundled default
3. **Built-in defaults** (shown in the table below)

**A config file is entirely optional.** Every scalar setting can be provided through environment variables alone.

---

### Configuration Reference

#### Core

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `log_level` | `DADNS_LOG_LEVEL` | `info` | Log verbosity: `debug`, `info`, `warning`, `error` |
| `timezone` | `DADNS_TIMEZONE` | `Pacific/Auckland` | Timezone for log timestamps |
| `queue_location` | `DADNS_QUEUE_LOCATION` | `./data/queues` | Path for the persistent zone-update queue |

#### App (HTTP server)

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `app.auth_username` | `DADNS_APP_AUTH_USERNAME` | `directdnsonly` | Basic auth username for all API routes (including `/internal`) |
| `app.auth_password` | `DADNS_APP_AUTH_PASSWORD` | `changeme` | Basic auth password — **always override in production** |
| `app.listen_port` | `DADNS_APP_LISTEN_PORT` | `2222` | TCP port the HTTP server binds to |
| `app.ssl_enable` | `DADNS_APP_SSL_ENABLE` | `false` | Enable TLS on the HTTP server |
| `app.proxy_support` | `DADNS_APP_PROXY_SUPPORT` | `true` | Trust `X-Forwarded-For` from a reverse proxy |
| `app.proxy_support_base` | `DADNS_APP_PROXY_SUPPORT_BASE` | `http://127.0.0.1` | Trusted proxy base address |

#### Datastore (internal SQLite)

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `datastore.type` | `DADNS_DATASTORE_TYPE` | `sqlite` | Internal datastore type (only `sqlite` supported) |
| `datastore.db_location` | `DADNS_DATASTORE_DB_LOCATION` | `data/directdns.db` | Path to the SQLite database file |

#### DNS backends — BIND

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `dns.default_backend` | `DADNS_DNS_DEFAULT_BACKEND` | _(none)_ | Name of the primary backend (used for status/health reporting) |
| `dns.backends.bind.enabled` | `DADNS_DNS_BACKENDS_BIND_ENABLED` | `false` | Enable the bundled BIND9 backend |
| `dns.backends.bind.zones_dir` | `DADNS_DNS_BACKENDS_BIND_ZONES_DIR` | `/etc/named/zones` | Directory where zone files are written |
| `dns.backends.bind.named_conf` | `DADNS_DNS_BACKENDS_BIND_NAMED_CONF` | `/etc/named.conf.local` | `named.conf` include file managed by directdnsonly |

#### DNS backends — NSD

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `dns.backends.nsd.enabled` | `DADNS_DNS_BACKENDS_NSD_ENABLED` | `false` | Enable the NSD backend |
| `dns.backends.nsd.zones_dir` | `DADNS_DNS_BACKENDS_NSD_ZONES_DIR` | `/etc/nsd/zones` | Directory where zone files are written |
| `dns.backends.nsd.nsd_conf` | `DADNS_DNS_BACKENDS_NSD_NSD_CONF` | `/etc/nsd/nsd.conf.d/zones.conf` | NSD zone include file managed by directdnsonly |

#### DNS backends — CoreDNS MySQL

The built-in env var mapping targets the backend named `coredns_mysql`. For multiple named CoreDNS backends (e.g. `coredns_dc1`, `coredns_dc2`) you must use a config file — see [Multi-backend via config file](#multi-backend-via-config-file) below.

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `dns.backends.coredns_mysql.enabled` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_ENABLED` | `false` | Enable the CoreDNS MySQL backend |
| `dns.backends.coredns_mysql.host` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_HOST` | `localhost` | MySQL host |
| `dns.backends.coredns_mysql.port` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_PORT` | `3306` | MySQL port |
| `dns.backends.coredns_mysql.database` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_DATABASE` | `coredns` | MySQL database name |
| `dns.backends.coredns_mysql.username` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_USERNAME` | `coredns` | MySQL username |
| `dns.backends.coredns_mysql.password` | `DADNS_DNS_BACKENDS_COREDNS_MYSQL_PASSWORD` | _(empty)_ | MySQL password |

#### Reconciliation poller

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `reconciliation.enabled` | `DADNS_RECONCILIATION_ENABLED` | `false` | Enable the background reconciliation poller |
| `reconciliation.dry_run` | `DADNS_RECONCILIATION_DRY_RUN` | `false` | Log orphans but do not queue deletes (safe first-run mode) |
| `reconciliation.interval_minutes` | `DADNS_RECONCILIATION_INTERVAL_MINUTES` | `60` | How often the poller runs |
| `reconciliation.verify_ssl` | `DADNS_RECONCILIATION_VERIFY_SSL` | `true` | Verify TLS certificates when querying DirectAdmin |

> The `reconciliation.directadmin_servers` list (DA hostnames, credentials) requires a config file — it cannot be expressed as simple env vars.

#### Peer sync

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `peer_sync.enabled` | `DADNS_PEER_SYNC_ENABLED` | `false` | Enable background peer-to-peer zone sync |
| `peer_sync.interval_minutes` | `DADNS_PEER_SYNC_INTERVAL_MINUTES` | `15` | How often each peer is polled |

> The `peer_sync.peers` list (peer URLs, credentials) requires a config file — it cannot be expressed as simple env vars.

---

### Environment-variable-only setup

No config file is needed for single-backend deployments. Pass all settings as container environment variables.

#### Topology A/C — NSD backend (env vars only, recommended)

```bash
DADNS_APP_AUTH_PASSWORD=my-strong-secret
DADNS_DNS_DEFAULT_BACKEND=nsd
DADNS_DNS_BACKENDS_NSD_ENABLED=true
DADNS_DNS_BACKENDS_NSD_ZONES_DIR=/etc/nsd/zones
DADNS_DNS_BACKENDS_NSD_NSD_CONF=/etc/nsd/nsd.conf.d/zones.conf
DADNS_QUEUE_LOCATION=/app/data/queues
DADNS_DATASTORE_DB_LOCATION=/app/data/directdns.db
```

`docker-compose.yml` snippet (Topology C — two instances with peer sync via config file):

```yaml
services:
  directdnsonly-syd:
    image: guisea/directdnsonly:2.5.0
    ports:
      - "2222:2222"
      - "53:53/udp"
    environment:
      DADNS_APP_AUTH_PASSWORD: my-strong-secret
      DADNS_DNS_DEFAULT_BACKEND: nsd
      DADNS_DNS_BACKENDS_NSD_ENABLED: "true"
    volumes:
      - ./config/syd:/app/config   # contains peer_sync.peers list
      - syd-data:/app/data

  directdnsonly-mlb:
    image: guisea/directdnsonly:2.5.0
    ports:
      - "2223:2222"
      - "54:53/udp"
    environment:
      DADNS_APP_AUTH_PASSWORD: my-strong-secret
      DADNS_DNS_DEFAULT_BACKEND: nsd
      DADNS_DNS_BACKENDS_NSD_ENABLED: "true"
    volumes:
      - ./config/mlb:/app/config   # contains peer_sync.peers list
      - mlb-data:/app/data

volumes:
  syd-data:
  mlb-data:
```

#### Topology A — BIND backend (env vars only)

```bash
# docker run / docker-compose environment:
DADNS_APP_AUTH_USERNAME=directdnsonly
DADNS_APP_AUTH_PASSWORD=my-strong-secret
DADNS_DNS_DEFAULT_BACKEND=bind
DADNS_DNS_BACKENDS_BIND_ENABLED=true
DADNS_DNS_BACKENDS_BIND_ZONES_DIR=/etc/named/zones
DADNS_DNS_BACKENDS_BIND_NAMED_CONF=/etc/named/named.conf.local
DADNS_QUEUE_LOCATION=/app/data/queues
DADNS_DATASTORE_DB_LOCATION=/app/data/directdns.db
```

`docker-compose.yml` snippet:

```yaml
services:
  directdnsonly:
    image: guisea/directdnsonly:2.5.0
    ports:
      - "2222:2222"
      - "53:53/udp"
    environment:
      DADNS_APP_AUTH_PASSWORD: my-strong-secret
      DADNS_DNS_DEFAULT_BACKEND: bind
      DADNS_DNS_BACKENDS_BIND_ENABLED: "true"
      DADNS_DNS_BACKENDS_BIND_ZONES_DIR: /etc/named/zones
      DADNS_DNS_BACKENDS_BIND_NAMED_CONF: /etc/named/named.conf.local
    volumes:
      - ddo-data:/app/data

volumes:
  ddo-data:
```

#### Topology B — single CoreDNS MySQL backend (env vars only)

```bash
DADNS_APP_AUTH_PASSWORD=my-strong-secret
DADNS_DNS_DEFAULT_BACKEND=coredns_mysql
DADNS_DNS_BACKENDS_COREDNS_MYSQL_ENABLED=true
DADNS_DNS_BACKENDS_COREDNS_MYSQL_HOST=mysql.dc1.internal
DADNS_DNS_BACKENDS_COREDNS_MYSQL_PORT=3306
DADNS_DNS_BACKENDS_COREDNS_MYSQL_DATABASE=coredns
DADNS_DNS_BACKENDS_COREDNS_MYSQL_USERNAME=coredns
DADNS_DNS_BACKENDS_COREDNS_MYSQL_PASSWORD=db-secret
DADNS_QUEUE_LOCATION=/app/data/queues
DADNS_DATASTORE_DB_LOCATION=/app/data/directdns.db
```

---

### Multi-backend via config file

When you need **multiple named backends** (e.g. two CoreDNS MySQL instances in different data centres), **peer sync**, or **reconciliation with DA servers**, use a config file mounted at `/app/config/app.yml` (or `/etc/directdnsonly/app.yml`):

```yaml
app:
  auth_username: directdnsonly
  auth_password: my-strong-secret   # or use DADNS_APP_AUTH_PASSWORD

dns:
  default_backend: coredns_dc1
  backends:
    coredns_dc1:
      type: coredns_mysql
      enabled: true
      host: 10.0.0.80
      port: 3306
      database: coredns
      username: coredns
      password: db-secret-dc1

    coredns_dc2:
      type: coredns_mysql
      enabled: true
      host: 10.0.1.29
      port: 3306
      database: coredns
      username: coredns
      password: db-secret-dc2

reconciliation:
  enabled: true
  dry_run: false
  interval_minutes: 60
  verify_ssl: true
  directadmin_servers:
    - hostname: da1.example.com
      port: 2222
      username: admin
      password: da-secret
      ssl: true

peer_sync:
  enabled: true
  interval_minutes: 15
  peers:
    - url: http://ddo-2:2222
      username: directdnsonly
      password: my-strong-secret
```

Credentials in the config file can still be overridden by env vars — for example, `DADNS_APP_AUTH_PASSWORD` overrides `app.auth_password` regardless of what the file says.