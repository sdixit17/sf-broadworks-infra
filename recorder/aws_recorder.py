#!/usr/bin/env python3
"""Telqon recorder on AWS: the SIPREC recording server BroadWorks Call Recording sends calls to.

    python recorder/aws_recorder.py up --allow 1.2.3.4/32,5.6.7.0/28   create it, or deploy the current code
    python recorder/aws_recorder.py status                              server, IP, allowed sources, service state
    python recorder/aws_recorder.py logs [--lines 200]                  the recorder's log (SIP details of each call)
    python recorder/aws_recorder.py selftest                            a two-party test call made on the server itself
    python recorder/aws_recorder.py list [--days 2]                     recent recordings
    python recorder/aws_recorder.py fetch <call>                        download one recording (WAV + details)
    python recorder/aws_recorder.py delete [--purge]                    remove it (--purge: also the recordings)

What `up` creates in --region (default eu-west-2), named <name> (default telqon-recorder):
    one EC2 server (t4g.small, Amazon Linux 2023) with a fixed public IP (Elastic IP)
    a security group open ONLY to --allow (the BroadWorks media servers): SIP 5060 UDP+TCP, RTP 30000-30999 UDP
    a private, encrypted S3 bucket <name>-recordings-<account>; recordings are deleted after --keep-days (30)
    an IAM role (Session Manager, read code bundles, write recordings)

Live transcription (stage 2): put the Deepgram key and the bridge's media token in your shell, never on the command line;
`up` stores them encrypted in SSM Parameter Store (/<name>/...), which only the recorder server can read:
    export DEEPGRAM_API_KEY='...'             # Deepgram console > API Keys
    export BRIDGE_MEDIA_TOKEN='...'           # the bridge's Settings > Media > Pipeline token (type a long random value there)
    python recorder/aws_recorder.py up --bridge-url https://bridge.telqon.com
`up --no-transcription` turns it off again.

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

    def params_path(self):
        return f"/{self.name}"

    def set_params(self):
        """Store the transcription settings (secrets from the environment) under /<name>/ in SSM."""
        p = self.params_path()
        if self.a.no_transcription:
            try:
                self.ssm.delete_parameter(Name=f"{p}/deepgram_api_key")
                step("live transcription turned off (Deepgram key removed)")
            except ClientError:
                pass
            return
        values = {"deepgram_api_key": (os.environ.get("DEEPGRAM_API_KEY", "").strip(), True),
                  "bridge_media_token": (os.environ.get("BRIDGE_MEDIA_TOKEN", "").strip(), True),
                  "bridge_url": ((self.a.bridge_url or "").strip().rstrip("/"), False),
                  "deepgram_model": ((self.a.deepgram_model or "").strip(), False),
                  "deepgram_language": ((self.a.deepgram_language or "").strip(), False),
                  "deepgram_url": ((self.a.deepgram_url or "").strip(), False)}
        if values["bridge_url"][0] and not values["bridge_url"][0].startswith("https://"):
            raise SystemExit("--bridge-url must start with https://")
        for name, (value, secret) in values.items():
            if value:
                self.ssm.put_parameter(Name=f"{p}/{name}", Value=value, Overwrite=True,
                                       Type="SecureString" if secret else "String")
                step(f"{name} stored{' (encrypted)' if secret else ': ' + value}")

    def params_now(self):
        out = {}
        try:
            for page in self.ssm.get_paginator("get_parameters_by_path").paginate(Path=self.params_path()):
                for prm in page["Parameters"]:
                    out[prm["Name"].rsplit("/", 1)[-1]] = "(set)" if prm["Type"] == "SecureString" else prm["Value"]
        except ClientError:
            pass
        return out

    def role_setup(self):
        super().role_setup()
        region, acct = self.a.region, self.account
        self.iam.put_role_policy(RoleName=self.role, PolicyName="settings", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                                                    "Action": ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"],
                                                    "Resource": [f"arn:aws:ssm:{region}:{acct}:parameter{self.params_path()}",
                                                                 f"arn:aws:ssm:{region}:{acct}:parameter{self.params_path()}/*"]}]}))
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
        self.set_params()
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
            f"bash /tmp/rd/deploy/deploy.sh {bundle} {self.a.region} {ip} '{allow}' {self.rec_bucket} {self.params_path()}/ "
            "2>&1 | tail -30"])
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
        prm = self.params_now()
        if prm.get("deepgram_api_key"):
            print(f"Live transcription: on (Deepgram {prm.get('deepgram_model', 'nova-2')}, {prm.get('deepgram_language', 'en-GB')}); "
                  f"lines to {prm.get('bridge_url') or 'no bridge (kept with the recordings)'}"
                  f"{'' if prm.get('bridge_media_token') or not prm.get('bridge_url') else ' (BRIDGE_MEDIA_TOKEN missing!)'}")
        else:
            print("Live transcription: off (export DEEPGRAM_API_KEY=... then up)")

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

    def selftest(self):
        iid = self.need()
        step("playing a two-party recording call into the recorder, on the server itself")
        status, out = self.run(iid, [
            "cd /opt/telqon-recorder/current",
            "sudo -u recorder python3 -m recorder.selftest --target 127.0.0.1:5060 --seconds 6; rc=$?",
            "sleep 4",
            "journalctl -u telqon-recorder --since '-40 s' --no-pager -o cat | grep -E 'recorded|uploaded|ERROR|failed' | tail -5",
            "exit $rc"], quiet=True)
        print(out.strip())
        if status == "Success":
            print("\nThe recorder works on AWS. Next: python recorder/aws_recorder.py list, then fetch the selftest call "
                  "(left channel 440 Hz, right channel 880 Hz).")
        else:
            print(f"\n(self-test status: {status}); see python recorder/aws_recorder.py logs")

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
        for pol in ("recordings", "settings"):
            try:
                self.iam.delete_role_policy(RoleName=self.role, PolicyName=pol)
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
    ap.add_argument("command", choices=["up", "status", "logs", "selftest", "list", "fetch", "delete"])
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
    ap.add_argument("--bridge-url", help="up: the bridge that gets the transcript lines, e.g. https://bridge.telqon.com")
    ap.add_argument("--deepgram-model", help="up: Deepgram model (default nova-2)")
    ap.add_argument("--deepgram-language", help="up: language (default en-GB)")
    ap.add_argument("--deepgram-url", help="up: Deepgram streaming URL (default wss://api.deepgram.com/v1/listen)")
    ap.add_argument("--no-transcription", action="store_true", help="up: turn live transcription off")
    a = ap.parse_args()
    s = RecorderServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
