#!/bin/bash

# Start BIND
/usr/sbin/named -u bind -f &

## Initialize MySQL schema if needed
#if [ -f /app/schema/coredns_mysql.sql ]; then
#    mysql -h mysql -u root -prootpassword coredns < /app/schema/coredns_mysql.sql
#fi

# Start the application
poetry run python directdnsonly/main.py