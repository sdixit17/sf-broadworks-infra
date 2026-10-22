# Telqon BroadWorks Bridge: AWS infrastructure

AWS side of the BroadWorks Bridge for Salesforce Voice. The app is **sdixit17/sf-broadworks-bridge** (its
CLAUDE.md has the product context, test commands and hard-won facts); this repo drives where it runs.
Owner: Sandesh (GitHub `sdixit17`), Telqon/AVC.

## Layout

| Path | What |
|---|---|
| `front-door/front_door.py` | bridge.telqon.com = S3 (private, versioned, eu-west-2) + CloudFront (OAC, ACM cert us-east-1, HSTS/nosniff, no X-Frame-Options) serving the phone page (index/login/main/login.js + config.js with `BRIDGE_DIRECTORY='/directory/'`) and the directory `directory/<org15>.json` = `{"bridge": url}` (customer name in S3 metadata). `up / deploy [--build] / add / remove / list / status / delete` |
| `test-server/aws_test_server.py` | boto3: EC2 t4g.small (AL2023 arm64, IMDSv2, 20 GB gp3 encrypted) in eu-west-2, Elastic IP, SG 80/443 only, IAM role (SSM core + read bundles), private S3 bundle bucket; `up` (create or deploy), `status`, `logs`, `setup-code`, `restart`, `stop [--server]`, `start`, `delete`; commands run via SSM `AWS-RunShellScript` |
| `test-server/server-deploy.sh` + `docker-compose.yml` + `Caddyfile` + `server-config.yaml` | On the server: bundle → `/opt/bridge/releases/<ts>`, `current` symlink, `/opt/bridge/data` (settings.json, secrets/, analytics.sqlite, logs) mounted at /data, `/opt/bridge/bridge.env` (BRIDGE_DOMAIN, BRIDGE_PUBLIC_URL, BRIDGE_SETUP_CODE) |
| `recorder/aws_recorder.py` + `deploy.sh` + `telqon-recorder.service` | SIPREC recorder (app `recorder/`) on EC2 t4g.small, EIP, SG only from `--allow` (SIP 5060 UDP/TCP, RTP 30000-30999 UDP), systemd service as user `recorder` (CAP_NET_BIND_SERVICE), env `/opt/telqon-recorder/recorder.env`, recordings to `<name>-recordings-<acct>` (private, SSE, expire 30 d; role may only PutObject); subclasses test-server `Server`; `up --allow / status / logs / list / fetch / delete [--purge]` |
| `media-pipeline/setup_media.py` + `forwarder/handler.py` | Voice Connector (no encryption), call analytics config (Transcribe Call Analytics en-GB + S3 WAV + Kinesis), forwarder Lambda → bridge `/api/media/*` matched by `numbers` |
| `tests/test_forwarder.py` | forwarder checks on sample events |

## How the two repos meet

`aws_test_server.py` packs the app checkout's tracked files (`--app-dir`, default `../sf-broadworks-bridge`) plus
this repo's `test-server/*` as `deploy/` in one bundle; the server builds the app's `Dockerfile`. Production will
instead deploy images the app repo's CI pushes to ECR.

## Facts (don't rediscover)

- AWS account 621554168803 is billed by **AISPL** (India): no Amazon Chime / Chime SDK at all (console error 451,
  API "AWS account is not enabled"). EC2, S3, SSM etc. work. Media pipeline needs a non-AISPL account (e.g. AVC's
  UK account) or the self-hosted SIPREC route (jambonz + Transcribe/Deepgram).
- The account is in Telqon's AWS Organization (management 943939514727, info@telqon.com) with SCP
  `AdvancedModeRegionRestrictionSecurityControlPolicy`; eu-west-2 was added to its RegionFloor list.
- CloudFront blocked (20 Oct): `CreateDistribution` -> AccessDenied "Your account must be verified before you can add new
  CloudFront resources". Support case opened; first verification failed (name couldn't be verified: account contact
  name vs ID/card, AISPL KYC). Owner chose to wait for AWS. Already created: front-door bucket, ACM cert for
  bridge.telqon.com (ISSUED; its `_...` CNAME stays in Wix). Fallbacks discussed: AVC's UK account (also fixes Chime),
  or Cloudflare/GitHub Pages for the static front door.
- Front door (app v0.12.0, 19 Oct): code ready, offline-tested only; the owner still has to run the switch-over in
  `front-door/README.md` (test server -> test.bridge.telqon.com, ACM DNS record + CNAME bridge -> CloudFront in Wix).
  Test server defaults now: `--domain test.bridge.telqon.com`, `--front-door https://bridge.telqon.com` (BRIDGE_FRONT_DOOR in bridge.env).
- Recorder (21 Oct, stage 1 = record only): Deepgram route chosen because Chime is impossible on AISPL. Built and
  offline-tested; not yet deployed. Needs from AVC: media server source IPs, a Call Recording platform -> recorder IP:5060
  (dual stream, G.711, no SRTP), Call Recording on Webextest1, firewall. Then inspect `call.json`/`metadata1.xml` for
  stream roles and IDs (extTrackingId) before stage 2 (Deepgram live transcription -> bridge `/api/media/*`).
- Recorder stage 2 (22 Oct): live Deepgram transcription per stream; settings in SSM `/telqon-recorder/{deepgram_api_key,
  bridge_media_token (SecureString), bridge_url, deepgram_model, deepgram_language, deepgram_url}`, read by the recorder at
  start (`RECORDER_PARAMS`); `up` stores them from env DEEPGRAM_API_KEY / BRIDGE_MEDIA_TOKEN + `--bridge-url`. Stage 1
  verified on AWS (recorder 13.42.226.59, selftest recording fetched, SIPp from the owner's Mac needs sudo for audio:
  raw sockets; home network = carrier-grade NAT / SIP ALG). The owner pasted a Deepgram key in chat on 21 Oct: told to rotate it.
- DNS for telqon.com is at **Wix** (ns2/ns3.wixdns.net): A record `bridge` → the test server's Elastic IP
  **51.24.196.195** (server created 18 Oct; live).
- Since 19 Oct Salesforce loads the connector straight from https://bridge.telqon.com/ (Telqon's provider package,
  contact center TelqonCC); the connector shim is no longer needed. Nothing runs on the owner's laptop.
- Test server running on real AWS since 18 Oct (eu-west-2). Media pipeline validated against botocore models only.

## Conventions

- Commit as `sdixit17 <info@telqon.com>`; push to `main`.
- Never commit credentials, keys, certificates, settings.json or Terraform state.
