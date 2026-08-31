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
	│  GET only — inbox listing AND the eArchive folders
	▼
epost/client.py ......... auth (password + refresh grant), pagination, retries,
	│                     PDF validation, credential redaction
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

### The sync covers the inbox *and* the archive

The inbox listing is not the whole letterbox. A letter archived by a user, or by
the n8n workflow, leaves the inbox listing entirely, and an inbox-only sync would
quietly drop it from ERPNext. So each run reads:

1. `GET /epost/v2/letters` — the inbox,
2. `GET /epost/v2/archives/letters` — Storage root,
3. `GET /epost/v2/archives/letters?directory-id=…` for every folder from
   `GET /epost/v2/archives/directories`.

Results are deduplicated on letter id, and where the letter was found is written
to the `ePost Folder` field (`INBOX`, `Storage`, or the folder name). Folder
names are NFC-normalised, because the service can return them decomposed and the
same folder would otherwise read as two different strings.

### Two facts the spec gets wrong, and how the client handles them

The vendored spec and a working reference client against the live service
disagree in a few places. Where they do, the client accepts both rather than
picking a winner it cannot verify:

- **`offset` may or may not work.** The spec documents it (spec:11641-11651); the
  reference client never sends it and treats the listing as a fixed window. The
  client pages on `offset` and watches the ids coming back. If a full page
  contributes nothing new, `offset` is being ignored, and it says so with
  `ePostPaginationLimit` naming how many letters are reachable, instead of
  looping forever or silently truncating. The letters inside the window are still
  synced and the run is marked `Partial`, not `Failed`.
- **`/letters/inbox/count`** returns a bare integer per the spec and `{"count": n}`
  per the reference. Both are accepted.
- **Search** takes `value` per the spec and `keyword` per the reference. Both are
  sent.
- **The sender name** has no field in the `Letter` schema at all. In practice
  `description` carries it ("Invoice from …"), so that is read first.

### Downloaded bytes are validated before they are stored

The content endpoint has been observed answering `200` with a gateway HTML error
page, and with zero bytes. Both look like success to everything except an
inspection of the body, and either one stored under a letter's name is
indistinguishable from the letter afterwards. So a download is accepted only if
it is non-empty and contains `%PDF-` within its first kilobyte (searched rather
than anchored, so a byte-order mark is tolerated). On failure no File is created,
the letter keeps its metadata row, and the reason lands in `Sync Error`.

File names are derived from the letter id and a sanitised title, never from
service-supplied strings: Frappe builds the stored path from `file_name`, so a
value shaped like `../..` would otherwise decide where the file lands.

Credentials are stripped from error text before it reaches an `ePost Sync Log`
row or the Error Log, because an upstream gateway can quote the request that
produced the error back at you, Authorization header and all.

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
  "listing_truncated": null,
  "in_sync": true
}
```

It counts the same ground the sync covers, inbox and archive together, so an
archived letter is not reported as missing.

`missing_in_erpnext` is the id set present in ePost but not here — run a sync.
`extra_in_erpnext` is present here but no longer in ePost, which normally means
the letter was deleted in ePost after we copied it; this app will not remove it.
`listing_truncated` is non-null when the service would not page past its window,
in which case the comparison was made against a partial listing and `in_sync` is
false regardless of the diffs — a clean diff over missing data is not agreement.
`in_sync` is the acceptance criterion.

## License

MIT
