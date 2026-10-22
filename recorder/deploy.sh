#!/bin/bash
# Runs on the recorder server (as root): install a code bundle from S3 and (re)start the recorder.
#   deploy.sh s3://bucket/bundles/<name>.tar.gz <region> <public IP> <allowed CIDRs, comma-separated> <recordings bucket> [SSM path]
set -euo pipefail
BUNDLE="$1"; REGION="$2"; PUBLIC_IP="$3"; ALLOW="$4"; BUCKET="$5"; PARAMS="${6:-}"
B=/opt/telqon-recorder
DATA=/var/lib/telqon-recorder
mkdir -p "$B/releases" "$DATA"
id recorder >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin recorder
chown recorder:recorder "$DATA"
chmod 750 "$DATA"
python3 -c "import boto3" 2>/dev/null || dnf install -y -q python3-boto3 || { dnf install -y -q python3-pip && pip3 install -q boto3; }
aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" /tmp/recorder.tar.gz
REL="$B/releases/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$REL"
tar -xzf /tmp/recorder.tar.gz -C "$REL"
ln -sfn "$REL" "$B/current"
cat > "$B/recorder.env" <<ENV
RECORDER_PUBLIC_IP=$PUBLIC_IP
RECORDER_ALLOW=$ALLOW
RECORDER_S3_BUCKET=$BUCKET
RECORDER_DIR=$DATA
RECORDER_PARAMS=$PARAMS
AWS_DEFAULT_REGION=$REGION
LOG_LEVEL=INFO
ENV
chown root:recorder "$B/recorder.env"
chmod 640 "$B/recorder.env"
install -m 644 "$REL/deploy/telqon-recorder.service" /etc/systemd/system/telqon-recorder.service
systemctl daemon-reload
systemctl enable -q telqon-recorder
systemctl restart telqon-recorder
ls -1dt "$B"/releases/* | tail -n +4 | xargs -r rm -rf
sleep 3
systemctl is-active telqon-recorder
journalctl -u telqon-recorder -n 12 --no-pager -o cat
