"""Suggest how an extracted supplier invoice is booked.

`suggest.py` decides, `history.py` remembers, `backtest.py` measures, and
`letter.py` stores the result on the ePost Letter. No invoice is created here:
`epost/import_invoice.py` turns a stored suggestion into a draft Purchase Invoice.
"""
