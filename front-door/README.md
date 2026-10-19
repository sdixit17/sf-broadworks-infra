# Front door: bridge.telqon.com on S3 + CloudFront

Salesforce loads the phone from the address in Telqon's provider package: `https://bridge.telqon.com/`. The front
door serves that page for **every** customer from S3 through CloudFront. There is no server to fail, and it is
served close to agents.

It also holds a small **directory**: one file per customer, keyed by the Salesforce org ID. When Salesforce starts
the phone, the phone looks its org up and from then on talks only to that customer's own bridge.

```
Salesforce (any customer) ── loads phone ──► bridge.telqon.com  (CloudFront + S3: phone page + directory)
                                                 │ /directory/<org ID>.json → {"bridge": "https://acme.bridge.telqon.com"}
                                                 ▼
                              acme.bridge.telqon.com, test.bridge.telqon.com, ...  (one bridge per customer)
```

How the parts of the system split:

- The bridges serve their own Settings page (`/admin.html`) and supervisor page.
- Each bridge accepts the phone from the front door (`BRIDGE_FRONT_DOOR`).
- Each bridge refuses phones from any Salesforce org other than its own.

## Commands

    python front-door/front_door.py up                   # create/update: certificate, bucket, CloudFront
    python front-door/front_door.py deploy --build       # publish the phone page (needs Node.js; app checkout via --app-dir)
    python front-door/front_door.py add --org 00D... --bridge https://acme.bridge.telqon.com --name "Acme Ltd"
    python front-door/front_door.py remove --org 00D...
    python front-door/front_door.py list
    python front-door/front_door.py status
    python front-door/front_door.py delete

What `up` creates:

- a private, encrypted, versioned S3 bucket `telqon-front-door-<account>` in eu-west-2;
- an ACM certificate for bridge.telqon.com in us-east-1 (CloudFront requires that region);
- a CloudFront distribution: HTTPS only, TLS 1.2+, HSTS, nosniff, no X-Frame-Options (Salesforce frames the phone).

Only CloudFront can read the bucket.

Cost: a few US cents a month at this size.

## Switching bridge.telqon.com to the front door (one time)

Your current bridge moves to **test.bridge.telqon.com**. It keeps answering on bridge.telqon.com too until the last
step, so nothing breaks in between, and you can go back by restoring the Wix A record.

1. **Wix:** add an A record. Host `test.bridge`, value `51.24.196.195`.
2. **Bridge v0.12.0 on both names:**

       python test-server/aws_test_server.py up --app-dir ../sf-broadworks-bridge --domain test.bridge.telqon.com,bridge.telqon.com

   Check that https://test.bridge.telqon.com/admin.html opens.
3. **Front door:** run `python front-door/front_door.py up`. It prints a CNAME record for the certificate.
   - Add it in Wix and keep it there; it renews the certificate.
   - Then run `up` again (or `up --wait` to wait). The second run creates CloudFront and prints its address, `dxxxx.cloudfront.net`.
4. **Publish the phone page and add your org:**

       python front-door/front_door.py deploy --build --app-dir ../sf-broadworks-bridge
       python front-door/front_door.py add --org 00DgK00000cA7yP --bridge https://test.bridge.telqon.com --name "Telqon test"

   Check `https://dxxxx.cloudfront.net/directory/00DgK00000cA7yP.json`. It should show the bridge address.
5. **Salesforce:**
   1. Deploy the Trusted URLs. This adds `https://*.bridge.telqon.com` for the Supervisor tab and Settings page:

          sf project deploy start --metadata-dir salesforce/setup/trusted-url --target-org scv --wait 10

      Run it from the app folder.
   2. In App Builder, set the Supervisor page's **Bridge URL** to `https://test.bridge.telqon.com`.
6. **Wix switch:** delete the A record with host `bridge`. Add a **CNAME** record with host `bridge` and value
   `dxxxx.cloudfront.net`. Agents loading the phone during the change get either the old server or the front
   door; both work.
7. **Test:** reload the Service Console, sign in to the phone, take a call.
   `python front-door/front_door.py status` should show `HTTPS via bridge.telqon.com: OK`.
8. **Tidy up**, a day later: run `python test-server/aws_test_server.py up --app-dir ../sf-broadworks-bridge`.
   The default domain is now test.bridge.telqon.com only.

## A new customer

1. Create their bridge:

       python test-server/aws_test_server.py up --name bridge-acme --domain acme.bridge.telqon.com --app-dir ../sf-broadworks-bridge

   Then add the A record `acme.bridge` in Wix. Production will use ECS instead.
2. On https://acme.bridge.telqon.com/admin.html, fill in their BroadWorks, Salesforce org ID and key, then run "Set up from BroadWorks".
3. `python front-door/front_door.py add --org <their org ID> --bridge https://acme.bridge.telqon.com --name "Acme Ltd"`.
4. In their Salesforce: install the Telqon package, deploy or add the Trusted URLs, set up the contact center, and assign the users.

## Updating the phone page

After an app release with connector changes, run `deploy --build`. All customers get the new phone page at once.
CloudFront's cache is refreshed by the deploy.
