# DaDNS - DNS Management System

## Features
- Multi-backend DNS management (BIND, CoreDNS MySQL)
- Atomic zone updates
- Thread-safe operations
- Loguru-based logging

## Installation
```bash
    poetry install
    poetry run dadns
```

## Configuration

Edit config/app.yml for backend settings

### Config Files
#### `config/app.yml`
```yaml
timezone: Pacific/Auckland
log_level: INFO
queue_location: ./data/queues

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