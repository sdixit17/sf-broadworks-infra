"""AWS Lambda: Chime SDK call analytics output (Kinesis Data Stream) -> the bridge's media API.

    Transcript lines (Transcribe Call Analytics UtteranceEvent, or plain Transcribe TranscriptEvent)
        -> POST {BRIDGE_URL}/api/media/transcript
    Finished recordings (S3RecordingSink)
        -> POST {BRIDGE_URL}/api/media/recording

AWS doesn't know the BroadWorks call ID, so each post carries "numbers": every phone number and SIP
address found in the event's metadata (Voice Connector from/to and the SIPREC participants). The bridge
matches the call by them (bridge/media.py find_call).

Environment:
    BRIDGE_URL      https://<public bridge address>   (on a Mac: a Cloudflare Tunnel address)
    SECRET_ARN      Secrets Manager secret holding the bridge's media pipeline token
    SEND_PARTIALS   1 (default) = also send lines still being spoken, for the supervisor's live view
    LOG_RAW         number of raw events to log per container (default 20), to see AWS's exact shapes
    CHANNEL_ROLES   for plain Transcribe events: "ch_0=agent,ch_1=customer" (default)

Errors: a call the bridge doesn't know (404) or a bad event is logged and skipped; the bridge being
unreachable or failing (5xx) raises, so Lambda retries the batch.
"""
import base64
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger()
log.setLevel(logging.INFO)

_token = None
_raw_logged = 0


class Retry(Exception):
    """The bridge is down or failing: let Lambda retry the batch."""


def token():
    global _token
    if _token is None:
        if os.environ.get("MEDIA_TOKEN"):                 # tests / local runs
            _token = os.environ["MEDIA_TOKEN"]
        else:
            import boto3
            _token = boto3.client("secretsmanager").get_secret_value(
                SecretId=os.environ["SECRET_ARN"])["SecretString"].strip()
    return _token


def post(path, body):
    url = os.environ["BRIDGE_URL"].rstrip("/") + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode(errors="replace")
        if e.code >= 500 or e.code == 429:
            raise Retry(f"{path} HTTP {e.code}: {detail}")
        log.warning("%s HTTP %s: %s (body %s)", path, e.code, detail, json.dumps(body)[:300])
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Retry(f"bridge not reachable at {url}: {e}")


# ------------------------------------------------------------------ parsing
NUM_RE = re.compile(r"(?:tel:|sip:|sips:)?(\+?\d[\d\-. ]{2,}\d)")


def numbers_in(event):
    """Phone numbers / SIP user parts from the event metadata (from/to, SIPREC participants)."""
    md = event.get("metadata") or {}
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            md = {"raw": md}
    found = []

    def add(v):
        if not v:
            return
        for m in NUM_RE.finditer(str(v)):
            n = re.sub(r"[\-. ]", "", m.group(1))
            if n not in found:
                found.append(n)

    for k in ("fromNumber", "toNumber", "from", "to"):
        add(md.get(k))
    one = md.get("oneTimeMetadata") or {}
    if isinstance(one, str):
        try:
            one = json.loads(one)
        except ValueError:
            one = {"siprecMetadata": one}
    siprec = one.get("siprecMetadata") or md.get("siprecMetadata") or ""
    for aor in re.findall(r'aor="([^"]+)"', siprec) + re.findall(r"<(?:\w+:)?(?:aor|nameID)[^>]*>([^<]+)<", siprec):
        add(aor)
    return found


