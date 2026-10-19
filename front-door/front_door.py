#!/usr/bin/env python3
"""Telqon's front door: https://bridge.telqon.com for every customer, on S3 + CloudFront (no server to go down).

Salesforce loads the phone from the address in Telqon's provider package (https://bridge.telqon.com/). The front
door serves that phone page (the app's connector build) and a directory: one small file per customer,
directory/<Salesforce org ID, 15 characters>.json -> {"bridge": "https://acme.bridge.telqon.com"}. The phone looks
its org up and then talks only to that customer's bridge.

    python front-door/front_door.py up                       create or update (certificate, bucket, CloudFront)
    python front-door/front_door.py deploy [--build]         publish the phone page from the app checkout
    python front-door/front_door.py add --org 00D... --bridge https://acme.bridge.telqon.com --name "Acme Ltd"
    python front-door/front_door.py remove --org 00D...
    python front-door/front_door.py list                     customers in the directory
    python front-door/front_door.py status                   certificate, CloudFront, DNS, HTTPS checks
    python front-door/front_door.py delete                   remove it all

The app comes from a checkout of sdixit17/sf-broadworks-bridge: --app-dir (default ../sf-broadworks-bridge, next
to this repository). Run `up` twice the first time: it asks for the certificate's DNS record in Wix, then (once
issued) creates CloudFront and tells you the CNAME for bridge.telqon.com.
"""
import argparse
import json
import mimetypes
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("pip install boto3 first")

INFRA = Path(__file__).resolve().parent.parent
NAME = "telqon-front-door"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"          # AWS managed cache policy
PHONE_FILES = ["index.html", "login.html", "main.js", "login.js"]   # the phone; admin/supervisor pages stay on bridges
ORG_RE = re.compile(r"00D[0-9A-Za-z]{12}([0-9A-Za-z]{3})?")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
BRIDGE_RE = re.compile(r"https://[a-z0-9.-]+(:\d+)?")


def step(msg):
    print(f"• {msg}", flush=True)


def hint(e):
    s = str(e)
    if "explicit deny" in s and "service control policy" in s:
        return (f"{s}\n\nTelqon's AWS Organization blocks this region or service (SCP "
                "AdvancedModeRegionRestrictionSecurityControlPolicy). CloudFront certificates must live in us-east-1: "
                "allow acm:* in us-east-1 (and cloudfront:*, a global service) in that policy, then run this again.")
    if "must be verified before you can add new CloudFront resources" in s:
        return (f"{s}\n\nAWS blocks CloudFront on new accounts until AWS Support verifies them (free). Open a case:\n"
                "  AWS console > Support > Create case > Account and billing > Service: Account, Category: Other\n"
                "  Subject: Please verify account for CloudFront\n"
                "  Text: paste the error above and add: 'We serve a small static web page (a Salesforce telephony\n"
                "        phone page) for our customers on bridge.telqon.com; expected traffic is low.'\n"
                "It usually takes 1-2 working days. Nothing has changed for your agents; run `up` again once AWS replies.")
    return s


