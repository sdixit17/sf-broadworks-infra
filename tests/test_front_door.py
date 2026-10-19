"""Offline checks for front-door/front_door.py: every AWS request is validated against botocore's API models,
and AWS is simulated in memory (S3 objects, one certificate, one CloudFront distribution).

    python tests/test_front_door.py
"""
import io
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import botocore.client
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "front-door"))
import front_door  # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeAws:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.cert_status = None
        self.dist = None
        self.bucket = False

    def err(self, code, op):
        return ClientError({"Error": {"Code": code, "Message": code}}, op)

    def __call__(self, client, op, params):
        model = client.meta.service_model.operation_model(op)
        report = ParamValidator().validate(params, model.input_shape)
        if report.has_errors():
            raise AssertionError(f"{op}: {report.generate_report()}")
        self.calls.append((op, params))
        svc = client.meta.service_model.service_name
        h = getattr(self, f"{svc}_{op}", None)
        return h(params) if h else {}

    # STS
    def sts_GetCallerIdentity(self, p): return {"Account": "123456789012"}

    # ACM
    def acm_ListCertificates(self, p):
        st = p["CertificateStatuses"][0]
        items = [{"CertificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/abc", "DomainName": "bridge.telqon.com"}] if self.cert_status == st else []
        return {"CertificateSummaryList": items}

    def acm_RequestCertificate(self, p):
        self.cert_status = "PENDING_VALIDATION"
        return {"CertificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/abc"}

    def acm_DescribeCertificate(self, p):
        return {"Certificate": {"CertificateArn": "arn:aws:acm:us-east-1:123456789012:certificate/abc", "Status": self.cert_status, "DomainValidationOptions": [
            {"DomainName": "bridge.telqon.com", "ResourceRecord": {
                "Name": "_abc123.bridge.telqon.com.", "Type": "CNAME", "Value": "_xyz.acm-validations.aws."}}]}}

    # S3
    def s3_HeadBucket(self, p):
        if not self.bucket:
            raise self.err("404", "HeadBucket")
        return {}

    def s3_CreateBucket(self, p):
        self.bucket = True
        return {}

    def s3_PutObject(self, p):
        body = p["Body"]
        self.objects[p["Key"]] = {"Body": body if isinstance(body, bytes) else body.read(),
                                  "Metadata": p.get("Metadata") or {}, "ContentType": p.get("ContentType"),
                                  "CacheControl": p.get("CacheControl")}
        return {}

    def s3_HeadObject(self, p):
        if p["Key"] not in self.objects:
            raise self.err("404", "HeadObject")
        return {}

    def s3_GetObject(self, p):
        if p["Key"] not in self.objects:
            raise self.err("NoSuchKey", "GetObject")
        o = self.objects[p["Key"]]
        return {"Body": io.BytesIO(o["Body"]), "Metadata": o["Metadata"]}

    def s3_DeleteObject(self, p):
        self.objects.pop(p["Key"], None)
        return {}

    def s3_ListObjectsV2(self, p):
        keys = [k for k in self.objects if k.startswith(p.get("Prefix", ""))]
        return {"Contents": [{"Key": k, "LastModified": datetime(2026, 10, 19, tzinfo=timezone.utc)} for k in keys]}

    # CloudFront
    def cloudfront_ListDistributions(self, p):
        return {"DistributionList": {"Items": [self.dist] if self.dist else []}}

    def cloudfront_ListOriginAccessControls(self, p): return {"OriginAccessControlList": {"Items": []}}

    def cloudfront_CreateOriginAccessControl(self, p): return {"OriginAccessControl": {"Id": "OAC1"}}

    def cloudfront_ListResponseHeadersPolicies(self, p): return {"ResponseHeadersPolicyList": {"Items": []}}

    def cloudfront_CreateResponseHeadersPolicy(self, p): return {"ResponseHeadersPolicy": {"Id": "RHP1"}}

    def cloudfront_CreateDistribution(self, p):
        self.dist = {"Id": "E123", "ARN": "arn:aws:cloudfront::123456789012:distribution/E123",
                     "DomainName": "d111.cloudfront.net", "Status": "InProgress", "Comment": p["DistributionConfig"]["Comment"]}
        return {"Distribution": dict(self.dist)}


def run(aws, *argv):
    out = io.StringIO()
    with mock.patch.object(botocore.client.BaseClient, "_make_api_call",
                           lambda self, op, params: aws(self, op, params)), \
            mock.patch.object(sys, "argv", ["front_door.py", *argv]), \
            mock.patch.object(front_door.FrontDoor, "https_check", staticmethod(lambda url: "not yet (offline)")), \
            mock.patch("sys.stdout", out):
        try:
            front_door.main()
        except SystemExit as e:
            out.write(f"\n[exit] {e}")
    return out.getvalue()


