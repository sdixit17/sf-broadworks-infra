# Telqon BroadWorks Bridge: AWS infrastructure

How the [BroadWorks Bridge for Salesforce Voice](https://github.com/sdixit17/sf-broadworks-bridge) runs on AWS.
The app (bridge, connector, Salesforce metadata, its `Dockerfile`) lives in **sf-broadworks-bridge**; this
repository only describes and drives where and how it runs.

| Path | What |
|---|---|
| `front-door/` | **bridge.telqon.com** for every customer: the phone page + customer directory on S3 + CloudFront (nothing to go down). `front_door.py up / deploy / add / remove / list / status`. See [front-door/README.md](front-door/README.md). |
| `test-server/` | A bridge on one EC2 server in London with Docker + Caddy (automatic HTTPS), managed with Session Manager: `test.bridge.telqon.com`, or a customer's with `--name/--domain`. `aws_test_server.py up / status / logs / restart / stop / start / setup-code / delete`. See [test-server/README.md](test-server/README.md). |
| `recorder/` | **Telqon recorder**: SIPREC recording server for BroadWorks Call Recording on EC2 (no Chime needed). Stage 1 records to a private S3 bucket (WAV + call details); stage 2 adds Deepgram transcription. `aws_recorder.py up --allow <media servers> / status / logs / list / fetch / delete`. See [recorder/README.md](recorder/README.md). |
| `media-pipeline/` | Call recording and real-time transcription: BroadWorks Call Recording → Chime SDK Voice Connector → Transcribe Call Analytics + S3 → forwarder Lambda → bridge. `setup_media.py`. See [media-pipeline/README.md](media-pipeline/README.md). |
| `tests/` | `test_forwarder.py` (forwarder on sample events), `test_front_door.py` (front_door.py on simulated AWS, requests checked against botocore models). No AWS needed. |

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
    python tests/test_front_door.py
    python tests/test_recorder_aws.py
