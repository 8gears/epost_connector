"""Read a letter with a Frappe Flow model.

The PDF's text layer goes to the model, which answers with the invoice fields as
JSON. Only a letter without a usable text layer, such as a scan with no OCR, is
sent as the PDF itself, because that costs several times more tokens.

The confidence stored on the letter is not the model's word for it. It starts
from the model's own estimate and loses points for every check the answer fails:
amounts that do not add up, a VAT breakdown that does not match the totals,
dates out of order, an unknown currency. Each check is kept in `raw["checks"]`,
so a reviewer sees why a letter scored low.
"""

from __future__ import annotations

import base64
import io
from typing import Any

import frappe
from frappe.utils import flt, getdate

from epost_connector.ai import ask_json
from epost_connector.extraction.base import DOCUMENT_KINDS, ExtractionResult, LetterExtractor

#: Below this many characters of text the PDF is sent as a file instead.
MIN_TEXT_CHARS = 200
MAX_TEXT_CHARS = 20000

#: Allowed rounding difference between amounts that must add up.
AMOUNT_TOLERANCE = 0.05

PENALTIES = {
	"totals_add_up": 0.3,
	"breakdown_matches_totals": 0.2,
	"breakdown_rates_consistent": 0.1,
	"dates_valid": 0.1,
	"currency_known": 0.1,
	"gross_present_on_invoice": 0.3,
	"amounts_only_on_financial_documents": 0.2,
}

FINANCIAL_KINDS = {"Invoice", "Credit Note", "Reminder", "Receipt"}

_NUMBER = {"type": ["number", "null"]}
_TEXT = {"type": ["string", "null"]}
SCHEMA = {
	"type": "object",
	"properties": {
		"document_kind": {"type": "string", "enum": list(DOCUMENT_KINDS)},
		"vendor_name": _TEXT,
		"vendor_tax_id": _TEXT,
		"vendor_country": _TEXT,
		"iban": _TEXT,
		"qr_reference": _TEXT,
		"invoice_number": _TEXT,
		"invoice_date": _TEXT,
		"due_date": _TEXT,
		"currency": _TEXT,
		"net_amount": _NUMBER,
		"vat_amount": _NUMBER,
		"gross_amount": _NUMBER,
		"vat_breakdown": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"rate": {"type": "number"},
					"net": {"type": "number"},
					"vat": {"type": "number"},
				},
				"required": ["rate", "net", "vat"],
			},
		},
		"line_items": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {"description": {"type": "string"}, "net": _NUMBER, "vat_rate": _NUMBER},
				"required": ["description"],
			},
		},
		"summary": _TEXT,
		"confidence": {"type": "number"},
	},
	"required": ["document_kind", "confidence"],
}

INSTRUCTIONS = """You read letters that arrive in a company's digital letterbox and extract supplier invoice data.

- document_kind: what the letter is.
- vendor_*: the party that issued the letter, not the recipient. vendor_country is the English country name of the vendor's address, e.g. "Switzerland", "Germany", "United States". vendor_tax_id is its VAT or UID number exactly as printed, e.g. "CHE-123.456.789 MWST".
- Dates as YYYY-MM-DD. Currency as an ISO 4217 code.
- Amounts as plain numbers in the invoice currency. net_amount excludes VAT, gross_amount is the total payable.
- vat_breakdown: one row per VAT rate the invoice shows, in percent. A foreign invoice that charges no VAT has one row with rate 0.
- line_items: at most 20, as printed.
- qr_reference: the Swiss QR-bill reference, if any. iban: the account the invoice asks to be paid to.
- Use null for anything the letter does not state. Never guess amounts.
- summary: one sentence on what the letter is about.
- confidence: 0 to 1, how sure you are the fields are right."""


class FlowExtractor(LetterExtractor):
	name = "Flow"

	def extract(self, letter_doc: Any, pdf_bytes: bytes) -> ExtractionResult | None:
		model = frappe.db.get_single_value("ePost Settings", "flow_model")
		if not model:
			return None

		text = _pdf_text(pdf_bytes)
		answer, usage = ask_json(model, _messages(letter_doc, text, pdf_bytes), SCHEMA)
		if not answer:
			return None

		result = _to_result(answer)
		checks = _checks(result)
		penalty = sum(PENALTIES[name] for name, passed in checks.items() if passed is False)
		result.confidence = round(max(0.0, min(flt(answer.get("confidence")), 1.0) - penalty), 4)
		result.raw = {
			"answer": answer,
			"checks": checks,
			"model": model,
			"usage": usage,
			"input": "text" if len(text) >= MIN_TEXT_CHARS else "pdf",
		}
		return result


