"""Finding the Supplier a letter is from, and remembering it.

The model is never called for real: it is replaced by a function returning a
fixed answer, so what is tested is the order of the checks and what is done
with an answer, including one that names a supplier that does not exist.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe

from epost_connector.inbox import matching
from epost_connector.tests.site_base import ensure_supplier, ePostSiteTestCase

MEILEN = "Gemeindesteueramt Meilen"
CARD = "Cornèr Banca SA"
KLARA = "KLARA Business AG"
ALL = [MEILEN, CARD, KLARA]


class MatchingTestCase(ePostSiteTestCase):
	def setUp(self) -> None:
		super().setUp()
		for name in ALL:
			ensure_supplier(name)
		self.addCleanup(self.cleanup)
		self.cleanup()
		self.letter = frappe._dict(name=None, sender_name=None)

	def cleanup(self) -> None:
		frappe.db.delete("Supplier Alias", {"supplier": ("in", ALL)})
		frappe.db.delete("Supplier Alias", {"alias_name": "Shared Brand"})
		frappe.db.delete("ePost Extraction Log", {"kind": "Supplier", "letter": ("is", "not set")})
		for name in ALL:
			frappe.db.set_value("Supplier", name, "tax_id", None)
		frappe.db.commit()

	def find(self, model=None, **values):
		return matching.find(self.letter, frappe._dict(values), model=model)


class ExactMatchTest(MatchingTestCase):
	def test_word_order_and_punctuation_do_not_matter(self):
		self.assertEqual(self.find(vendor_name="Meilen Gemeindesteueramt").supplier, MEILEN)
		# The name as the letter from DEV prints it: one word more, reordered.
		self.assertEqual(self.find(vendor_name="Gemeinde Meilen, Gemeindesteueramt").supplier, MEILEN)

	def test_accents_do_not_matter(self):
		self.assertEqual(self.find(vendor_name="Corner Banca SA").supplier, CARD)

	def test_the_tax_id_matches_whatever_the_name_says(self):
		frappe.db.set_value("Supplier", KLARA, "tax_id", "CHE-111.222.333 MWST")

		match = self.find(vendor_name="Something Else Entirely", vendor_tax_id="CHE111222333")

		self.assertEqual((match.supplier, match.source), (KLARA, "Tax ID"))

	def test_an_alias_wins_before_the_name(self):
		frappe.get_doc({"doctype": "Supplier Alias", "alias_name": "Cornercard", "supplier": CARD}).insert()

		match = self.find(vendor_name="Cornercard")

		self.assertEqual((match.supplier, match.source), (CARD, "Alias"))


class ModelPickTest(MatchingTestCase):
	def test_the_model_is_asked_only_when_exact_checks_fail(self):
		with patch("epost_connector.ai.ask_json") as ask:
			self.find(model="any", vendor_name="Meilen Gemeindesteueramt")
		ask.assert_not_called()

	def test_a_supplier_the_model_picks_from_the_list_is_taken(self):
		answer = {"supplier": CARD, "reason": "Cornèrcard is Cornèr Banca's card brand."}
		with patch("epost_connector.ai.ask_json", return_value=(answer, None)):
			match = self.find(model="any", vendor_name="Cornèrcard Services")

		self.assertEqual((match.supplier, match.source), (CARD, "LLM"))

	def test_a_supplier_that_does_not_exist_is_refused(self):
		answer = {"supplier": "Invented Supplier AG", "reason": "x"}
		with patch("epost_connector.ai.ask_json", return_value=(answer, None)):
			match = self.find(model="any", vendor_name="Unknown Vendor Ltd")

		self.assertIsNone(match.supplier)

	def test_none_keeps_the_letter_waiting_with_the_models_reason(self):
		answer = {"supplier": None, "reason": "ePost Service AG has a different UID than KLARA."}
		with patch("epost_connector.ai.ask_json", return_value=(answer, None)):
			match = self.find(model="any", vendor_name="ePost Service AG", vendor_tax_id="CHE-103.727.240")

		self.assertIsNone(match.supplier)
		self.assertIn("different UID", match.reason)

	def test_every_model_call_is_logged(self):
		with patch(
			"epost_connector.ai.ask_json",
			return_value=({"supplier": None, "reason": "x"}, {"prompt_tokens": 7}),
		):
			self.find(model="any", vendor_name="Unknown Vendor Ltd")

		log = frappe.get_last_doc("ePost Extraction Log", filters={"kind": "Supplier"})
		self.assertEqual(log.prompt_tokens, 7)


class LearningTest(MatchingTestCase):
	def test_a_confirmed_match_teaches_the_tax_id_and_the_name(self):
		values = frappe._dict(vendor_name="ePost Klara Services", vendor_tax_id="CHE-999.888.777 MWST")

		matching.learn(KLARA, values, confirmed=True)

		self.assertEqual(frappe.db.get_value("Supplier", KLARA, "tax_id"), "CHE-999.888.777 MWST")
		self.assertEqual(self.find(vendor_name="ePost Klara Services").supplier, KLARA)
		# The alias carries the tax id too, so it answers before the Supplier's own.
		self.assertEqual(self.find(vendor_name="?", vendor_tax_id="CHE999888777").supplier, KLARA)

	def test_an_existing_tax_id_is_not_overwritten(self):
		frappe.db.set_value("Supplier", KLARA, "tax_id", "CHE-111.111.111")

		matching.learn(
			KLARA, frappe._dict(vendor_name=KLARA, vendor_tax_id="CHE-999.999.999"), confirmed=True
		)

		self.assertEqual(frappe.db.get_value("Supplier", KLARA, "tax_id"), "CHE-111.111.111")

	def test_the_suppliers_own_name_is_not_stored_as_an_alias(self):
		matching.learn(KLARA, frappe._dict(vendor_name="KLARA Business AG"), confirmed=True)

		self.assertEqual(frappe.db.count("Supplier Alias", {"supplier": KLARA}), 0)

	def test_a_model_pick_teaches_the_name_but_never_a_tax_id(self):
		"""A wrong guess with a tax id would make every later letter match it exactly."""
		matching.learn(
			KLARA,
			frappe._dict(vendor_name="Klara Scan Service", vendor_tax_id="CHE-555.555.555"),
			confirmed=False,
		)

		self.assertFalse(frappe.db.get_value("Supplier", KLARA, "tax_id"))
		alias = frappe.get_all(
			"Supplier Alias", filters={"supplier": KLARA}, fields=["alias_name", "tax_id", "source"]
		)
		self.assertEqual(
			[(a.alias_name, a.tax_id, a.source) for a in alias], [("Klara Scan Service", None, "Learned")]
		)

	def test_a_name_already_mapped_to_another_supplier_is_not_remapped(self):
		matching.learn(CARD, frappe._dict(vendor_name="Shared Brand"), confirmed=True)

		matching.learn(KLARA, frappe._dict(vendor_name="Shared Brand"), confirmed=True)

		self.assertEqual(
			frappe.get_all("Supplier Alias", filters={"alias_name": "Shared Brand"}, pluck="supplier"), [CARD]
		)

	def test_a_tax_id_another_supplier_holds_is_not_copied(self):
		frappe.db.set_value("Supplier", CARD, "tax_id", "CHE-444.444.444")

		matching.learn(
			KLARA, frappe._dict(vendor_name="x", vendor_tax_id="CHE-444.444.444 MWST"), confirmed=True
		)

		self.assertFalse(frappe.db.get_value("Supplier", KLARA, "tax_id"))
