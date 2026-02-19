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
        │                               writes zone file
        │                               reloads named
        │                               (serves authoritative DNS on :53)
        │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-2  (container, BIND backend)
                                               │
                                         writes zone file
                                         reloads named
                                         (serves authoritative DNS on :53)
```

**Each instance is completely independent** — no shared state, no cross-talk. Redundancy comes from DA pushing to both. If one container goes down, DA continues to push to the other.

> **DNS consistency note:** DirectAdmin pushes to each Extra DNS server sequentially, not atomically. Two brief consistency windows exist:
>
> - **Transient gap** — between the first and second push completing, the two instances will return different answers. This is typically sub-second and resolves on its own.
> - **Permanent drift** — if the push to one instance fails permanently (network outage, container down), that instance will serve stale or missing zone data until DA retries or the zone is updated again. The built-in reconciliation poller detects *orphaned zones* (present in our DB but deleted from DA) but does **not** compare zone content between instances.
>
> For workloads where split-brain DNS is unacceptable, use Topology B (single write path → multiple MySQL replicas) instead.

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
    image: guisea/directdnsonly:2.0.0
    ports:
      - "2222:2222"   # DA pushes here
      - "53:53/udp"   # authoritative DNS
    volumes:
      - ./config:/app/config
      - ./data:/app/data
```

Register both containers as separate Extra DNS entries in DA → DNS Administration → Extra DNS Servers, with the same credentials configured in each `config/app.yml`.

---

### Topology B — Single Instance, Dual CoreDNS MySQL Backends (Multi-DC)

One DirectDNSOnly instance receives zone pushes from DirectAdmin and fans out to two (or more) CoreDNS MySQL databases in parallel. CoreDNS servers in each data centre read from their local database. The directdnsonly instance is the sole write path — it does **not** serve DNS itself.

```
DirectAdmin
        │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly  (single container)
                                                │
                                     Persistent Queue (survive restarts)
                                                │
                                     ThreadPoolExecutor (one thread per backend)
                                         │               │
                                         ▼               ▼
                               coredns_mysql_primary   coredns_mysql_secondary
                               (MySQL DC1 10.0.0.80)   (MySQL DC2 10.0.1.29)
                                         │               │
                                         ▼               ▼
                                  CoreDNS (DC1)    CoreDNS (DC2)
                               serves :53 from DB  serves :53 from DB
```

Both MySQL backends are written **concurrently** within the same zone update. A slow or unreachable secondary does not block the primary write. Per-backend record verification runs after each write.

#### `config/app.yml`

```yaml
app:
  auth_username: directdnsonly
  auth_password: your-secret

dns:
  default_backend: coredns_mysql_primary
  backends:
    coredns_mysql_primary:
      type: coredns_mysql
      enabled: true
      host: 10.0.0.80
      port: 3306
      database: coredns
      username: coredns
      password: your-db-password

    coredns_mysql_secondary:
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
| DNS server | BIND9 (bundled in container) | CoreDNS (separate, reads MySQL) |
| Redundancy | Two independent app+DNS units | One app, N MySQL replicas |
| Zone storage | Zone files on container disk | MySQL database rows |
| DA registration | Two Extra DNS server entries | One Extra DNS server entry |
| Failure mode | One container can go down | MySQL connectivity required |
| Horizontal scaling | Add more DA Extra DNS entries | Add more MySQL backends in config |
| Best for | Simple HA, no external DB | Multi-DC, existing CoreDNS fleet |

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