def event_ms(event):
    t = event.get("time")
    if not t:
        return None
    try:
        return int(datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def roles():
    out = {}
    for part in (os.environ.get("CHANNEL_ROLES") or "ch_0=agent,ch_1=customer").split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def transcript_lines(event):
    """-> list of {speaker, text, final, startMs, endMs, segmentId}"""
    end_ms = event_ms(event)
    lines = []
    u = event.get("UtteranceEvent")
    if u:                                            # Transcribe Call Analytics: AGENT / CUSTOMER roles
        role = {"AGENT": "agent", "CUSTOMER": "customer"}.get((u.get("ParticipantRole") or "").upper())
        text = (u.get("Transcript") or "").strip()
        if role and text:
            span = max(0, int(u.get("EndOffsetMillis") or 0) - int(u.get("BeginOffsetMillis") or 0))
            lines.append({"speaker": role, "text": text, "final": not u.get("IsPartial", False),
                          "endMs": end_ms, "startMs": end_ms - span if end_ms else None,
                          "segmentId": u.get("UtteranceId")})
        return lines
    te = event.get("TranscriptEvent")
    if te:                                           # plain Transcribe: speaker by channel
        r = roles()
        for res in ((te.get("Transcript") or {}).get("Results") or []):
            alts = res.get("Alternatives") or []
            text = (alts[0].get("Transcript") if alts else "") or ""
            speaker = r.get(res.get("ChannelId") or "")
            if speaker and text.strip():
                span = int(((res.get("EndTime") or 0) - (res.get("StartTime") or 0)) * 1000)
                lines.append({"speaker": speaker, "text": text.strip(), "final": not res.get("IsPartial", False),
                              "endMs": end_ms, "startMs": end_ms - span if end_ms else None,
                              "segmentId": res.get("ResultId")})
    return lines


S3_URI = re.compile(r"s3://[a-z0-9.\-]+/[^\s\"']+")
S3_CONSOLE = re.compile(r"https://[^\"'\s]*console\.aws\.amazon\.com/s3/object/([a-z0-9.\-]+)\?[^\"'\s]*prefix=([^\"'&\s]+)")


def recording_location(event):
    """An S3 location of a finished recording, if this event announces one."""
    kind = str(event.get("detail-type") or event.get("detailType") or "")
    blob = json.dumps(event)
    if "ecording" not in kind and "s3MediaObject" not in blob:
        return None
    m = S3_URI.search(blob)
    if m:
        return m.group(0)
    m = S3_CONSOLE.search(blob)
    if m:
        from urllib.parse import unquote
        return f"s3://{m.group(1)}/{unquote(m.group(2))}"
    return None


# ------------------------------------------------------------------ handler
def handle_event(event):
    global _raw_logged
    if _raw_logged < int(os.environ.get("LOG_RAW", "20")):
        _raw_logged += 1
        log.info("raw event: %s", json.dumps(event)[:4000])
    nums = numbers_in(event)
    loc = recording_location(event)
    if loc:
        body = {"numbers": nums, "location": loc}
        md = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        dur = event.get("durationInSeconds") or md.get("durationInSeconds")
        if dur:
            body["durationSec"] = dur
        status, _ = post("/api/media/recording", body)
        log.info("recording %s -> HTTP %s (numbers %s)", loc, status, nums)
        return
    lines = transcript_lines(event)
    if os.environ.get("SEND_PARTIALS", "1") != "1":
        lines = [ln for ln in lines if ln["final"]]
    if lines:
        status, _ = post("/api/media/transcript",
                         {"numbers": nums, "segments": [{k: v for k, v in ln.items() if v is not None} for ln in lines]})
        if status != 200:
            log.info("transcript line not taken (HTTP %s), numbers %s", status, nums)


def lambda_handler(event, context):
    """Kinesis batch. Bad records are skipped; a bridge outage raises so the batch is retried."""
    failures = []
    for rec in event.get("Records", []):
        try:
            data = json.loads(base64.b64decode(rec["kinesis"]["data"]))
        except (KeyError, ValueError) as e:
            log.warning("unreadable record: %s", e)
            continue
        try:
            handle_event(data)
        except Retry as e:
            log.error("%s", e)
            failures.append({"itemIdentifier": rec["kinesis"]["sequenceNumber"]})
            break                                   # keep order: retry from this record on
        except Exception:
            log.exception("event skipped")
    return {"batchItemFailures": failures}
