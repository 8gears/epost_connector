"""One JSON answer from a Frappe Flow model.

Flow is an optional app, so everything here degrades to `None` rather than
raising: a letter the model could not read is the same outcome as a letter no
model was asked about.

The schema goes into the prompt instead of `response_format`. litellm 1.83.7
emulates `response_format` for Anthropic models it does not list as supporting
structured output by forcing a tool call, and newer models refuse that with
"tool_choice: type "tool" and "any" are not supported for this model". Every
caller validates the answer against what it may contain anyway, so a prompt-level
schema loses nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

import frappe

FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def ask_json(model_name: str, messages: list[dict], schema: dict) -> tuple[dict | None, dict | None]:
	"""Return (answer, usage). The answer is None when there is no usable JSON object."""
	try:
		from flow.lib.model import Model
	except ImportError:
		frappe.log_error("ePost: Frappe Flow is not installed", reference_doctype="ePost Settings")
		return None, None

	messages = [dict(m) for m in messages]
	messages[0]["content"] = (
		f"{messages[0]['content']}\n\nReply with one JSON object and nothing else. "
		f"It must validate against this JSON Schema:\n{json.dumps(schema)}"
	)

	try:
		response = Model(model_name).chat(messages)
	except Exception:
		frappe.log_error("ePost: model call failed", reference_doctype="ePost Settings")
		return None, None

	usage = getattr(response, "usage", None)
	return parse_object(response.content), usage


def parse_object(content: str | None) -> dict | None:
	"""The JSON object in `content`, tolerating a Markdown fence around it."""
	if not content:
		return None
	text = FENCE.sub("", content.strip())
	start, end = text.find("{"), text.rfind("}")
	if start < 0 or end < start:
		return None
	try:
		value: Any = json.loads(text[start : end + 1])
	except ValueError:
		return None
	return value if isinstance(value, dict) else None
