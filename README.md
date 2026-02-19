# DirectDNSOnly - DNS Management System

## Deployment Topologies

Two reference topologies are documented below. Choose the one that matches your infrastructure.

---

### Topology A — Dual BIND Instances (High-Availability / Multi-Server)

Two independent DirectDNSOnly containers, each running a bundled BIND9 instance. Both are registered as Extra DNS servers in the same DirectAdmin Multi-Server environment, so DA pushes every zone change to both simultaneously.

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
    image: guisea/directdnsonly:2.3.0
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

### Topology Comparison

| | Topology A — Dual BIND | Topology B — CoreDNS MySQL |
|---|---|---|
| **DNS server** | BIND9 (bundled in container) | CoreDNS (separate, reads MySQL) |
| **Write path** | DA → each instance independently | DA → single instance → all backends |
| **Zone storage** | Zone files on container disk | MySQL database rows |
| **DA registration** | Two Extra DNS server entries | One Extra DNS server entry |
| **Redundancy model** | Independent app+DNS units | One app, N database backends |
| **Transient backend failure** | Retry queue (exp. backoff, 5 attempts) | Retry queue (exp. backoff, 5 attempts) |
| **Prolonged backend outage** | No auto-recovery — waits for next DA push to that zone | Reconciler healing pass re-pushes all missing zones using stored `zone_data` (no DA involvement) |
| **Container down during push** | Zone missed entirely — no retry possible at DA level | Zone missed at DA level — same limitation |
| **Cross-node consistency** | No sync between instances — drift until next DA push | All backends share same write path — reconciler enforces consistency |
| **Orphan detection** | Yes — reconciler removes zones deleted from DA | Yes — reconciler removes zones deleted from DA |
| **External DB required** | No | Yes (MySQL per CoreDNS node) |
| **Horizontal scaling** | Add DA Extra DNS entries + deploy new containers | Add backend stanzas in `config/app.yml` |
| **Best for** | Simple HA, no external DB | Multi-DC, stronger consistency guarantees |

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

### Is there a lighter alternative to bundle instead of BIND9?

Yes. **NSD (Name Server Daemon)** from NLnet Labs is the strongest candidate for a drop-in replacement:

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

- **Today, ~100–300 zones, no external DB:** NSD is a better bundled choice than BIND9 — lighter, faster, simpler config for authoritative-only use.
- **300–1 000+ zones:** CoreDNS MySQL wins — zone data in MySQL means no daemon reload at all.
- **Need zero-interruption zone swaps:** Knot DNS.
- **Need an HTTP API for zone management (no file I/O):** PowerDNS Authoritative with its native HTTP API and file/SQLite backend.

> NSD backend support is a planned future addition. A pull request is welcome — the implementation is straightforward since zone file format and reload semantics are nearly identical to the existing BIND backend.

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
- Multi-backend DNS management (BIND, CoreDNS MySQL)
- Parallel backend dispatch — all enabled backends updated simultaneously
- Persistent queue — zone updates survive restarts
- Automatic record-count verification and drift reconciliation
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

Edit `config/app.yml` for backend settings. Credentials can be overridden via
environment variables using the `DADNS_` prefix (e.g.
`DADNS_APP_AUTH_PASSWORD`).

### Config Files
#### `config/app.yml`
```yaml
timezone: Pacific/Auckland
log_level: INFO
queue_location: ./data/queues

app:
  auth_username: directdnsonly
  auth_password: changeme   # override with DADNS_APP_AUTH_PASSWORD

dns:
  default_backend: bind
  backends:
    bind:
      enabled: true
      zones_dir: ./data/zones
      named_conf: ./data/named.conf.include

    coredns_mysql:
      enabled: true
      host: "127.0.0.1"
      port: 3306
      database: "coredns"
      username: "coredns"
      password: "password"