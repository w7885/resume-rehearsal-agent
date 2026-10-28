"""Comparison views only. Never use these strings as source quotes or before."""
from collections import Counter
import re
import unicodedata


_GAP = re.compile(r' {4,}')
_BULLET = re.compile(r'^[▸▹►•●▪◆◇\-]')
# This CJK radical is not converted by NFKC; it is common in extracted PDFs.
_GLYPHS = str.maketrans({'⻓': '长'})


def _columns(lines):
    """Split only a repeated, explicit two-column layout; never guess words."""
    boundaries = Counter()
    marked = Counter()
    for line in lines:
        for gap in _GAP.finditer(line):
            right = line[gap.end():]
            if line[:gap.start()].strip() and right.strip():
                boundaries[gap.end()] += 1
                if _BULLET.match(right):
                    marked[gap.end()] += 1
    candidates = [cut for cut, count in boundaries.items() if count >= 3 and marked[cut] >= 2]
    if len(candidates) != 1:
        return [lines]
    cut = candidates[0]
    # Every row crossing the boundary must actually have a whitespace gutter.
    # Otherwise retain the original rows and let the ordinary guard reject.
    if any(len(line) > cut and line[:cut].strip() and not line[cut - 1].isspace()
           for line in lines):
        return [lines]
    return [[line[:cut].rstrip() for line in lines],
            [line[cut:].rstrip() if len(line) > cut else '' for line in lines]]


def claim_comparison_text(text: str) -> str:
    normalized = unicodedata.normalize('NFKC', text).translate(_GLYPHS)
    # Retain newlines and indentation until column/continuation boundaries
    # have been checked. In particular, never join across a source boundary.
    groups = _columns(normalized.splitlines())
    output = []
    for group in groups:
        rows = []
        for line in group:
            if not line.strip():
                rows.append('')
                continue
            continuation = line.startswith(('  ', '\t'))
            # An unclassified wide gap is a boundary, never a license to
            # concatenate two possible cells into a skill name.
            stripped = _GAP.sub('；', line.strip())
            if (rows and rows[-1] and continuation
                    and not _BULLET.match(stripped)
                    and not re.search(r'[。.!！?？;；:：]$', rows[-1])):
                rows[-1] += stripped
            else:
                rows.append(stripped)
        output.extend(rows)
        output.append('')
    return '\n'.join(output)
