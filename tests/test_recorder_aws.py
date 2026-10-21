"""Offline checks for recorder/aws_recorder.py: AWS simulated in memory, every request validated against botocore's
API models.

    python tests/test_recorder_aws.py      (needs the app checkout next to this repository, for the bundle)
"""
import io
import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import botocore.client
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "recorder"))
import aws_recorder  # noqa: E402

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


class FakeAws:
    def __init__(self):
        self.calls, self.objects, self.buckets = [], {}, set()
        self.instance = None
        self.sg = None
        self.eip = None
        self.role = False

    def err(self, code, op):
        return ClientError({"Error": {"Code": code, "Message": code}}, op)

    def __call__(self, client, op, params):
        model = client.meta.service_model.operation_model(op)
        report = ParamValidator().validate(params, model.input_shape)
        if report.has_errors():
            raise AssertionError(f"{op}: {report.generate_report()}")
        self.calls.append((op, params))
        h = getattr(self, f"{client.meta.service_model.service_name}_{op}", None)
        return h(params) if h else {}

    def ops(self, name):
        return [p for op, p in self.calls if op == name]

    def sts_GetCallerIdentity(self, p): return {"Account": "123456789012"}

    # S3
    def s3_HeadBucket(self, p):
        if p["Bucket"] not in self.buckets:
            raise self.err("404", "HeadBucket")
        return {}

    def s3_CreateBucket(self, p):
        self.buckets.add(p["Bucket"])
        return {}

    def s3_PutObject(self, p):
        self.objects[(p["Bucket"], p["Key"])] = p["Body"]
        return {}

    def s3_ListObjectsV2(self, p):
        keys = [k for b, k in self.objects if b == p["Bucket"] and k.startswith(p.get("Prefix", ""))]
        return {"Contents": [{"Key": k, "LastModified": datetime.now(timezone.utc)} for k in keys]}

    def s3_GetObject(self, p):
        return {"Body": io.BytesIO(self.objects[(p["Bucket"], p["Key"])])}

    # IAM
    def iam_GetRole(self, p):
        if not self.role:
            raise self.err("NoSuchEntity", "GetRole")
        return {"Role": {"RoleName": p["RoleName"], "Arn": "arn:aws:iam::123456789012:role/x", "Path": "/",
                         "RoleId": "AROAXXXXXXXXXXXXXXXXX", "CreateDate": datetime.now(timezone.utc)}}

    def iam_CreateRole(self, p):
        self.role = True
        return {"Role": {"RoleName": p["RoleName"], "Arn": "arn:aws:iam::123456789012:role/x", "Path": "/",
                         "RoleId": "AROAXXXXXXXXXXXXXXXXX", "CreateDate": datetime.now(timezone.utc)}}

    def iam_GetInstanceProfile(self, p):
        raise self.err("NoSuchEntity", "GetInstanceProfile")

    # SSM
    def ssm_GetParameter(self, p): return {"Parameter": {"Value": "ami-0123456789abcdef0"}}

    def ssm_DescribeInstanceInformation(self, p): return {"InstanceInformationList": [{"InstanceId": "i-0123456789abcdef0"}]}

    def ssm_SendCommand(self, p): return {"Command": {"CommandId": "11111111-2222-3333-4444-555555555555"}}

    def ssm_GetCommandInvocation(self, p):
        return {"Status": "Success", "StandardOutputContent": "active\nrecorder listening on 0.0.0.0:5060"}

    # EC2
    def ec2_DescribeVpcs(self, p): return {"Vpcs": [{"VpcId": "vpc-1"}]}

    def ec2_DescribeSecurityGroups(self, p):
        return {"SecurityGroups": [self.sg] if self.sg else []}

    def ec2_CreateSecurityGroup(self, p):
        self.sg = {"GroupId": "sg-1", "IpPermissions": []}
        return {"GroupId": "sg-1"}

    def ec2_AuthorizeSecurityGroupIngress(self, p):
        self.sg["IpPermissions"] = p["IpPermissions"]
        return {}

    def ec2_RevokeSecurityGroupIngress(self, p):
        self.sg["IpPermissions"] = []
        return {}

    def ec2_DescribeInstances(self, p):
        return {"Reservations": [{"Instances": [self.instance]}] if self.instance else []}

    def ec2_RunInstances(self, p):
        self.instance = {"InstanceId": "i-0123456789abcdef0", "State": {"Name": "running"}, "InstanceType": p["InstanceType"]}
        self.user_data = p["UserData"]
        return {"Instances": [self.instance]}

    def ec2_DescribeAddresses(self, p):
        return {"Addresses": [self.eip] if self.eip else []}

    def ec2_AllocateAddress(self, p):
        self.eip = {"AllocationId": "eipalloc-1", "PublicIp": "18.130.1.2"}
        return dict(self.eip)

    def ec2_AssociateAddress(self, p):
        self.eip["InstanceId"] = p["InstanceId"]
        return {}


