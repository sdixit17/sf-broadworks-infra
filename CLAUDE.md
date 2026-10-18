# Telqon BroadWorks Bridge: AWS infrastructure

AWS side of the BroadWorks Bridge for Salesforce Voice. The app is **sdixit17/sf-broadworks-bridge** (its
CLAUDE.md has the product context, test commands and hard-won facts); this repo drives where it runs.
Owner: Sandesh (GitHub `sdixit17`), Telqon/AVC.

## Layout

| Path | What |
|---|---|
| `test-server/aws_test_server.py` | boto3: EC2 t4g.small (AL2023 arm64, IMDSv2, 20 GB gp3 encrypted) in eu-west-2, Elastic IP, SG 80/443 only, IAM role (SSM core + read bundles), private S3 bundle bucket; `up` (create or deploy), `status`, `logs`, `setup-code`, `delete`; commands run via SSM `AWS-RunShellScript` |
| `test-server/server-deploy.sh` + `docker-compose.yml` + `Caddyfile` + `server-config.yaml` | On the server: bundle → `/opt/bridge/releases/<ts>`, `current` symlink, `/opt/bridge/data` (settings.json, secrets/, analytics.sqlite, logs) mounted at /data, `/opt/bridge/bridge.env` (BRIDGE_DOMAIN, BRIDGE_PUBLIC_URL, BRIDGE_SETUP_CODE) |
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
- DNS for telqon.com is at **Wix** (ns2/ns3.wixdns.net): A record `bridge` → the test server's Elastic IP
  **51.24.196.195** (server created 18 Oct; record added in Wix the same day, propagation pending).
- Salesforce's quick-start provider loads the connector from https://127.0.0.1:8080, so agents run the app's
  `tools/connector_shim.py --bridge https://bridge.telqon.com` until Telqon's own provider package exists.
- Validated against botocore models and (test server) a local Docker build/run; not yet run against real AWS.

## Conventions

- Commit as `sdixit17 <info@telqon.com>`; push to `main`.
- Never commit credentials, keys, certificates, settings.json or Terraform state.
