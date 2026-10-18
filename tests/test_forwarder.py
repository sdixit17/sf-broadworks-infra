"""Checks for the media pipeline forwarder (no AWS needed).

    python tests/test_forwarder.py
"""
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

fails = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("AWS forwarder (Chime call analytics -> bridge)")
spec = importlib.util.spec_from_file_location("forwarder", Path(__file__).resolve().parent.parent / "media-pipeline/forwarder/handler.py")
fw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fw)
SIPREC_MD = ('<recording xmlns="urn:ietf:params:xml:ns:recording:1"><participant participant_id="p1">'
             '<nameID aor="sip:+441743539042@avcone.co.uk"><name>Webex Test</name></nameID></participant>'
             '<participant participant_id="p2"><nameID aor="tel:+447404509725"/></participant></recording>')
utter = {"time": "2026-10-18T10:00:05.000Z", "service-type": "CallAnalytics", "detail-type": "Transcribe",
         "metadata": {"voiceConnectorId": "abc", "callId": "sip-call-1", "fromNumber": "+447404509725",
                      "toNumber": "+441244298273", "oneTimeMetadata": {"siprecMetadata": SIPREC_MD}},
         "UtteranceEvent": {"UtteranceId": "u1", "IsPartial": False, "ParticipantRole": "CUSTOMER",
                            "BeginOffsetMillis": 3000, "EndOffsetMillis": 4500, "Transcript": "My router keeps dropping."}}
rec_event = {"time": "2026-10-18T10:03:00Z", "detail-type": "Recording", "metadata": utter["metadata"],
             "s3MediaObjectConsoleUrl": "https://s3.console.aws.amazon.com/s3/object/telqon-media-recordings-1?region=eu-west-2&prefix=abc%2Fsip-call-1.wav"}
sent = []
os.environ.update(BRIDGE_URL="https://bridge.example", MEDIA_TOKEN="t" * 40)
with mock.patch.object(fw, "post", side_effect=lambda path, body: sent.append((path, body)) or (200, {})):
    batch = {"Records": [{"kinesis": {"sequenceNumber": str(i), "data": base64.b64encode(json.dumps(ev).encode()).decode()}}
                         for i, ev in enumerate([utter, rec_event, {"time": "x", "UtteranceEvent": {"ParticipantRole": "AGENT", "Transcript": ""}}])]}
    out = fw.lambda_handler(batch, None)
check(out == {"batchItemFailures": []} and len(sent) == 2, f"two posts from three records (empty line skipped): {sent}")
path, body = sent[0] if sent else ("", {})
seg = (body.get("segments") or [{}])[0]
check(path == "/api/media/transcript" and seg.get("speaker") == "customer" and seg.get("final") is True
      and seg.get("text") == "My router keeps dropping." and seg.get("endMs") - seg.get("startMs") == 1500,
      "utterance -> customer line with timing")
check({"+447404509725", "+441244298273", "+441743539042"} <= set(body.get("numbers", [])),
      f"numbers from Voice Connector metadata and SIPREC participants: {body.get('numbers')}")
check(sent[1:] and sent[1][0] == "/api/media/recording"
      and sent[1][1]["location"] == "s3://telqon-media-recordings-1/abc/sip-call-1.wav", "recording notice -> s3 location")
with mock.patch.object(fw, "post", side_effect=fw.Retry("bridge down")):
    out = fw.lambda_handler(batch, None)
check(out == {"batchItemFailures": [{"itemIdentifier": "0"}]}, "bridge down -> batch retried from the failed record")

print("\nFAIL:\n  " + "\n  ".join(fails) if fails else "\nALL FORWARDER CHECKS PASSED")
sys.exit(1 if fails else 0)
