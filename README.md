# DirectDNSOnly — DNS Management for DirectAdmin

Stop wrestling with BIND configuration, AXFR transfer rules, and zone file syntax. DirectDNSOnly is a lightweight containerised DNS server that plugs directly into DirectAdmin as an additional DNS server — DA pushes zone changes, DirectDNSOnly handles everything else.

**Two containers, a docker run, and you have redundant authoritative nameservers. No named.conf. No zone files. No AXFR.**

```bash
docker run \
  -e DADNS_APP_AUTH_PASSWORD=my-secret \
  -e DADNS_DNS_DEFAULT_BACKEND=nsd \
  -e DADNS_DNS_BACKENDS_NSD_ENABLED=true \
  -p 53:53/udp -p 2222:2222 \
  cybercinch/directdnsonly:2.5.0
```

Register the container's IP as a server in DA → Server Manager → Multi Server Setup → Add Server. Done.

---

## Why Not Just Use BIND + AXFR?

| | BIND + AXFR | DirectDNSOnly |
|---|---|---|
| **Setup** | Edit named.conf, configure transfers, reload | `docker run` with env vars |
| **Zone updates** | DA pushes, AXFR replicates (eventually) | DA pushes directly to each instance |
| **Failure recovery** | Manual intervention or hope AXFR retries | Persistent queue with exponential backoff |
| **Orphan zones** | Accumulate silently | Reconciliation poller detects and cleans up |
| **Multi-server sync** | AXFR transfer rules per server | Peer sync — instances heal each other automatically |
| **Container support** | Technically possible, painful | First-class — built for it |

---

## Quickstart

### Single instance (simplest)

```bash
docker run -d \
  --name directdnsonly \
  -e DADNS_APP_AUTH_PASSWORD=my-secret \
  -e DADNS_DNS_DEFAULT_BACKEND=nsd \
  -e DADNS_DNS_BACKENDS_NSD_ENABLED=true \
  -p 53:53/udp \
  -p 2222:2222 \
  -v ddo-data:/app/data \
  cybercinch/directdnsonly:2.5.0
```

Register in DirectAdmin:
- DA → Server Manager → Multi Server Setup → Add Server
- URL: `http://your-server-ip:2222`
- Username: `directdnsonly`
- Password: whatever you set for `DADNS_APP_AUTH_PASSWORD`
- Enable **Zone Transfer** — allows DA to push zone changes to this server
- Enable **Domain Check** — prevents DA from adding domains that already exist on this server

### Two instances with peer sync (recommended HA)

```yaml
services:
  directdnsonly-1:
    image: cybercinch/directdnsonly:2.5.0
    ports:
      - "53:53/udp"
      - "2222:2222"
    environment:
      DADNS_APP_AUTH_PASSWORD: my-secret
      DADNS_DNS_DEFAULT_BACKEND: nsd
      DADNS_DNS_BACKENDS_NSD_ENABLED: "true"
      DADNS_PEER_SYNC_ENABLED: "true"
      DADNS_PEER_SYNC_AUTH_USERNAME: peersync
      DADNS_PEER_SYNC_AUTH_PASSWORD: peer-secret
      DADNS_PEER_SYNC_PEER_URL: http://directdnsonly-2:2222
      DADNS_PEER_SYNC_PEER_USERNAME: peersync
      DADNS_PEER_SYNC_PEER_PASSWORD: peer-secret
    volumes:
      - ddo1-data:/app/data

  directdnsonly-2:
    image: cybercinch/directdnsonly:2.5.0
    ports:
      - "54:53/udp"
      - "2223:2222"
    environment:
      DADNS_APP_AUTH_PASSWORD: my-secret
      DADNS_DNS_DEFAULT_BACKEND: nsd
      DADNS_DNS_BACKENDS_NSD_ENABLED: "true"
      DADNS_PEER_SYNC_ENABLED: "true"
      DADNS_PEER_SYNC_AUTH_USERNAME: peersync
      DADNS_PEER_SYNC_AUTH_PASSWORD: peer-secret
      DADNS_PEER_SYNC_PEER_URL: http://directdnsonly-1:2222
      DADNS_PEER_SYNC_PEER_USERNAME: peersync
      DADNS_PEER_SYNC_PEER_PASSWORD: peer-secret
    volumes:
      - ddo2-data:/app/data

volumes:
  ddo1-data:
  ddo2-data:
```

Register both as separate server entries in DA → Server Manager → Multi Server Setup → Add Server. Each instance is completely independent — DA pushes to both simultaneously.

---

## Features

