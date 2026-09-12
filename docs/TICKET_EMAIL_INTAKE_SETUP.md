# Email-to-Draft-Ticket intake setup

Anyone can raise a Service Ticket by sending an email to a dedicated Injaaz address —
no login required. The system parses the email and creates a `status='draft'` ticket
that a supervisor/admin reviews and converts into a real ticket under
**Tickets → Draft Tickets (Email)**. See the in-app guide at
**Tickets → Settings → Email a Ticket** for the format shared with requesters.

**Current path (no paid Brevo inbound plan):** the app reads
`contact@kynvera.net` through Microsoft Graph (Entra app registration) and
creates drafts from unread inbox mail. **Later path:** paid Brevo inbound
parsing (webhook) — keep the code and `reply.kynvera.net` MX; unset the Graph
secret when that is live.

A Mailjet Parse API webhook (`inbound_email_webhook()`) also exists for accounts
that use Mailjet instead.

---

## How it works today

```
requester email → Microsoft 365 (contact@kynvera.net)
  → app Graph poller (every ~60s)
  → draft Ticket created, supervisors notified
```

Code: `module_ticketing/inbound_mailbox.py` — `poll_intake_mailbox()`,
`message_to_intake()` → `_process_inbound_email_intake()`.

Every processed message is logged to `ticket_email_intakes`.

Until Graph credentials are set, sending mail only fills Outlook.

### Current setup (Microsoft Graph)

1. In Entra, register app **Kynvera ticket intake** (this organizational directory only).
2. **API permissions** → Add → **Microsoft Graph** → **Application** permissions →
   **Mail.ReadWrite** → Add. Then **Grant admin consent**.
3. **Certificates & secrets** → **New client secret**. Copy the **Value** once.
4. **Disable** the Exchange rule `Bcc ticket intake to Brevo` until a paid
   Brevo inbound-parse plan exists.
5. Set on Render and in `.env`:

```env
TICKET_INTAKE_EMAIL=contact@kynvera.net
TICKET_INTAKE_GRAPH_TENANT_ID=<Directory (tenant) ID>
TICKET_INTAKE_GRAPH_CLIENT_ID=<Application (client) ID>
TICKET_INTAKE_GRAPH_CLIENT_SECRET=<client secret Value>
TICKET_INTAKE_GRAPH_MAILBOX=contact@kynvera.net
```

6. Restart the app. Logs should show `Ticket intake Graph poller started`.
7. Send a new email to `contact@kynvera.net`. Within about a minute it appears
   under **Tickets → Email drafts** (production: https://operations.kynvera.net).
   The poller reads recent inbox mail (last 24 hours) and de-duplicates on
   `Message-Id`, so opening the message in Outlook — or a local app polling the
   same mailbox — does not stop live from creating the draft.

Recommended lock-down (Exchange Online PowerShell), so the app can read only
this mailbox:

```powershell
New-ApplicationAccessPolicy -AppId <Application (client) ID> `
  -PolicyScopeGroupId contact@kynvera.net `
  -AccessRight RestrictAccess `
  -Description "Kynvera ticket intake mailbox only"
```

---

## Later: Brevo inbound parsing (paid)

This keeps `contact@kynvera.net` on Microsoft 365. A copy of each inbound
message is Bcc'd to a parse-only subdomain whose MX points at Brevo. Use this
only after inbound email parsing is on the Brevo plan; then unset
`TICKET_INTAKE_GRAPH_CLIENT_SECRET` so the Graph poller stops.

### 1. Add a dedicated parse subdomain in DNS

Brevo requires the receiving domain to differ from your sending domain, so use
something not used for anything else, e.g. `reply.kynvera.net`. In the DNS provider
for `kynvera.net` (GoDaddy, per the domain's SPF record), add:

| Type | Host    | Priority | Value |
|------|---------|----------|-------|
| MX   | `reply` | 10       | `inbound1.sendinblue.com.` |
| MX   | `reply` | 20       | `inbound2.sendinblue.com.` |

This only affects `*.reply.kynvera.net` — it does not touch the root domain's
existing Microsoft 365 MX record, so `contact@kynvera.net` keeps working normally.
DNS propagation can take a few hours — do this early.

### 2. Pick the receiving address on that subdomain

Any mailbox on the new subdomain works, e.g. `intake@reply.kynvera.net`. This is
never seen by requesters — it's purely the internal target for the forwarded copy.
`TICKET_INTAKE_EMAIL` should stay `contact@kynvera.net`, since that's still what's
shown in-app (see step 6).

### 3. Add an Exchange mail-flow rule to Bcc a copy

In the Microsoft 365 admin center → Exchange admin center → **Mail flow → Rules**
→ **Add a rule**:

- Condition: *The recipient is* `contact@kynvera.net`
- Action: *Add recipients* → **Bcc the message to...** → `intake@reply.kynvera.net`

This leaves normal delivery to the `contact@kynvera.net` mailbox untouched and
additionally routes a copy out to the parse subdomain, where Brevo picks it up.

Microsoft 365 blocks automatic external forwarding by default as an anti-phishing
measure. If the Bcc copy doesn't arrive, your tenant admin will need to allow this
specific rule to forward externally (an exception in the **Anti-spam outbound
policy** / **Remote Domains** settings, or setting `SCL: -1` on the rule) — this is
a common extra step, not a sign the rule is misconfigured.

### 4. Verify the domain with Brevo

In the Brevo dashboard, add and verify `reply.kynvera.net` as a receiving domain for
inbound parsing (Transactional → Settings → Inbound Parsing, or via the domain
verification flow). Verification confirms the MX records from step 1 resolved.

### 5. Generate a webhook secret

Pick a long random string, e.g.:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add it to Render env vars (and `.env` locally) as:

```env
TICKET_INBOUND_WEBHOOK_SECRET=<the generated value>
```

The webhook URL is then:

```
https://<your-app-host>/tickets/api/inbound-email-brevo/<TICKET_INBOUND_WEBHOOK_SECRET>
```

Requests to that path with the wrong (or missing) secret get a `404` — there is no
signature check on Brevo's inbound payload itself, so this secret in the URL path is
the only thing guarding the endpoint.

### 6. Register the inbound webhook with Brevo

Using your `BREVO_API_KEY`:

```bash
curl -X POST https://api.brevo.com/v3/webhooks \
  -H "api-key: $BREVO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "inbound",
    "events": ["inboundEmailProcessed"],
    "url": "https://<your-app-host>/tickets/api/inbound-email-brevo/<TICKET_INBOUND_WEBHOOK_SECRET>",
    "domain": "reply.kynvera.net",
    "description": "Ticket intake draft creation"
  }'
