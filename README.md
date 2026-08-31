# ePost Connector

Syncs the 8gears ePost / Klara digital letterbox (Swiss Post, `api.epost.ch`) into
ERPNext. It lists received letters, downloads each PDF into a private Frappe File,
lets you filter, sort, tag and preview them in Desk, and creates **draft** Purchase
Invoices from them.

Frappe v16 / ERPNext v16, Python 3.11+.

## The one rule: this app is read-only toward ePost

An n8n workflow (`7sytwdFCRMkgSED9`) processes the same letterbox in parallel and
owns the letter lifecycle there. This app must never race it, so it never calls a
state-changing ePost endpoint: no `/read`, `/accept`, `/reject`, `/archive`,
`/restore`, no `DELETE`.

That is enforced in two places, not just documented:

- `epost/client.py` contains **no method** that would perform such a call.
- `ePostClient._guard_read_only` raises `ePostWriteAttempt` on any verb other than
  `GET` below `/epost/`. `POST` survives only for the two `/core/latest/` auth
  endpoints.

**ePost is the source of truth. `ePost Letter` rows are copies.** The sync is
one-way and idempotent, keyed on the ePost letter id, so re-running converges
rather than duplicating.

## Architecture

```
ePost API (api.epost.ch)
	│  GET only
	▼
epost/client.py ......... auth (password + refresh grant), pagination, retries
	▼
epost/sync.py ........... upsert ePost Letter, download PDF -> private File,
	│                     write one ePost Sync Log per run.  Hourly, and on demand.
	▼
extraction/ ............. LetterExtractor interface + registry.
	│                     Ships NoopExtractor only; no LLM implementation.
	▼
epost/import_invoice.py . draft Purchase Invoice, never submitted.
```

Pipeline status on `ePost Letter`, which only ever moves forward:

`New` → `Downloaded` → `Analyzed` → `Imported` / `Ignored`

`Imported` and `Ignored` are terminal. The sync still refreshes their ePost
metadata but never touches their pipeline state, supplier, or extraction fields.

## Install

```bash
bench get-app epost_connector <repo-url>
bench --site <site> install-app epost_connector
bench --site <site> migrate
```

`erpnext` is a required app.

## Configuration

### 1. Set an ePost API password

The Klara/ePost web login may use SSO; the API needs a real password on the
account. Set one at:

`login.epost.ch/auth/realms/klara/account` → **Authentication** → set password.

### 2. Fill in ePost Settings

Desk → **ePost Settings** (or the ePost workspace):

| Field | Notes |
|---|---|
| `Enabled` | Off by default. When off the hourly job does nothing; manual syncs still run. |
| `API Base URL` | `https://api.epost.ch` |
| `Username` / `Password` | The ePost login and the password from step 1. Stored in Frappe's encrypted `__Auth` table. |
| `Tenant ID` / `Company ID` | Press **Fetch Tenants** after saving. One tenant fills them in; several open a picker. A token is only ever valid for one tenant/company pair. |
| `Extractor` | `None` by default. See *Extraction* below. |
| `Company`, `Default Item`, `Default Expense Account`, `Default Cost Center` | Purchase Invoice defaults. |

Press **Test Connection** to authenticate and read the unread count. That is the
only "does it work" check that touches the live API, and it is a `GET`.

### 3. Sync

The sync runs **hourly** on the `hourly_long` scheduler event. `Sync Now` on the
settings form or the letter list enqueues a background run. Every run writes an
`ePost Sync Log` row with counts and per-letter errors; a run left at `Running`
means the worker died before finishing.

A letter that fails is rolled back to a savepoint, recorded on the log and on the
letter's `Sync Error` field, and the run continues.

## Using it

Open a letter to see an inline PDF preview and:

- **Download PDF** — fetches the PDF if it is missing.
- **Analyze** — re-runs the configured extractor.
- **Create Purchase Invoice** — opens a supplier picker, pre-filled with the
  best-effort match, and creates a **draft** invoice.
- **Open PDF** — opens the private file in a new tab.

Tagging is Frappe's native `_user_tags`: the tag area on the form and the Tags
filter in the list sidebar work with no configuration.

### The Purchase Invoice is a starting point

It is created as a draft and **never submitted**. Supplier comes from the dialog,
else the letter's `Supplier`, else a match of `vendor_name` / `sender_name`
against existing Suppliers (exact first, then normalised, ignoring case,
punctuation and legal-form suffixes). An ambiguous match is treated as no match
so you are asked rather than handed a guess.

One item line is created, using `Default Item` if set, otherwise a non-stock line
carrying the letter title. `bill_no`, `bill_date` and `due_date` are filled from
extraction fields when present, and the letter's PDF is linked to the invoice.
Amounts are `0` until an extractor fills them in.

A foreign currency is *not* applied to the draft, because a conversion rate is a
decision only a human can make; the detected currency is noted in `remarks`
instead.

## Extraction

`extraction/` defines the interface and ships **no working extractor**. The
default `NoopExtractor` returns `None`, so extraction fields stay empty and
letters stop at `Downloaded`.

To add one, for example an LLM-backed extractor:

1. Subclass `LetterExtractor` in `extraction/anthropic.py`, set `name`, implement
   `extract(self, letter_doc, pdf_bytes) -> ExtractionResult | None`.
2. Register it in `extraction/registry.py::EXTRACTORS` under a key, and add the
   same key to the `extractor` Select options on `ePost Settings`.

Nothing in the sync engine or the invoice importer changes. Results land in
read-only fields on the letter, so a wrong answer is visible and correctable by a
human before anything is booked.

## Parallel operation

This app runs **alongside** the existing systems; it replaces nothing.

- **Source of truth:** the ePost letterbox. ERPNext rows are copies.
- **Direction:** one-way, ePost → ERPNext. Nothing is written back, ever.
- **The n8n workflow `7sytwdFCRMkgSED9` keeps running, untouched.** This app does
  not mark letters read, so it does not change what that workflow sees.
- **How to stop:** untick `Enabled` in ePost Settings, or uninstall the app. Both
  leave ePost and the n8n workflow intact and authoritative. Nothing this app
  does is destructive toward ePost, so there is no unrecoverable state.

## Reconciliation

Prove the two sides agree, repeatedly, with a number:

```bash
bench --site <site> execute epost_connector.epost.sync.reconcile
```

Prints JSON:

```json
{
  "epost_letters": 412,
  "erpnext_letters": 412,
  "in_both": 412,
  "missing_in_erpnext": [],
  "missing_in_erpnext_count": 0,
  "extra_in_erpnext": [],
  "extra_in_erpnext_count": 0,
  "in_sync": true
}
```

`missing_in_erpnext` is the id set present in ePost but not here — run a sync.
`extra_in_erpnext` is present here but no longer in ePost, which normally means
the letter was deleted or archived in ePost after we copied it; this app will not
remove it. `in_sync` is the acceptance criterion.

It paginates the live letterbox, so it costs one API round trip per 200 letters.

## License

MIT