class FrontDoor:
    def __init__(self, a):
        self.a = a
        s = boto3.session.Session(region_name=a.region)
        self.s3 = s.client("s3")
        self.cf = s.client("cloudfront")
        self.acm = boto3.session.Session(region_name="us-east-1").client("acm")   # CloudFront needs us-east-1
        self.account = s.client("sts").get_caller_identity()["Account"]
        self.bucket = f"{NAME}-{self.account}"

    # ------------------------------------------------------------ lookups
    def cert(self):
        for status in (["ISSUED"], ["PENDING_VALIDATION"]):
            for page in self.acm.get_paginator("list_certificates").paginate(CertificateStatuses=status):
                for c in page["CertificateSummaryList"]:
                    if c["DomainName"] == self.a.domain:
                        return self.acm.describe_certificate(CertificateArn=c["CertificateArn"])["Certificate"]
        return None

    def distribution(self):
        for page in self.cf.get_paginator("list_distributions").paginate():
            for d in (page.get("DistributionList") or {}).get("Items") or []:
                if d.get("Comment") == NAME:
                    return d
        return None

    def oac(self):
        for o in (self.cf.list_origin_access_controls().get("OriginAccessControlList") or {}).get("Items") or []:
            if o["Name"] == NAME:
                return o["Id"]
        return self.cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": NAME, "Description": "Telqon front door reads its S3 bucket", "SigningProtocol": "sigv4",
            "SigningBehavior": "always", "OriginAccessControlOriginType": "s3"})["OriginAccessControl"]["Id"]

    def headers_policy(self):
        for p in (self.cf.list_response_headers_policies(Type="custom").get("ResponseHeadersPolicyList") or {}).get("Items") or []:
            if p["ResponseHeadersPolicy"]["ResponseHeadersPolicyConfig"]["Name"] == NAME:
                return p["ResponseHeadersPolicy"]["Id"]
        # No X-Frame-Options: Salesforce shows the phone in a frame.
        return self.cf.create_response_headers_policy(ResponseHeadersPolicyConfig={
            "Name": NAME, "Comment": "Telqon front door security headers",
            "SecurityHeadersConfig": {
                "StrictTransportSecurity": {"Override": True, "AccessControlMaxAgeSec": 31536000,
                                            "IncludeSubdomains": False, "Preload": False},
                "ContentTypeOptions": {"Override": True},
                "ReferrerPolicy": {"Override": True, "ReferrerPolicy": "strict-origin-when-cross-origin"}}
        })["ResponseHeadersPolicy"]["Id"]

    # ------------------------------------------------------------ up
    def up(self):
        step(f"account {self.account}, front door {self.a.domain}")
        self.bucket_setup()
        cert = self.cert_setup()
        if cert["Status"] != "ISSUED":
            return
        d = self.distribution()
        if d is None:
            d = self.create_distribution(cert["CertificateArn"])
        else:
            step(f"CloudFront {d['Id']} exists ({d['DomainName']})")
        self.bucket_policy(d["ARN"])
        if not self.has_phone():
            print("\nNext: publish the phone page:  python front-door/front_door.py deploy --build")
        self.dns_report(d)

    def bucket_setup(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
            step(f"bucket {self.bucket} exists")
            return
        except ClientError:
            pass
        try:
            args = {"Bucket": self.bucket}
            if self.a.region != "us-east-1":
                args["CreateBucketConfiguration"] = {"LocationConstraint": self.a.region}
            self.s3.create_bucket(**args)
        except ClientError as e:
            raise SystemExit(hint(e))
        self.s3.put_public_access_block(Bucket=self.bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        self.s3.put_bucket_encryption(Bucket=self.bucket, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        self.s3.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"})
        self.s3.put_bucket_tagging(Bucket=self.bucket, Tagging={"TagSet": [{"Key": "app", "Value": "telqon-bridge"}]})
        step(f"bucket {self.bucket} created (private, encrypted, versioned: earlier directory entries can be restored)")

    def cert_setup(self):
        try:
            cert = self.cert()
            if cert is None:
                method = "EMAIL" if self.a.email_validation else "DNS"
                arn = self.acm.request_certificate(DomainName=self.a.domain, ValidationMethod=method,
                                                   Tags=[{"Key": "app", "Value": "telqon-bridge"}])["CertificateArn"]
                step(f"certificate requested for {self.a.domain} ({method} validation)")
                for _ in range(30):                  # the DNS record appears after a few seconds
                    cert = self.acm.describe_certificate(CertificateArn=arn)["Certificate"]
                    opts = cert.get("DomainValidationOptions") or [{}]
                    if method == "EMAIL" or opts[0].get("ResourceRecord"):
                        break
                    time.sleep(2)
        except ClientError as e:
            raise SystemExit(hint(e))
        if cert["Status"] == "ISSUED":
            step(f"certificate for {self.a.domain} issued")
            return cert
        opt = (cert.get("DomainValidationOptions") or [{}])[0]
        rr = opt.get("ResourceRecord")
        print(f"\nThe certificate for {self.a.domain} is waiting for validation.")
        if rr:
            host = rr["Name"].rstrip(".")
            zone = ".".join(self.a.domain.split(".")[-2:])
            short = host[: -len(zone) - 1] if host.endswith("." + zone) else host
            print(f"In Wix: Domains > {zone} > ⋯ > Manage DNS records > CNAME > Add record:\n"
                  f"    Host:   {short}\n    Value:  {rr['Value'].rstrip('.')}\n"
                  "then Save. Keep this record: it renews the certificate every year.\n"
                  "Wix accepts it within minutes; AWS checks every few minutes. Then run `up` again.\n"
                  "If Wix refuses the host name (it starts with an underscore), delete the certificate in the AWS console\n"
                  "(Certificate Manager, region us-east-1) and run `up --email-validation` instead: AWS then emails\n"
                  f"admin@/hostmaster@/webmaster@{zone} a link to approve.")
        else:
            print(f"AWS emailed the approval link to admin@ / hostmaster@ / webmaster@ "
                  f"{'.'.join(self.a.domain.split('.')[-2:])}. Approve it, then run `up` again.")
        if self.a.wait:
            step("waiting for the certificate (up to 60 minutes)")
            self.acm.get_waiter("certificate_validated").wait(CertificateArn=cert["CertificateArn"],
                                                              WaiterConfig={"Delay": 30, "MaxAttempts": 120})
            return self.cert()
        return cert

    def create_distribution(self, cert_arn):
        origin = f"{self.bucket}.s3.{self.a.region}.amazonaws.com"
        cfg = {
            "CallerReference": f"{NAME}-{int(time.time())}", "Comment": NAME, "Enabled": True,
            "Aliases": {"Quantity": 1, "Items": [self.a.domain]},
            "DefaultRootObject": "index.html",
            "Origins": {"Quantity": 1, "Items": [{
                "Id": "phone", "DomainName": origin, "OriginAccessControlId": self.oac(),
                "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {
                "TargetOriginId": "phone", "ViewerProtocolPolicy": "redirect-to-https", "Compress": True,
                "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                                   "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
                "CachePolicyId": CACHING_OPTIMIZED, "ResponseHeadersPolicyId": self.headers_policy()},
            "ViewerCertificate": {"ACMCertificateArn": cert_arn, "SSLSupportMethod": "sni-only",
                                  "MinimumProtocolVersion": "TLSv1.2_2021"},
            "PriceClass": "PriceClass_200", "HttpVersion": "http2and3", "IsIPV6Enabled": True,
        }
        try:
            d = self.cf.create_distribution(DistributionConfig=cfg)["Distribution"]
        except ClientError as e:
            raise SystemExit(hint(e))
        step(f"CloudFront {d['Id']} created ({d['DomainName']}); it takes 5-15 minutes to reach every location")
        return {"Id": d["Id"], "ARN": d["ARN"], "DomainName": d["DomainName"], "Status": d["Status"]}

    def bucket_policy(self, dist_arn):
        self.s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Sid": "CloudFrontReads", "Effect": "Allow",
                           "Principal": {"Service": "cloudfront.amazonaws.com"},
                           "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{self.bucket}/*",
                           "Condition": {"StringEquals": {"AWS:SourceArn": dist_arn}}}]}))

    def has_phone(self):
        try:
            self.s3.head_object(Bucket=self.bucket, Key="index.html")
            return True
        except ClientError:
            return False

    def dns_report(self, d):
        zone = ".".join(self.a.domain.split(".")[-2:])
        short = self.a.domain[: -len(zone) - 1]
        try:
            current = socket.gethostbyname_ex(self.a.domain)
        except OSError:
            current = None
        points_here = bool(current) and (d["DomainName"] in current[1] or d["DomainName"] == current[0])
        if points_here:
            print(f"\nDNS: {self.a.domain} -> {d['DomainName']} ✓")
        else:
            print(f"\nDNS: {self.a.domain} doesn't point at CloudFront yet. When the phone page is published and the\n"
                  f"customer bridges are ready, in Wix: Domains > {zone} > ⋯ > Manage DNS records:\n"
                  f"    1. delete the A record with host \"{short}\"\n"
                  f"    2. add a CNAME record: Host \"{short}\", Value {d['DomainName']}\n"
                  f"Before switching, test: https://{d['DomainName']}/ (blank page = fine) and\n"
                  f"https://{d['DomainName']}/directory/<org ID>.json")

    # ------------------------------------------------------------ phone page
    def deploy(self):
        app = Path(self.a.app_dir).expanduser().resolve()
        conn = app / "connector"
        if not (conn / "package.json").is_file():
            raise SystemExit(f"{app} is not a checkout of sf-broadworks-bridge; pass --app-dir")
        if self.a.build:
            step("building the connector (npm ci && npm run build)")
            for cmd in (["npm", "ci"], ["npm", "run", "build"]):
                if subprocess.run(cmd, cwd=conn).returncode:
                    raise SystemExit(f"{' '.join(cmd)} failed in {conn}")
        dist = conn / "dist"
        missing = [f for f in PHONE_FILES if not (dist / f).is_file()]
        if missing:
            raise SystemExit(f"{dist} lacks {', '.join(missing)}: run with --build (needs Node.js)")
        if "BRIDGE_DIRECTORY" not in (dist / "main.js").read_text(errors="ignore"):
            raise SystemExit("this connector build has no directory lookup (app older than v0.12.0): git pull, then --build")
        d = self.distribution()
        if not d:
            raise SystemExit("no front door yet: run `up` first")
        version = (app / "VERSION").read_text().strip() if (app / "VERSION").is_file() else "?"
        for f in PHONE_FILES + [p.name for p in dist.glob("*.LICENSE.txt")]:
            self.put(f, (dist / f).read_bytes())
        self.put("config.js", b"/* Telqon front door: the phone looks up each customer's bridge in /directory/. */\n"
                              b"window.BRIDGE_URL = '';\nwindow.BRIDGE_DIRECTORY = '/directory/';\n")
        self.put("version.json", json.dumps({"app": version}).encode())
        self.invalidate(["/*"])
        step(f"phone page v{version} published to {self.a.domain} (live everywhere within a few minutes)")

    def put(self, key, body, cache="no-cache", meta=None):
        ext = Path(key).suffix
        ctype = TYPES.get(ext) or mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=ctype, CacheControl=cache,
                           Metadata=meta or {})

    def invalidate(self, paths):
        d = self.distribution()
        if d:
            self.cf.create_invalidation(DistributionId=d["Id"], InvalidationBatch={
                "Paths": {"Quantity": len(paths), "Items": paths}, "CallerReference": str(time.time())})

    # ------------------------------------------------------------ directory
    @staticmethod
    def org_key(org):
        org = (org or "").strip()
        if not ORG_RE.fullmatch(org):
            raise SystemExit(f"'{org}' is not a Salesforce org ID (00D followed by 12 or 15 letters/digits)")
        return org[:15]

    def add(self):
        org = self.org_key(self.a.org)
        bridge = (self.a.bridge or "").strip().rstrip("/")
        if not BRIDGE_RE.fullmatch(bridge):
            raise SystemExit("--bridge must be the customer's bridge address, e.g. https://acme.bridge.telqon.com")
        check = self.https_check(f"{bridge}/healthz")
        if not check.startswith("OK"):
            print(f"warning: {bridge} doesn't answer yet ({check}); adding anyway")
        self.put(f"directory/{org}.json", json.dumps({"bridge": bridge}).encode(), cache="max-age=60",
                 meta={"customer": (self.a.name or "")[:200].encode("ascii", "replace").decode()})
        self.invalidate([f"/directory/{org}.json"])
        step(f"org {org} -> {bridge}{' (' + self.a.name + ')' if self.a.name else ''}; phones pick it up within a minute")
        print(f"On {bridge}/admin.html check: Salesforce > Org ID = {org}, and Bridge > Front door = https://{self.a.domain}")

    def remove(self):
        org = self.org_key(self.a.org)
        self.s3.delete_object(Bucket=self.bucket, Key=f"directory/{org}.json")
        self.invalidate([f"/directory/{org}.json"])
        step(f"org {org} removed from the directory (its phones show 'not set up' on the next load)")

    def entries(self):
        out = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="directory/"):
            for o in page.get("Contents") or []:
                h = self.s3.get_object(Bucket=self.bucket, Key=o["Key"])
                body = json.loads(h["Body"].read() or b"{}")
                out.append((o["Key"][len("directory/"):-5], body.get("bridge", "?"), h["Metadata"].get("customer", ""),
                            o["LastModified"]))
        return out

    def list(self):
        rows = self.entries()
        if not rows:
            return print("No customers in the directory yet (add one with: add --org ... --bridge ...)")
        print(f"{'Org ID':16} {'Bridge':40} {'Customer':24} Added/changed")
        for org, bridge, name, when in sorted(rows, key=lambda r: r[2].lower()):
            print(f"{org:16} {bridge:40} {name:24} {when:%Y-%m-%d %H:%M}")

    # ------------------------------------------------------------ status / delete
    def status(self):
        cert = self.cert()
        print(f"Certificate {self.a.domain}: {cert['Status'] if cert else 'none (run up)'}")
        d = self.distribution()
        if not d:
            return print("CloudFront: none (run up)")
        print(f"CloudFront {d['Id']}: {d['Status']}, {d['DomainName']}")
        try:
            v = json.loads(self.s3.get_object(Bucket=self.bucket, Key="version.json")["Body"].read())
            print(f"Phone page: app v{v.get('app')}")
        except ClientError:
            print("Phone page: not published (run deploy --build)")
        print(f"Directory: {len(self.entries())} customer(s)")
        self.dns_report(d)
        cf_host = d["DomainName"]
        print(f"HTTPS via CloudFront address: {self.https_check(f'https://{cf_host}/config.js')}")
        print(f"HTTPS via {self.a.domain}: {self.https_check(f'https://{self.a.domain}/config.js')}")

    @staticmethod
    def https_check(url):
        import ssl
        import urllib.request
        from botocore.httpsession import get_cert_path
        ctx = ssl.create_default_context(cafile=get_cert_path(True))    # python.org's macOS Python has no CAs
        try:
            with urllib.request.urlopen(url, timeout=10, context=ctx) as r:
                return f"OK ({r.status})"
        except Exception as e:
            return f"not yet ({e})"

    def delete(self):
        d = self.distribution()
        if d:
            full = self.cf.get_distribution_config(Id=d["Id"])
            cfg, etag = full["DistributionConfig"], full["ETag"]
            if cfg["Enabled"]:
                cfg["Enabled"] = False
                etag = self.cf.update_distribution(Id=d["Id"], DistributionConfig=cfg, IfMatch=etag)["ETag"]
                step("CloudFront disabled; waiting until that has spread (5-15 minutes)")
            self.cf.get_waiter("distribution_deployed").wait(Id=d["Id"])
            self.cf.delete_distribution(Id=d["Id"], IfMatch=self.cf.get_distribution(Id=d["Id"])["ETag"])
            step("CloudFront deleted")
        try:
            b = boto3.resource("s3", region_name=self.a.region).Bucket(self.bucket)
            b.object_versions.delete()
            b.delete()
            step("bucket deleted (phone page and directory)")
        except ClientError:
            pass
        cert = self.cert()
        if cert:
            for _ in range(20):
                try:
                    self.acm.delete_certificate(CertificateArn=cert["CertificateArn"])
                    step("certificate deleted (remove its CNAME in Wix)")
                    break
                except ClientError:
                    time.sleep(15)                         # still attached to CloudFront for a moment
        print(f"Point {self.a.domain} somewhere else in Wix, or Salesforce phones stop loading.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "deploy", "add", "remove", "list", "status", "delete"])
    ap.add_argument("--domain", default="bridge.telqon.com", help="the address in Telqon's provider package")
    ap.add_argument("--region", default="eu-west-2", help="where the bucket lives")
    ap.add_argument("--app-dir", default=str(INFRA.parent / "sf-broadworks-bridge"),
                    help="checkout of sdixit17/sf-broadworks-bridge (default: next to this repository)")
    ap.add_argument("--build", action="store_true", help="deploy: build the connector first (npm ci && npm run build)")
    ap.add_argument("--org", help="add/remove: the customer's Salesforce org ID (15 or 18 characters)")
    ap.add_argument("--bridge", help="add: the customer's bridge, e.g. https://acme.bridge.telqon.com")
    ap.add_argument("--name", help="add: customer name (only shown by list)")
    ap.add_argument("--wait", action="store_true", help="up: wait for the certificate instead of stopping")
    ap.add_argument("--email-validation", action="store_true", help="up: validate the certificate by email, not DNS")
    a = ap.parse_args()
    f = FrontDoor(a)
    getattr(f, a.command)()


if __name__ == "__main__":
    main()
