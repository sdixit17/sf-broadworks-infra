#!/usr/bin/env python3
"""The marketing website on AWS: one small server with a fixed IP, serving the static site with nginx.

    python website/aws_website.py up [--demo-number "+44 ..."] [--brand "Telcom-Company"]   create it, or deploy changes
    python website/aws_website.py status                                                  server, IP, HTTP check
    python website/aws_website.py delete                                                  remove everything it made

Site options for `up` (they end up in config.js; pages are unchanged):
    --brand               the company name on the site (default Telcom-Company)
    --demo-number         the phone number that reaches the AI demo (Voice AI); without it the buttons go to the demo form
    --hubspot-portal, --hubspot-form-demo, --hubspot-form-partner
                          HubSpot portal ID and form GUIDs: forms then submit straight to HubSpot
    --contact-email       without HubSpot: forms open an email to this address

What `up` creates in --region (default eu-west-2), named <name> (default telqon-website):
    one EC2 server (t4g.nano, Amazon Linux 2023, ARM, IMDSv2, encrypted disk) running nginx
    a fixed public IP (Elastic IP): the site is http://<IP>/ until a domain is pointed at it
    a security group open on 80 and 443 only (no SSH: Session Manager)
    an IAM role for Session Manager and for reading site bundles; a private S3 bucket for the bundles

Each `up` builds the site (website/build.py), uploads it and switches the server to it in one step; the last five
versions are kept on the server.
"""
import argparse
import io
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent
sys.path.insert(0, str(INFRA / "test-server"))
sys.path.insert(0, str(HERE))
import aws_test_server as base  # noqa: E402
from aws_test_server import step  # noqa: E402
import build as site  # noqa: E402


def cmd():
    return f"python {sys.argv[0]}"


class WebsiteServer(base.Server):
    def bundle(self):
        """Build the site with this run's options and upload it (dist/ + nginx.conf + deploy.sh)."""
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            pages = site.build(self.a.brand, self.a.demo_number or "", self.a.hubspot_portal or "",
                               {"demo": self.a.hubspot_form_demo, "partner": self.a.hubspot_form_partner},
                               self.a.contact_email or "", dist=dist)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(dist, arcname="dist")
                t.add(HERE / "nginx.conf", arcname="nginx.conf")
                t.add(HERE / "deploy.sh", arcname="deploy.sh")
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-site.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        step(f"site built ({len(pages)} pages, brand {self.a.brand}, demo number "
             f"{self.a.demo_number or 'not set'}) and uploaded ({len(buf.getvalue()) // 1024} KB)")
        return f"s3://{self.bucket}/{key}"

    def user_data(self, bundle, setup_code):
        # Only preparation: `up` deploys the site through Session Manager once the server is running.
        return "#!/bin/bash\ndnf install -y -q nginx || true\n"

    def up(self):
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
        step(f"deploying the site on {iid} (IP {ip})")
        status, out = self.run(iid, [
            "set -e; rm -rf /tmp/wd; mkdir -p /tmp/wd",
            "for i in $(seq 1 60); do command -v aws >/dev/null && break; sleep 5; done",
            f"aws s3 cp --region {self.a.region} --only-show-errors {bundle} /tmp/wd/b.tar.gz",
            "tar -xzf /tmp/wd/b.tar.gz -C /tmp/wd deploy.sh",
            f"bash /tmp/wd/deploy.sh {bundle} {self.a.region} 2>&1 | tail -15"])
        print()
        print(out.strip())
        if status != "Success":
            print(f"\n(command status: {status})")
        self.report(ip)

    def report(self, ip):
        ok = self.http_check(ip)
        print(f"""
Website: http://{ip}/   {ok}
Update after changing pages or options:  {cmd()} up [--demo-number ...]
A domain later: point an A record at {ip}, then add HTTPS (ask Claude: certbot or Caddy on this server).""")

    @staticmethod
    def http_check(ip):
        try:
            with urllib.request.urlopen(f"http://{ip}/", timeout=10) as r:
                return f"(HTTP {r.status}, {len(r.read()) // 1024} KB)"
        except Exception as e:
            return f"(not answering yet: {e})"

    def status(self):
        inst = self.instance()
        if not inst:
            return print(f"No website server yet: {cmd()} up")
        addr = self.eip()
        ip = addr["PublicIp"] if addr else inst.get("PublicIpAddress")
        print(f"Server {inst['InstanceId']}: {inst['State']['Name']}   IP {ip}   region {self.a.region}")
        if ip and inst["State"]["Name"] == "running":
            print(f"Website: http://{ip}/   {self.http_check(ip)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "delete"])
    ap.add_argument("--brand", default="Telcom-Company")
    ap.add_argument("--demo-number", help="up: the phone number that reaches the AI demo")
    ap.add_argument("--hubspot-portal", help="up: HubSpot portal ID for the forms")
    ap.add_argument("--hubspot-form-demo", help="up: HubSpot form GUID for 'Book a demo'")
    ap.add_argument("--hubspot-form-partner", help="up: HubSpot form GUID for partner registration")
    ap.add_argument("--contact-email", help="up: forms open an email to this address when HubSpot isn't set")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="telqon-website")
    ap.add_argument("--instance-type", default="t4g.nano")
    a = ap.parse_args()
    s = WebsiteServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
