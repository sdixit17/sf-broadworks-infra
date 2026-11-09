# Plane (project management) on AWS

Open-source Plane, self-hosted for the project team, from one command on your Mac (infra `.venv` active):

    python plane/aws_plane.py up                                              # first: HTTP on the server's IP
    python plane/aws_plane.py up --domain plane.<your domain> --cert-email <email>   # after the DNS A record points at the IP
    python plane/aws_plane.py status

1. Run `up` without `--domain`. It prints the fixed IP (about 10 minutes the first time: Plane pulls its images).
2. In DNS (Wix), add an A record `plane` -> that IP. Then `up --domain plane.<your domain> --cert-email <email>`:
   Plane's proxy gets a Let's Encrypt certificate and answers on HTTPS.
3. Open `https://plane.<your domain>/god-mode/`, create the instance admin (keep the password safe).
4. Create the workspace, invite Atul and Shubham, and create a user **Mr Builder**. Sign in as Mr Builder >
   Profile settings > API tokens > create one. Give it to Claude through the secure credential form in a cloud session
   (never in chat): the host is `plane.<your domain>`, header `X-API-Key`.

Cost: t4g.medium + 30 GB disk + IP, roughly $35/month. Data lives in Docker volumes on the server's disk: snapshot
the volume before upgrading `--release` (EC2 console > Volumes > Create snapshot).
