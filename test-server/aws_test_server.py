#!/usr/bin/env python3
"""The bridge's test server on AWS: create, update and look after it from your Mac.

    python test-server/aws_test_server.py up                 create it, or deploy the app's current code
    python test-server/aws_test_server.py status             server, IP, DNS and HTTPS check
    python test-server/aws_test_server.py logs [--lines 300] the bridge's recent log
    python test-server/aws_test_server.py setup-code         the Settings page setup code
    python test-server/aws_test_server.py restart            restart the bridge (settings and history kept)
    python test-server/aws_test_server.py stop               stop the bridge, or with --server the whole server
    python test-server/aws_test_server.py start              start the server (if stopped) and the bridge
    python test-server/aws_test_server.py delete             remove the server (keeps nothing)

The app comes from a checkout of sdixit17/sf-broadworks-bridge: --app-dir (default ../sf-broadworks-bridge,
next to this repository). Its Dockerfile builds the image; this repository adds how it runs (test-server/).

What `up` creates in --region (default eu-west-2), all named <name> (default telqon-bridge-test):
    one EC2 server (t4g.small, Amazon Linux 2023, ARM, 20 GB encrypted disk, IMDSv2)
    a fixed public IP (Elastic IP) for the DNS record
    a security group open on 80 and 443 only (no SSH: manage it with Session Manager)
    an IAM role for Session Manager and for reading the code bundle
    a private S3 bucket for code bundles

The code is packed from the app checkout (tracked files only: never config.yaml, keys or settings) plus this
repository's test-server/ files (as deploy/ in the bundle), uploaded to the bucket, and built on the server: Docker + Caddy (automatic HTTPS for --domain). Settings live on the
server in /opt/bridge/data and survive updates. A shell on the server:
    aws ssm start-session --target <instance id> --region eu-west-2      (needs the Session Manager plugin)
or EC2 console > the instance > Connect > Session Manager.
"""
import argparse
import io
import secrets
import socket
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    sys.exit("pip install boto3 first")

INFRA = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
COMPOSE_URL = "https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-aarch64"


def step(msg):
    print(f"• {msg}", flush=True)


def domains(value):
    """--domain "a,b" -> ["a", "b"]; the first is the main address."""
    return [d.strip() for d in value.split(",") if d.strip()]


