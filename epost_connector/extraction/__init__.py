"""Document extraction for ePost letters.

The pipeline is `sync -> download -> extract -> suggest booking -> import`. This
package owns the *extract* step. `NoopExtractor` is the default and returns
`None`, which leaves every extraction field empty and the status at
`Downloaded`. `FlowExtractor` asks a Frappe Flow model.

Adding another extractor is two edits and no change to the sync engine or the
Purchase Invoice importer: subclass `LetterExtractor` and implement `extract`,
then register it in `registry.EXTRACTORS` under a key that is also one of the
`extractor` Select options on `ePost Settings`.

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
