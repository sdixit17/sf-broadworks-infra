#!/bin/bash
# On the website server (run by aws_website.py through Session Manager): put a site bundle live.
#   deploy.sh <s3://bucket/key.tar.gz> <region>
set -euo pipefail
BUNDLE="$1"; REGION="$2"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# first boot: let the user data (swap + nginx install) finish instead of running a second dnf next to it
timeout 600 cloud-init status --wait >/dev/null 2>&1 || true
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile swap swap defaults 0 0' >> /etc/fstab
fi
command -v nginx >/dev/null || dnf install -y -q nginx
aws s3 cp --region "$REGION" --only-show-errors "$BUNDLE" "$WORK/site.tar.gz"
tar -xzf "$WORK/site.tar.gz" -C "$WORK"

# new release next to the old one, then switch (no half-copied site is ever served)
REL="/usr/share/nginx/releases/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "$REL"
cp -r "$WORK/dist/." "$REL/"
chmod -R a+rX "$REL"
ln -sfn "$REL" /usr/share/nginx/site.new
mv -Tf /usr/share/nginx/site.new /usr/share/nginx/site
ls -1dt /usr/share/nginx/releases/* | tail -n +6 | xargs -r rm -rf      # keep the last 5

install -m 644 "$WORK/nginx.conf" /etc/nginx/nginx.conf
nginx -t
systemctl enable --now nginx
systemctl reload nginx
echo "site live: $(ls "$REL" | tr '\n' ' ')"
