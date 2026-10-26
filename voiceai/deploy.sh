#!/bin/bash
# Runs on the Voice AI server (as root): install a code bundle from S3 and (re)start the service.
#   deploy.sh s3://bucket/bundles/<name>.tar.gz <region> <public IP> <allowed CIDRs, comma-separated> <SSM path> [s3 greeting WAV]
set -euo pipefail
BUNDLE="$1"; REGION="$2"; PUBLIC_IP="$3"; ALLOW="$4"; PARAMS="$5"; GREETING="${6:-}"
B=/opt/telqon-voiceai
DATA=/var/lib/telqon-voiceai
mkdir -p "$B/releases" "$DATA"
id voiceai >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin voiceai
python3 -c "import boto3" 2>/dev/null || dnf install -y -q python3-boto3 || { dnf install -y -q python3-pip && pip3 install -q boto3; }
aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" /tmp/voiceai.tar.gz
REL="$B/releases/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$REL"
tar -xzf /tmp/voiceai.tar.gz -C "$REL"
ln -sfn "$REL" "$B/current"
if [ -n "$GREETING" ]; then
  aws s3 cp --region "$REGION" --only-show-errors "$GREETING" "$DATA/greeting.wav"
fi
chown -R voiceai:voiceai "$DATA"
chmod 750 "$DATA"
cat > "$B/voiceai.env" <<ENV
VOICEAI_PUBLIC_IP=$PUBLIC_IP
VOICEAI_ALLOW=$ALLOW
VOICEAI_PARAMS=$PARAMS
VOICEAI_GREETING=$( [ -f "$DATA/greeting.wav" ] && echo "$DATA/greeting.wav" )
AWS_DEFAULT_REGION=$REGION
LOG_LEVEL=INFO
ENV
chown root:voiceai "$B/voiceai.env"
chmod 640 "$B/voiceai.env"
install -m 644 "$REL/deploy/telqon-voiceai.service" /etc/systemd/system/telqon-voiceai.service
systemctl daemon-reload
systemctl enable -q telqon-voiceai
systemctl restart telqon-voiceai
ls -1dt "$B"/releases/* | tail -n +4 | xargs -r rm -rf
sleep 4
systemctl is-active telqon-voiceai
journalctl -u telqon-voiceai -n 12 --no-pager -o cat
