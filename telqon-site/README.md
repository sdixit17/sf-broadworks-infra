# telqon.com (new site)

Replacement for the Wix site: Telqon as the partner that runs service providers' voice platforms (BroadWorks,
AudioCodes, Oracle SBC), delivers contact centres (Zoom, Five9, Amazon Connect) and builds the software on top
(CRM integration layer, provisioning portals, Voice AI). Plain static files, no framework.

    python telqon-site/build.py                       # -> telqon-site/dist/
    python -m http.server 8765 --directory telqon-site/dist

- Pages: `src/pages/*.html`, each starting with `<!-- title: ...\ndescription: ... -->`; wrapped in `src/_layout.html`.
- Home hero: animated architecture diagram (CRM, integration layer, cloud AI, SBC, your telephony) in `src/pages/index.html`.
- Contact form: HubSpot (`--hubspot-portal`, `--hubspot-form-contact`) or, without them, an email to `--contact-email`.
- On AWS (like `website/`): `python telqon-site/aws_telqon_site.py up | status | delete` -> its own t4g.micro + nginx +
  Elastic IP (name `telqon-site`), plain HTTP on the IP; `nginx.conf` here (CSP allows Google Fonts and HubSpot forms).
- Going live: Wix DNS A records `@` and `www` -> the IP (leave MX and `bridge` alone), then HTTPS on the server.
- The privacy notice is a draft to review.
