#!/bin/bash
# Runs on the Voice AI server (as root): install a code bundle from S3 and (re)start the service.
#   deploy.sh s3://bucket/bundles/<name>.tar.gz <region> <public IP> <allowed CIDRs, comma-separated> <SSM path>
#             [greeting: s3 WAV | none | -] [knowledge: s3 text file | none | -] [partner knowledge: same]
set -euo pipefail
BUNDLE="$1"; REGION="$2"; PUBLIC_IP="$3"; ALLOW="$4"; PARAMS="$5"; GREETING="${6:--}"; KNOWLEDGE="${7:--}"; PARTNER="${8:--}"
B=/opt/telqon-voiceai
DATA=/var/lib/telqon-voiceai
mkdir -p "$B/releases" "$DATA"
id voiceai >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /sbin/nologin voiceai
# Python 3.11 in a virtualenv: the Anthropic SDK needs Python 3.10 or later (Amazon Linux 2023's python3 is 3.9)
command -v python3.11 >/dev/null || dnf install -y -q python3.11 python3.11-pip
[ -x "$B/venv/bin/python" ] || python3.11 -m venv "$B/venv"
"$B/venv/bin/pip" install -q --disable-pip-version-check "anthropic==2.3.0" boto3
aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" /tmp/voiceai.tar.gz
REL="$B/releases/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$REL"
tar -xzf /tmp/voiceai.tar.gz -C "$REL"
ln -sfn "$REL" "$B/current"
case "$GREETING" in
  s3://*) aws s3 cp --region "$REGION" --only-show-errors "$GREETING" "$DATA/greeting.wav" ;;
  none) rm -f "$DATA/greeting.wav" ;;
esac
case "$KNOWLEDGE" in
  s3://*) aws s3 cp --region "$REGION" --only-show-errors "$KNOWLEDGE" "$DATA/knowledge.txt" ;;
  none) rm -f "$DATA/knowledge.txt" ;;
esac
case "$PARTNER" in
  s3://*) aws s3 cp --region "$REGION" --only-show-errors "$PARTNER" "$DATA/partner-knowledge.txt" ;;
  none) rm -f "$DATA/partner-knowledge.txt" ;;
esac
chown -R voiceai:voiceai "$DATA"
chmod 750 "$DATA"
cat > "$B/voiceai.env" <<ENV
VOICEAI_PUBLIC_IP=$PUBLIC_IP
VOICEAI_ALLOW=$ALLOW
VOICEAI_PARAMS=$PARAMS
VOICEAI_GREETING=$( [ -f "$DATA/greeting.wav" ] && echo "$DATA/greeting.wav" )
VOICEAI_KNOWLEDGE=$( [ -f "$DATA/knowledge.txt" ] && echo "$DATA/knowledge.txt" )
VOICEAI_PARTNER_KNOWLEDGE=$( [ -f "$DATA/partner-knowledge.txt" ] && echo "$DATA/partner-knowledge.txt" )
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
