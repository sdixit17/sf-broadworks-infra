#!/usr/bin/env python3
"""Telqon Voice AI on AWS: the server that registers with BroadWorks as the Voice AI user's phone and answers.

    python voiceai/aws_voiceai.py up --allow <SBC IP> --registrar <SBC IP or name>[:port] --domain avcone.co.uk \\
        --sip-user <line/port user> [--auth-user <name>] --queue <call center number> --bridge-url https://...
    python voiceai/aws_voiceai.py up --greeting greeting.wav      upload a greeting (8 kHz mono 16-bit WAV;
                                                                  with the conversation on it plays before the AI's hello)
    python voiceai/aws_voiceai.py status                          server, IP, settings, registered or not
    python voiceai/aws_voiceai.py logs [--lines 200] [--since '30 min ago']
    python voiceai/aws_voiceai.py delete

Secrets come from your shell, never the command line; `up` stores them encrypted in SSM (/<name>/...):
    export VOICEAI_SIP_PASSWORD='...'     # the Voice AI user's Authentication password (BroadWorks)
    export VOICEAI_BRIDGE_TOKEN='...'     # the bridge's Settings > Voice AI > Voice AI server token
  Stage 2, the conversation (both keys needed; without them the server greets and hands off):
    export ANTHROPIC_API_KEY='...'        # console.anthropic.com > API keys
    export DEEPGRAM_API_KEY='...'         # Deepgram console > API keys (listening and the AI's voice)
    python voiceai/aws_voiceai.py up [--model claude-haiku-5-5] [--effort low] [--voice aura-athena-en]
        [--company AVC] [--allow-writes yes|no] [--knowledge info.txt | --knowledge none] [--greeting none]
        [--callbacks yes|no] [--partner-knowledge partner.txt | none]   (website "Let our AI call you")
  `--no-ai` removes the two keys again (back to stage 1).

What `up` creates in --region (default eu-west-2), named <name> (default telqon-voiceai):
    one EC2 server (t4g.small, Amazon Linux 2023) with a fixed public IP (Elastic IP)
    a security group open ONLY to --allow (the SBC): SIP 5060 UDP, RTP 30000-30999 UDP
    an IAM role (Session Manager, read code bundles, read its own SSM settings)

The code comes from the app checkout (sf-broadworks-bridge/voiceai/ + recorder/, --app-dir); this folder adds how it
runs (systemd service, deploy.sh).
"""
import argparse
import io
import json
import os
import re
import shlex
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
INFRA = HERE.parent
sys.path.insert(0, str(INFRA / "test-server"))
sys.path.insert(0, str(INFRA / "recorder"))
import aws_recorder as rec  # noqa: E402
from aws_recorder import ClientError, step  # noqa: E402

SIP_PORT, RTP_LO, RTP_HI = 5060, 30000, 30999
SETTINGS = ("registrar", "domain", "sip_user", "auth_user", "queue", "bridge_url", "user_agent",
            "model", "effort", "voice", "company", "allow_writes", "stt_model", "language", "voice_gain", "callbacks")
SECRETS = {"sip_password": "VOICEAI_SIP_PASSWORD", "bridge_token": "VOICEAI_BRIDGE_TOKEN",
           "anthropic_api_key": "ANTHROPIC_API_KEY", "deepgram_api_key": "DEEPGRAM_API_KEY"}


def cmd():
    return f"python {sys.argv[0]}"


