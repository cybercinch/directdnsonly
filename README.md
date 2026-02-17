# DaDNS - DNS Management System

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

DaDNS propagates every zone update to all enabled backends in parallel using a
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