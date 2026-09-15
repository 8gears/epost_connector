# Installing epost_connector

A Frappe app, installed like any other. It requires **ERPNext** (declared in `hooks.py` as
`required_apps`), Frappe v16, and Python 3.14 or newer.

## Install

```bash
cd /path/to/frappe-bench
bench get-app https://github.com/8gears/epost_connector
bench --site <your-site> install-app epost_connector
bench --site <your-site> migrate
```

On a containerised bench (`frappe_docker`), add the repo to your image's `apps.json` and install the
app in the site-creation or migration job, as you would for any other Frappe app.

## Configure

You need an ePost API credential. The app supports both schemes the ePost API documents, and
**either one alone is sufficient**:

- **`X-API-KEY`** — a single key. Preferred for a server-side integration: there is no token to
  refresh and it is unaffected by two-factor authentication on the account.
- **Username and password** — the documented password grant
  (`POST /core/latest/tenants` → `POST /core/latest/token` → Bearer). Set a password at
  `https://login.epost.ch/auth/realms/klara/account/` → *Authentication*. **This flow does not work
  on accounts with 2FA enabled**; use an API key there.

If both are configured, both are sent.

Then, in the Desk:

1. Open **ePost Settings**.
2. Paste the API key (or the username and password).
3. Press **Test Connection**. It reports which authentication mode is actually in play.
4. Tick **Enabled**.
5. Press **Sync Now**, or wait for the hourly scheduled sync.

Letters appear under **ePost Letter**, each with its PDF attached.

## What it does, and what it deliberately does not

The sync is **one-way and strictly read-only toward ePost**. The client refuses any request below
`/epost/` that is not a `GET`, checked against the resolved URL, with redirects disabled. There is no
code path that marks a letter read, archives it, moves it or deletes it. This is enforced
structurally and covered by tests, so an ePost mailbox can be shared with another system without the
two racing each other.

ePost is the source of truth. `ePost Letter` rows are copies, keyed on the ePost letter id, and
re-running the sync converges rather than duplicating.

**Seven fields are refreshed from ePost on every sync** — `title`, `sender_name`, `received_at`,
`document_types`, `epost_status`, `letter_type` and `raw_metadata`. Editing one of those in ERPNext
is reverted at the next sync, by design. Everything you set yourself — tags, supplier, status, the
linked Purchase Invoice, the attached PDF — is left alone.

`Create Purchase Invoice` always produces a **draft**. Nothing is submitted or booked automatically.

## Document extraction is an interface, not an implementation

The app ships a no-op extractor. `epost_connector/extraction/` defines the contract:

```python
class LetterExtractor(ABC):
    def extract(self, letter_doc, pdf_bytes) -> ExtractionResult | None: ...
```

`ExtractionResult` carries vendor, invoice number, invoice and due date, currency, net/VAT/gross,
IBAN, QR reference, a summary and a confidence score — every field optional, so a partial read is
legitimate. Register an implementation in `extraction/registry.py`, add it to the `extractor` Select
on `ePost Settings`, and the sync pipeline calls it between download and import. Implementations must
be side-effect free and must return `None` rather than raise when they find nothing usable.

Until an extractor is registered, the Extraction section stays hidden and the amount fields are not
shown — deliberately, so the UI never displays a figure nobody read off the document.

## Verifying a deployment

```bash
bench --site <your-site> execute epost_connector.epost.sync.reconcile
```

It counts the mailbox against `ePost Letter` and returns `in_sync`, with the id sets that differ. Run
it after the first sync; it is the app's health check.

## Running the tests

See [CONTRIBUTING.md](../CONTRIBUTING.md). The suite runs against a mock ePost server and never
contacts the live API. Note that it takes over the site it runs on — it empties the app's tables and
rewrites `ePost Settings` — so do not point it at a site holding real letters.