- **NSD and BIND9 backends** — NSD recommended (lighter, faster, authoritative-only)
- **Persistent queue** — zone updates survive container restarts, replayed with exponential backoff
- **Peer sync** — instances heal each other; if one misses a DA push while offline it recovers automatically
- **Reconciliation poller** — detects orphan zones and queues cleanup without DA intervention
- **Parallel backend dispatch** — all enabled backends updated simultaneously via ThreadPoolExecutor
- **Record-count verification** — automatically detects and reconciles write drift
- **ZeroCLI** — every setting configurable via environment variables, no config file required for standard deployments

---

## Deployment Topologies

Three topologies to match your infrastructure.

---

### Topology A — Dual Independent Instances

Two DirectDNSOnly containers registered as separate servers in DA → Server Manager → Multi Server Setup → Add Server. DA pushes to each independently — no shared state, no cross-talk. Simple and reliable.

```
DirectAdmin Multi-Server
        │
        ├─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-1  (NSD)
        │                                     │
        │                               Persistent Queue
        │                               └─ retry on failure (exp. backoff)
        │                               (serves authoritative DNS on :53)
        │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-2  (NSD)
                                               │
                                         Persistent Queue
                                         └─ retry on failure (exp. backoff)
                                         (serves authoritative DNS on :53)
```

**Failure behaviour**

| Scenario | What happens |
|---|---|
| One container down during DA push | That instance misses the update and serves stale data until the next DA push for that zone |
| DNS daemon crashes, container stays up | Zone write lands in the persistent queue, replayed with backoff (30s → 2m → 5m → 15m → 30m, 5 attempts) |
| Zone deleted from DA while instance was down | Reconciliation poller detects the orphan on the next pass and queues a delete |
| Two instances diverge | No automatic cross-instance sync — drift persists until DA re-pushes the affected zone |

> For workloads where split-brain DNS is unacceptable, **directdnsonly Pro** (Topology B — MySQL-backed multi-DC) provides a single-write-path architecture that eliminates this risk.

---

### Topology B — MySQL-backed Multi-DC *(directdnsonly Pro)*

> **Available in directdnsonly Pro.**

One directdnsonly instance writes to N CoreDNS MySQL databases in parallel across multiple data centres. Single write path, zero daemon reloads, CoreDNS JSON cache fallback during database outages.

- One write → all backends updated concurrently
- Failed backends enter the retry queue automatically
- Adding a data centre is a single config stanza — no code changes
- CoreDNS reads from local MySQL at query time — no reload, no disruption

---

### Topology C — Multi-Instance with Peer Sync *(Most Robust Community HA)*

Multiple independent instances, each with a local DNS backend, registered as separate servers. Peer sync provides eventual consistency — if one instance misses a DA push while offline, it recovers from a peer on the next sync interval.

```
DirectAdmin Multi-Server
        │
        ├─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-syd  (NSD)
        │                                     │
        │                            Persistent Queue + zone_data store
        │                                     │
        │                             ◀──── peer sync (15m) ────▶
        │                                     │
        └─ POST /CMD_API_DNS_ADMIN ──▶  directdnsonly-mlb  (NSD)
                                               │
                                        Persistent Queue + zone_data store
```

**Why this is the most robust community topology:**
- DA pushes to each instance independently — no single point of failure in the write path
- If one instance misses a push while offline, peer sync recovers the zone from another instance automatically
- If both instances are offline during a push, they sync from each other on recovery — no DA re-push needed
- Reconciliation poller handles orphan zones independently on each instance

**Failure behaviour**

| Scenario | What happens |
|---|---|
| One instance down during DA push | Other instance(s) receive and serve the update. Downed instance recovers via peer sync |
| Both instances down during DA push | DNS is down — this is a critical outage and the immediate priority is restoring service, not zone sync. Deploy instances across independent infrastructure to make this scenario effectively impossible |
| Peer offline | Peer sync skips unreachable peers silently, resumes automatically on recovery |
| Zone deleted from DA | Reconciliation poller detects orphan and queues delete on each instance independently |

---

### Topology Comparison

| | Topology A — Dual NSD/BIND | Topology B — MySQL-backed *(Pro)* | Topology C — Multi-Instance + Peer Sync |
|---|---|---|---|
| **DNS server** | NSD or BIND9 | CoreDNS (reads MySQL) — *Pro* | NSD or BIND9 |
| **Write path** | DA → each instance independently | DA → single instance → all backends — *Pro* | DA → each instance independently |
| **Zone storage** | Zone files on disk | MySQL database — *Pro* | Zone files + SQLite zone_data store |
| **Redundancy model** | Independent app+DNS units | One app, N database backends — *Pro* | Independent instances + peer sync |
| **Transient backend failure** | Retry queue (exp. backoff, 5 attempts) | Retry queue — *Pro* | Retry queue |
| **Prolonged outage recovery** | Waits for next DA push | Reconciler re-pushes all missing zones — *Pro* | Peer sync pulls missed zones from healthy peer |
| **Cross-node consistency** | No sync | All backends share same write path — *Pro* | Peer sync (eventual consistency) |
| **External DB required** | No | Yes (MySQL per CoreDNS node) — *Pro* | No |
| **Best for** | Simple HA | Best resilience at scale — single write path, no daemon reloads | Most robust community HA |