def main():
    aws = FakeAws()
    app = Path(__import__("tempfile").mkdtemp())
    dist = app / "connector" / "dist"
    dist.mkdir(parents=True)
    (app / "connector" / "package.json").write_text("{}")
    (app / "VERSION").write_text("0.12.0\n")
    for f in front_door.PHONE_FILES:
        (dist / f).write_text("window.BRIDGE_DIRECTORY" if f == "main.js" else f)
    (dist / "admin.html").write_text("admin")

    print("up, first time: certificate requested, Wix record shown")
    out = run(aws, "up")
    check(any(op == "RequestCertificate" and p["ValidationMethod"] == "DNS" for op, p in aws.calls), "certificate requested (DNS)")
    check("Host:   _abc123.bridge" in out and "_xyz.acm-validations.aws" in out, "Wix CNAME for validation printed")
    check(not any(op == "CreateDistribution" for op, _ in aws.calls), "no CloudFront before the certificate is issued")
    check(any(op == "PutPublicAccessBlock" for op, _ in aws.calls), "bucket kept private")

    print("up again, certificate issued: CloudFront created")
    aws.cert_status = "ISSUED"
    out = run(aws, "up")
    cd = next(p for op, p in aws.calls if op == "CreateDistribution")["DistributionConfig"]
    check(cd["Aliases"]["Items"] == ["bridge.telqon.com"], "CloudFront answers for bridge.telqon.com")
    check(cd["ViewerCertificate"]["ACMCertificateArn"] == "arn:aws:acm:us-east-1:123456789012:certificate/abc", "uses the certificate")
    check(cd["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https", "HTTPS only")
    rh = next(p for op, p in aws.calls if op == "CreateResponseHeadersPolicy")["ResponseHeadersPolicyConfig"]
    check("FrameOptions" not in rh["SecurityHeadersConfig"], "no X-Frame-Options (Salesforce frames the phone)")
    pol = json.loads(next(p for op, p in aws.calls if op == "PutBucketPolicy")["Policy"])
    check(pol["Statement"][0]["Condition"]["StringEquals"]["AWS:SourceArn"].endswith("distribution/E123"),
          "bucket readable by this CloudFront only")
    check("CNAME record: Host \"bridge\", Value d111.cloudfront.net" in out, "Wix switch-over instructions printed")
    n = len([1 for op, _ in aws.calls if op == "CreateDistribution"])
    run(aws, "up")
    check(len([1 for op, _ in aws.calls if op == "CreateDistribution"]) == n, "up again changes nothing")

    print("deploy: phone page and front-door config")
    out = run(aws, "deploy", "--app-dir", str(app))
    check(all(f in aws.objects for f in front_door.PHONE_FILES), "phone files uploaded")
    check("admin.html" not in aws.objects, "Settings page not on the front door (it stays on each bridge)")
    cfg = aws.objects["config.js"]["Body"].decode()
    check("BRIDGE_DIRECTORY = '/directory/'" in cfg and "BRIDGE_URL = ''" in cfg, "config.js turns on the directory")
    check(aws.objects["main.js"]["ContentType"] == "text/javascript; charset=utf-8", "JavaScript content type")
    check(any(op == "CreateInvalidation" for op, _ in aws.calls), "CloudFront cache refreshed")
    (dist / "main.js").write_text("old build")
    out = run(aws, "deploy", "--app-dir", str(app))
    check("no directory lookup" in out, "refuses a connector build without the directory lookup")

    print("directory")
    out = run(aws, "add", "--org", "00DgK00000cA7yPUAS", "--bridge", "https://test.bridge.telqon.com/", "--name", "Telqon test")
    e = aws.objects.get("directory/00DgK00000cA7yP.json")
    check(e and json.loads(e["Body"]) == {"bridge": "https://test.bridge.telqon.com"}, "18-char org stored under 15 chars, bridge only")
    check(e and e["Metadata"]["customer"] == "Telqon test" and e["CacheControl"] == "max-age=60", "name kept private in metadata; 60 s cache")
    out = run(aws, "add", "--org", "12345", "--bridge", "https://x.bridge.telqon.com")
    check("not a Salesforce org ID" in out, "bad org ID refused")
    out = run(aws, "add", "--org", "00D5g000001ABCD", "--bridge", "http://acme.example")
    check("--bridge must be" in out, "non-https bridge refused")
    out = run(aws, "list")
    check("00DgK00000cA7yP" in out and "Telqon test" in out, "list shows customers")
    run(aws, "remove", "--org", "00DgK00000cA7yP")
    check("directory/00DgK00000cA7yP.json" not in aws.objects, "remove deletes the entry")

    print("status")
    out = run(aws, "status")
    check("CloudFront E123" in out and "app v0.12.0" in out, "status shows CloudFront and the published version")

    print(f"\n{'ALL FRONT DOOR SCRIPT CHECKS PASSED' if not fails else f'{len(fails)} CHECK(S) FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
