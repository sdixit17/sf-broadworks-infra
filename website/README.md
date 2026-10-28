# Marketing website (sample)

Static site for the channel-first AI voice offer: home, AI Front Desk, CRM Calling, Call Insight, Partners,
Packages, ROI calculator, Contact. Brand name is a setting (default **Telcom-Company**); the main call to action is
**Call our AI now** (the Voice AI demo number).

    python website/build.py --demo-number "+44 ..."        # local build into website/dist/ (open dist/index.html)
    python website/aws_website.py up --demo-number "+44 ..."   # on AWS: http://<Elastic IP>/
    python website/aws_website.py status | delete

- Pages: `src/pages/*.html` (first comment = title + description), wrapped in `src/_layout.html`.
  Tokens: `{{brand}}`, `{{icon:name}}` (icons in build.py). Styles `src/assets/site.css`, behaviour `site.js`.
- `config.js` (written at build): demo number, HubSpot portal + form GUIDs (forms post to HubSpot's form API),
  or a contact email (mailto) when HubSpot isn't set; with neither, forms show a "preview" thank-you.
- Without `--demo-number` every "Call our AI" button goes to the demo form instead.
- Server: t4g.micro + 1 GB swap + nginx (t4g.nano stalled for 25+ min installing nginx on first boot, 28 Oct) (`nginx.conf`: security headers, CSP allows only our files + api.hsforms.com),
  releases switched atomically by `deploy.sh`, last five kept. HTTP only until a domain exists (then certbot/Caddy).
- Figures on the site: "~1 s" first reply (measured on the Voice AI, 27 Oct), the monthly report numbers are
  marked illustrative; no customer logos or testimonials (none yet).
