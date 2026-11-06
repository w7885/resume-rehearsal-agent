"""Recover layout spacing using independently extracted source text."""
from collections import Counter
import re


def form_layout(page, layout, plain):
    """Recover top-level Form text on a temporary canvas, never the original.

    Complex/nested Forms deliberately fall back. Both the old glyphs and the
    exact missing glyph count must agree before adopting the new layout.
    """
    from pypdf import PageObject, Transformation, mult
    from pypdf.generic import ContentStream, NameObject, RectangleObject

    count = lambda value: Counter(char for char in value if not char.isspace())
    missing = count(plain) - count(layout)
    if not missing:
        return layout
    resources = page['/Resources']
    operations = ContentStream(page.get_contents(), page.pdf).operations
    if len(operations) > 50_000:
        raise ValueError('form operation limit')
    matrix = [1., 0., 0., 1., 0., 0.]
    stack, forms = [], []
    total_bytes = 0
    for args, op in operations:
        if op == b'q':
            stack.append(matrix[:])
        elif op == b'Q':
            matrix = stack.pop()
        elif op == b'cm':
            matrix = mult([float(value) for value in args], matrix)
        elif op == b'Do':
            obj = resources['/XObject'][args[0]].get_object()
            if obj.get('/Subtype') != '/Form':
                continue
            total_bytes += len(obj.get_data())
            if len(forms) >= 16 or total_bytes > 4 * 1024 * 1024:
                raise ValueError('form extraction limit')
            child_ops = ContentStream(obj, page.pdf).operations
            if any(child_op == b'Do' for _, child_op in child_ops):
                raise ValueError('nested form or image needs separate verification')
            transform = mult([float(v) for v in obj.get('/Matrix', [1, 0, 0, 1, 0, 0])], matrix)
            if abs(transform[1]) > 1e-6 or abs(transform[2]) > 1e-6:
                raise ValueError('rotated form needs separate verification')
            forms.append((obj, transform))
    if not forms:
        return layout
    canvas = PageObject.create_blank_page(pdf=page.pdf, width=float(page.mediabox.width), height=float(page.mediabox.height))
    canvas[NameObject('/MediaBox')] = RectangleObject(page.mediabox)
    canvas.merge_page(page)
    for obj, transform in forms:
        child = PageObject(page.pdf)
        child[NameObject('/Contents')] = obj
        child[NameObject('/Resources')] = obj['/Resources']
        child[NameObject('/MediaBox')] = RectangleObject(obj['/BBox'])
        canvas.merge_transformed_page(child, Transformation(transform))
    recovered = canvas.extract_text(extraction_mode='layout', layout_mode_space_vertically=False, layout_mode_strip_rotated=False) or ''
    before, after = count(layout), count(recovered)
    if before - after or after - before != missing:
        raise ValueError('form glyph counts disagree')
    return recovered


def reading_text(layout, plain):
    """Preserve glyphs; normalize spaces only where plain text agrees exactly."""
    source_lines = [line.strip() for line in plain.splitlines() if line.strip()]
    sources = []
    for line in source_lines:
        positions = [i for i, char in enumerate(line) if not char.isspace()]
        sources.append((''.join(line[i] for i in positions), line, positions))

    def restore(value, cell=False):
        value = value.strip()
        prefix = ''
        if value.startswith('▸'):
            prefix, value = '▸', value[1:].lstrip()
        key = ''.join(value.split())
        if not key:
            return prefix, False
        for source, line, positions in sources:
            start = source.find(key)
            if start >= 0:
                glyphs = [i for i, char in enumerate(value) if not char.isspace()]
                recovered = key[0]
                for offset in range(1, len(key)):
                    old_gap = value[glyphs[offset - 1] + 1:glyphs[offset]]
                    source_gap = line[positions[start + offset - 1] + 1:positions[start + offset]]
                    # A real source separator may be a gutter. Preserve it,
                    # while still recovering other broken words on this line.
                    gap = old_gap if not cell and len(old_gap) >= 4 and source_gap else source_gap
                    recovered += gap + key[offset]
                return prefix + recovered, True
        return prefix + value, False

    lines = layout.splitlines()
    output = []
    index = 0
    while index < len(lines):
        # Restrict column reading to a two-column list with a heading row,
        # paired bullets, then wrapped continuations. Tables/isolated dates
        # retain their horizontal order and spacing.
        run = []
        cursor = index
        second_column = None
        while cursor < len(lines):
            line = lines[cursor].strip()
            gaps = list(re.finditer(r' {4,}', line))
            if not gaps:
                break
            # Internal word spacing may also exceed four spaces. Only the
            # uniquely widest, aligned gutter may separate two columns.
            gaps.sort(key=lambda item: item.end() - item.start(), reverse=True)
            gap = gaps[0]
            if len(gaps) > 1 and gap.end() - gap.start() == gaps[1].end() - gaps[1].start():
                break
            if second_column is not None and abs(gap.end() - second_column) > 4:
                break
            left, right = restore(line[:gap.start()], cell=True), restore(line[gap.end():], cell=True)
            if not left[1] or not right[1]:
                break
            run.append((left[0], right[0]))
            second_column = gap.end()
            cursor += 1
        paired_list = (len(run) >= 3
                       and all(not cell.startswith('▸') for cell in run[0])
                       and all(cell.startswith('▸') for cell in run[1])
                       and all(not cell.startswith('▸') for cell in run[2]))
        if paired_list:
            output.extend(cell[column] for column in (0, 1) for cell in run)
            index = cursor
            continue
        restored, matched = restore(lines[index])
        output.append(restored if matched else lines[index])
        index += 1
    result = '\n'.join(output)
    if Counter(c for c in result if not c.isspace()) != Counter(c for c in layout if not c.isspace()):
        return layout
    return result
