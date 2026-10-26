# Telqon Voice AI server

Runs the app's `voiceai/` (sf-broadworks-bridge, v0.21.0 or later) on EC2. It registers with BroadWorks through
the SBC's access side as the Voice AI user's phone and answers calls the Auto Attendant forwards to it. The full
setup (BroadWorks user, SBC, bridge settings, Salesforce) is in the app's `voiceai/README.md`.

    export VOICEAI_SIP_PASSWORD='...'   # Voice AI user's Authentication password
    export VOICEAI_BRIDGE_TOKEN='...'   # bridge Settings > Voice AI > Voice AI server token
    python voiceai/aws_voiceai.py up --allow <SBC IP> --registrar <SBC IP>[:port] --domain avcone.co.uk \
        --sip-user <line/port user> [--auth-user <name>] --queue <queue number> \
        --bridge-url https://bridge.telqon.com [--greeting greeting.wav]
    python voiceai/aws_voiceai.py status | logs [--since '30 min ago'] | delete

`up` creates the following, or deploys new code if they exist:
- EC2 t4g.small, Elastic IP;
- security group: SIP 5060 UDP and RTP 30000-30999 UDP, from `--allow` only;
- IAM role: Session Manager, bundles, its SSM settings.

Settings live in SSM under `/telqon-voiceai/`:
- plain: registrar, domain, sip_user, auth_user, queue, bridge_url;
- SecureString: sip_password, bridge_token.

The service reads them at start (`VOICEAI_PARAMS`). After a later `up`, only the options you pass change.

On the server:
- the service is `telqon-voiceai` (systemd, user `voiceai`, CAP_NET_BIND_SERVICE);
- code in `/opt/telqon-voiceai/current`;
- greeting at `/var/lib/telqon-voiceai/greeting.wav`;
- with no greeting, it plays a beep.
