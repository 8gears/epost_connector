"""Extractor name -> class. Driven by `ePost Settings.extractor`."""

from __future__ import annotations

from epost_connector.extraction.base import LetterExtractor
from epost_connector.extraction.noop import NoopExtractor

#: Keys must match the `extractor` Select options on `ePost Settings`. The empty
#: key is the unset field; both it and "None" mean "do not extract".
EXTRACTORS: dict[str, type[LetterExtractor]] = {
	"": NoopExtractor,
	"None": NoopExtractor,
}


def get_extractor(name: str | None) -> LetterExtractor:
	"""Instantiate the configured extractor."""
	key = (name or "").strip()
	extractor_class = EXTRACTORS.get(key)
	if extractor_class is None:
		known = ", ".join(repr(k) for k in EXTRACTORS)
		raise ValueError(f"Unknown extractor {key!r}. Registered extractors: {known}")
	return extractor_class()
