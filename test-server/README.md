# Test server on AWS (bridge.telqon.com)

```
agent's browser (Salesforce)                               AWS London (EC2 t4g.small)
  ├─ phone widget  https://127.0.0.1:8080  ──── HTTPS ───►  Caddy (Let's Encrypt) ──► bridge (Docker)
  │   (tools/connector_shim.py, until Telqon's            https://bridge.telqon.com      │  /opt/bridge/data:
  │    own provider package)                                                             │  settings, history, logs
  └─ Supervisor tab ─────────────────────────── HTTPS ───►         same                  └──► BroadWorks XSP (XSI, OCI-P)
                                                                                         └──► Salesforce (Telephony API, REST)
```

About $15 a month (server, disk, fixed IP). Your Mac no longer runs the bridge.

## 1. Create the server (from your Mac)

Clone both repositories side by side, then from `sf-broadworks-infra` (a Python environment with boto3 and
AWS credentials configured, `aws sts get-caller-identity`):

```
~/code/sf-broadworks-bridge      the app      (git pull origin main)
~/code/sf-broadworks-infra       this repo    (git pull origin main)

cd ~/code/sf-broadworks-infra
python test-server/aws_test_server.py up --domain bridge.telqon.com
```

The app is taken from `../sf-broadworks-bridge` (or `--app-dir <path>`): its committed files, built with its
`Dockerfile`. Commit or stash app changes you don't want on the server (local changes to tracked files are
included and the bundle is marked `-dirty`).

The first run takes about 10 minutes (it installs Docker and builds the bridge on the server). It prints the
server's **IP** and the **setup code**.

## 2. DNS in Wix

Wix → Domains → telqon.com → ⋯ → **Manage DNS records** → **A (Host)** → **Add record**:
host `bridge`, value = the IP printed in step 1. Then check:

```
dig bridge.telqon.com +short                         # prints the IP
python test-server/aws_test_server.py status               # DNS ✓ and HTTPS OK
```

HTTPS starts by itself a few minutes after the record works.

## 3. Set up the bridge

Open **https://bridge.telqon.com/admin.html**, sign in with the setup code, then:

1. **Set up from BroadWorks**: enterprise admin login → Fetch → tick the call centers → Preview → Apply.
   From then on, sign in with the enterprise admin login.
2. **BroadWorks**: XSP host `xsp-cluster1.avcone.co.uk` (port 443, https).
3. **Salesforce**: Telephony API URL, org ID `00DgK00000cA7yP`, contact center `QuickStartCC`, paste the private
   key; call reporting (consumer key, integration user) if you use it.
4. **Restart bridge** when the page asks.

If AVC restricts XSI or OCI-P by source address, give them the server's IP.

## 4. Salesforce

```
sf project deploy start --metadata-dir salesforce/setup/trusted-url --target-org scv --wait 10   # in sf-broadworks-bridge
```

adds `https://bridge.telqon.com` as a Trusted URL. In App Builder, set the **Supervisor** page's
`supervisorConsole` component property **Bridge URL** to `https://bridge.telqon.com` and save.

## 5. Each agent's computer (until Telqon's own provider package)

Salesforce's quick-start provider loads the phone from `https://127.0.0.1:8080/`, so agents run the small
connector page (no BroadWorks or Salesforce credentials in it):

```
# in sf-broadworks-bridge
(cd connector && npm install && npm run build)
python tools/connector_shim.py --bridge https://bridge.telqon.com --cert local_tls.pem --key local_tls.key
```

Stop the old local bridge first (same port).

## Day to day

```
python test-server/aws_test_server.py up          # deploy the current code (settings and history are kept)
python test-server/aws_test_server.py logs        # recent bridge log
python test-server/aws_test_server.py status
python test-server/aws_test_server.py setup-code
python test-server/aws_test_server.py delete      # remove everything (then delete the Wix record)
```

A shell on the server: EC2 console → the instance → **Connect** → **Session Manager**
(or `aws ssm start-session --target <instance id> --region eu-west-2` with the Session Manager plugin).
Files: `/opt/bridge/data` (settings.json, secrets/, analytics.sqlite, logs/), code in `/opt/bridge/current`,
`docker logs bridge`, `docker logs caddy`.

## Test, not production

One server, no load balancer, SQLite, settings in a file. Production (ECS Fargate × 2, RDS Postgres, Redis,
Terraform, CI/CD, monitoring, backups) is the next stage; see the production plan in the product document.