def run(aws, *argv):
    out = io.StringIO()
    with mock.patch.object(botocore.client.BaseClient, "_make_api_call", lambda self, op, params: aws(self, op, params)), \
            mock.patch.object(sys, "argv", ["aws_recorder.py", *argv]), \
            mock.patch("time.sleep", lambda s: None), mock.patch("sys.stdout", out):
        try:
            aws_recorder.main()
        except SystemExit as e:
            out.write(f"\n[exit] {e}")
    return out.getvalue()


def main():
    app = ROOT.parent / "sf-broadworks-bridge"
    if not (app / "recorder" / "server.py").is_file():
        print(f"skipped: no app checkout with recorder/ at {app}")
        return
    aws = FakeAws()

    print("first run needs --allow")
    out = run(aws, "up")
    check("pass --allow" in out and not aws.ops("RunInstances"), "refuses to start without the BroadWorks addresses")
    out = run(aws, "up", "--allow", "0.0.0.0/0")
    check("anyone" in out and not aws.ops("RunInstances"), "refuses 0.0.0.0/0")

    print("up --allow")
    out = run(aws, "up", "--allow", "185.5.188.10,185.5.188.0/28", "--app-dir", str(app))
    perms = aws.sg["IpPermissions"]
    rules = {(p["IpProtocol"], p["FromPort"], p["ToPort"]) for p in perms}
    check(rules == {("udp", 5060, 5060), ("tcp", 5060, 5060), ("udp", 30000, 30999)}, "only SIP 5060 and RTP 30000-30999")
    check(all({r["CidrIp"] for r in p["IpRanges"]} == {"185.5.188.10/32", "185.5.188.0/28"} for p in perms),
          "open only to the allowed sources (single IP becomes /32)")
    check("telqon-recorder-recordings-123456789012" in aws.buckets, "recordings bucket created")
    lc = aws.ops("PutBucketLifecycleConfiguration")[-1]["LifecycleConfiguration"]["Rules"][0]
    check(lc["Expiration"]["Days"] == 30, "recordings expire after 30 days")
    check(aws.ops("PutBucketEncryption") and aws.ops("PutPublicAccessBlock"), "bucket private and encrypted")
    pol = [json.loads(p["PolicyDocument"]) for p in aws.ops("PutRolePolicy") if p["PolicyName"] == "recordings"][0]
    check(pol["Statement"][0]["Action"] == "s3:PutObject" and pol["Statement"][0]["Resource"].endswith("/recordings/*"),
          "server may only write recordings")
    bundle = next(v for (b, k), v in aws.objects.items() if "/bundles/" in f"/{k}" and "recorder" in k)
    names = tarfile.open(fileobj=io.BytesIO(bundle)).getnames()
    check("recorder/server.py" in names and "deploy/deploy.sh" in names and "deploy/telqon-recorder.service" in names
          and not any(n.startswith("bridge/") for n in names), "bundle: recorder code + deploy files only")
    cmd = " ".join(aws.ops("SendCommand")[-1]["Parameters"]["commands"])
    check("18.130.1.2" in cmd and "185.5.188.10/32,185.5.188.0/28" in cmd and "telqon-recorder-recordings" in cmd,
          "installed with its public IP, allow list and bucket")
    check("address   18.130.1.2   port 5060" in out, "prints what AVC needs")

    print("up again keeps the rules; --allow changes them")
    n = len(aws.ops("AuthorizeSecurityGroupIngress"))
    run(aws, "up", "--app-dir", str(app))
    check(len(aws.ops("AuthorizeSecurityGroupIngress")) == n and len(aws.ops("RunInstances")) == 1, "no change without --allow")
    run(aws, "up", "--allow", "185.5.188.20", "--app-dir", str(app))
    check({r["CidrIp"] for p in aws.sg["IpPermissions"] for r in p["IpRanges"]} == {"185.5.188.20/32"},
          "new --allow replaces the old sources")

    print("list")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    aws.objects[("telqon-recorder-recordings-123456789012", f"recordings/{day}/101500-call-1_bw/call.json")] = json.dumps(
        {"seconds": 63.2, "endReason": "BYE", "channels": [{"aor": "sip:+441244298273@avcone.co.uk"}, {"label": "2"}]}).encode()
    out = run(aws, "list")
    check("101500-call-1_bw" in out and "63.2s" in out and "sip:+441244298273" in out, "recent recordings listed")

    print(f"\n{'ALL RECORDER SCRIPT CHECKS PASSED' if not fails else f'{len(fails)} CHECK(S) FAILED'}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
