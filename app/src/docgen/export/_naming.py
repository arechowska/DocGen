"""Shared filesystem-safe filename derivation for the export renderers.

Markdown/HTML/DOCX/PDF each independently derive their `RenderedFile
.filename` from the document title (see `Exporter.render` in
`docgen.export.protocol`), then `ExportStorage.save` appends
`-{template_id}` before the extension to build the actual on-disk name (see
`ExportStorage._destination_name`). Two things about the *previous*
per-exporter implementations of this were unsound:

1. They truncated the title-derived stem by *character* count. Most
   filesystems limit a filename to 255 *bytes*, and Cyrillic characters are
   2 bytes each in UTF-8 -- a 240-character Cyrillic stem can be ~480 bytes,
   almost double the actual limit.
2. They never accounted for the `-{template_id}` suffix ExportStorage
   appends afterward, so even a byte-correct stem could still overflow once
   the suffix was added.

Document titles are allowed up to 200 characters elsewhere in the app (see
`editor/routes.py`), so a long Cyrillic title overflowing the filesystem
limit was a genuinely reachable production bug -- `ExportStorage.save`
would fail with an OS-level "file name too long" error. This module is the
single place that gets the byte budget right, used by all four exporters.
"""

from __future__ import annotations

import re

_UNSAFE_CHARS_RE = re.compile(r"[^a-zA-Z0-9а-яА-ЯёЁ\s\-]")
_WHITESPACE_RE = re.compile(r"\s+")

# Most filesystems (ext4, most other POSIX filesystems, NTFS in UTF-8 mode)
# cap a single path component -- i.e. one filename -- at 255 *bytes*.
_FILENAME_MAX_BYTES = 255

_FALLBACK_STEM = "document"


def make_safe_filename(title: str, extension: str, *, reserved_suffix: str = "") -> str:
    """Build a filesystem-safe filename from a document title.

    Args:
        title: The document title to derive the filename stem from.
        extension: The filename extension, including the leading dot (e.g.
            ``".pdf"``).
        reserved_suffix: Text that will be appended to the returned
            filename's stem *after* this function returns -- in practice,
            ``ExportStorage.save()`` appending ``-{template_id}`` before the
            extension (e.g. pass ``"-docgen-light"``). Its UTF-8 byte
            length is reserved out of the filename's byte budget so the
            eventual on-disk name (stem + reserved_suffix + extension)
            still fits within the filesystem limit.

    Returns:
        A filename ending in `extension`, whose stem is stripped of
        filesystem-unsafe characters and truncated (on a whole-character
        boundary) so that appending `reserved_suffix` afterward cannot push
        the final name past the filesystem's byte limit.
    """
    safe_name = _UNSAFE_CHARS_RE.sub("", title)
    safe_name = _WHITESPACE_RE.sub("-", safe_name.strip())
    safe_name = safe_name.strip("-")

    if not safe_name:
        safe_name = _FALLBACK_STEM

    reserved_bytes = len(reserved_suffix.encode("utf-8")) + len(extension.encode("utf-8"))
    max_stem_bytes = max(_FILENAME_MAX_BYTES - reserved_bytes, len(_FALLBACK_STEM))

    safe_name = _truncate_to_byte_length(safe_name, max_stem_bytes)
    safe_name = safe_name.strip("-") or _FALLBACK_STEM

    return f"{safe_name}{extension}"


def _truncate_to_byte_length(value: str, max_bytes: int) -> str:
    """Truncate `value` so its UTF-8 encoding fits within `max_bytes`.

    Truncation happens on a whole-character boundary: a byte-exact cutoff
    could split a multi-byte Cyrillic character in half, producing invalid
    UTF-8. Dropping any trailing partial character costs at most a few
    bytes, negligible against the 255-byte budget.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


__all__ = ["make_safe_filename"]
