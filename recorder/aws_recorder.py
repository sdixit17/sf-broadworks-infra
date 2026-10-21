#!/usr/bin/env python3
"""Telqon recorder on AWS: the SIPREC recording server BroadWorks Call Recording sends calls to.

    python recorder/aws_recorder.py up --allow 1.2.3.4/32,5.6.7.0/28   create it, or deploy the current code
    python recorder/aws_recorder.py status                              server, IP, allowed sources, service state
    python recorder/aws_recorder.py logs [--lines 200]                  the recorder's log (SIP details of each call)
    python recorder/aws_recorder.py list [--days 2]                     recent recordings
    python recorder/aws_recorder.py fetch <call>                        download one recording (WAV + details)
    python recorder/aws_recorder.py delete [--purge]                    remove it (--purge: also the recordings)

What `up` creates in --region (default eu-west-2), named <name> (default telqon-recorder):
    one EC2 server (t4g.small, Amazon Linux 2023) with a fixed public IP (Elastic IP)
    a security group open ONLY to --allow (the BroadWorks media servers): SIP 5060 UDP+TCP, RTP 30000-30999 UDP
    a private, encrypted S3 bucket <name>-recordings-<account>; recordings are deleted after --keep-days (30)
    an IAM role (Session Manager, read code bundles, write recordings)

The recorder code comes from the app checkout (sf-broadworks-bridge/recorder/, --app-dir); this folder adds how it
runs (systemd service, deploy.sh). Audio arrives unencrypted (RTP), so keep --allow tight; a VPN comes later.
"""
import argparse
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent
sys.path.insert(0, str(INFRA / "test-server"))
import aws_test_server as base  # noqa: E402
from aws_test_server import ClientError, step  # noqa: E402

SIP_PORT, RTP_LO, RTP_HI = 5060, 30000, 30999


