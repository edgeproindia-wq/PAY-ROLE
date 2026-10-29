"""Indian rupee amounts in words, as printed on payslips:
46400.50 -> 'Rupees Forty Six Thousand Four Hundred and Fifty Paise Only' (lakh and crore, not million)."""
from decimal import ROUND_HALF_UP, Decimal

ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen',
        'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']


def _hundreds(n):
    """1..999 in words."""
    words = []
    if n >= 100:
        words.append(ONES[n // 100] + ' Hundred')
        n %= 100
    if n >= 20:
        words.append(TENS[n // 10] + (' ' + ONES[n % 10] if n % 10 else ''))
    elif n:
        words.append(ONES[n])
    return ' '.join(words)


def _integer_words(n):
    if n == 0:
        return 'Zero'
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1_000)
    parts = []
    if crore:
        parts.append(_integer_words(crore) + ' Crore')
    if lakh:
        parts.append(_hundreds(lakh) + ' Lakh')
    if thousand:
        parts.append(_hundreds(thousand) + ' Thousand')
    if n:
        parts.append(_hundreds(n))
    return ' '.join(parts)


def rupees_in_words(amount):
    amount = Decimal(amount).quantize(Decimal('0.01'), ROUND_HALF_UP)
    minus = 'Minus ' if amount < 0 else ''
    amount = abs(amount)
    rupees = int(amount)
    paise = int((amount - rupees) * 100)
    text = 'Rupees ' + _integer_words(rupees)
    if paise:
        text += ' and ' + _integer_words(paise) + ' Paise'
    return minus + text + ' Only'
