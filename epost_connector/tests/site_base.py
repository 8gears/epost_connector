"""Shared setup for the tests that need a Frappe site and a mock ePost.

Not a `test_*.py` module, so the runner does not collect it.

Isolation here cannot lean on the framework's rollback. `LetterSync` commits on
purpose — it is a background job, and a letter that synced should survive the
letter that failed after it — so anything it writes outlives the transaction the
test runs in. Every case therefore purges what it created, before and after, and
`setUp` purging as well means a run that crashed halfway does not poison the next.
"""

from __future__ import annotations

import contextlib
import os
from typing import ClassVar

import frappe
from frappe.tests import IntegrationTestCase

from epost_connector.tests import mock_epost
from epost_connector.tests.mock_epost import COMPANY_ID, TENANT_ID, MockePost

#: Doctypes this app owns outright. Nothing outside them links to their rows.
OWNED_DOCTYPES = ("ePost Letter", "ePost Sync Log")


def purge() -> None:
	"""Remove every row this app owns, plus the drafts it created.

	The rows go out through `frappe.db.delete` rather than `delete_doc`. Per
	document, `delete_doc` scans every doctype with a Link field for references
	and cascades into attachments, which for ten letters and their files costs
	more than the sync being tested — it turned a two-second run into a
	half-minute one, twice per test. Nothing outside these three tables points at
	them, so the scan has nothing to find.

	Purchase Invoices still go the long way: they are ERPNext's, not this app's.
	"""
	for name in frappe.get_all(
		"Purchase Invoice", filters={"remarks": ("like", "%Imported from ePost letter%")}, pluck="name"
	):
		frappe.delete_doc("Purchase Invoice", name, force=True, ignore_permissions=True)

	attachments = frappe.get_all(
		"File",
		filters={"attached_to_doctype": ("in", OWNED_DOCTYPES)},
		fields=["file_url", "is_private"],
	)
	frappe.db.delete("File", {"attached_to_doctype": ("in", OWNED_DOCTYPES)})
	for doctype in OWNED_DOCTYPES:
		frappe.db.delete(doctype)

	# The hostile thumbnail fixture makes the sync log one Error Log row per run.
	# Nothing prunes those between runs, and they had reached four figures on the
	# dev site — noise in a table people read when something is actually wrong.
	frappe.db.delete("Error Log", {"error": ("like", "%epost%")})
	frappe.db.commit()

	# The rows are gone; the bytes they named would otherwise pile up in the
	# site folder for the life of the container.
	for attachment in attachments:
		if not attachment.file_url:
			continue
		with contextlib.suppress(OSError):
			os.remove(frappe.get_site_path(attachment.file_url.lstrip("/")))


class ePostSiteTestCase(IntegrationTestCase):
	"""A site, a mock ePost on the loopback, and settings pointing at it."""

	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")

		self.mock = MockePost().start()
		self.addCleanup(self.mock.stop)
		self.state = self.mock.state

		purge()
		self.addCleanup(purge)
		self.configure_settings()

	#: Every field on the single this class manages, with the value a test that
	#: does not mention it should see. `ePost Settings` outlives the transaction
	#: — `configure_settings` commits — so a field left off this list keeps
	#: whatever the previous test set and silently changes the next one's
	#: fixture. `default_item_code` did exactly that.
	SETTINGS_BASELINE: ClassVar[dict] = {
		"enabled": 1,
		"extractor": "None",
		"company": None,
		"default_item_code": None,
		"default_expense_account": None,
		"default_cost_center": None,
		"last_sync_at": None,
		"last_sync_status": None,
	}

	def configure_settings(self, **overrides) -> None:
		"""Point `ePost Settings` at the mock, resetting everything else.

		The tenant/company pair is set explicitly so the client does not spend a
		call resolving it, and so a test that changes the tenant list does not
		change what every other test authenticates as.
		"""
		settings = frappe.get_doc("ePost Settings")
		settings.update(
			{
				**self.SETTINGS_BASELINE,
				"api_base_url": self.mock.base_url,
				"username": mock_epost.USERNAME,
				"tenant_id": TENANT_ID,
				"company_id": str(COMPANY_ID),
				**overrides,
			}
		)
		settings.password = mock_epost.PASSWORD
		settings.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.clear_document_cache("ePost Settings", "ePost Settings")

	def letter_doc(self, letter_id: str):
		return frappe.get_doc("ePost Letter", {"letter_id": letter_id})

	def letter_ids(self) -> set[str]:
		return set(frappe.get_all("ePost Letter", pluck="letter_id"))


@contextlib.contextmanager
def registered_extractor(key: str, extractor_class):
	"""Register `extractor_class` and select it, for the duration of the block.

	The setting is written straight to the field rather than through the doc.
	`ePost Settings.extractor` is a Select, and Frappe refuses any value not
	among the options on the DocType — which is the point of the field, and
	exactly what a test-only extractor cannot satisfy without editing the schema.
	"""
	from epost_connector.extraction import registry

	previous = frappe.db.get_single_value("ePost Settings", "extractor")
	registry.EXTRACTORS[key] = extractor_class
	try:
		frappe.db.set_single_value("ePost Settings", "extractor", key)
		frappe.db.commit()
		frappe.clear_document_cache("ePost Settings", "ePost Settings")
		yield
	finally:
		frappe.db.set_single_value("ePost Settings", "extractor", previous)
		frappe.db.commit()
		frappe.clear_document_cache("ePost Settings", "ePost Settings")
		registry.EXTRACTORS.pop(key, None)


