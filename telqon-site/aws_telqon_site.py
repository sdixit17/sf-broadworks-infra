#!/usr/bin/env python3
"""telqon.com on AWS: one small server with a fixed public IP serving the static site with nginx.

    python telqon-site/aws_telqon_site.py up        create it, or deploy changed pages
    python telqon-site/aws_telqon_site.py status    server, IP, HTTP check
    python telqon-site/aws_telqon_site.py delete    remove everything it made

Options for `up` (they end up in config.js; pages are unchanged):
    --hubspot-portal, --hubspot-form-contact   the contact form then posts straight to HubSpot
    --contact-email                            without HubSpot the form opens an email to this address (default info@telqon.com)

Same set-up as the sample website (website/aws_website.py), under its own name (default telqon-site), so the two
never touch each other: one EC2 t4g.micro (Amazon Linux 2023, ARM, IMDSv2, encrypted disk) with nginx, an Elastic IP,
a security group open on 80/443 only (no SSH: Session Manager), an IAM role and a private bucket for site bundles.
Each `up` builds the site (telqon-site/build.py), uploads it and switches the server to it; the last five versions
are kept on the server. Plain HTTP on the IP until telqon.com points here; then add HTTPS on this server.
"""
import argparse
import importlib.util
import io
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent
sys.path.insert(0, str(INFRA / "website"))
import aws_website  # noqa: E402  (also puts test-server/ on the path)
from aws_test_server import step  # noqa: E402

_spec = importlib.util.spec_from_file_location("telqon_build", HERE / "build.py")
site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(site)


class TelqonSiteServer(aws_website.WebsiteServer):
    def bundle(self):
        """Build telqon.com and upload it (dist/ + nginx.conf + the website's deploy.sh, which is site-agnostic)."""
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            pages = site.build(self.a.hubspot_portal or "", self.a.hubspot_form_contact or "",
                               self.a.contact_email or "info@telqon.com", dist=dist)
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                t.add(dist, arcname="dist")
                t.add(HERE / "nginx.conf", arcname="nginx.conf")
                t.add(INFRA / "website" / "deploy.sh", arcname="deploy.sh")
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-site.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        form = "HubSpot" if self.a.hubspot_portal and self.a.hubspot_form_contact else \
            f"email to {self.a.contact_email or 'info@telqon.com'}"
        step(f"site built ({len(pages)} pages, contact form: {form}) and uploaded ({len(buf.getvalue()) // 1024} KB)")
        return f"s3://{self.bucket}/{key}"

    def report(self, ip):
        print(f"""
telqon.com (new site): http://{ip}/   {self.http_check(ip)}
Update after changing pages:  python {sys.argv[0]} up
To go live on telqon.com: in Wix DNS point the @ and www A records at {ip} (leave MX and the bridge record alone),
then add HTTPS on this server (ask Claude: Caddy or certbot).""")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "delete"])
    ap.add_argument("--hubspot-portal", help="up: HubSpot portal ID for the contact form")
    ap.add_argument("--hubspot-form-contact", help="up: HubSpot form GUID for the contact form")
    ap.add_argument("--contact-email", help="up: the form opens an email to this address when HubSpot isn't set")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="telqon-site")
    ap.add_argument("--instance-type", default="t4g.micro")
    a = ap.parse_args()
    s = TelqonSiteServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
