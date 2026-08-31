"""Document extraction interfaces for ePost letters.

The pipeline is `sync -> download -> extract -> import`. This package owns the
*extract* step and deliberately ships no working extractor: `NoopExtractor` is
the default and returns `None`, which leaves every extraction field on the
`ePost Letter` empty and the status at `Downloaded`.

Adding a real extractor (for example an LLM-backed one) is two edits and no
change to the sync engine or the Purchase Invoice importer:

1. Write the class in `epost_connector/extraction/anthropic.py`: subclass
   `LetterExtractor`, set `name`, and implement
   `extract(self, letter_doc, pdf_bytes) -> ExtractionResult | None` by calling
   the model and mapping its answer onto `ExtractionResult`.

2. Register it in `registry.EXTRACTORS` under a key, and add that same key to
   the `extractor` Select options on the `ePost Settings` DocType.

Whatever the extractor returns is written to read-only fields on the letter, so
a wrong answer is visible and correctable by a human before anything is booked.
"""

from epost_connector.extraction.base import ExtractionResult, LetterExtractor
from epost_connector.extraction.noop import NoopExtractor
from epost_connector.extraction.registry import EXTRACTORS, get_extractor

__all__ = [
	"EXTRACTORS",
	"ExtractionResult",
	"LetterExtractor",
	"NoopExtractor",
	"get_extractor",
]