---

## DNS Backend Selection

The container image ships with both **NSD** and **BIND9** installed. The entrypoint starts only the daemon matching your configured backend.

| | BIND9 | NSD | Knot DNS |
|---|---|---|---|
| **Design** | Authoritative + recursive + DNSSEC signing | Authoritative only — no recursive queries | Authoritative only — no recursive queries |
| **DNSSEC** | Full signing + serving | Full signing + serving; in this architecture serves DA-signed zones downstream | Full signing + serving with atomic zone swaps |
| **Base memory** | ~13–15 MB | ~5–10 MB | ~10–15 MB |
| **500-zone memory** | ~100–300 MB | <100 MB | ~100–200 MB |
| **Zone update** | `rndc reload <zone>` | `nsd-control reload` | Atomic RCU — zero query interruption |
| **Throughput** | Baseline | ~2–5× BIND9 | ~5–10× BIND9 |
| **Production use** | Wide adoption | `.nl`, `.se` TLD servers | CZ.NIC, Cloudflare internal |

**Recommendation:** Use NSD. It is lightweight, fast, authoritative-only, and uses the same RFC 1035 zone file format as BIND. In the DirectDNSOnly architecture DirectAdmin handles DNSSEC key management and zone signing — NSD simply serves whatever DA pushes, signed zones included, with no additional configuration required.

> The "authoritative only" design of NSD and Knot DNS is a feature in this context — neither will accidentally answer recursive queries from the internet, which is exactly the correct behaviour for a public-facing authoritative nameserver.

> **CoreDNS MySQL** (zero daemon reloads, scales to thousands of zones) is available in **directdnsonly Pro**.

---

## Architecture

### Queue-based backend dispatch

```
DirectAdmin zone push
        │
        ▼
  Persistent Queue  (survives restarts)
        │
        ▼
  save_queue_worker  (single daemon thread)
        │
        ├─ 1 backend ──▶  direct call
        │
        └─ N backends ──▶  ThreadPoolExecutor(max_workers=N)
                                 │
                           ┌─────┴─────┐
                           ▼           ▼
                         bind        nsd  ...
                        (concurrent, as_completed)
```

1. Single background thread drains the persistent queue — one zone at a time, in order
2. Single backend: direct call, no thread overhead
3. Multiple backends: parallel dispatch via ThreadPoolExecutor, slow or failing backends do not block others
4. After each write, stored record count is verified against the DA zone — mismatches trigger automatic reconciliation
5. Batch telemetry emitted on queue drain: zones processed, failures, elapsed time, throughput

```
INFO  | 📥 Batch started — 12 zone(s) queued for processing
SUCCESS | 📦 Batch complete — 12/12 zone(s) processed successfully in 1.8s (6.7 zones/sec)
```

---

## Configuration

All settings can be provided as environment variables — no config file required for standard deployments.

Settings resolve in this order (highest wins):
1. Environment variables (`DADNS_` prefix)
2. Config file (`app.yml` in `/etc/directdnsonly`, `.`, `./config`)
3. Built-in defaults

### Core

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `log_level` | `DADNS_LOG_LEVEL` | `info` | Log verbosity: `debug`, `info`, `warning`, `error` |
| `timezone` | `DADNS_TIMEZONE` | `Pacific/Auckland` | Timezone for log timestamps |
| `queue_location` | `DADNS_QUEUE_LOCATION` | `./data/queues` | Persistent zone-update queue path |

### App (HTTP server)

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `app.auth_username` | `DADNS_APP_AUTH_USERNAME` | `directdnsonly` | Basic auth username |
| `app.auth_password` | `DADNS_APP_AUTH_PASSWORD` | `changeme` | Basic auth password — **always override in production** |
| `app.listen_port` | `DADNS_APP_LISTEN_PORT` | `2222` | HTTP server port |
| `app.ssl_enable` | `DADNS_APP_SSL_ENABLE` | `false` | Enable TLS |
| `app.proxy_support` | `DADNS_APP_PROXY_SUPPORT` | `true` | Trust `X-Forwarded-For` from reverse proxy |