def _messages(letter_doc: Any, text: str, pdf_bytes: bytes) -> list[dict]:
	header = f"Letter title: {getattr(letter_doc, 'title', None) or '-'}\nSender: {getattr(letter_doc, 'sender_name', None) or '-'}"
	if len(text) >= MIN_TEXT_CHARS:
		content: Any = f"{header}\n\nLetter text:\n{text[:MAX_TEXT_CHARS]}"
	else:
		encoded = base64.b64encode(pdf_bytes).decode()
		content = [
			{"type": "text", "text": header},
			{"type": "file", "file": {"file_data": f"data:application/pdf;base64,{encoded}"}},
		]
	return [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": content}]


def _pdf_text(pdf_bytes: bytes) -> str:
	import pdfplumber

	try:
		with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
			return "\n\n".join((page.extract_text() or "") for page in pdf.pages).strip()
	except Exception:
		# An unreadable text layer is what the PDF fallback is for.
		return ""


def _to_result(answer: dict) -> ExtractionResult:
	kind = answer.get("document_kind")
	country = _text(answer.get("vendor_country"))
	return ExtractionResult(
		document_kind=kind if kind in DOCUMENT_KINDS else None,
		vendor_name=_text(answer.get("vendor_name")),
		vendor_tax_id=_text(answer.get("vendor_tax_id")),
		vendor_country=country if country and frappe.db.exists("Country", country) else None,
		iban=_text(answer.get("iban")),
		qr_reference=_text(answer.get("qr_reference")),
		invoice_number=_text(answer.get("invoice_number")),
		invoice_date=_text(answer.get("invoice_date")),
		due_date=_text(answer.get("due_date")),
		currency=(_text(answer.get("currency")) or "").upper() or None,
		net_amount=_number(answer.get("net_amount")),
		vat_amount=_number(answer.get("vat_amount")),
		gross_amount=_number(answer.get("gross_amount")),
		vat_breakdown=[
			{"rate": flt(row.get("rate")), "net": flt(row.get("net")), "vat": flt(row.get("vat"))}
			for row in answer.get("vat_breakdown") or []
			if isinstance(row, dict)
		],
		line_items=[row for row in (answer.get("line_items") or [])[:20] if isinstance(row, dict)],
		summary=_text(answer.get("summary")),
	)


def _checks(result: ExtractionResult) -> dict[str, bool | None]:
	"""Each check is True, False, or None when there was nothing to check."""
	net, vat, gross = result.net_amount, result.vat_amount, result.gross_amount
	checks: dict[str, bool | None] = dict.fromkeys(PENALTIES)

	if None not in (net, vat, gross):
		checks["totals_add_up"] = abs(net + vat - gross) <= AMOUNT_TOLERANCE

	if result.vat_breakdown:
		rows = result.vat_breakdown
		if net is not None and vat is not None:
			checks["breakdown_matches_totals"] = abs(
				sum(r["net"] for r in rows) - net
			) <= AMOUNT_TOLERANCE * len(rows) and abs(
				sum(r["vat"] for r in rows) - vat
			) <= AMOUNT_TOLERANCE * len(rows)
		checks["breakdown_rates_consistent"] = all(
			abs(r["net"] * r["rate"] / 100 - r["vat"]) <= max(AMOUNT_TOLERANCE, abs(r["vat"]) * 0.01)
			for r in rows
		)

	dates = [_date(result.invoice_date), _date(result.due_date)]
	if result.invoice_date or result.due_date:
		valid = all(d is not False for d in dates)
		if valid and dates[0] and dates[1]:
			valid = dates[1] >= dates[0]
		checks["dates_valid"] = valid

	if result.currency:
		checks["currency_known"] = bool(frappe.db.exists("Currency", result.currency))

	if result.document_kind == "Invoice":
		checks["gross_present_on_invoice"] = gross is not None
	elif result.document_kind:
		checks["amounts_only_on_financial_documents"] = (
			result.document_kind in FINANCIAL_KINDS or gross is None
		)
	return checks


def _date(value: str | None):
	"""A date, None when absent, False when present but unparseable."""
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return False


def _text(value: Any) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def _number(value: Any) -> float | None:
	if value is None or value == "":
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None
