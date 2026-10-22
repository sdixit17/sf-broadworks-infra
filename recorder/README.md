# Telqon recorder (stage 1: recording, stage 2: live transcription)

A SIPREC recording server on AWS for **BroadWorks Call Recording**. The BroadWorks media server sends each recorded
call to it: one SIP call carrying two audio streams (agent and customer) plus details of who is on the call. The
recorder keeps it in a private S3 bucket:

| File | What |
|---|---|
| `call.wav` | One channel per stream (normally left = first party, right = second), 8 kHz 16-bit |
| `call.json` | Start/end, length, end reason, each channel's participant (from the metadata), packets, losses, codec |
| `sip.txt` | The SIP messages received (INVITE, re-INVITEs, BYE) |
| `metadata1.xml` … | The recording metadata exactly as BroadWorks sent it |

Stage 1 exists to learn what BroadWorks actually sends:
- which stream is the agent and which the customer;
- which IDs the metadata carries (user, extTrackingId), so recordings can be matched to the call in Salesforce.

Stage 2 adds live transcription (Deepgram) and hands transcripts and recording links to the customer's bridge
(`/api/media/*`, already built in the app).

The recorder code is in the app repository (`sf-broadworks-bridge/recorder/`, tested by `tools/test_recorder.py`).
This folder holds how it runs: `aws_recorder.py`, `deploy.sh`, `telqon-recorder.service`.

## Before you start: ask AVC

1. **Which addresses** their media server(s) send call recordings from. The recorder answers no one else.
2. That they'll configure a **Call Recording platform** pointing at the recorder:
   - address: the IP `up` prints; port 5060, UDP (TCP also works);
   - **dual stream**;
   - **G.711** (PCMU/PCMA);
   - no SRTP.
3. That the **Call Recording** service is assigned to the test agent (Webextest1), mode "Always".
4. Their firewall lets the media server reach the recorder on **SIP 5060** and **RTP UDP 30000–30999**.

## Commands (from your Mac, AWS credentials as for the test server)

    python recorder/aws_recorder.py up --allow <media server IP>[,<IP or CIDR>...]   # create, or deploy new code
    python recorder/aws_recorder.py status
    python recorder/aws_recorder.py logs              # each call's SIP details and a summary when it ends
    python recorder/aws_recorder.py selftest          # a two-party test call made on the server itself (no outside network)
    python recorder/aws_recorder.py list              # recordings from the last 2 days (--days N)
    python recorder/aws_recorder.py fetch <name>      # download one into ./recordings/<name>/
    python recorder/aws_recorder.py delete            # remove the server (recordings kept; --purge deletes them)

`up` without `--allow` keeps the current allowed sources. `--allow` replaces them.

The app checkout is found next to this repository, or with `--app-dir`. If this repo sits inside your app folder,
run from the app folder:

    python sf-broadworks-infra/recorder/aws_recorder.py up --app-dir . --allow <IPs>

## First test

0. `selftest` checks the recorder and S3 on AWS without any outside network. Testing from home with SIPp
   can fail on carrier-grade NAT or a router's SIP ALG; that isn't a recorder fault.

1. `up --allow <AVC media server IPs>`, then send AVC the address it prints.
2. When AVC has configured it, make a queue call to Webextest1 (from 03330200219), talk for 20 seconds, then hang up.
3. Run `logs` to see the call arrive and finish. Run `list`, then `fetch <name>`, and listen to `call.wav`.
4. Paste the `logs` output, or share `call.json` and `metadata1.xml`. Numbers in them are real, so share privately.
   From them I'll confirm the stream roles and the IDs for matching.

## Live transcription (stage 2)

Each party's audio is streamed live to Deepgram (model nova-2, language en-GB by default). Each finished sentence:
- goes to the bridge (`POST /api/media/transcript`), which puts it on the Salesforce Voice Call and in the supervisor's
  transcript panel;
- is saved as `transcript.txt` / `transcript.json` with the recording.

The bridge decides agent or customer from the number of whoever spoke (`speakerNumber`): the signed-in agent's
own number or extension means agent. That needs bridge v0.14.0 or later.

1. On the bridge's Settings page (Media), set **Pipeline token** to a long random value and save.
2. In your terminal (never on the command line, never in chat):

       export DEEPGRAM_API_KEY='<new Deepgram key>'
       export BRIDGE_MEDIA_TOKEN='<the same Pipeline token>'

3. Deploy:

       python sf-broadworks-infra/recorder/aws_recorder.py up --app-dir . --bridge-url https://bridge.telqon.com

   The key and token are stored encrypted in SSM Parameter Store (`/telqon-recorder/...`), and only the recorder
   server may read them. `status` shows whether transcription is on.
4. Test with speech, played on the server itself (no SIPp, no home network):

       say -o speech.wav --data-format=LEI16@8000 "Hello, I would like to cancel my order."
       python sf-broadworks-infra/recorder/aws_recorder.py selftest --wav speech.wav

   The output shows `heard ...` lines with Deepgram's words. "bridge has no call" is expected, because there is no
   live agent call for a test. With a real BroadWorks call, the transcript appears on the Voice Call.

Turn it off with `up --no-transcription`. Change the model or language with `--deepgram-model` / `--deepgram-language`.
To use Deepgram's EU endpoint, pass `--deepgram-url` with its address.

## Security notes

- The audio is unencrypted (RTP over the internet) because the SBC has no SRTP. Keep `--allow` to AVC's media
  servers only. Before production, add an IPsec VPN between AVC and AWS.
- The bucket is private and encrypted, and recordings expire after 30 days (`--keep-days`). The server may only
  write recordings, not read or delete them.
- Calls in progress are cut off when the recorder restarts (`up` after a code change). Deploy outside call times.