class RecorderServer(base.Server):
    def __init__(self, a):
        super().__init__(a)
        self.rec_bucket = f"{a.name}-recordings-{self.account}"

    # ------------------------------------------------------------ pieces
    def bundle(self):
        app = Path(self.a.app_dir).expanduser().resolve()
        if not (app / "recorder" / "server.py").is_file():
            raise SystemExit(f"{app} has no recorder/ (sf-broadworks-bridge v0.13.0 or later); pass --app-dir")
        files = [f for f in self.git(app, "ls-files", "recorder").split() if f.endswith(".py")]
        dirty = self.git(app, "status", "--porcelain", "--untracked-files=no", "recorder").strip()
        rev = self.git(app, "rev-parse", "--short", "HEAD").strip()
        version = (app / "VERSION").read_text().strip() if (app / "VERSION").is_file() else "?"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for f in files:
                t.add(app / f, arcname=f)
            t.add(app / "VERSION", arcname="VERSION")
            for p in sorted(HERE.iterdir()):
                if p.is_file() and p.suffix in (".sh", ".service"):
                    t.add(p, arcname=f"deploy/{p.name}")
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-recorder-v{version}-{rev}{'-dirty' if dirty else ''}.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        step(f"recorder from app v{version} ({rev}{', with local changes' if dirty else ''}) uploaded")
        return f"s3://{self.bucket}/{key}"

    def recordings_bucket(self):
        try:
            self.s3.head_bucket(Bucket=self.rec_bucket)
        except ClientError:
            self.s3.create_bucket(Bucket=self.rec_bucket,
                                  CreateBucketConfiguration={"LocationConstraint": self.a.region})
            self.s3.put_public_access_block(Bucket=self.rec_bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            self.s3.put_bucket_encryption(Bucket=self.rec_bucket, ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
            self.s3.put_bucket_tagging(Bucket=self.rec_bucket, Tagging={"TagSet": [
                {"Key": "app", "Value": "telqon-bridge"}, {"Key": "data", "Value": "call-recordings"}]})
            step(f"recordings bucket {self.rec_bucket} created (private, encrypted)")
        self.s3.put_bucket_lifecycle_configuration(Bucket=self.rec_bucket, LifecycleConfiguration={"Rules": [{
            "ID": "recordings-expire", "Status": "Enabled", "Filter": {"Prefix": "recordings/"},
            "Expiration": {"Days": self.a.keep_days}}]})

    def role_setup(self):
        super().role_setup()
        self.iam.put_role_policy(RoleName=self.role, PolicyName="recordings", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:PutObject",
                                                    "Resource": f"arn:aws:s3:::{self.rec_bucket}/recordings/*"}]}))

    def group(self):
        vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            self.ec2.create_default_vpc()
            vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        vpc = vpcs[0]["VpcId"]
        g = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [self.name]},
                                                       {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
        return (g[0] if g else None), vpc

    def allowed_now(self, g):
        cidrs = []
        for p in (g or {}).get("IpPermissions", []):
            for r in p.get("IpRanges", []):
                if r["CidrIp"] not in cidrs:
                    cidrs.append(r["CidrIp"])
        return cidrs

    def network(self):
        """Security group for SIP + RTP from the allowed sources only; re-synced on every `up --allow`."""
        g, vpc = self.group()
        if g is None:
            gid = self.ec2.create_security_group(
                GroupName=self.name, VpcId=vpc, Description="Telqon recorder: SIP+RTP from BroadWorks only",
                TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tag()}])["GroupId"]
            g = {"GroupId": gid, "IpPermissions": []}
            step("security group created")
        allow = self.allow_list()
        if allow and sorted(allow) != sorted(self.allowed_now(g)):
            if g["IpPermissions"]:
                self.ec2.revoke_security_group_ingress(GroupId=g["GroupId"], IpPermissions=g["IpPermissions"])
            ranges = [{"CidrIp": c, "Description": "BroadWorks media server"} for c in allow]
            self.ec2.authorize_security_group_ingress(GroupId=g["GroupId"], IpPermissions=[
                {"IpProtocol": "udp", "FromPort": SIP_PORT, "ToPort": SIP_PORT, "IpRanges": ranges},
                {"IpProtocol": "tcp", "FromPort": SIP_PORT, "ToPort": SIP_PORT, "IpRanges": ranges},
                {"IpProtocol": "udp", "FromPort": RTP_LO, "ToPort": RTP_HI, "IpRanges": ranges}])
            step(f"open to {', '.join(allow)} only: SIP {SIP_PORT} UDP/TCP, RTP {RTP_LO}-{RTP_HI} UDP")
        return g["GroupId"]

    def allow_list(self):
        out = []
        for c in (self.a.allow or "").split(","):
            c = c.strip()
            if not c:
                continue
            if "/" not in c:
                c += "/32"
            if c in ("0.0.0.0/0",) and not self.a.allow_any:
                raise SystemExit("--allow 0.0.0.0/0 would let anyone send SIP/audio to the recorder; "
                                 "add --allow-any if you really mean it (testing only)")
            out.append(c)
        return out

    def user_data(self, bundle, setup_code):
        # Only preparation: the recorder needs its public IP (known after the Elastic IP is attached), so `up`
        # installs and starts it through Session Manager.
        return "#!/bin/bash\ndnf install -y -q python3-boto3 || true\n"

    # ------------------------------------------------------------ commands
    def up(self):
        step(f"account {self.account}, region {self.a.region}")
        g, _ = self.group()
        if not self.a.allow and not self.allowed_now(g):
            raise SystemExit("first run: pass --allow with the BroadWorks media server address(es), e.g. "
                             "--allow 185.5.188.10,185.5.188.11 (ask AVC which servers send call recordings)")
        self.bucket_setup()
        self.recordings_bucket()
        bundle = self.bundle()
        inst = self.instance()
        if inst is None:
            self.role_setup()
            iid, _ = self.launch(bundle)
        else:
            iid = inst["InstanceId"]
            self.role_setup()
            self.network()
            if inst["State"]["Name"] != "running":
                if inst["State"]["Name"] == "stopping":
                    self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
                self.ec2.start_instances(InstanceIds=[iid])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
        ip = self.ip_setup(iid)
        allow = ",".join(self.allowed_now(self.group()[0]))
        step(f"installing the recorder on {iid} (public IP {ip})")
        status, out = self.run(iid, [
            "set -e; rm -rf /tmp/rd; mkdir -p /tmp/rd",
            "for i in $(seq 1 60); do command -v aws >/dev/null && break; sleep 5; done",
            f"aws s3 cp --region {self.a.region} --only-show-errors {bundle} /tmp/rd/b.tar.gz",
            "tar -xzf /tmp/rd/b.tar.gz -C /tmp/rd deploy/deploy.sh",
            f"bash /tmp/rd/deploy/deploy.sh {bundle} {self.a.region} {ip} '{allow}' {self.rec_bucket} 2>&1 | tail -30"])
        print()
        print(out.strip())
        if status != "Success":
            print(f"\n(command status: {status})")
        self.report(iid, ip, allow)

    def report(self, iid, ip, allow):
        print(f"""
Recorder {iid}   IP {ip}   region {self.a.region}
Allowed sources: {allow or 'none yet'}

For AVC (BroadWorks Call Recording platform):
    address   {ip}   port {SIP_PORT}   transport UDP (TCP also works)
    media     dual stream (one per party), G.711 (PCMU or PCMA), no SRTP
    their media server(s) must send from: {allow or '(the --allow addresses)'}
    firewall on their side: SIP {SIP_PORT} and RTP UDP {RTP_LO}-{RTP_HI} to {ip}
Recordings: s3://{self.rec_bucket}/recordings/<day>/<time>-<call>/  (call.wav, call.json, sip.txt, metadata*.xml)
            kept {self.a.keep_days} days.  python recorder/aws_recorder.py list""")

    def status(self):
        inst = self.instance()
        if not inst:
            return print("No recorder yet: python recorder/aws_recorder.py up --allow <BroadWorks media server IPs>")
        addr = self.eip()
        ip = addr["PublicIp"] if addr else inst.get("PublicIpAddress")
        print(f"Server {inst['InstanceId']}: {inst['State']['Name']}")
        if inst["State"]["Name"] == "running":
            _, out = self.run(inst["InstanceId"], ["systemctl is-active telqon-recorder || true"], quiet=True)
            print(f"Recorder service: {out.strip()}")
        self.report(inst["InstanceId"], ip, ",".join(self.allowed_now(self.group()[0])))

    def logs(self):
        iid = self.need()
        _, out = self.run(iid, [f"journalctl -u telqon-recorder -n {self.a.lines} --no-pager -o short-iso"], quiet=True)
        print(out)

    def recordings(self, days):
        out = []
        for d in range(days):
            day = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
            for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.rec_bucket,
                                                                          Prefix=f"recordings/{day}/"):
                for o in page.get("Contents") or []:
                    if o["Key"].endswith("/call.json"):
                        out.append(o["Key"].rsplit("/", 1)[0])
        return sorted(out, reverse=True)

    def list(self):
        rows = self.recordings(self.a.days)
        if not rows:
            return print(f"No recordings in the last {self.a.days} day(s).")
        for prefix in rows[:100]:
            info = json.loads(self.s3.get_object(Bucket=self.rec_bucket, Key=f"{prefix}/call.json")["Body"].read())
            who = " / ".join(ch.get("aor") or f"stream {ch.get('label') or ch.get('channel')}" for ch in info.get("channels", []))
            print(f"{prefix.split('/', 2)[2]:58} {info.get('seconds', 0):7.1f}s  {who}  [{info.get('endReason')}]")
        print("\nDownload one: python recorder/aws_recorder.py fetch <name or part of it>")

    def fetch(self):
        if not self.a.call:
            raise SystemExit("which call? python recorder/aws_recorder.py fetch <name> (see list)")
        match = [p for p in self.recordings(max(self.a.days, 30)) if self.a.call in p]
        if not match:
            raise SystemExit(f"no recording matching '{self.a.call}' in the last 30 days")
        prefix = match[0]
        dest = Path("recordings") / prefix.rsplit("/", 1)[1]
        dest.mkdir(parents=True, exist_ok=True)
        for o in self.s3.list_objects_v2(Bucket=self.rec_bucket, Prefix=prefix + "/").get("Contents") or []:
            self.s3.download_file(self.rec_bucket, o["Key"], str(dest / o["Key"].rsplit("/", 1)[1]))
        step(f"saved to {dest}/ (call.wav: left channel = first stream, right = second; details in call.json)")

    def delete(self):
        try:
            self.iam.delete_role_policy(RoleName=self.role, PolicyName="recordings")
        except ClientError:
            pass
        super().delete()
        if self.a.purge:
            try:
                b = base.boto3.resource("s3", region_name=self.a.region).Bucket(self.rec_bucket)
                b.objects.all().delete()
                b.delete()
                step("recordings bucket deleted")
            except ClientError:
                pass
        else:
            print(f"Recordings kept in s3://{self.rec_bucket}/ (delete --purge removes them).")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "logs", "list", "fetch", "delete"])
    ap.add_argument("call", nargs="?", help="fetch: recording name (or part of it) from list")
    ap.add_argument("--allow", help="up: BroadWorks media server IPs/CIDRs allowed to send SIP and audio (comma-separated)")
    ap.add_argument("--allow-any", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--app-dir", default=str(INFRA.parent / "sf-broadworks-bridge"),
                    help="checkout of sdixit17/sf-broadworks-bridge (default: next to this repository)")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="telqon-recorder")
    ap.add_argument("--instance-type", default="t4g.small")
    ap.add_argument("--keep-days", type=int, default=30, help="delete recordings after this many days")
    ap.add_argument("--lines", type=int, default=200)
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--purge", action="store_true", help="delete: also delete all recordings")
    a = ap.parse_args()
    s = RecorderServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
