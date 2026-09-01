# CLAUDE.md

A Frappe app that copies the Swiss Post ePost / KLARA digital letterbox into
ERPNext: it syncs letters and their PDFs one way, and can turn one into a
**draft** Purchase Invoice. It is **read-only toward ePost** — an n8n workflow
owns the letter lifecycle there, and a write from here would race it.

Run the suite on the Frappe Manager bench:
`fm shell epost -c "cd /workspace/frappe-bench && bench --site epost.localhost run-tests --app epost_connector"`

**Read [CONTRIBUTING.md](CONTRIBUTING.md) before editing a fixture JSON or
running the suite.** It documents five traps that each fail silently and far
from their cause — including that the suite empties tables and rewrites a
Single, so it owns the site while it runs and two of them at once produce
failures that look like app defects and are not.
