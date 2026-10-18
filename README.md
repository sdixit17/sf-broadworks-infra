# Telqon BroadWorks Bridge: AWS infrastructure

How the [BroadWorks Bridge for Salesforce Voice](https://github.com/sdixit17/sf-broadworks-bridge) runs on AWS.
The app (bridge, connector, Salesforce metadata, its `Dockerfile`) lives in **sf-broadworks-bridge**; this
repository only describes and drives where and how it runs.

| Path | What |
|---|---|
| `test-server/` | Test server for `bridge.telqon.com`: one EC2 server in London with Docker + Caddy (automatic HTTPS), managed with Session Manager. `aws_test_server.py up / status / logs / setup-code / delete`. See [test-server/README.md](test-server/README.md). |
| `media-pipeline/` | Call recording and real-time transcription: BroadWorks Call Recording → Chime SDK Voice Connector → Transcribe Call Analytics + S3 → forwarder Lambda → bridge. `setup_media.py`. See [media-pipeline/README.md](media-pipeline/README.md). |
| `tests/` | `test_forwarder.py`: the forwarder against sample AWS events (no AWS needed). |

Planned: `terraform/` for production (ECS Fargate, RDS Postgres, Redis, ALB/WAF, staging + production) and
CI/CD that deploys images the app repository builds.

## Working copies side by side

```
~/code/sf-broadworks-bridge     git clone https://github.com/sdixit17/sf-broadworks-bridge
~/code/sf-broadworks-infra      git clone https://github.com/sdixit17/sf-broadworks-infra
```

Scripts here find the app at `../sf-broadworks-bridge` (or `--app-dir`). They need Python 3 with `boto3` and AWS
credentials (`aws configure`; `aws sts get-caller-identity` to check).

## Test

    python tests/test_forwarder.py
