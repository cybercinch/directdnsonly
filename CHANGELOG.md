# CHANGELOG


## v2.6.1 (2026-02-26)

### Bug Fixes

- Update Docker Hub password secret in release workflow :bug:
  ([`49f3f90`](https://github.com/cybercinch/directdnsonly/commit/49f3f901c2eb56d89811afb6a1a0fa08fb9e2e49))


## v2.6.0 (2026-02-26)

### Bug Fixes

- Remove stale schema COPY and libmysqlclient-dev from Dockerfile 🐛
  ([`0fb8d8a`](https://github.com/cybercinch/directdnsonly/commit/0fb8d8a4266005013d288fd42a3eb775853c4cb2))

### Build System

- Add build-docker-tagged and build-docker-release justfile recipes 🐳
  ([`df0a802`](https://github.com/cybercinch/directdnsonly/commit/df0a80247bdcef6bcb287e91d3869cf1f30d359c))

Adds two new recipes: - build-docker-tagged: builds and pushes a specific tag + :latest -
  build-docker-release: reads version from pyproject.toml and pushes :{version} + :latest

Also fixes build-docker to use the cybercinch Docker config.

### Continuous Integration

- Add semantic release pipeline and GitHub Actions workflows 🚀
  ([`27f4ac5`](https://github.com/cybercinch/directdnsonly/commit/27f4ac5e786d1e648ac13b02057e056848d39978))

- python-semantic-release v9 reads conventional commits to determine version bump; writes back to
  pyproject.toml[project.version] - CI workflow runs pytest on every push and PR - Release workflow
  on master: PSR creates tag + GitHub release, then Docker job builds multi-arch (amd64/arm64) and
  pushes to Docker Hub as cybercinch/directdnsonly:{version} and :latest - v2.5.0 baseline tag
  created locally; push with --tags on first push - Requires secrets: DOCKERHUB_USERNAME,
  DOCKERHUB_TOKEN

### Documentation

- Revise README for clarity and organization
  ([`d1fd3f6`](https://github.com/cybercinch/directdnsonly/commit/d1fd3f671a3738988e8f015c4a4be3a753ecaa2d))

Updated README to improve clarity and structure, including changes to sections on deployment,
  features, and configuration.

### Features

- Track zone ownership transfer during DA server migration 🔄
  ([`d02f3bf`](https://github.com/cybercinch/directdnsonly/commit/d02f3bff27ace7d1e7630f6d1b063282295dc4f0))

When a zone push arrives from a server that differs from the recorded hostname,
  update_zone_hostname() now transfers ownership in the DB so the reconciler and delete guard always
  reference the current master.

Logs [migration] Zone master transfer on ownership change, noting if the DA username also changed.
  The delete guard already rejects deletes from non-owner servers; its log message now explicitly
  calls out the 'Keep DNS' scenario to aid operator diagnosis.


## v2.5.0 (2026-02-25)

### Bug Fixes

- Add __main__.py so python -m directdnsonly works in container 🐛
  ([`29903a1`](https://github.com/cybercinch/directdnsonly/commit/29903a1c501edf2063bb020f61645e4a06d2f3fb))

- directdnsonly/__main__.py: inserts package dir into sys.path before importing main.py (which uses
  short-form relative imports) then calls main(); works for both `python -m directdnsonly` and the
  dadns script - pyproject.toml: wire up `dadns` console script entry point

- Correct RDATA encoding and batch processing in CoreDNS MySQL backend 🐛
  ([`d4c59fa`](https://github.com/cybercinch/directdnsonly/commit/d4c59fad2cd0594bb8945bfa2890ffa499b288cb))

- Fix dnspython silently relativizing in-zone FQDN targets to '@' by calling
  rdata.to_text(origin=origin, relativize=False); CoreDNS MySQL requires absolute FQDNs in RDATA and
  was serving '.' for any CNAME/MX pointing to the zone apex - Reorder write_zone to delete stale
  records before inserting new ones so a brief NXDOMAIN is preferred over briefly serving duplicate
  records - Rework save-queue batch loop: keep batch open until queue is empty rather than closing
  after a fixed timeout, so sequential DA zone pushes accumulate into a single batch - Add
  managed_by='directadmin' to _ensure_zone_exists for new and legacy NULL rows

- Migrate remaining session.query() calls to SQLAlchemy 2.0 select() 🔧
  ([`92d5768`](https://github.com/cybercinch/directdnsonly/commit/92d57684a1363338b372d2245af325a6878ac7fd))

- Remove stale COPY config from Dockerfile 🐛
  ([`50d45de`](https://github.com/cybercinch/directdnsonly/commit/50d45de3121770f6e72a29e7fefeab567c0605a8))

Root config/ directory was removed when the duplicate config/app.yml was deleted — the canonical
  config is now bundled inside directdnsonly/config/ and is already covered by the existing COPY
  directdnsonly step.

- Update .gitignore to include dist/ and modify build command in justfile :bug:
  ([`e4ca3bc`](https://github.com/cybercinch/directdnsonly/commit/e4ca3bcb8a38f3b00df0916e3db1b290ab02fffd))

### Chores

- Add .gitkeep to logs directory for empty directory preservation
  ([`f4b8ba2`](https://github.com/cybercinch/directdnsonly/commit/f4b8ba2d010236082df3a98c7e3c3fd444aee97b))

- Bump version to 2.4.0 🚀
  ([`47e33a2`](https://github.com/cybercinch/directdnsonly/commit/47e33a21187c87cae1aa2d93822d7112fe6ac97d))

- Clean out previous version of directdnsonly 🔥
  ([`1d1c12b`](https://github.com/cybercinch/directdnsonly/commit/1d1c12b66128828c4769d8c0b35c982aa9148782))

- Complete SQLAlchemy 2.0 migration in coredns_mysql backend and tests ⬆️
  ([`4f25fed`](https://github.com/cybercinch/directdnsonly/commit/4f25fedcbd42d6bbecde408a10b95639df249b05))

Migrate remaining session.query() calls in coredns_mysql.py to select()/session.execute() style;
  update bulk delete to delete() construct and count to func.count(); drop sessionmaker(bind=).
  Update test fixtures and assertions to match.

Zero session.query() calls remaining across the entire codebase.

- Remove CoreDNS MySQL backend (relocated to Pro tier) 🔒
  ([`6424f49`](https://github.com/cybercinch/directdnsonly/commit/6424f4986d3b402d5114d078feb7a1174413b41c))

Strips coredns_mysql backend, schema, and tests from the community edition. The backend, test suite,
  and SQL schema are preserved in /tmp/directdnsonly-pro-exports for the Pro package.

- Remove directdnsonly/app/backends/coredns_mysql.py - Remove tests/test_coredns_mysql.py - Remove
  schema/coredns_mysql.sql - backends/__init__.py: remove CoreDNSMySQLBackend import + registration
  - config/__init__.py: remove coredns_mysql vyper defaults - config/app.yml: strip coredns backend
  examples, add nsd comment - docker-compose.yml: remove MySQL service and coredns env vars

- Remove unimplemented PowerDNS MySQL backend 🗑️
  ([`ba792bb`](https://github.com/cybercinch/directdnsonly/commit/ba792bb883887d917a5dcfbb8b8750f2c3eca151))

Dead code from v1 planning — never implemented, superseded by the CoreDNS MySQL backend. Also
  carried a broken stale import that would have caused an ImportError on load.

- Rename repo and image to cybercinch/directdnsonly 🏷️
  ([`8c3d5cb`](https://github.com/cybercinch/directdnsonly/commit/8c3d5cbc5ecec9f1b42be2f57af75a3dc77885f6))

- Rewrite justfile for pyenv + poetry dev workflow 🔧
  ([`10a38f9`](https://github.com/cybercinch/directdnsonly/commit/10a38f9777e1898dc2d971385817d1f746e9dbd6))

Replaces outdated PyInstaller-only recipe with full task runner: install, test, coverage,
  coverage-html, test-one, fmt, fmt-check, ci, run, build, clean. PATH export wires in pyenv shims
  and poetry automatically.

- Upgrade SQLAlchemy to 2.0 and bump all stale deps ⬆️
  ([`d574a6a`](https://github.com/cybercinch/directdnsonly/commit/d574a6a4de1da18939837d5b512ecf61248d77c4))

- SQLAlchemy 1.4 → 2.0.46: migrate all session.query() calls to select() / session.execute() style;
  move declarative_base import from ext.declarative to sqlalchemy.orm; explicit conn.commit() after
  DDL in _migrate(); drop sessionmaker(bind=) keyword - persist-queue 1.0 → 1.1, pymysql 1.1.1 →
  1.1.2, dnspython 2.7 → 2.8, pyyaml 6.0.2 → 6.0.3 - pytest 8.3 → 9.0.2, pytest-cov 6.1 → 7.0,
  pytest-mock 3.14 → 3.15.1, black 25.1 → 26.1

97 tests pass, zero deprecation warnings

### Code Style

- Apply black formatting across codebase 🎨
  ([`c9ec369`](https://github.com/cybercinch/directdnsonly/commit/c9ec369184f3522a81a76616a480739dacde1365))

No logic changes — pure reformatting of line lengths, dict literals, method-chain line breaks, and
  trailing newlines to satisfy black's style.

### Documentation

- Add DNS server resource and scale guide with NSD/Knot comparison 📊
  ([`0c9b1b6`](https://github.com/cybercinch/directdnsonly/commit/0c9b1b6857d6dc4bb9a6491b34af7beb829e1e2d))

Cover memory profiles, zone-count thresholds, reload behaviour, and throughput characteristics for
  BIND9, CoreDNS MySQL, NSD, and Knot DNS. Call out NSD as the recommended lighter bundled
  alternative to BIND9 (~5-10 MB base, near-identical zone file format, same reload semantics) and
  note the ~300-zone crossover where CoreDNS MySQL starts to win.

- Clarify Knot DNS and PowerDNS are not implemented backends 📝
  ([`1bc2943`](https://github.com/cybercinch/directdnsonly/commit/1bc29437218c2244d4068e37993c2f40e866af09))

Add explicit note that only nsd, bind, and coredns_mysql are available — Knot and PowerDNS are
  listed as architectural context only.

- Coredns MySQL is the recommended choice at all scale levels 🏆
  ([`f321b8c`](https://github.com/cybercinch/directdnsonly/commit/f321b8c4b229d7dc4af41e6bed62c603575cd12d))

The cybercinch fork's resilience features (cache fallback, health monitoring, zero downtime,
  connection pooling) make it the best DNS backend regardless of zone count — not just at 300+
  zones. Update summary recommendation and topology comparison "Best for" row to reflect this.

- Document CoreDNS fork resilience features accurately 📋
  ([`88413b7`](https://github.com/cybercinch/directdnsonly/commit/88413b708a1d4cc2ba199dad475752a1ae8f6b22))

Replace vague "file caching" description with the confirmed feature set: connection pooling,
  degraded operation (JSON cache fallback), smart caching, health monitoring, zero downtime. Update
  Topology B failure table to reflect that CoreDNS serves from cache throughout MySQL outages. Add
  write/read split summary — retry queue covers writes, CoreDNS cache covers reads.

- Document peer-sync auth credentials as distinct from DA-facing auth 🔑
  ([`4db8705`](https://github.com/cybercinch/directdnsonly/commit/4db8705c8d062d36ba88752e6cdaf63fcf8dbfba))

Add peer_sync.auth_username / peer_sync.auth_password to config reference, explain the two auth
  realms (/internal vs zone push routes), and fix the Topology C docker-compose example which was
  incorrectly reusing the DA password for peer credentials.

- Replace CoreDNS MySQL sections with Pro tier callouts 🔒
  ([`8d3b653`](https://github.com/cybercinch/directdnsonly/commit/8d3b6532a91eb7e7bf6e4b8b53ce602d9c12bdb0))

All CoreDNS MySQL technical content (scale guide, fork feature table, Topology B failure modes) is
  retained as reference for the Pro edition. Community README now clearly marks CoreDNS MySQL as
  Pro-only throughout: topology comparison, config reference, env-var examples, and recommendations.

- Rewrite topology comparison with accurate failure-mode analysis 📋
  ([`16db61c`](https://github.com/cybercinch/directdnsonly/commit/16db61cccfcfa92c2972c9784d892bb6e4d9ad02))

Expand both topology diagrams to show the retry queue and healing pass in the flow. Add per-topology
  failure-behaviour tables covering transient backend failure, prolonged outage,
  container-down-during-push, and cross-node drift. Rewrite the comparison table to call out the key
  architectural difference: Topology A has no auto-recovery from prolonged BIND failure (needs next
  DA push); Topology B's reconciler healing pass re-syncs missing backends from stored zone_data
  without any DA involvement.

### Features

- Add CMD_MULTI_SERVER methods to DirectAdminClient 🔌
  ([`f15aba7`](https://github.com/cybercinch/directdnsonly/commit/f15aba712371e9580553bd2f047c0146eb01aecf))

Adds get_extra_dns_servers(), add_extra_dns_server(), and the high-level ensure_extra_dns_server()
  which registers a node and enforces dns=yes + domain_check=yes in a single call. Also adds the
  generic post() helper. 10 new tests, 141 total.

- Add initial_delay_minutes to reconciler for LB stagger 🕐
  ([`1312115`](https://github.com/cybercinch/directdnsonly/commit/1312115767583f631c028d55b64934cca957d314))

Configurable startup delay before the first reconciliation pass so that multiple receivers behind a
  load balancer can be offset without relying on container start order (which is lost on reboot).
  Set to half the interval on the secondary receiver — e.g. interval 60m → delay 30m. Default is 0
  (no change to existing behaviour). Stop event is respected during the delay so the worker shuts
  down cleanly even mid-wait.

- Add NSD backend and Topology C (multi-instance with peer sync) 🏗️
  ([`0b1d44b`](https://github.com/cybercinch/directdnsonly/commit/0b1d44ba13082f967d72c189e5a8450d84a3d36b))

- New NSDBackend: zone files + nsd-control reload, zone registration via nsd.conf.d include file;
  mirrors BIND backend interface exactly - BackendRegistry now supports type "nsd"; config defaults
  for nsd.zones_dir and nsd.nsd_conf - Dockerfile installs both NSD and BIND9 — entrypoint detects
  configured backend type(s) and starts only the required daemon; CoreDNS MySQL deployments start
  neither - docker/nsd.conf: minimal NSD base config with remote-control and zones.conf include -
  entrypoint.sh: reads config file + env vars to determine which daemon to start; runs
  nsd-control-setup on first boot - 20 new NSD backend tests (117 total, all passing) - README:
  Topology C (multi-instance + peer sync) documented as most robust HA option; NSD config reference;
  updated topology comparison table; NSD env-var-only compose examples; version 2.5.0

- Add peer sync worker for zone_data exchange between nodes 🔄
  ([`31d00ec`](https://github.com/cybercinch/directdnsonly/commit/31d00ec5abdc1bd7039aed1c2d9a75d1e2eb5772))

Adds optional peer-to-peer zone_data replication between directdnsonly instances. Enables eventual
  consistency in DA Multi-Server topologies without a shared datastore.

- InternalAPI: GET /internal/zones (list) and ?domain= (detail) exposes zone_data to peers via
  existing basic auth - PeerSyncWorker: interval-based daemon thread that fetches zone_data from
  configured peers, storing newer entries locally; peer downtime is silently skipped and retried
  next interval - WorkerManager: wires PeerSyncWorker alongside reconciler; exposes
  peer_syncer_alive in queue_status - Config: peer_sync block with enabled/interval_minutes/peers[]
  - Tests: 13 tests covering sync, skip-older, skip-unreachable, empty peer list, bad status, and
  missing zone_data scenarios

- Add test suite, fix backend bugs, remove legacy artifacts 🧪
  ([`db08355`](https://github.com/cybercinch/directdnsonly/commit/db08355da3ec77783a231cffada132ca46797041))

- Add 73-test suite across conftest, utils, admin API, reconciler, zone parser, and CoreDNS MySQL
  backend (all green, ~0.5s) - Fix zone_exists filter using wrong column name (name → zone_name) -
  Fix delete_zone missing dot_fqdn normalization on lookup - Remove spurious unused `from config
  import config` in coredns_mysql.py - Fix config loader to search module-relative path so tests
  find app.yml without needing a root-level config/ directory - Remove legacy v1 Flask prototype
  (app.py), empty config.json, and duplicate root config/app.yml

- Conditional BIND startup; config search path priority fix 🔧
  ([`5aa4f71`](https://github.com/cybercinch/directdnsonly/commit/5aa4f719f1ee815a45a8d4db964d0e0f29a02fbc))

- entrypoint: only start named when a bind backend is configured and enabled in app.yml;
  CoreDNS-only deployments skip named entirely - config: user-supplied paths (/etc/directdnsonly,
  ./config) now searched before the bundled app.yml so mounted configs take effect - docs:
  deployment topology reference — Topology A (dual BIND HA) and Topology B (single instance,
  multi-DC CoreDNS MySQL) - chore: bump version to 2.1.0 - justfile: add build-docker recipe

- Enhance README with detailed concurrent multi-backend processing architecture and usage
  instructions :zap:
  ([`5e2ce3e`](https://github.com/cybercinch/directdnsonly/commit/5e2ce3e3ddfad6cc98069a31c85009d2f572cbc6))

- Mesh peer sync with health tracking and separate peer credentials 🔗
  ([`fe1430b`](https://github.com/cybercinch/directdnsonly/commit/fe1430bf66c315e477994a913eeaf7f887f6d6f5))

- Separate peer_sync.auth_username/password from the DA-facing credentials so /internal/* uses its
  own basic auth; a compromised peer cannot push zones or access the admin API - Per-peer health
  tracking: consecutive failure count, degraded/recovered log events at FAILURE_THRESHOLD (3) and on
  first successful contact after degradation - Gossip-lite mesh discovery: each sync pass calls
  /internal/peers on every known peer and adds newly discovered node URLs automatically; a linear
  chain of initial connections is sufficient to form a full mesh - /internal/peers endpoint returns
  the node's live peer URL list - Support DADNS_PEER_SYNC_PEER_N_URL/USERNAME/PASSWORD numbered env
  vars for multi-peer env-var-only deployments (up to 9); original single-peer
  DADNS_PEER_SYNC_PEER_URL retained for backward compatibility

- Migrate to Poetry and implement multi-backend DNS management ✨
  ([`d970b5e`](https://github.com/cybercinch/directdnsonly/commit/d970b5eada567a710e9b68cff7cf3ecec402bc2e))

- Migrated from setuptools to Poetry; added pyproject.toml, poetry.lock, poetry.toml and
  .python-version (Python 3.11.12) - Built out full directdnsonly Python package with BIND and
  CoreDNS MySQL backends, CherryPy REST API, persist-queue worker, and vyper-based config - Auth
  credentials now read from config/env (app.auth_username/password) rather than hardcoded; override
  via DADNS_APP_AUTH_PASSWORD env var - Added Dockerfile.deepseek: Python 3.11 slim + BIND9 + Poetry
  install - Rewrote docker-compose.yml for local dev stack (MySQL + dadns services) - Added SQL
  schema, docker/ BIND configs, justfile, tests, and README - Expanded .gitignore for Poetry/Python
  project artifacts

- Operational status endpoint + reconciler/peer state tracking 📊
  ([`2e1e017`](https://github.com/cybercinch/directdnsonly/commit/2e1e0175b7cb5c88fccb93a92248ccf7fdfd6ba8))

- ReconciliationWorker._last_run stores per-pass stats (da_servers_polled, zones_in_da/db,
  orphans_found/queued, hostnames_backfilled/migrated, zones_healed, duration_seconds, dry_run flag)
  - ReconciliationWorker.get_status() exposes state for API/UI consumption - _heal_backends() now
  returns healed count - PeerSyncWorker.get_peer_status() serialises _peer_health to JSON-safe dict
  (url, healthy, consecutive_failures, last_seen) with summary totals - WorkerManager tracks
  dead-letter count; queue_status() now returns nested reconciler/peer_sync dicts replacing flat
  reconciler_alive/peer_syncer_alive - New GET /status endpoint (StatusAPI) aggregates queue depths,
  worker liveness, reconciler last-run, peer health, and live zone count; computes ok/degraded/error
  - .gitignore: exclude .claude/, .vscode/, .env (always local) - app.yml: add documented datastore
  section (SQLite default + MySQL commented) - 164 tests passing (23 new tests added)

- Peer sync configurable via env vars + document CoreDNS file cache 🔗
  ([`b87c2ab`](https://github.com/cybercinch/directdnsonly/commit/b87c2ab6c40e028ff06712b632a0b358d5ff71bf))

- PeerSyncWorker reads DADNS_PEER_SYNC_PEER_URL / _USERNAME / _PASSWORD env vars to populate a
  single peer without a config file; deduped against any config-file peers so the URL never appears
  twice - 2 new tests (119 total, all passing) - README: peer sync single-peer env var table;
  Topology C compose example updated to use env vars only (no config file needed for two-node setup)
  - README: document cybercinch/coredns_mysql_extend built-in file caching — serves from cache
  during MySQL outages, eliminates per-query round-trips

- Retry queue, backend healing, and zone_data persistence 🔁
  ([`c7df5aa`](https://github.com/cybercinch/directdnsonly/commit/c7df5aa4b60b5e2fadc0070d1d6b618117189252))

- worker.py: third persistent retry queue with exponential backoff (30s→30m, max 5 attempts); failed
  backends tracked per-item so retries target only the failing nodes; zone_data stored in DB after
  every successful write - Domain model: zone_data TEXT + zone_updated_at DATETIME columns; additive
  migration applied on startup so existing deployments upgrade in place - ReconciliationWorker:
  Option C healing pass — checks every configured backend for zone presence after each
  reconciliation cycle and re-queues any zone missing from a backend using stored zone_data,
  enabling automatic recovery from prolonged backend outages without waiting for DirectAdmin to
  re-push - 82 tests, all passing

- Update dependencies in poetry.lock and pyproject.toml :sparkles:
  ([`f48fddb`](https://github.com/cybercinch/directdnsonly/commit/f48fddb4f170ccb71e846346d007aca9ea2efeb2))

- Added `certifi` version 2026.1.4 and `charset-normalizer` version 3.4.4 to poetry.lock. -
  Introduced `idna` version 3.11 to poetry.lock. - Updated `requests` to version 2.32.5 in
  poetry.lock and added it as a dependency in pyproject.toml. - Updated `urllib3` to version 2.6.3
  in poetry.lock. - Added extras for `requests` and `urllib3` in poetry.lock.

- Update Dockerfile for improved BIND configuration and application setup
  ([`8e362f5`](https://github.com/cybercinch/directdnsonly/commit/8e362f5bc03e0e331ac15348467a3de4a3213a10))

### Refactoring

- Extract DirectAdminClient into directdnsonly.app.da module 🏗️
  ([`2648bf0`](https://github.com/cybercinch/directdnsonly/commit/2648bf094b48371c204e14d64a083dc78fae7649))

Move all outbound DirectAdmin HTTP logic out of ReconciliationWorker and into a dedicated,
  independently testable DirectAdminClient class:

- directdnsonly/app/da/client.py: list_domains (paginated JSON + legacy fallback), get
  (authenticated GET to any CMD_* endpoint), _login (DA Evo session-cookie fallback),
  _parse_legacy_domain_list - directdnsonly/app/da/__init__.py: public re-export of
  DirectAdminClient - reconciler.py: now purely reconciliation logic; instantiates a client per
  configured server — no HTTP code remaining - tests/test_da_client.py: 16 dedicated tests for
  DirectAdminClient - tests/test_reconciler.py: mocks at the DirectAdminClient class boundary
  instead of the internal _fetch_da_domains method

Bumped to 2.2.0 — DirectAdminClient is now a first-class public API.


## v1.0.9 (2021-09-07)
