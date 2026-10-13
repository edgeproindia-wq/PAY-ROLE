"""Check that an uploaded file really is what its extension says (PDF / PNG / JPEG).

Uploads are always sent back as downloads, never opened in the browser, so this is a second
layer: a web page or program renamed to .pdf is refused instead of being stored."""
import os

from django.core.exceptions import ValidationError

_SIGNATURES = {
    '.pdf': (b'%PDF-',),
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
}
_NAMES = {'.pdf': 'PDF', '.png': 'PNG image', '.jpg': 'JPEG image', '.jpeg': 'JPEG image'}


def check_file_matches_extension(f):
    """Raise ValidationError when the first bytes do not match the file extension."""
    ext = os.path.splitext(getattr(f, 'name', '') or '')[1].lower()
    signatures = _SIGNATURES.get(ext)
    if not signatures:
        return
    try:
        f.seek(0)
        head = f.read(1024)
        f.seek(0)
    except Exception:
        raise ValidationError('The file could not be read. Please upload it again.')
    # a PDF may carry a few bytes of junk before the header; images must start with theirs
    ok = any((sig in head) if ext == '.pdf' else head.startswith(sig) for sig in signatures)
    if not ok:
        raise ValidationError(f'This file is not a real {_NAMES[ext]}. Upload the original PDF, JPG or PNG file.')