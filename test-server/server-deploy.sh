#!/bin/bash
# Runs on the test server (as root): unpack a code bundle from S3 and (re)start the containers.
#   server-deploy.sh s3://bucket/bundles/<name>.tar.gz <region> [domains] [front door]
set -euo pipefail
BUNDLE="$1"; REGION="$2"; DOMAINS="${3:-}"      # optional "a,b": addresses Caddy serves (the first is the main one)
FRONT_DOOR="${4:-}"                              # optional: where Salesforce loads the phone (https://bridge.telqon.com)
B=/opt/bridge
mkdir -p "$B/data" "$B/releases"
aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" /tmp/bundle.tar.gz
REL="$B/releases/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$REL"
tar -xzf /tmp/bundle.tar.gz -C "$REL"
ln -sfn "$REL" "$B/current"
if [ -n "$DOMAINS" ]; then
  LIST=$(echo "$DOMAINS" | sed 's/,/, /g'); FIRST=${DOMAINS%%,*}
  sed -i -e "s#^BRIDGE_DOMAIN=.*#BRIDGE_DOMAIN=\"$LIST\"#" -e "s#^BRIDGE_PUBLIC_URL=.*#BRIDGE_PUBLIC_URL=https://$FIRST#" "$B/bridge.env"
fi
if [ -n "$FRONT_DOOR" ]; then
  if grep -q '^BRIDGE_FRONT_DOOR=' "$B/bridge.env"; then
    sed -i "s#^BRIDGE_FRONT_DOOR=.*#BRIDGE_FRONT_DOOR=$FRONT_DOOR#" "$B/bridge.env"
  else
    echo "BRIDGE_FRONT_DOOR=$FRONT_DOOR" >> "$B/bridge.env"
  fi
fi
set -a; . "$B/bridge.env"; set +a
if [ ! -f "$B/data/config.yaml" ]; then
  sed "s#__DOMAIN__#${BRIDGE_DOMAIN%%,*}#g" "$REL/deploy/server-config.yaml" > "$B/data/config.yaml"
fi
chown -R 10001:10001 "$B/data"
cd "$B/current/deploy"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
ls -1dt "$B"/releases/* | tail -n +4 | xargs -r rm -rf
sleep 3
docker compose ps
echo "--- bridge"
docker logs --tail 15 bridge 2>&1 | grep -v "Setup code" || true
