#!/bin/bash
# On the Plane server (run by aws_plane.py through Session Manager): install or update Plane.
#   deploy.sh <s3://bucket/key.tar.gz> <region> <release> <address> <cert email or "-">
# <address> is a domain (HTTPS through Plane's proxy, certificate from Let's Encrypt) or the server's IP (HTTP only).
# Secrets are made once, on the first run, and kept in /opt/plane/plane.env (owner-only); updates keep them and the data.
set -euo pipefail
BUNDLE="$1"; REGION="$2"; RELEASE="$3"; ADDRESS="$4"; CERT_EMAIL="$5"
DIR=/opt/plane
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

timeout 900 cloud-init status --wait >/dev/null 2>&1 || true          # first boot: docker install still running
command -v docker >/dev/null || { echo "docker missing: first boot not finished, run up again"; exit 1; }
systemctl enable --now docker >/dev/null

aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" "$WORK/b.tar.gz"
tar -xzf "$WORK/b.tar.gz" -C "$WORK"
mkdir -p "$DIR"
install -m 644 "$WORK/docker-compose.yml" "$DIR/docker-compose.yml"

ENV="$DIR/plane.env"
if [ ! -f "$ENV" ]; then
  install -m 600 "$WORK/variables.env" "$ENV"
  rnd() { openssl rand -hex "$1"; }
  PGPASS=$(rnd 16); MQPASS=$(rnd 16)
  sed -i \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=$(rnd 32)|" \
    -e "s|^LIVE_SERVER_SECRET_KEY=.*|LIVE_SERVER_SECRET_KEY=$(rnd 32)|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$PGPASS|" \
    -e "s|^RABBITMQ_PASSWORD=.*|RABBITMQ_PASSWORD=$MQPASS|" \
    -e "s|^AWS_ACCESS_KEY_ID=.*|AWS_ACCESS_KEY_ID=plane$(rnd 6)|" \
    -e "s|^AWS_SECRET_ACCESS_KEY=.*|AWS_SECRET_ACCESS_KEY=$(rnd 24)|" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://plane:$PGPASS@plane-db/plane|" \
    -e "s|^AMQP_URL=.*|AMQP_URL=amqp://plane:$MQPASS@plane-mq:5672/plane|" \
    "$ENV"
  echo "new secrets made in $ENV"
fi

setv() {   # set KEY=VALUE in plane.env (add it if missing)
  if grep -q "^$1=" "$ENV"; then sed -i "s|^$1=.*|$1=$2|" "$ENV"; else echo "$1=$2" >> "$ENV"; fi
}
setv APP_RELEASE "$RELEASE"
setv APP_DOMAIN "$ADDRESS"
if [[ "$ADDRESS" =~ ^[0-9.]+$ ]]; then                  # no domain yet: plain HTTP on the IP
  setv WEB_URL "http://$ADDRESS"
  setv CORS_ALLOWED_ORIGINS "http://$ADDRESS"
  setv SITE_ADDRESS ":80"
else                                                   # domain: Plane's proxy gets a Let's Encrypt certificate
  setv WEB_URL "https://$ADDRESS"
  setv CORS_ALLOWED_ORIGINS "https://$ADDRESS"
  setv SITE_ADDRESS "$ADDRESS"
  [ "$CERT_EMAIL" != "-" ] && setv CERT_EMAIL "$CERT_EMAIL"
fi
chmod 600 "$ENV"

cd "$DIR"
docker compose -p plane -f docker-compose.yml --env-file plane.env pull --quiet
docker compose -p plane -f docker-compose.yml --env-file plane.env up -d --remove-orphans
docker image prune -f >/dev/null

for i in $(seq 1 60); do                               # the migrator runs first; the web app answers after it
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $ADDRESS" http://127.0.0.1/ || true)
  [[ "$code" =~ ^(200|301|302|307|308)$ ]] && break
  sleep 5
done
echo "Plane $RELEASE: local check HTTP ${code:-none}"
docker compose -p plane -f docker-compose.yml --env-file plane.env ps --format '{{.Service}}: {{.State}}' | sort
