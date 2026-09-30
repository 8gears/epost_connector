"""Find the Supplier a letter is from.

Exact evidence first, a model only when it runs out: a tax id is printed on
nearly every invoice and names one legal entity; an alias or IBAN learned from
an earlier letter is as good; a name is fuzzy; a model can resolve what string
comparison cannot (spellings, word order, a brand for its company) but must
choose from the Suppliers that exist, or none.

Every confirmed match is learned (`learn`), so a vendor needs the model once.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import frappe

ALIAS_DOCTYPE = "Supplier Alias"

#: Stripped before comparing names.
LEGAL_SUFFIXES = {
	"ag", "sa", "sarl", "sagl", "gmbh", "ug", "kg", "ohg", "klg", "ltd", "limited", "llc", "inc",
	"corp", "co", "plc", "bv", "nv", "srl", "spa", "oy", "ab", "as",
}  # fmt: skip

#: Above this many Suppliers the model gets the closest ones only.
MAX_CANDIDATES_ALL = 300
MAX_CANDIDATES = 50

SCHEMA = {
	"type": "object",
	"properties": {
		"supplier": {"type": ["string", "null"]},
		"reason": {"type": "string"},
	},
	"required": ["supplier", "reason"],
}

INSTRUCTIONS = """You match the issuer of a supplier invoice to the company's existing suppliers.

