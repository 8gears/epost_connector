# Contributing

Five things about this repo that cost someone hours to find out. None of them
announce themselves — each one fails silently, or fails somewhere far from the
cause.

## A hand-edited fixture needs its `modified` bumped, or migrate skips it

Frappe decides whether to import a fixture JSON — a Workspace, a Dashboard,
anything exported as a doc — by comparing the file's `modified` timestamp
against the record already in the database. If they match, the file is
considered unchanged and skipped. No error, no warning, and `bench migrate`
reports a clean run.

So editing `workspace/epost/epost.json` by hand and running migrate changes
nothing at all. Bump `modified` to the current time in the same edit. The
escape hatch is to make the change through Desk or REST with `developer_mode`
on: Frappe writes the file back with a fresh timestamp for you, which is why
this never bites anyone working through the UI.

Proven by resetting the database row's `modified` to 2020 and re-running a
plain `bench migrate` — the content then synced.

## A fixture may omit a mandatory field, and migrate will not mind

`Workspace.type` is a required Select in Frappe 16 —
`Workspace\nLink\nURL`, default `Workspace`, `reqd: 1`
(`frappe/desk/doctype/workspace/workspace.json`). Our `epost.json` did not have
it. Migrate imported the workspace anyway and left `type` NULL, because fixture
import inserts the record rather than validating it the way a save would.

Nothing complains until something *saves* that workspace — rearranging a block
in the Desk editor, or a REST `PUT` — and then it dies with
`MandatoryError: Value missing for Workspace: Type` on a document nobody
believes they changed. Fixed in 62d7b49 by putting `"type": "Workspace"` in the
file; the same save that failed before then succeeded.

Same shape as the `modified` trap above, and the same lesson: a fixture JSON is
not validated on the way in, so a field the doctype requires can be missing for
as long as nobody saves. When hand-writing one, check the doctype for `reqd: 1`
fields rather than copying an existing file and trusting it.

## The app directory looks empty from the host, and that means nothing

Under Frappe Manager, `apps/epost_connector` inside the bench is a bind mount
of this repo. From macOS it lists as an empty directory, because the mount only
exists inside the container's namespace. `docker exec … ls` is the truth.

The failure this actually causes looks unrelated: every request 500s with
`ModuleNotFoundError: No module named 'epost_connector'` raised from
`frappe.init` → `setup_module_map`, and the traceback is topped by a misleading
`AttributeError: is_ajax` because the error handler runs before `frappe.init`
finished. The cause is a web process that started *before* `bench pip install -e`
wrote the `.pth` file, and a `.pth` is read once at interpreter startup. Restart
the containers — `fm restart epost --supervisor`. Do not reinstall the app, and
do not touch `sites/apps.txt`; both are already correct.

## `install-app erpnext` is not a set-up ERPNext

A site built with `bench new-site --install-app erpnext` and no setup wizard run
has **no UOMs, no Item Groups, no Warehouse Types and no Fiscal Years**. The
records this app reaches are created in `tests/site_base.py::ensure_erpnext_fixtures`
and the helpers beside it, deliberately as a short list rather than a stand-in
for the wizard.

Each gap fails in a way that reads like a bug in this app rather than a missing
fixture, which is the reason for writing it down:

- no Warehouse Type `Transit` → creating a Company dies partway through, after
  the chart of accounts and before the cost centres, leaving a half-built
  company that later runs happily skip because the Company now exists;
- no UOM `Nos` → every invoice import fails with `Could not find Row #1: UOM: Nos`;
- no Fiscal Year → a Purchase Invoice cannot even be *saved*, because the year
  is resolved during validation and not at submission.

## The test suite owns the site while it runs

`tests/site_base.py` empties `ePost Letter`, `ePost Sync Log` and this app's
`Error Log` rows before and after every test, and rewrites the `ePost Settings`
single in `setUp`. That is deliberate — `LetterSync` commits on purpose, so the
framework's rollback cannot isolate these tests — but it makes the suite
**exclusive to one process per site**.

Two suites against one site, or a suite running while someone browses seeded
demo data, produces failures that look like app defects and are not:
`DoesNotExistError` on rows another process deleted, `TimestampMismatchError`
from two `settings.save()` calls racing, and `Lock wait timeout exceeded` on
`tabSingles … FOR UPDATE` when both processes reach `configure_settings` at
once. Take turns, or give each worker its own site.
