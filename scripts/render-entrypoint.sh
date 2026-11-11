#!/bin/sh
set -eu
mkdir -p /var/data
chown dividend:dividend /var/data
exec su -s /bin/sh dividend -c "exec uvicorn dividend.main:create_app --factory --host 0.0.0.0 --port ${PORT:-8000}"
