#!/usr/bin/env python3
"""Build telqon.com into telqon-site/dist/ (plain static files: S3, CloudFront, nginx or any static host can serve them).

    python telqon-site/build.py [--hubspot-portal 123456 --hubspot-form-contact <guid>] [--contact-email info@telqon.com]
                                [--site-url https://www.telqon.com]

Pages live in src/pages/*.html and start with a comment:

    <!-- title: Page title
    description: One sentence for search results. -->

They are wrapped in src/_layout.html. Tokens: {{icon:name}}, {{year}}, {{active:<page>}} (marks the menu item).
Browser settings (HubSpot form, contact email) go into dist/config.js, so changing them doesn't touch the pages.
Without a HubSpot form the contact form opens an email to the contact address instead.
"""
import argparse
import html
import json
import re
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC, DIST = HERE / "src", HERE / "dist"

# Line icons (24x24, stroke), inline so the site makes no third-party requests.
_I = {
    "server": '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01M11 7.5h6M11 16.5h6"/>',
    "headset": '<path d="M3 14v-2a9 9 0 0 1 18 0v2"/><rect x="2" y="14" width="5" height="6" rx="2"/><rect x="17" y="14" width="5" height="6" rx="2"/>',
    "code": '<path d="M8 6l-6 6 6 6M16 6l6 6-6 6M14 4l-4 16"/>',
    "compass": '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 17h6"/>',
    "hub": '<circle cx="12" cy="12" r="3"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><circle cx="5" cy="5" r="2"/><path d="M14.1 9.9l3.5-3.5M9.9 14.1l-3.5 3.5M14.1 14.1l3.5 3.5M9.9 9.9L6.4 6.4"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "wave": '<path d="M2 12h2l2-6 3 12 3-9 2 6 2-3h6"/>',
    "chart": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "screen": '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/><circle cx="12" cy="10.5" r="2.5"/>',
    "users": '<circle cx="9" cy="8" r="4"/><path d="M2 21v-1a6 6 0 0 1 12 0v1M16 3.1a4 4 0 0 1 0 7.8M22 21v-1a6 6 0 0 0-4-5.7"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5M3 21v-5h5"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "pin": '<path d="M12 22s7-6.3 7-12a7 7 0 0 0-14 0c0 5.7 7 12 7 12z"/><circle cx="12" cy="10" r="2.5"/>',
    "check": '<path d="M20 6L9 17l-5-5"/>',
    "cloud": '<path d="M17.5 19H7a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10.5a4.3 4.3 0 0 1-1.5 8.5z"/>',
}


def icon(name):
    paths = _I.get(name)
    if paths is None:
        raise SystemExit(f"unknown icon {name}")
    return ('<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


def page(path, layout, stamp, site_url):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\s*<!--\s*title:\s*(.*?)\ndescription:\s*(.*?)\s*-->\s*", text, re.S)
    if not m:
        raise SystemExit(f"{path.name}: start with <!-- title: ...\\ndescription: ... -->")
    title, desc, body = m.group(1).strip(), m.group(2).strip(), text[m.end():]
    name = path.stem
    out = layout.replace("{{content}}", body)
    out = out.replace("{{title}}", html.escape(title, quote=True))
    out = out.replace("{{description}}", html.escape(desc, quote=True))
    canonical = site_url.rstrip("/") + "/" + ("" if name == "index" else f"{name}.html")
    out = out.replace("{{canonical}}", html.escape(canonical, quote=True))
    section = name.split("-")[0] if name.startswith("insights") else name
    out = re.sub(r"\{\{active:([\w-]+)\}\}", lambda mm: ' aria-current="page"' if mm.group(1) == section else "", out)
    out = re.sub(r"\{\{icon:([\w-]+)\}\}", lambda mm: icon(mm.group(1)), out)
    out = out.replace("{{year}}", time.strftime("%Y")).replace("{{build}}", stamp)
    left = re.findall(r"\{\{[^}]*\}\}", out)
    if left:
        raise SystemExit(f"{path.name}: unknown tokens {sorted(set(left))}")
    return out


def build(hubspot_portal="", hubspot_form="", contact_email="info@telqon.com", site_url="https://www.telqon.com",
          dist=DIST):
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(SRC / "assets", dist / "assets")
    stamp = str(int(time.time()))
    layout = (SRC / "_layout.html").read_text(encoding="utf-8")
    pages = sorted((SRC / "pages").glob("*.html"))
    for p in pages:
        (dist / p.name).write_text(page(p, layout, stamp, site_url), encoding="utf-8")
    cfg = {"hubspotPortal": hubspot_portal, "hubspotForm": hubspot_form, "contactEmail": contact_email}
    (dist / "config.js").write_text("window.SITE = " + json.dumps(cfg, indent=2) + ";\n", encoding="utf-8")
    base = site_url.rstrip("/")
    (dist / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n", encoding="utf-8")
    urls = "".join(f"  <url><loc>{base}/{'' if p.stem == 'index' else p.name}</loc></url>\n" for p in pages)
    (dist / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                      f"{urls}</urlset>\n", encoding="utf-8")
    return [p.name for p in pages]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hubspot-portal", default="", help="HubSpot portal ID for the contact form")
    ap.add_argument("--hubspot-form-contact", default="", help="HubSpot form GUID for the contact form")
    ap.add_argument("--contact-email", default="info@telqon.com")
    ap.add_argument("--site-url", default="https://www.telqon.com")
    a = ap.parse_args()
    names = build(a.hubspot_portal, a.hubspot_form_contact, a.contact_email, a.site_url)
    print(f"built {len(names)} pages into {DIST}: {', '.join(names)}")


if __name__ == "__main__":
    main()