# ----------------------------------------------------------------------
# ERPNext master data for the tests that book something
# ----------------------------------------------------------------------

TEST_COMPANY = "ePost Test Company"
TEST_COMPANY_ABBR = "EPTC"
TEST_CURRENCY = "CHF"


def ensure_erpnext_fixtures() -> None:
	"""The reference data ERPNext's setup wizard installs, and `install-app` does not.

	A site built with `bench new-site --install-app erpnext` and no wizard run has
	no UOMs, no Item Groups and no Warehouse Types at all. Two of those records
	are reached by the code under test, and each fails in a way that reads like
	an app bug rather than a missing fixture:

	  * Warehouse Type "Transit" — `Company.on_update` creates default warehouses
		that link to it, so creating a Company dies partway through, after the
		chart of accounts and before the cost centres, leaving a half-built
		company behind;
	  * UOM "Nos" — `import_invoice._build_item` names it on every invoice line,
		so every import fails with `Could not find Row #1: UOM: Nos`.

	Only these two are created. A fixture file that quietly grows to stand in for
	the whole setup wizard stops being evidence of anything.
	"""
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)

	if not frappe.db.exists("UOM", "Nos"):
		frappe.get_doc({"doctype": "UOM", "uom_name": "Nos", "must_be_whole_number": 1}).insert(
			ignore_permissions=True
		)

	frappe.db.commit()


def ensure_fiscal_years(*years: int) -> None:
	"""Fiscal Years covering the dates the tests post on.

	Also the setup wizard's job. Without one, a Purchase Invoice cannot even be
	*saved* — the year is resolved during validation, not at submission — so a
	draft-only importer still needs the years its documents fall in.
	"""
	for year in years:
		if frappe.db.exists("Fiscal Year", str(year)):
			continue
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": str(year),
				"year_start_date": f"{year}-01-01",
				"year_end_date": f"{year}-12-31",
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()


def ensure_company() -> str:
	"""A company with a chart of accounts, created once and then reused.

	Committed rather than rolled back: building a chart of accounts costs real
	seconds and nothing in these tests depends on it being fresh.
	"""
	ensure_erpnext_fixtures()
	if frappe.db.exists("Company", TEST_COMPANY):
		return TEST_COMPANY

	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": TEST_COMPANY,
			"abbr": TEST_COMPANY_ABBR,
			"default_currency": TEST_CURRENCY,
			"country": "Switzerland",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return TEST_COMPANY


def expense_account(company: str) -> str:
	"""A leaf account a Purchase Invoice line can be booked to.

	`account_type` is preferred but not relied on: the Standard chart leaves it
	empty on most leaves, so `root_type` is what actually identifies an expense.
	"""
	name = frappe.db.get_value(
		"Account", {"company": company, "account_type": "Expense Account", "is_group": 0}, "name"
	) or frappe.db.get_value("Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name")

	if not name:
		raise AssertionError(f"{company} has no leaf expense account; the chart of accounts is missing")
	return name


def cost_center(company: str) -> str:
	"""A leaf cost centre, created here if the company was set up without one."""
	name = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	if name:
		return name

	# ERPNext's own routine, rather than a second implementation of it: the root
	# centre needs `ignore_mandatory` because it is the one Cost Center allowed
	# to have no parent.
	frappe.get_doc("Company", company).create_default_cost_center()
	frappe.db.commit()
	return frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")


def ensure_service_item(item_code: str = "ePost Test Service") -> str:
	"""A non-stock service Item for the `default_item_code` path.

	Non-stock deliberately: the invoice line the importer builds has no warehouse
	and no stock movement behind it, and a stock Item would demand both.
	"""
	if frappe.db.exists("Item", item_code):
		return item_code

	ensure_erpnext_fixtures()
	group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	if not group:
		root = frappe.db.get_value("Item Group", {"is_group": 1}, "name")
		group = (
			frappe.get_doc(
				{
					"doctype": "Item Group",
					"item_group_name": "ePost Test Services",
					"is_group": 0,
					**({"parent_item_group": root} if root else {}),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": group,
			"stock_uom": "Nos",
			"is_stock_item": 0,
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return item.name


def ensure_supplier(supplier_name: str) -> str:
	if frappe.db.exists("Supplier", supplier_name):
		return supplier_name

	supplier = frappe.get_doc(
		{"doctype": "Supplier", "supplier_name": supplier_name, "supplier_group": ensure_supplier_group()}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return supplier.name


def ensure_supplier_group() -> str:
	existing = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
	if existing:
		return existing

	group = frappe.get_doc(
		{"doctype": "Supplier Group", "supplier_group_name": "ePost Test Group", "is_group": 0}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	return group.name
