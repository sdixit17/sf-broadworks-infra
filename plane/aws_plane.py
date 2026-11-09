#!/usr/bin/env python3
"""Plane (open-source project management) on AWS: one server running Plane's official containers.

    python plane/aws_plane.py up --domain plane.example.com --cert-email you@example.com   create it, or update it
    python plane/aws_plane.py up                                                            no domain yet: http://<IP>
    python plane/aws_plane.py status                                                        server, IP, HTTP check
    python plane/aws_plane.py delete                                                        remove the server (and its data)

What `up` creates in --region (default eu-west-2), named <name> (default plane):
    one EC2 server (t4g.medium: 2 vCPU, 4 GB; Amazon Linux 2023, ARM, 30 GB encrypted disk, IMDSv2) with Docker
    a fixed public IP (Elastic IP) for the DNS record
    a security group open on 80 and 443 only (no SSH: Session Manager)
    an IAM role for Session Manager and for reading its bundle; a private S3 bucket for bundles

Plane itself (web, API, workers, Postgres, Valkey, RabbitMQ, file store, proxy) runs from Plane's own
docker-compose.yml for the pinned --release, fetched from Plane's GitHub on your Mac and shipped in the bundle.
With --domain the proxy gets an HTTPS certificate from Let's Encrypt: point the domain's A record at the IP first
(the script prints it). Secrets are made on the server on the first run and kept in /opt/plane/plane.env; updates
keep them and all data (Docker volumes). Back up before upgrading --release.

After the first `up`: open the address, then /god-mode/ to create the instance admin, then create the workspace and
invite the team. For Mr Builder: a user "Mr Builder" with an API key (Profile settings > API tokens).
"""
import argparse
import io
import sys
import tarfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent
sys.path.insert(0, str(INFRA / "test-server"))
import aws_test_server as base  # noqa: E402
from aws_test_server import COMPOSE_URL, step  # noqa: E402

RAW = "https://raw.githubusercontent.com/makeplane/plane/{release}/deployments/cli/community/{file}"


def cmd():
    return f"python {sys.argv[0]}"


def fetch(release, name):
    url = RAW.format(release=release, file=name)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except Exception as e:
        raise SystemExit(f"could not download Plane's {name} for {release} ({url}): {e}")


class PlaneServer(base.Server):
    VOLUME_GB = 30

    def bundle(self):
        compose, env = fetch(self.a.release, "docker-compose.yml"), fetch(self.a.release, "variables.env")
        if b"makeplane/plane-backend" not in compose or b"SECRET_KEY" not in env:
            raise SystemExit(f"Plane's files for {self.a.release} don't look right; check --release")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in (("docker-compose.yml", compose), ("variables.env", env)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            t.add(HERE / "deploy.sh", arcname="deploy.sh")
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-plane-{self.a.release}.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        step(f"Plane {self.a.release} compose file and settings template uploaded")
        return f"s3://{self.bucket}/{key}"

    def user_data(self, bundle, setup_code):
        # Docker + compose + swap only; `up` installs Plane through Session Manager once the server runs.
        return f"""#!/bin/bash
set -eux
dnf install -y docker
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL {COMPOSE_URL} -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile;
  echo '/swapfile swap swap defaults 0 0' >> /etc/fstab; fi
"""

    def up(self):
        if self.a.domain and not self.a.cert_email:
            raise SystemExit("with --domain, also give --cert-email (Let's Encrypt sends certificate notices there)")
        step(f"account {self.account}, region {self.a.region}")
        self.bucket_setup()
        bundle = self.bundle()
        inst = self.instance()
        self.role_setup()
        if inst is None:
            iid, _ = self.launch(bundle)
        else:
            iid = inst["InstanceId"]
            self.network()
            if inst["State"]["Name"] != "running":
                if inst["State"]["Name"] == "stopping":
                    self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
                self.ec2.start_instances(InstanceIds=[iid])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
        ip = self.ip_setup(iid)
        address = self.a.domain or ip
        step(f"installing Plane {self.a.release} on {iid} (IP {ip}, address {address}); the first run takes ~10 min")
        status, out = self.run(iid, [
            "set -e; rm -rf /tmp/wd; mkdir -p /tmp/wd",
            "for i in $(seq 1 60); do command -v aws >/dev/null && break; sleep 5; done",
            f"aws s3 cp --region {self.a.region} --only-show-errors {bundle} /tmp/wd/b.tar.gz",
            "tar -xzf /tmp/wd/b.tar.gz -C /tmp/wd deploy.sh",
            f"bash /tmp/wd/deploy.sh {bundle} {self.a.region} {self.a.release} {address} "
            f"{self.a.cert_email or '-'} 2>&1 | tail -25"], timeout=2400)
        print()
        print(out.strip())
        if status != "Success":
            print(f"\n(command status: {status})")
        self.report(ip)

    def report(self, ip):
        url = f"https://{self.a.domain}" if self.a.domain else f"http://{ip}"
        print(f"""
Plane: {url}/   {self.http_check(url)}
First time: {url}/god-mode/ creates the instance admin; then create the workspace and invite the team.
{'DNS: point an A record for ' + self.a.domain + ' at ' + ip + ' (HTTPS works once it resolves).' if self.a.domain
 else 'A domain later: A record -> ' + ip + ', then ' + cmd() + ' up --domain <name> --cert-email <email>'}
Update Plane: {cmd()} up {'--domain ' + self.a.domain + ' --cert-email ...' if self.a.domain else ''} --release <new>""")

    @staticmethod
    def http_check(url):
        try:
            with urllib.request.urlopen(url + "/", timeout=15) as r:
                return f"(HTTP {r.status})"
        except Exception as e:
            return f"(not answering yet: {e})"

    def status(self):
        inst = self.instance()
        if not inst:
            return print(f"No Plane server yet: {cmd()} up")
        addr = self.eip()
        ip = addr["PublicIp"] if addr else inst.get("PublicIpAddress")
        print(f"Server {inst['InstanceId']}: {inst['State']['Name']}   IP {ip}   region {self.a.region}")
        if ip and inst["State"]["Name"] == "running":
            url = f"https://{self.a.domain}" if self.a.domain else f"http://{ip}"
            print(f"Plane: {url}/   {self.http_check(url)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "delete"])
    ap.add_argument("--domain", help="e.g. plane.example.com (HTTPS); without it Plane answers on http://<IP>")
    ap.add_argument("--cert-email", help="with --domain: email for Let's Encrypt certificate notices")
    ap.add_argument("--release", default="v1.4.3", help="Plane release to run (default v1.4.3)")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="plane")
    ap.add_argument("--instance-type", default="t4g.medium")
    a = ap.parse_args()
    s = PlaneServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