### DNS backends — NSD

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `dns.backends.nsd.enabled` | `DADNS_DNS_BACKENDS_NSD_ENABLED` | `false` | Enable NSD backend |
| `dns.backends.nsd.zones_dir` | `DADNS_DNS_BACKENDS_NSD_ZONES_DIR` | `/etc/nsd/zones` | Zone file directory |
| `dns.backends.nsd.nsd_conf` | `DADNS_DNS_BACKENDS_NSD_NSD_CONF` | `/etc/nsd/nsd.conf.d/zones.conf` | NSD zone include file |

### DNS backends — BIND9

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `dns.backends.bind.enabled` | `DADNS_DNS_BACKENDS_BIND_ENABLED` | `false` | Enable BIND9 backend |
| `dns.backends.bind.zones_dir` | `DADNS_DNS_BACKENDS_BIND_ZONES_DIR` | `/etc/named/zones` | Zone file directory |
| `dns.backends.bind.named_conf` | `DADNS_DNS_BACKENDS_BIND_NAMED_CONF` | `/etc/named.conf.local` | named.conf include file |

### Reconciliation poller

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `reconciliation.enabled` | `DADNS_RECONCILIATION_ENABLED` | `false` | Enable reconciliation poller |
| `reconciliation.dry_run` | `DADNS_RECONCILIATION_DRY_RUN` | `false` | Log orphans without acting (safe first-run) |
| `reconciliation.interval_minutes` | `DADNS_RECONCILIATION_INTERVAL_MINUTES` | `60` | Poll interval |
| `reconciliation.verify_ssl` | `DADNS_RECONCILIATION_VERIFY_SSL` | `true` | Verify TLS when querying DA |

> `reconciliation.directadmin_servers` (DA hostnames and credentials) requires a config file — cannot be expressed as env vars.

### Peer sync

Peer sync uses **separate credentials** from the DA-facing API — keep them distinct.

| Config key | Environment variable | Default | Description |
|---|---|---|---|
| `peer_sync.enabled` | `DADNS_PEER_SYNC_ENABLED` | `false` | Enable peer sync |
| `peer_sync.interval_minutes` | `DADNS_PEER_SYNC_INTERVAL_MINUTES` | `15` | Sync interval |
| `peer_sync.auth_username` | `DADNS_PEER_SYNC_AUTH_USERNAME` | `peersync` | Username this node accepts from peers |
| `peer_sync.auth_password` | `DADNS_PEER_SYNC_AUTH_PASSWORD` | `changeme` | Password this node accepts from peers — **always override** |
| `DADNS_PEER_SYNC_PEER_URL` | *(unset)* | Single peer URL (e.g. `http://ddo-2:2222`) |
| `DADNS_PEER_SYNC_PEER_USERNAME` | `peersync` | Username sent to peer |
| `DADNS_PEER_SYNC_PEER_PASSWORD` | *(empty)* | Password sent to peer |

> For multiple peers use a config file with the `peer_sync.peers` list.

---

## directdnsonly Pro

Community edition ships with NSD and BIND9 backends. Pro adds:

- **CoreDNS MySQL backend** — zero daemon reloads, scales to thousands of zones, automatic JSON cache fallback during database outages
- **Topology B** — single write path fanning out to N CoreDNS MySQL databases across multiple data centres
- **Automatic DirectAdmin registration** — point Pro at DA with credentials and it configures itself as an additional DNS server, checks for existing domain conflicts, and triggers DA to push all zones. Zero console interaction required
- **Management UI** — browser-based configuration and status dashboard served from the same container

Watch the repository or open an issue to register interest in the Pro beta.

---

## Built With

- [Python](https://python.org) + [CherryPy](https://cherrypy.dev)
- [NSD](https://nlnetlabs.nl/projects/nsd/) (default DNS backend)
- [BIND9](https://www.isc.org/bind/) (alternative DNS backend)
- [persist-queue](https://github.com/peter-wangxu/persist-queue) (durable zone update queue)
- [Loguru](https://github.com/Delgan/loguru) (logging)
- [Vyper-py](https://github.com/sn3d/vyper-py) (configuration)

Pro additionally uses [cybercinch/coredns_mysql_extend](https://github.com/cybercinch/coredns_mysql_extend) — a patched CoreDNS fork with correct AA flag handling, wildcard records, connection pooling, and automatic JSON cache fallback.

---

## Contributing

Pull requests welcome, particularly for additional DNS backends (Knot DNS, PowerDNS). Open an issue first to discuss scope.

Source is open — if you're evaluating whether to trust a container with your DA admin credentials, read it.
