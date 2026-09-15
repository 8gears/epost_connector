# Security

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/8gears/epost_connector/security/advisories/new)
on this repository — that is the preferred route and it reaches the maintainers directly.

Please include what you did, what happened, and what you expected. If the issue involves a
credential, describe the class of problem rather than including a working credential or a
step-by-step extraction path.

## What this app handles

It holds an **ePost API credential** and downloads **business correspondence**, so two things matter
more here than in a typical Frappe app.

**The credential** is stored in `ePost Settings.api_key` / `.password`, both `Password` fields, so
Frappe keeps them encrypted in `__Auth` rather than in the doctype table. They are read through
`get_password()` and are never written to a log, an error message or a Sync Log row: the client
redacts the key, the password and both tokens from every error string before it is stored. There is a
test asserting that an API response echoing the key back does not leak it into the Sync Log, the
letter's `sync_error`, or the Error Log.

**The documents** are downloaded to **private** Frappe Files, so they are subject to Frappe's
permission checks rather than being publicly addressable.

## Design decisions with security consequences

**The client is read-only toward ePost, structurally.** Any request below `/epost/` that is not a
`GET` is refused. The guard checks the **resolved** URL, not the string the caller passed, because
`"epost/v2/…"` and `"//epost/v2/…"` reach the same endpoint after joining. Redirects are disabled and
a 3xx is raised as an error with the `Location` redacted — without that, a redirecting gateway could
turn an authentication POST into a write against the mailbox and carry the credential to whatever
answered. Both properties are covered by tests, including one that stands up two loopback servers to
prove the redirect case.

**Downloaded content is validated before it is stored.** A response must be non-empty and contain
`%PDF-` within its first kilobyte. A gateway that answers `200` with an HTML error page therefore
cannot be filed as a letter's PDF.

**Filenames are derived, never taken from the service.** They are built from the letter id plus a
sanitised title, so a service-supplied value shaped like `../../` cannot decide where a file lands.

**Creating a Purchase Invoice checks Purchase Invoice permissions.** The invoice is inserted with
`ignore_permissions`, so the app checks `create` on `Purchase Invoice` explicitly — write access to a
letter is not by itself authority to create an accounting document.

## Supported versions

The app tracks Frappe v16 and current ERPNext. Fixes are made on `main`; there are no maintained
release branches.