- Answer with one supplier name copied exactly from CANDIDATES, or null.
- Match the legal entity that issued the document, not a brand, product or service it sells. A different VAT or UID number means a different supplier: answer null.
- Spelling variants, word order, accents and legal-form suffixes of the same company are the same supplier.
- When unsure, answer null. A human then picks or creates the supplier.
- reason: one short sentence."""


@dataclass
class Match:
	supplier: str | None = None
	source: str | None = None
	reason: str | None = None
	candidates: list[dict] = field(default_factory=list)


def find(letter: Any, values: Any, model: str | None = None) -> Match:
	"""Match on `values` (an extraction log or anything with its fields)."""
	tax_id = tax_key(getattr(values, "vendor_tax_id", None))
	iban = compact(getattr(values, "iban", None))
	names = [n for n in (getattr(values, "vendor_name", None), getattr(letter, "sender_name", None)) if n]

	for check in (
		lambda: _by_alias(names, tax_id, iban),
		lambda: _by_tax_id(tax_id),
		lambda: _by_name(names),
		lambda: _by_iban(iban),
	):
		match = check()
		if match.supplier:
			return match

	candidates = closest(names, limit=3)
	if model and names:
		match = _by_model(model, letter, values, names)
		if match.supplier:
			return match
		reason = match.reason
	else:
		reason = None
	return Match(candidates=candidates, reason=reason)


def find_supplier(letter: Any) -> str | None:
	"""Name-only match, for callers that have no extraction values."""
	names = [n for n in (getattr(letter, "vendor_name", None), getattr(letter, "sender_name", None)) if n]
	return _by_name(names).supplier


def learn(supplier: str, values: Any, confirmed: bool) -> None:
	"""Remember how `supplier` appeared, so the next letter matches without a guess.

	What is learned depends on who decided. A human who mapped or created the
	supplier (`confirmed`) vouches for the whole issuer: its tax id, name and
	IBAN are learned. A model's pick vouches for the name only. A tax id learned
	from a guess would make every later letter carrying it match exactly, and a
	wrong guess would then spread silently, so a model never teaches one.

	Nothing is learned that already points at another supplier: a conflict is
	left for a human rather than resolved by whoever came last.
	"""
	tax_id = getattr(values, "vendor_tax_id", None) if confirmed else None
	iban = getattr(values, "iban", None) if confirmed else None
	name = getattr(values, "vendor_name", None)

	if tax_id and not frappe.db.get_value("Supplier", supplier, "tax_id"):
		key = tax_key(tax_id)
		taken = [row.name for row in _suppliers() if row.tax_id and tax_key(row.tax_id) == key]
		if not taken:
			frappe.db.set_value("Supplier", supplier, "tax_id", tax_id, update_modified=False)

	supplier_name = frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier
	if not name or normalise(name) == normalise(supplier_name):
		return
	aliases = frappe.get_all(ALIAS_DOCTYPE, fields=["alias_name", "supplier"])
	if any(normalise(row.alias_name) == normalise(name) for row in aliases):
		return
	frappe.get_doc(
		{
			"doctype": ALIAS_DOCTYPE,
			"alias_name": name,
			"supplier": supplier,
			"tax_id": tax_id,
			"iban": iban,
			"source": "Manual" if confirmed else "Learned",
		}
	).insert(ignore_permissions=True)


def forget(supplier: str, values: Any) -> None:
	"""Undo what made `supplier` match an issuer a person says it is not.

	Aliases of `supplier` carrying the issuer's name, VAT id or IBAN go, and so
	does the VAT id on the Supplier itself when it is the issuer's: the letter
	prints it, and a person just said it belongs to someone else. A comment on
	the Supplier records what was removed.
	"""
	name = normalise(getattr(values, "vendor_name", None))
	tax_id = tax_key(getattr(values, "vendor_tax_id", None))
	iban = compact(getattr(values, "iban", None))
	removed = []
	for alias in frappe.get_all(
		ALIAS_DOCTYPE, filters={"supplier": supplier}, fields=["name", "alias_name", "tax_id", "iban"]
	):
		if (
			(name and normalise(alias.alias_name) == name)
			or (tax_id and tax_key(alias.tax_id) == tax_id)
			or (iban and compact(alias.iban) == iban)
		):
			frappe.delete_doc(ALIAS_DOCTYPE, alias.name, ignore_permissions=True)
			removed.append(alias.alias_name)
	own_tax_id = frappe.db.get_value("Supplier", supplier, "tax_id")
	if tax_id and own_tax_id and tax_key(own_tax_id) == tax_id:
		frappe.db.set_value("Supplier", supplier, "tax_id", None, update_modified=False)
		removed.append(own_tax_id)
	if removed:
		frappe.get_doc("Supplier", supplier).add_comment(
			"Info",
			frappe._("ePost review: {0} belongs to another supplier; removed from this one.").format(
				", ".join(removed)
			),
		)


def closest(names: list[str], limit: int = 3) -> list[dict]:
	"""Suppliers sharing the most name words with `names`, for a human to choose from."""
	wanted = set().union(*(tokens(n) for n in names)) if names else set()
	if not wanted:
		return []
	scored = []
	for row in _suppliers():
		shared = wanted & tokens(row.supplier_name)
		if shared:
			scored.append((len(shared) / len(wanted | tokens(row.supplier_name)), row.name))
	scored.sort(reverse=True)
	return [{"supplier": name, "score": round(score, 2)} for score, name in scored[:limit]]


def normalise(value: str | None) -> str:
	return "".join(sorted(tokens(value)))


def tokens(value: str | None) -> set[str]:
	"""Lower-case, accent-free words of a name, legal-form suffixes dropped."""
	if not value:
		return set()
	folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
	words = re.sub(r"[^a-z0-9\s]", " ", folded.lower()).split()
	return {w for w in words if w not in LEGAL_SUFFIXES}


def compact(value: str | None) -> str:
	return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


#: What Swiss UIDs carry after the number when the company is VAT-registered.
VAT_SUFFIXES = ("MWST", "TVA", "IVA", "VAT")


def tax_key(value: str | None) -> str:
	"""A VAT or UID number reduced to what identifies the entity.

	A letter prints "CHE-103.727.240 MWST"; the same number may be stored as
	"CHE-103.727.240" or "CHE103727240".
	"""
	key = compact(value)
	for suffix in VAT_SUFFIXES:
		if key.endswith(suffix) and len(key) > len(suffix):
			return key[: -len(suffix)]
	return key


def _by_alias(names: list[str], tax_id: str, iban: str) -> Match:
	rows = frappe.get_all(ALIAS_DOCTYPE, fields=["alias_name", "supplier", "tax_id", "iban"])
	wanted = {normalise(n) for n in names}
	for row in rows:
		if (tax_id and tax_key(row.tax_id) == tax_id) or (iban and compact(row.iban) == iban):
			return Match(row.supplier, "Alias")
	hits = {row.supplier for row in rows if normalise(row.alias_name) in wanted}
	return Match(hits.pop(), "Alias") if len(hits) == 1 else Match()


def _by_tax_id(tax_id: str) -> Match:
	if not tax_id:
		return Match()
	hits = {row.name for row in _suppliers() if row.tax_id and tax_key(row.tax_id) == tax_id}
	return Match(hits.pop(), "Tax ID") if len(hits) == 1 else Match()


def _by_name(names: list[str]) -> Match:
	"""Exact, then same words in any order, then one name's words inside the other's.

	Each step accepts only a unique hit: an ambiguous name is left to a human.
	"""
	suppliers = _suppliers()
	for name in names:
		exact = [row.name for row in suppliers if row.supplier_name == name]
		if len(exact) == 1:
			return Match(exact[0], "Name")
	for name in names:
		wanted = tokens(name)
		if not wanted:
			continue
		same = {row.name for row in suppliers if tokens(row.supplier_name) == wanted}
		if len(same) == 1:
			return Match(same.pop(), "Name")
		if len("".join(wanted)) >= 4:
			contained = {
				row.name
				for row in suppliers
				if (other := tokens(row.supplier_name)) and (wanted <= other or other <= wanted)
			}
			if len(contained) == 1:
				return Match(contained.pop(), "Name")
	return Match()


def _by_iban(iban: str) -> Match:
	if not iban:
		return Match()
	owners = {
		row.party
		for row in frappe.get_all(
			"Bank Account", filters={"party_type": "Supplier"}, fields=["party", "iban"]
		)
		if compact(row.iban) == iban
	}
	return Match(owners.pop(), "IBAN") if len(owners) == 1 else Match()


def _by_model(model: str, letter: Any, values: Any, names: list[str]) -> Match:
	from epost_connector.ai import ask_json

	suppliers = _suppliers()
	if len(suppliers) > MAX_CANDIDATES_ALL:
		keep = {c["supplier"] for c in closest(names, limit=MAX_CANDIDATES)}
		suppliers = [row for row in suppliers if row.name in keep]
	if not suppliers:
		return Match()

	listing = "\n".join(
		f"- {row.name} | tax id {row.tax_id or '-'} | {row.country or '-'}" for row in suppliers
	)
	issuer = "\n".join(
		filter(
			None,
			(
				f"Name: {getattr(values, 'vendor_name', None) or '-'}",
				f"Sender on the envelope: {getattr(letter, 'sender_name', None) or '-'}",
				f"VAT/UID: {getattr(values, 'vendor_tax_id', None) or '-'}",
				f"Country: {getattr(values, 'vendor_country', None) or '-'}",
				f"Address: {getattr(values, 'vendor_address', None) or '-'}",
				f"IBAN: {getattr(values, 'iban', None) or '-'}",
			),
		)
	)
	answer, usage = ask_json(
		model,
		[
			{"role": "system", "content": INSTRUCTIONS},
			{
				"role": "user",
				"content": f"ISSUER\n{issuer}\n\nCANDIDATES (name | tax id | country)\n{listing}",
			},
		],
		SCHEMA,
	)
	_log_call(letter, model, answer, usage)
	if not answer:
		return Match()
	chosen, reason = answer.get("supplier"), str(answer.get("reason") or "")[:300]
	valid = {row.name for row in suppliers}
	if isinstance(chosen, str) and chosen in valid:
		return Match(chosen, "LLM", reason)
	return Match(reason=reason)


def _log_call(letter: Any, model: str, answer: dict | None, usage: dict | None) -> None:
	usage = usage or {}
	frappe.get_doc(
		{
			"doctype": "ePost Extraction Log",
			"letter": getattr(letter, "name", None),
			"kind": "Supplier",
			"status": "Success" if answer else "Failed",
			"model": model,
			"prompt_tokens": usage.get("prompt_tokens") or 0,
			"completion_tokens": usage.get("completion_tokens") or 0,
			"answer": frappe.as_json(answer or {}),
		}
	).insert(ignore_permissions=True)


def _suppliers() -> list:
	return frappe.get_all(
		"Supplier", filters={"disabled": 0}, fields=["name", "supplier_name", "tax_id", "country"]
	)