```

Brevo will POST parsed JSON to `url` whenever mail lands on `domain`.

### 7. `TICKET_INTAKE_EMAIL` stays as the public address

`TICKET_INTAKE_EMAIL` should be `contact@kynvera.net` — that's what's shown on the
Email template info button and under **Tickets → Settings → Email a Ticket**. It's
already the default, so this only matters if it was previously overridden.

```env
TICKET_INTAKE_EMAIL=contact@kynvera.net
```

### 8. Test end-to-end

1. Send an email to `contact@kynvera.net` following the format shown by the
   **Email template** info button (or **Tickets → Settings → Email a Ticket**).
2. Within a few seconds, check **Tickets → Draft Tickets (Email)** as a supervisor/admin.
3. If nothing appears, check the `ticket_email_intakes` table (or app logs), and confirm
   the Bcc copy actually left Exchange (message trace in the Microsoft 365 admin center)
   before assuming Brevo or the webhook is at fault.

**Local simulation** (no Brevo needed):

```bash
curl -s -X POST "http://localhost:5002/tickets/api/inbound-email-brevo/<your-secret>" \
  -H "Content-Type: application/json" \
  -d '{
    "items": [{
      "MessageId": "test-001@example.com",
      "From": {"Address": "requester@example.com", "Name": "Requester Name"},
      "Recipients": [{"Address": "intake@reply.kynvera.net", "Name": null}],
      "Subject": "[Project Alpha] Plumbing / high — Leaking pipe",
      "RawTextBody": "Property: Building A\nZone: Floor 2\nUnit: 201\n\nWater leaking under sink.",
      "Attachments": []
    }]
  }'
```

---

## Notes

- **Attachments:** Brevo doesn't inline attachment bytes in the webhook payload like
  Mailjet does — each item's `Attachments` entries carry a `DownloadToken`, and
  `_brevo_inbound_attachments()` fetches each one individually from
  `GET https://api.brevo.com/v3/inbound/attachments/{token}` (auth: `api-key` header,
  needs `BREVO_API_KEY` set). Only image types (png/jpg/jpeg/gif/webp/heic/heif) are
  stored as `TicketImage` rows; other file types are noted on the ticket but not stored.
- **Batching:** a single Brevo webhook call can carry multiple emails in its `items`
  array; `inbound_email_webhook_brevo()` processes each independently, so one bad
  item doesn't block the rest.
- **Unknown senders:** if the sender's email doesn't match a registered `User`, the
  draft is attributed to the system **"Email Intake"** account — the real sender's
  name/email is still shown on the draft.
- **De-duplication:** `_process_inbound_email_intake()` de-duplicates on the email's
  `MessageId`, so any retried webhook delivery won't create duplicate drafts.
