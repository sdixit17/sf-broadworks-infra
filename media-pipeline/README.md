# AWS: call recording and real-time transcription

```
agent's call ── BroadWorks Call Recording (SIPREC, dual stream, unencrypted) ──► Chime SDK Voice Connector
                                                                                     │
                     Transcribe Call Analytics (en-GB) ◄── call analytics config ◄───┘
                        │ live lines                         │ recording (WAV)
                        ▼                                    ▼
                 Kinesis data stream                    S3 bucket
                        │  (also "recording ready")
                        ▼
                 Lambda forwarder ── HTTPS ──► bridge /api/media/transcript, /api/media/recording
                                                  └─► Salesforce Voice Call (live transcript, recording)
                                                  └─► supervisor console (live transcript panel)
```

## 1. Your Mac

```
brew install awscli cloudflared
pip install boto3
```

**AWS credentials.** In the AWS console: IAM → Users → Create user (e.g. `telqon-admin`) → attach
`AdministratorAccess` → Security credentials → Create access key (Command Line Interface). Then:

```
aws configure            # access key, secret, region eu-west-2, output json
```

**A public address for the bridge** (AWS has to reach it). Start the bridge as usual, then in a second
terminal:

```
cloudflared tunnel --url https://localhost:8080 --no-tls-verify
```

It prints an address like `https://something-random.trycloudflare.com`. It changes each time the tunnel
starts; run step 2 again with the new address (it only updates the forwarder).

## 2. Create everything in AWS

Ask AVC for the **public IP** that BroadWorks' call recording SIP comes from (the media server's, or the
SBC's if it goes out through the SBC). Then:

```
python media-pipeline/setup_media.py --bridge-url https://something-random.trycloudflare.com --allowed-ip 203.0.113.10
```

It creates the S3 bucket, Kinesis stream, token, IAM roles, forwarder function, call analytics
configuration and the Voice Connector, and prints:

- the **Voice Connector address** for BroadWorks,
- the **pipeline token**, bucket and region for the bridge's Settings page (shown once; it stays in Secrets Manager).

Options: `--region` (default eu-west-2), `--keep-days` (recordings, default 30), `--language` (default en-GB),
`--name` (default telqon-media). Remove it all with `--delete` (`--delete-bucket` also removes recordings).

## 3. BroadWorks (AVC system admin)

1. System → Resources → Call Recording Platforms → Add:
   address = the Voice Connector address, port 5060, UDP or TCP (no TLS), media stream **Dual**, SIPREC if
   the release asks for a type.
2. Assign the platform to the enterprise or group.
3. Give the agents (e.g. Webextest1) the **Call Recording** service; mode **Always** for the test.
4. Firewall: SIP 5060 and RTP from BroadWorks to AWS's Chime SDK Voice Connector address ranges for London
   (listed in the Chime SDK documentation, "Network configuration and bandwidth requirements").
5. If the SBC is in the path: it must pass the SIPREC body (multipart/mixed with `rs-metadata`) unchanged.

## 4. The bridge

Settings page → **Recording and transcripts**: Pipeline token (Change → paste), Recording storage `s3`,
S3 bucket and region as printed. Save and restart the bridge. It signs playback links with the AWS
credentials from step 1.

## 5. Test call

Call the queue, answer as the agent and talk for a minute. You should see:

- the transcript building up live in the supervisor console (Transcript button on the agent's row) and on
  the Voice Call page in Salesforce;
- after hanging up, the recording on the Voice Call (play button).

Watch the forwarder while you call:

```
aws logs tail /aws/lambda/telqon-media-forwarder --follow --region eu-west-2
```

It logs the first 20 raw events from AWS. If lines don't reach the bridge, share that output and the bridge
log (`python tools/share_logs.py`): the exact event shapes and SIPREC metadata from BroadWorks are what to
check first (which side AWS calls agent and customer, and which numbers it passes for matching the call).

## Costs (test)

Voice Connector streaming, Transcribe Call Analytics (per minute, per call), S3 and Lambda (pennies). A few
hours of test calls is a few dollars, covered by the account's credits. Set a budget alert (Billing →
Budgets, e.g. $10).
