#!/usr/bin/env python3
"""Build the marketing website into website/dist/ (static files: any web server or S3 can serve them).

    python website/build.py [--brand "Telcom-Company"] [--demo-number "+44 20 0000 0000"]
                            [--hubspot-portal 123456 --hubspot-form-demo <guid> --hubspot-form-partner <guid>]
                            [--contact-email sales@example.com]

Pages live in src/pages/*.html: a first comment with `title:` and `description:`, then the page's sections. They are
wrapped in src/_layout.html (header, footer). Tokens: {{brand}}, {{icon:name}}. Settings the browser needs (demo
number, HubSpot form IDs) go into dist/config.js, so changing them doesn't touch the pages.
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

# Line icons (24x24, stroke): kept inline so the site makes no third-party requests.
_I = {
    "bot": '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 17h6"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.5 2.7.6a2 2 0 0 1 1.7 2z"/>',
    "spark": '<path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 15l.7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7z"/>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0 5 5l-8.4 8.4a2.1 2.1 0 0 1-3-3z"/><path d="M14.7 6.3L17 4"/>',
    "signal": '<path d="M2 20h.01M7 20v-4M12 20v-8M17 20V8M22 4v16"/>',
    "clinic": '<path d="M3 21h18M5 21V7l7-4 7 4v14"/><path d="M12 9v6M9 12h6"/>',
    "building": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M9 7h.01M15 7h.01M9 11h.01M15 11h.01M9 15h.01M15 15h.01M10 21v-3h4v3"/>',
    "cart": '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.7 12.4a2 2 0 0 0 2 1.6h8.6a2 2 0 0 0 2-1.6L22 7H6"/>',
    "headset": '<path d="M3 14v-2a9 9 0 0 1 18 0v2"/><rect x="2" y="14" width="5" height="6" rx="2"/><rect x="17" y="14" width="5" height="6" rx="2"/>',
    "ear": '<path d="M6 8.5a6 6 0 1 1 12 0c0 3-2 4-3 5.5s-1 3.5-3 4.5-4-.5-4-2"/><path d="M9 9a3 3 0 0 1 6 0"/>',
    "id": '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="M6 16c.6-1.4 1.7-2 3-2s2.4.6 3 2M15 10h3M15 13h3"/>',
    "list": '<path d="M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    "ticket": '<path d="M3 8V6a1 1 0 0 1 1-1h16a1 1 0 0 1 1 1v2a2 2 0 0 0 0 4v2 2a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-2a2 2 0 0 0 0-4z"/><path d="M13 5v2M13 11v2M13 17v2"/>',
    "handoff": '<path d="M16 3h5v5M21 3l-7 7M8 21H3v-5M3 21l7-7"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    "cloud": '<path d="M17.5 19H7a5 5 0 1 1 .9-9.9A6 6 0 0 1 19 10.5a4.3 4.3 0 0 1-1.5 8.5z"/>',
    "hub": '<circle cx="12" cy="12" r="3"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M14.1 9.9L17.6 6.4M9.9 14.1l-3.5 3.5M14.1 14.1l3.5 3.5"/>',
    "sliders": '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
    "log": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/>',
    "mic": '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4"/>',
    "text": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M8 9h8M8 13h5"/>',
    "smile": '<circle cx="12" cy="12" r="9"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/>',
    "bulb": '<path d="M9 18h6M10 22h4M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/>',
    "chart": '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
    "tag": '<path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "pound": '<path d="M17 20H7c2 0 3-2 3-4V8a4 4 0 0 1 7.4-2M6 13h8"/>',
    "rocket": '<path d="M5 15c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.2 2.2 0 0 0-2.9-.1z"/><path d="M12 15l-3-3a22 22 0 0 1 2-4A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22.4 22.4 0 0 1-4 2z"/><path d="M9 12H4s.6-3 2-4c1.6-1.1 5 0 5 0M12 15v5s3-.6 4-2c1.1-1.6 0-5 0-5"/>',
    "book": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/>',
    "users": '<circle cx="9" cy="8" r="4"/><path d="M2 21v-1a6 6 0 0 1 12 0v1M16 3.1a4 4 0 0 1 0 7.8M22 21v-1a6 6 0 0 0-4-5.7"/>',
}


def icon(name):
    paths = _I.get(name)
    if paths is None:
        raise SystemExit(f"unknown icon {name}")
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{paths}</svg>')


def page(path, layout, brand, build):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"\s*<!--\s*title:\s*(.*?)\ndescription:\s*(.*?)\s*-->\s*", text, re.S)
    if not m:
        raise SystemExit(f"{path.name}: start with <!-- title: ...\\ndescription: ... -->")
    title, desc, body = m.group(1).strip(), m.group(2).strip(), text[m.end():]
    name = path.stem
    out = layout.replace("{{content}}", body)
    out = out.replace("{{title}}", html.escape(title, quote=True)).replace("{{description}}", html.escape(desc, quote=True))
    out = re.sub(r"\{\{active:([\w-]+)\}\}", lambda mm: ' aria-current="page"' if mm.group(1) == name else "", out)
    out = re.sub(r"\{\{icon:([\w-]+)\}\}", lambda mm: icon(mm.group(1)), out)
    out = out.replace("{{brand}}", html.escape(brand)).replace("{{build}}", build)
    left = re.findall(r"\{\{[^}]*\}\}", out)
    if left:
        raise SystemExit(f"{path.name}: unknown tokens {sorted(set(left))}")
    return out


def build(brand="Telcom-Company", demo_number="", hubspot_portal="", hubspot_forms=None, contact_email="", dist=DIST):
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(SRC / "assets", dist / "assets")
    stamp = str(int(time.time()))
    layout = (SRC / "_layout.html").read_text(encoding="utf-8")
    pages = sorted((SRC / "pages").glob("*.html"))
    for p in pages:
        (dist / p.name).write_text(page(p, layout, brand, stamp), encoding="utf-8")
    cfg = {"brand": brand, "demoNumber": demo_number, "hubspotPortal": hubspot_portal,
           "hubspotForms": {k: v for k, v in (hubspot_forms or {}).items() if v}, "contactEmail": contact_email}
    (dist / "config.js").write_text("window.SITE = " + json.dumps(cfg, indent=2) + ";\n", encoding="utf-8")
    (dist / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")
    return [p.name for p in pages]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brand", default="Telcom-Company")
    ap.add_argument("--demo-number", default="", help="the phone number that reaches the AI demo, e.g. +44 20 0000 0000")
    ap.add_argument("--hubspot-portal", default="")
    ap.add_argument("--hubspot-form-demo", default="")
    ap.add_argument("--hubspot-form-partner", default="")
    ap.add_argument("--contact-email", default="")
    a = ap.parse_args()
    names = build(a.brand, a.demo_number, a.hubspot_portal,
                  {"demo": a.hubspot_form_demo, "partner": a.hubspot_form_partner}, a.contact_email)
    print(f"built {len(names)} pages into {DIST}: {', '.join(names)}")


if __name__ == "__main__":
    main()
