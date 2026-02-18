FROM python:3.11.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    bind9 \
    bind9utils \
    dnsutils \
    gcc \
    python3-dev \
    default-libmysqlclient-dev \
    && rm -rf /var/lib/apt/lists/*

# Configure BIND
RUN mkdir -p /etc/named/zones && \
    chown -R bind:bind /etc/named && \
    chmod 755 /etc/named/zones

COPY docker/named.conf.local /etc/bind/
COPY docker/named.conf.options /etc/bind/
RUN chown root:bind /etc/bind/named.conf.*

# Install Python dependencies
WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./

# Install specific Poetry version that matches your lock file
RUN pip install "poetry==2.1.2"  # Adjust version to match your lock file

# Copy application files
COPY directdnsonly ./directdnsonly
COPY schema ./schema

RUN poetry config virtualenvs.create false && \
    poetry install



# Create data directories
RUN mkdir -p /app/data/queues && \
    mkdir -p /app/data/zones && \
    mkdir -p /app/logs && \
    chmod -R 755 /app/data

# Configure BIND zone directory to match app config
#RUN ln -s /app/data/zones /etc/named/zones/dadns

# Start script
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2222 53/udp
CMD ["/entrypoint.sh"]