class Server:
    def __init__(self, a):
        self.a = a
        self.s = boto3.session.Session(region_name=a.region)
        self.ec2 = self.s.client("ec2")
        self.iam = self.s.client("iam")
        self.ssm = self.s.client("ssm")
        self.s3 = self.s.client("s3")
        self.account = self.s.client("sts").get_caller_identity()["Account"]
        self.name = a.name
        self.bucket = f"{a.name}-deploy-{self.account}"
        self.role = f"{a.name}-ec2"

    # ------------------------------------------------------------- lookups
    def instance(self):
        r = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [self.name]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])
        found = [i for res in r["Reservations"] for i in res["Instances"]]
        return found[0] if found else None

    def eip(self):
        r = self.ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [self.name]}])["Addresses"]
        return r[0] if r else None

    def tag(self, extra=None):
        return [{"Key": "Name", "Value": self.name}, {"Key": "app", "Value": "telqon-bridge"}] + (extra or [])

    # ------------------------------------------------------------- pieces
    def bucket_setup(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.s3.create_bucket(Bucket=self.bucket, CreateBucketConfiguration={"LocationConstraint": self.a.region})
            self.s3.put_public_access_block(Bucket=self.bucket, PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
            self.s3.put_bucket_lifecycle_configuration(Bucket=self.bucket, LifecycleConfiguration={"Rules": [{
                "ID": "old-bundles", "Status": "Enabled", "Filter": {"Prefix": "bundles/"}, "Expiration": {"Days": 30}}]})
            step(f"bucket {self.bucket} created")

    @staticmethod
    def git(repo, *args):
        r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"git {' '.join(args)} in {repo}: {r.stderr.strip()}")
        return r.stdout

    def bundle(self):
        app = Path(self.a.app_dir).expanduser().resolve()
        if not (app / "Dockerfile").is_file() or not (app / "main.py").is_file():
            raise SystemExit(f"{app} is not a checkout of sf-broadworks-bridge (no Dockerfile/main.py); "
                             "pass --app-dir")
        files = self.git(app, "ls-files").split()
        dirty = self.git(app, "status", "--porcelain", "--untracked-files=no").strip()
        rev = self.git(app, "rev-parse", "--short", "HEAD").strip()
        version = (app / "VERSION").read_text().strip() if (app / "VERSION").is_file() else "?"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for f in files:
                p = app / f
                if p.is_file() and not f.startswith("deploy/"):
                    t.add(p, arcname=f)
            for p in sorted(HERE.iterdir()):                  # how it runs: this repository's test-server/
                if p.is_file() and p.suffix != ".pyc":
                    t.add(p, arcname=f"deploy/{p.name}")
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-v{version}-{rev}{'-dirty' if dirty else ''}.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        step(f"app v{version} ({rev}{', with local changes' if dirty else ''}) from {app} uploaded "
             f"({len(buf.getvalue()) // 1024} KB)")
        return f"s3://{self.bucket}/{key}"

    def role_setup(self):
        trust = '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
        try:
            self.iam.get_role(RoleName=self.role)
        except ClientError:
            self.iam.create_role(RoleName=self.role, AssumeRolePolicyDocument=trust)
            self.iam.attach_role_policy(RoleName=self.role,
                                        PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
            step(f"role {self.role} created")
        self.iam.put_role_policy(RoleName=self.role, PolicyName="bundles", PolicyDocument=(
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject",'
            f'"Resource":"arn:aws:s3:::{self.bucket}/*"}}]}}'))
        try:
            self.iam.get_instance_profile(InstanceProfileName=self.role)
        except ClientError:
            self.iam.create_instance_profile(InstanceProfileName=self.role)
            self.iam.add_role_to_instance_profile(InstanceProfileName=self.role, RoleName=self.role)
            time.sleep(10)                                   # new profiles take a moment before EC2 accepts them

    def network(self):
        vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            self.ec2.create_default_vpc()
            step("default VPC created")
            vpcs = self.ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        vpc = vpcs[0]["VpcId"]
        groups = self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [self.name]},
                                                            {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
        if groups:
            return groups[0]["GroupId"]
        gid = self.ec2.create_security_group(GroupName=self.name, VpcId=vpc, Description="Telqon bridge test: HTTPS",
                                             TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tag()}])["GroupId"]
        self.ec2.authorize_security_group_ingress(GroupId=gid, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
             "Ipv6Ranges": [{"CidrIpv6": "::/0"}]} for p in (80, 443)])
        step("security group created (80, 443 open; no SSH)")
        return gid

    def user_data(self, bundle, setup_code):
        return f"""#!/bin/bash
set -euxo pipefail
dnf install -y docker
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL {COMPOSE_URL} -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
if [ ! -f /swapfile ]; then fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile;
  echo '/swapfile swap swap defaults 0 0' >> /etc/fstab; fi
mkdir -p /opt/bridge
cat > /opt/bridge/bridge.env <<'ENV'
BRIDGE_DOMAIN="{", ".join(domains(self.a.domain))}"
BRIDGE_PUBLIC_URL=https://{domains(self.a.domain)[0]}
BRIDGE_SETUP_CODE={setup_code}
BRIDGE_FRONT_DOOR={self.a.front_door}
AWS_DEFAULT_REGION={self.a.region}
ENV
chmod 600 /opt/bridge/bridge.env
mkdir -p /tmp/first
aws s3 cp --region {self.a.region} {bundle} /tmp/first/b.tar.gz
tar -xzf /tmp/first/b.tar.gz -C /tmp/first deploy/server-deploy.sh
bash /tmp/first/deploy/server-deploy.sh {bundle} {self.a.region} > /var/log/bridge-first-deploy.log 2>&1
"""

    def launch(self, bundle):
        sg = self.network()
        ami = self.ssm.get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64")["Parameter"]["Value"]
        setup_code = "-".join(secrets.token_hex(2) for _ in range(3))
        for i in range(6):
            try:
                inst = self.ec2.run_instances(
                    ImageId=ami, InstanceType=self.a.instance_type, MinCount=1, MaxCount=1, SecurityGroupIds=[sg],
                    IamInstanceProfile={"Name": self.role}, UserData=self.user_data(bundle, setup_code),
                    MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled",
                                     "HttpPutResponseHopLimit": 2},    # 2: the bridge container reaches the role
                    BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 20, "VolumeType": "gp3",
                                                                             "Encrypted": True}}],
                    TagSpecifications=[{"ResourceType": "instance", "Tags": self.tag()},
                                       {"ResourceType": "volume", "Tags": self.tag()}])["Instances"][0]
                break
            except ClientError as e:
                if "instance profile" in str(e).lower() and i < 5:
                    time.sleep(5)
                    continue
                raise SystemExit(f"starting the server failed: {e}")
        step(f"server {inst['InstanceId']} starting ({self.a.instance_type})")
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[inst["InstanceId"]])
        return inst["InstanceId"], setup_code

    def ip_setup(self, instance_id):
        addr = self.eip()
        if not addr:
            addr = self.ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip",
                                                                               "Tags": self.tag()}])
            step(f"fixed IP {addr['PublicIp']} allocated")
        if addr.get("InstanceId") != instance_id:
            self.ec2.associate_address(AllocationId=addr["AllocationId"], InstanceId=instance_id)
        return addr["PublicIp"]

    # ----------------------------------------------------------- commands
    def run(self, instance_id, commands, timeout=1800, quiet=False):
        """A shell command on the server through Session Manager -> (status, output)."""
        for _ in range(60):                                   # the agent needs a minute after first boot
            info = self.ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
            if info["InstanceInformationList"]:
                break
            time.sleep(5)
        else:
            raise SystemExit("the server isn't reachable through Session Manager yet; try again in a few minutes")
        cid = self.ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript",
                                    Parameters={"commands": commands, "executionTimeout": [str(timeout)]})["Command"]["CommandId"]
        time.sleep(2)
        while True:
            try:
                r = self.ssm.get_command_invocation(CommandId=cid, InstanceId=instance_id)
            except ClientError:
                time.sleep(2)
                continue
            if r["Status"] not in ("Pending", "InProgress", "Delayed"):
                out = (r.get("StandardOutputContent") or "") + (r.get("StandardErrorContent") or "")
                return r["Status"], out
            if not quiet:
                print(".", end="", flush=True)
            time.sleep(5)

    def up(self):
        step(f"account {self.account}, region {self.a.region}")
        self.bucket_setup()
        bundle = self.bundle()
        inst = self.instance()
        setup_code = None
        if inst is None:
            self.role_setup()
            iid, setup_code = self.launch(bundle)
            ip = self.ip_setup(iid)
            step("first boot: installing Docker and building the bridge (about 5-10 minutes)")
            status, out = self.run(iid, ["cloud-init status --wait >/dev/null; cat /var/log/bridge-first-deploy.log | tail -30"])
        else:
            iid = inst["InstanceId"]
            if inst["State"]["Name"] == "stopped":
                self.ec2.start_instances(InstanceIds=[iid])
                self.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
            ip = self.ip_setup(iid)
            step(f"updating server {iid}")
            # run the deploy script from the new bundle, so changes to it apply on this very update
            status, out = self.run(iid, [
                "set -e; rm -rf /tmp/nd; mkdir -p /tmp/nd",
                f"aws s3 cp --region {self.a.region} --only-show-errors {bundle} /tmp/nd/b.tar.gz",
                "tar -xzf /tmp/nd/b.tar.gz -C /tmp/nd deploy/server-deploy.sh",
                f"bash /tmp/nd/deploy/server-deploy.sh {bundle} {self.a.region} '{','.join(domains(self.a.domain))}' "
                f"'{self.a.front_door}' "
                "2>&1 | tail -40"])
        self.recordings_access(iid)
        print()
        print(out.strip())
        if status != "Success":
            print(f"\n(command status: {status})")
        self.report(iid, ip, setup_code)

    def recordings_access(self, iid):
        """The bridge signs playback links for call recordings in the recorder's bucket (S3 presigned GET), so its
        role may read recordings/*, and the container must reach the instance role (IMDSv2 hop limit 2)."""
        bucket = self.a.recordings_bucket or f"telqon-recorder-recordings-{self.account}"
        self.iam.put_role_policy(RoleName=self.role, PolicyName="recordings-read", PolicyDocument=(
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject",'
            f'"Resource":"arn:aws:s3:::{bucket}/recordings/*"}}]}}'))
        self.ec2.modify_instance_metadata_options(InstanceId=iid, HttpTokens="required", HttpEndpoint="enabled",
                                                  HttpPutResponseHopLimit=2)
        step(f"bridge may read recordings in s3://{bucket}/recordings/ (for playback links)")

    def report(self, iid, ip, setup_code=None):
        print(f"\nServer {iid}   IP {ip}   region {self.a.region}")
        live = None
        for d in domains(self.a.domain):
            try:
                resolved = socket.gethostbyname(d)
            except OSError:
                resolved = None
            if resolved != ip:
                print(f"DNS: {d} {'points at ' + resolved if resolved else 'does not resolve yet'}")
                if not d.endswith("sslip.io"):
                    zone = ".".join(d.split(".")[-2:])
                    print(f"     In Wix: Domains > {zone} > ⋯ > Manage DNS records > A (Host) > Add record: "
                          f"host \"{d[:-len(zone) - 1]}\", value {ip}, then Save.")
                continue
            https = self.https_check(d)
            print(f"DNS: {d} -> {ip} ✓   HTTPS: {https}")
            if https.startswith("OK") and not live:
                live = d
        if not live and not any(d.endswith("sslip.io") for d in domains(self.a.domain)):
            print(f"     To test before DNS works: python test-server/aws_test_server.py up "
                  f"--domain {domains(self.a.domain)[0]},{ip.replace('.', '-')}.sslip.io")
        d = live or domains(self.a.domain)[0]
        print(f"\nSettings page: https://{d}/admin.html")
        if setup_code:
            print(f"Setup code:    {setup_code}   (only until administrators are set; later: "
                  "python test-server/aws_test_server.py setup-code)")
        print(f"Shell:         aws ssm start-session --target {iid} --region {self.a.region}")

    @staticmethod
    def https_check(domain):
        import ssl
        import urllib.request
        from botocore.httpsession import get_cert_path
        # python.org's Python on macOS has no CA certificates of its own; use the bundle boto3 already ships
        ctx = ssl.create_default_context(cafile=get_cert_path(True))
        try:
            with urllib.request.urlopen(f"https://{domain}/healthz", timeout=10, context=ctx) as r:
                return f"OK {r.read(200).decode(errors='replace')}"
        except Exception as e:
            return f"not yet ({e})"

    def status(self):
        inst = self.instance()
        if not inst:
            return print("No test server. Create it with: python test-server/aws_test_server.py up --domain test.bridge.telqon.com")
        addr = self.eip()
        print(f"Server {inst['InstanceId']}: {inst['State']['Name']}, {inst['InstanceType']}")
        self.report(inst["InstanceId"], addr["PublicIp"] if addr else inst.get("PublicIpAddress"))

    def logs(self):
        inst = self.need()
        status, out = self.run(inst, [f"docker logs --tail {self.a.lines} bridge 2>&1 | grep -v 'Setup code' | tail -c 23000"], quiet=True)
        print(out)

    def setup_code(self):
        inst = self.need()
        status, out = self.run(inst, ["grep BRIDGE_SETUP_CODE /opt/bridge/bridge.env | cut -d= -f2"], quiet=True)
        print(f"Setup code: {out.strip()}   (works only while no Settings administrators or enterprise are set)")

    # bridge.env must be loaded: Caddy takes BRIDGE_DOMAIN from it (without it Caddy serves no site)
    COMPOSE = "cd /opt/bridge/current/deploy && set -a && . /opt/bridge/bridge.env && set +a && docker compose"

    def restart(self):
        inst = self.need()
        step("restarting the bridge")
        status, out = self.run(inst, [f"{self.COMPOSE} restart bridge && sleep 5 && {self.COMPOSE} ps"], quiet=True)
        print(out)
        self.status()

    def stop(self):
        inst = self.instance()
        if not inst or inst["State"]["Name"] in ("stopping", "stopped"):
            return print("The test server is already stopped.")
        if not self.a.server:
            step("stopping the bridge (the server keeps running; start it again with: start)")
            status, out = self.run(inst["InstanceId"], [f"{self.COMPOSE} stop bridge && {self.COMPOSE} ps"], quiet=True)
            return print(out)
        self.ec2.stop_instances(InstanceIds=[inst["InstanceId"]])
        step(f"server {inst['InstanceId']} stopping (disk, settings and IP are kept; start it again with: start)")
        self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[inst["InstanceId"]])
        step("stopped")

    def start(self):
        inst = self.instance()
        if not inst:
            raise SystemExit("No test server. Create it with: python test-server/aws_test_server.py up")
        iid = inst["InstanceId"]
        if inst["State"]["Name"] != "running":
            if inst["State"]["Name"] == "stopping":
                self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[iid])
            self.ec2.start_instances(InstanceIds=[iid])
            step(f"server {iid} starting")
            self.ec2.get_waiter("instance_running").wait(InstanceIds=[iid])
        step("starting the bridge")
        status, out = self.run(iid, [f"{self.COMPOSE} up -d && sleep 5 && {self.COMPOSE} ps"], quiet=True)
        print(out)
        for _ in range(12):                                   # Caddy and the bridge need a few seconds
            if self.https_check(domains(self.a.domain)[0]).startswith("OK"):
                break
            time.sleep(5)
        self.status()

    def need(self):
        inst = self.instance()
        if not inst or inst["State"]["Name"] != "running":
            raise SystemExit("the test server isn't running (python test-server/aws_test_server.py status)")
        return inst["InstanceId"]

    def delete(self):
        inst = self.instance()
        if inst:
            self.ec2.terminate_instances(InstanceIds=[inst["InstanceId"]])
            step(f"server {inst['InstanceId']} terminating (its settings and history go with it)")
            self.ec2.get_waiter("instance_terminated").wait(InstanceIds=[inst["InstanceId"]])
        addr = self.eip()
        if addr:
            if addr.get("AssociationId"):
                self.ec2.disassociate_address(AssociationId=addr["AssociationId"])
            self.ec2.release_address(AllocationId=addr["AllocationId"])
            step(f"IP {addr['PublicIp']} released (remove the DNS record in Wix)")
        for g in self.ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [self.name]}])["SecurityGroups"]:
            for i in range(10):
                try:
                    self.ec2.delete_security_group(GroupId=g["GroupId"])
                    step("security group deleted")
                    break
                except ClientError:
                    time.sleep(5)
        try:
            self.iam.remove_role_from_instance_profile(InstanceProfileName=self.role, RoleName=self.role)
            self.iam.delete_instance_profile(InstanceProfileName=self.role)
            self.iam.detach_role_policy(RoleName=self.role, PolicyArn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore")
            self.iam.delete_role_policy(RoleName=self.role, PolicyName="bundles")
            try:
                self.iam.delete_role_policy(RoleName=self.role, PolicyName="recordings-read")
            except ClientError:
                pass
            self.iam.delete_role(RoleName=self.role)
            step("role deleted")
        except ClientError:
            pass
        try:
            b = boto3.resource("s3", region_name=self.a.region).Bucket(self.bucket)
            b.objects.all().delete()
            b.delete()
            step("bundle bucket deleted")
        except ClientError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "logs", "setup-code", "restart", "stop", "start", "delete"])
    ap.add_argument("--domain", default="test.bridge.telqon.com",
                    help="this bridge's address(es), comma-separated; bridge.telqon.com itself is the front door")
    ap.add_argument("--front-door", default="https://bridge.telqon.com",
                    help="where Salesforce loads the phone (front-door/front_door.py); the bridge accepts it")
    ap.add_argument("--app-dir", default=str(INFRA.parent / "sf-broadworks-bridge"),
                    help="checkout of sdixit17/sf-broadworks-bridge (default: next to this repository)")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--recordings-bucket", default="",
                    help="call recordings the bridge may play back (default: the recorder's telqon-recorder-recordings-<account>)")
    ap.add_argument("--name", default="telqon-bridge-test")
    ap.add_argument("--instance-type", default="t4g.small")
    ap.add_argument("--lines", type=int, default=300)
    ap.add_argument("--server", action="store_true", help="stop: stop the whole EC2 server, not just the bridge")
    a = ap.parse_args()
    s = Server(a)
    {"up": s.up, "status": s.status, "logs": s.logs, "setup-code": s.setup_code,
     "restart": s.restart, "stop": s.stop, "start": s.start, "delete": s.delete}[a.command]()


if __name__ == "__main__":
    main()
