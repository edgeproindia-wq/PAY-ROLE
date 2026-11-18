"""One rule for a bank reference (UTR) typed by hand or read from a bank statement.

A real reference is 8 to 35 letters/digits (NEFT 16, RTGS 22, IMPS/UPI 12; a few banks add - or /).
A one-letter "x" used to be accepted and marked the salary as paid, so it is refused now.
The reference is stored in capitals without spaces, so "utr 123" and "UTR123" are the same reference
and cannot be used for two payments."""
import re

UTR_PATTERN = re.compile(r'^[A-Z0-9][A-Z0-9/\-]{7,34}$')
UTR_HELP = ('Enter the bank reference (UTR) exactly as the bank shows it: 8 to 35 letters or digits '
            '(for example HDFCN52026111700123 or 432112345678).')


def normalise_utr(value):
    return re.sub(r'\s+', '', str(value or '')).upper()


def utr_problem(value):
    """'' when the reference looks real, otherwise the message to show."""
    ref = normalise_utr(value)
    if not ref:
        return 'A bank UTR / reference number is required.'
    if not UTR_PATTERN.match(ref):
        return UTR_HELP
    return ''
