FROM python:3.11.12-slim

# Install system dependencies.
# Both NSD and BIND are installed so the image works with any DNS backend type.
# The entrypoint detects which one is configured and starts only that daemon.
# CoreDNS MySQL users: neither daemon is started — the image is still usable.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bind9 \
    bind9utils \
    nsd \
    dnsutils \
    gcc \
    python3-dev \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# BIND setup
# ---------------------------------------------------------------------------
RUN mkdir -p /etc/named/zones && \
    chown -R bind:bind /etc/named && \
    chmod 755 /etc/named/zones

COPY docker/named.conf.local /etc/bind/
COPY docker/named.conf.options /etc/bind/
RUN chown root:bind /etc/bind/named.conf.*

# ---------------------------------------------------------------------------
# NSD setup
# ---------------------------------------------------------------------------
RUN mkdir -p /etc/nsd/zones /etc/nsd/nsd.conf.d && \
    chown -R nsd:nsd /etc/nsd && \
    chmod 755 /etc/nsd/zones

COPY docker/nsd.conf /etc/nsd/nsd.conf
RUN chown nsd:nsd /etc/nsd/nsd.conf

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./

RUN pip install "poetry==2.1.2"

COPY directdnsonly ./directdnsonly
COPY schema ./schema

RUN poetry config virtualenvs.create false && \
    poetry install

# Create data directories
RUN mkdir -p /app/data/queues /app/data/zones /app/logs && \
    chmod -R 755 /app/data

# Start script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2222 53/udp
CMD ["/entrypoint.sh"]