class VoiceAIServer(rec.RecorderServer):
    SG_DESCRIPTION = "Telqon Voice AI: SIP+RTP from the SBC only"
    SOURCE_LABEL = "SBC"
    PORTS = (("udp", SIP_PORT, SIP_PORT), ("udp", RTP_LO, RTP_HI))

    def bundle(self):
        app = Path(self.a.app_dir).expanduser().resolve()
        if not (app / "voiceai" / "sipua.py").is_file():
            raise SystemExit(f"{app} has no voiceai/ (sf-broadworks-bridge v0.21.0 or later); pass --app-dir")
        files = [f for f in self.git(app, "ls-files", "voiceai", "recorder").split()
                 if f.endswith(".py") or f.startswith("voiceai/playbooks/")]          # code + built-in playbooks
        dirty = self.git(app, "status", "--porcelain", "--untracked-files=no", "voiceai", "recorder").strip()
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
        key = f"bundles/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-voiceai-v{version}-{rev}{'-dirty' if dirty else ''}.tar.gz"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=buf.getvalue())
        step(f"Voice AI from app v{version} ({rev}{', with local changes' if dirty else ''}) uploaded")
        return f"s3://{self.bucket}/{key}"

    def greeting(self):
        """--greeting: check it here, upload it next to the bundles -> its s3:// path, 'none' or '-'."""
        if not self.a.greeting:
            return "-"
        if self.a.greeting == "none":
            return "none"
        p = Path(self.a.greeting).expanduser()
        if not p.is_file():
            raise SystemExit(f"{p} not found")
        print(f"• {p.name}: {rec.wav_info(p)}")
        key = f"greetings/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.wav"
        self.s3.upload_file(str(p), self.bucket, key)
        step("greeting uploaded")
        return f"s3://{self.bucket}/{key}"

    def knowledge(self, k=None, what="knowledge"):
        """--knowledge: a text file the assistant should know (opening hours, FAQs) -> its s3:// path, 'none' or '-'.
        Also --partner-knowledge (what the AI tells website callbacks about the partner programme)."""
        k = self.a.knowledge if k is None else k
        if not k:
            return "-"
        if k == "none":
            return "none"
        p = Path(k).expanduser()
        if not p.is_file():
            raise SystemExit(f"{p} not found")
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > 40000:
            raise SystemExit(f"{p} is {len(text)} characters; keep it under 40,000 (the assistant reads all of it)")
        key = f"{what}/{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.txt"
        self.s3.upload_file(str(p), self.bucket, key)
        step(f"{what} uploaded ({len(text)} characters)")
        return f"s3://{self.bucket}/{key}"

    def set_params(self):
        p = self.params_path()
        if self.a.no_ai:
            for name in ("anthropic_api_key", "deepgram_api_key"):
                try:
                    self.ssm.delete_parameter(Name=f"{p}/{name}")
                except ClientError:
                    pass
            step("conversation turned off (Anthropic and Deepgram keys removed): greeting + hand-off only")
        for name in SETTINGS:
            value = (getattr(self.a, name, None) or "").strip()
            if name == "bridge_url":
                value = value.rstrip("/")
                if value and not value.startswith("https://"):
                    raise SystemExit("--bridge-url must start with https://")
            if name in ("allow_writes", "callbacks") and value:
                value = "1" if value.lower() in ("1", "yes", "true", "on") else "0"
            if name == "effort" and value and value not in ("low", "medium", "high"):
                raise SystemExit("--effort: low, medium or high")
            if value:
                self.ssm.put_parameter(Name=f"{p}/{name}", Value=value, Overwrite=True, Type="String")
                step(f"{name}: {value}")
        for name, env in SECRETS.items():
            value = "" if self.a.no_ai and name in ("anthropic_api_key", "deepgram_api_key") else \
                os.environ.get(env, "").strip()
            if value:
                self.ssm.put_parameter(Name=f"{p}/{name}", Value=value, Overwrite=True, Type="SecureString")
                step(f"{name} stored (encrypted, from ${env})")

    def missing(self):
        have = self.params_now()
        need = ["registrar", "domain", "sip_user", "bridge_url", "sip_password", "bridge_token"]
        return [n for n in need if not have.get(n)]

    def role_setup(self):
        rec.base.Server.role_setup(self)
        region, acct = self.a.region, self.account
        self.iam.put_role_policy(RoleName=self.role, PolicyName="settings", PolicyDocument=json.dumps({
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow",
                                                    "Action": ["ssm:GetParametersByPath", "ssm:GetParameters", "ssm:GetParameter"],
                                                    "Resource": [f"arn:aws:ssm:{region}:{acct}:parameter{self.params_path()}",
                                                                 f"arn:aws:ssm:{region}:{acct}:parameter{self.params_path()}/*"]}]}))

    def up(self):
        step(f"account {self.account}, region {self.a.region}")
        g, _ = self.group()
        if not self.a.allow and not self.allowed_now(g):
            raise SystemExit("first run: pass --allow with the SBC address(es) that send SIP and audio (access side)")
        self.bucket_setup()
        self.set_params()
        missing = self.missing()
        if missing:
            env = [SECRETS[m] for m in missing if m in SECRETS]
            opts = ["--" + m.replace("_", "-") for m in missing if m not in SECRETS]
            raise SystemExit("still missing: " + ", ".join(opts + [f"export {e}=..." for e in env]))
        greeting = self.greeting()
        knowledge = self.knowledge()
        partner = self.knowledge(self.a.partner_knowledge or "", "partner-knowledge")
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
        step(f"installing the Voice AI on {iid} (public IP {ip})")
        status, out = self.run(iid, [
            "set -e; rm -rf /tmp/vd; mkdir -p /tmp/vd",
            "for i in $(seq 1 60); do command -v aws >/dev/null && break; sleep 5; done",
            f"aws s3 cp --region {self.a.region} --only-show-errors {bundle} /tmp/vd/b.tar.gz",
            "tar -xzf /tmp/vd/b.tar.gz -C /tmp/vd deploy/deploy.sh",
            f"bash /tmp/vd/deploy/deploy.sh {bundle} {self.a.region} {ip} '{allow}' {self.params_path()}/ {greeting} {knowledge} {partner} "
            "2>&1 | tail -30"])
        print()
        print(out.strip())
        if status != "Success":
            print(f"\n(command status: {status})")
        self.report(iid, ip, allow)

    def report(self, iid, ip, allow):
        prm = self.params_now()
        print(f"""
Voice AI {iid}   IP {ip}   region {self.a.region}
Registers as {prm.get('sip_user', '?')}@{prm.get('domain', '?')} (auth {prm.get('auth_user') or prm.get('sip_user', '?')}) with {prm.get('registrar', '?')}
Bridge {prm.get('bridge_url', '?')}   fallback queue {prm.get('queue') or '-'}
Allowed sources (SBC): {allow or 'none yet'}
Conversation: {"on, Claude " + (prm.get('model') or 'claude-haiku-5-5') + ", effort " + (prm.get('effort') or 'low')
               + ", voice " + (prm.get("voice") or "aura-athena-en") + ", may create cases: "
               + ("no" if prm.get('allow_writes') == '0' else "yes")
               if prm.get('anthropic_api_key') and prm.get('deepgram_api_key')
               else "off (greeting + hand-off; export ANTHROPIC_API_KEY and DEEPGRAM_API_KEY, then up)"}

For the SBC (access side, like a phone): accept REGISTER/INVITE from {ip}:{SIP_PORT} UDP, RTP UDP {RTP_LO}-{RTP_HI};
    User-Agent "Avaya-One-compatible Telqon-VoiceAI/..." (matches Avaya.*One.*); G.711 only, no SRTP.""")

    def status(self):
        inst = self.instance()
        if not inst:
            return print(f"No Voice AI server yet: {cmd()} up --allow <SBC IP> ...")
        addr = self.eip()
        ip = addr["PublicIp"] if addr else inst.get("PublicIpAddress")
        print(f"Server {inst['InstanceId']}: {inst['State']['Name']}")
        if inst["State"]["Name"] == "running":
            _, out = self.run(inst["InstanceId"], [
                "systemctl is-active telqon-voiceai || true",
                "journalctl -u telqon-voiceai --since '-30 min' --no-pager -o cat | "
                "grep -E 'registered|REGISTER|ERROR' | tail -3"], quiet=True)
            print(f"Service: {out.strip()}")
        self.report(inst["InstanceId"], ip, ",".join(self.allowed_now(self.group()[0])))

    def need(self):
        inst = self.instance()
        if not inst:
            raise SystemExit(f"No Voice AI server yet: run {cmd()} up ... first (see voiceai/README.md)")
        if inst["State"]["Name"] != "running":
            raise SystemExit(f"The Voice AI server is {inst['State']['Name']}; {cmd()} up starts it")
        return inst["InstanceId"]

    def logs(self):
        iid = self.need()
        since = f" --since {shlex.quote(self.a.since)}" if self.a.since else ""
        _, out = self.run(iid, [f"journalctl -u telqon-voiceai -n {self.a.lines}{since} --no-pager -o short-iso"
                                " | sed -E 's/ [^ ]+ (python3|systemd)\\[[0-9]+\\]: / /' | tail -c 23000"], quiet=True)
        print(out)

    def delete(self):
        try:
            self.iam.delete_role_policy(RoleName=self.role, PolicyName="settings")
        except ClientError:
            pass
        rec.base.Server.delete(self)
        names = [f"{self.params_path()}/{n}" for n in (*SETTINGS, *SECRETS)]
        for i in range(0, len(names), 10):
            self.ssm.delete_parameters(Names=names[i:i + 10])
        step("settings removed from SSM")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "status", "logs", "delete"])
    ap.add_argument("--allow", help="up: the SBC address(es)/CIDRs that send SIP and audio (comma-separated)")
    ap.add_argument("--allow-any", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--registrar", help="up: where to REGISTER: the SBC's access-side address, host[:port]")
    ap.add_argument("--domain", help="up: BroadWorks SIP domain, e.g. avcone.co.uk")
    ap.add_argument("--sip-user", help="up: the Voice AI device's line/port user part")
    ap.add_argument("--auth-user", help="up: authentication user name (default: --sip-user)")
    ap.add_argument("--queue", help="up: call center number to REFER to if the bridge can't transfer")
    ap.add_argument("--bridge-url", help="up: the customer's bridge, e.g. https://bridge.telqon.com")
    ap.add_argument("--user-agent", help=argparse.SUPPRESS)
    ap.add_argument("--greeting", help="up: WAV played on answer (8 kHz mono 16-bit; macOS: "
                                       "say -o greeting.wav --data-format=LEI16@8000 \"...\")")
    ap.add_argument("--model", help="up: Claude model for the conversation (default claude-haiku-5-5)")
    ap.add_argument("--effort", help="up: low | medium | high (default low: fastest replies)")
    ap.add_argument("--voice", help="up: Deepgram Aura voice (default aura-athena-en, British female; Aura-2 voices e.g. aura-2-athena-en sound newer but reply 1-2 s slower)")
    ap.add_argument("--company", help="up: the name the assistant answers with (default AVC)")
    ap.add_argument("--voice-gain", help="up: the AI's voice volume in dB, e.g. -6 if mobile callers hear crackle "
                                         "(default 0)")
    ap.add_argument("--allow-writes", help="up: yes = may create cases and add comments, no = look up and hand over")
    ap.add_argument("--stt-model", help=argparse.SUPPRESS)
    ap.add_argument("--language", help=argparse.SUPPRESS)
    ap.add_argument("--knowledge", help="up: text file with what the assistant should know; 'none' removes it")
    ap.add_argument("--callbacks", help="up: yes = call back website visitors (\"Let our AI call you\"), no = off "
                                        "(default yes; the bridge's Settings > Website callbacks controls the form)")
    ap.add_argument("--partner-knowledge", help="up: text file about the partner programme for website callbacks "
                                                "(default: the app's voiceai/playbooks/partner-programme.txt); 'none' "
                                                "goes back to the default")
    ap.add_argument("--no-ai", action="store_true", help="up: remove the AI keys (back to greeting + hand-off)")
    ap.add_argument("--app-dir", default=str(INFRA.parent / "sf-broadworks-bridge"),
                    help="checkout of sdixit17/sf-broadworks-bridge (default: next to this repository)")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--name", default="telqon-voiceai")
    ap.add_argument("--instance-type", default="t4g.small")
    ap.add_argument("--keep-days", type=int, default=30, help=argparse.SUPPRESS)
    ap.add_argument("--lines", type=int, default=200)
    ap.add_argument("--since", help="logs: only lines since then, e.g. '30 min ago', 'today'")
    a = ap.parse_args()
    if a.sip_user and "@" in a.sip_user:                          # user@domain given: split it
        a.sip_user, _, dom = a.sip_user.strip().removeprefix("sip:").partition("@")
        if a.domain and dom.lower() != a.domain.lower():
            raise SystemExit(f"--sip-user is @{dom} but --domain is {a.domain}")
        a.domain = a.domain or dom
    if a.registrar and not re.fullmatch(r"[A-Za-z0-9.\-]+(:\d+)?", a.registrar):
        raise SystemExit("--registrar: host or IP, optionally :port")
    s = VoiceAIServer(a)
    getattr(s, a.command)()


if __name__ == "__main__":
    main()
