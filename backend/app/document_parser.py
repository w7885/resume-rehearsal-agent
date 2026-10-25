"""Bounded, local text extraction. Parsing never classifies personal facts."""
import base64
from io import BytesIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

MAX_TEXT = 50_000
MAX_BLOCKS = 1000
MAX_PAGES = 40
MAX_XML = 8 * 1024 * 1024
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'


def result(status, blocks=(), warnings=(), page_count=None):
    return {'status': status, 'blocks': list(blocks), 'report': {
        'parser': 'local-text-v1', 'status': status, 'pageCount': page_count,
        'warnings': list(warnings), 'message': {
            'parsed': '文本已提取，请对照原件核对内容；不代表事实已确认。',
            'partial': '仅提取部分内容，或存在阅读顺序风险，请核对原件。',
            'failed': '未能提取可用文本，原件仍已保存；可转换文件或粘贴文字。',
            'pending': '原件已保存，图片文字未识别；可重试、换更清晰的图或粘贴文字。'}[status]}}


class Collector:
    def __init__(self):
        self.blocks, self.warnings, self.characters = [], [], 0

    def warn(self, code, message):
        if not any(item['code'] == code and item['message'] == message for item in self.warnings):
            self.warnings.append({'code': code, 'message': message})

    def add(self, text, origin):
        if not text.strip():
            return
        if self.characters + len(text) > MAX_TEXT or len(self.blocks) >= MAX_BLOCKS:
            self.warn('text_limit', '内容超过本次提取上限，部分段落未保存为文本，请拆分文件。')
            return
        self.blocks.append({'text': text, 'origin': origin})
        self.characters += len(text)

    def finish(self, page_count=None):
        if not self.blocks:
            self.warn('no_text', '没有提取到可用文字；请检查空白、扫描页、损坏或加密情况。')
        return result('partial' if self.warnings and self.blocks else 'parsed' if self.blocks else 'failed',
                      self.blocks, self.warnings, page_count)


def extract_docx(raw):
    from defusedxml.ElementTree import fromstring
    c = Collector()
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(entry.file_size for entry in entries) > 30 * 1024 * 1024:
            raise ValueError('archive limit')
        names = [entry.filename for entry in entries]
        if len(names) != len(set(names)):
            raise ValueError('duplicate archive entry')
        parts = ['word/document.xml'] + sorted(name for name in names if re.fullmatch(r'word/(?:header|footer)[0-9]+\.xml', name))
        if len(parts) > 1:
            c.warn('header_footer', '页眉／页脚单独列出，请核对它们与正文的阅读顺序。')
        if any(re.fullmatch(r'word/(?:footnotes|endnotes)\.xml', name) for name in names):
            c.warn('notes', '脚注／尾注尚未提取，请对照原件补充。')
        paragraph = table = 0
        for part in parts:
            entry = archive.getinfo(part)
            if entry.file_size > MAX_XML:
                raise ValueError('xml limit')
            root = fromstring(archive.read(part), forbid_dtd=True)
            if part == 'word/document.xml' and root.tag != W + 'document':
                raise ValueError('not word xml')
            for tag, code, message in [
                ('drawing', 'images', '文档含图片或绘图，其中的文字尚未识别。'),
                ('pict', 'images', '文档含图片或绘图，其中的文字尚未识别。'),
                ('txbxContent', 'floating_text', '文本框文字已提取；仅在坐标可靠时局部排序，复杂布局仍请核对原件。'),
                ('ins', 'revisions', '文档含修订；提取插入文字并略去删除文字，请核对最终稿。'),
                ('del', 'revisions', '文档含修订；提取插入文字并略去删除文字，请核对最终稿。'),
                ('numPr', 'numbering', '自动列表编号未还原，段落文字保留，请核对编号。'),
                ('gridSpan', 'merged_cells', '表格含合并单元格，行列号按文档结构记录，请核对原表。'),
                ('vMerge', 'merged_cells', '表格含合并单元格，行列号按文档结构记录，请核对原表。'),
                ('altChunk', 'embedded', '存在嵌入内容，尚未提取，请对照原件补充。')]:
                if root.find('.//' + W + tag) is not None:
                    c.warn(code, message)
            for columns in root.iter(W + 'cols'):
                if int(columns.get(W + 'num', '1')) > 1:
                    c.warn('columns', '文档存在分栏，采用文档结构顺序，请核对阅读顺序。')

            def selected_children(element):
                if element.tag.endswith('}AlternateContent'):
                    c.warn('alternate_content', '文档含兼容性替代内容，只提取首个分支，请核对原件。')
                    return list(element)[:1]
                return element

            sections = list(root.iter(W + 'sectPr'))
            page_size = sections[0].find(W + 'pgSz') if len(sections) == 1 else None
            page_height = int(page_size.get(W + 'h', '0')) * 635 if page_size is not None else 0
            single_column = all(int(n.get(W + 'num', '1')) == 1 for n in root.iter(W + 'cols'))

            def ordered_events(events, element, location):
                boxes = [event for event in events if not isinstance(event, str)]
                if len(boxes) < 2:
                    return events
                # A text-free anchoring paragraph belongs to one page. Never
                # compare page-relative coordinates across separate paragraphs,
                # sections, table cells, or normal flowing body text.
                groups = {}
                try:
                    if (part != 'word/document.xml' or location or not single_column or not page_height
                            or any(isinstance(e, str) and e.strip() for e in events)
                            or any(n.tag in {W + 'br', W + 'lastRenderedPageBreak', W + 'pageBreakBefore', W + 'sectPr'} for n in element.iter())):
                        raise ValueError('ambiguous page or flow')
                    for box, anchor in boxes:
                        if anchor is None:
                            raise ValueError('unpositioned box')
                        groups.setdefault(anchor, []).append((box, anchor))
                    positioned = []
                    horizontal_reference = None
                    for anchor, members in groups.items():
                        text_boxes = [box for box, _ in members if any((n.text or '').strip() for n in box.iter(W + 't'))]
                        if not text_boxes:
                            # Empty decorative boxes carry no text or ordering.
                            continue
                        if len(text_boxes) != 1 or anchor.get('simplePos', '0') not in {'0', 'false'}:
                            raise ValueError('group needs internal layout')
                        parents = {child: node for node in anchor.iter() for child in node}
                        for node in anchor.iter():
                            if node.get('rot', '0') != '0' or node.get('flipH') in {'1', 'true'} or node.get('flipV') in {'1', 'true'}:
                                # xfrm belongs to spPr/grpSpPr; its owning shape
                                # carries the text, while decorative siblings
                                # may be rotated without rotating that text.
                                owner = parents.get(parents.get(node, node), node)
                                if any((t.text or '').strip() for t in owner.iter(W + 't')):
                                    raise ValueError('rotated text')
                        vertical, horizontal, extent = (anchor.find(WP + name) for name in ('positionV', 'positionH', 'extent'))
                        if vertical is None or horizontal is None or extent is None:
                            raise ValueError('missing position')
                        reference = horizontal.get('relativeFrom')
                        if vertical.get('relativeFrom') != 'page' or reference not in {'page', 'margin', 'column'}:
                            raise ValueError('incomparable position')
                        if horizontal_reference is not None and reference != horizontal_reference:
                            raise ValueError('different horizontal origins')
                        horizontal_reference = reference
                        y = int(vertical.findtext(WP + 'posOffset'))
                        x = int(horizontal.findtext(WP + 'posOffset'))
                        width, height = int(extent.get('cx')), int(extent.get('cy'))
                        if not (width > 0 and height > 0 and 0 <= y < y + height <= page_height):
                            raise ValueError('outside page')
                        positioned.append((y, x, width, members))
                    # A shared horizontal lane excludes separate text columns.
                    # It still permits a short left/right personal-info area.
                    if len(positioned) < 2 or max(p[1] for p in positioned) >= min(p[1] + p[2] for p in positioned):
                        raise ValueError('separate columns or insufficient positions')
                except (ValueError, TypeError):
                    c.warn('floating_order_unresolved', '文本框坐标、分页或分栏无法可靠比较，保留结构顺序；标题与正文归属需核对。')
                    return events
                c.warn('floating_order', '同一锚定段落的文本框已按页面纵坐标排序；框内顺序保留，仍请核对原件。')
                return [event for _, _, _, members in sorted(positioned, key=lambda p: (p[0], p[1])) for event in members]

            def paragraph_events(element, graphic=False, anchor=None):
                # Text boxes are separate paragraphs, never concatenated into
                # their anchoring paragraph. Only one compatibility branch is
                # visited; identical real paragraphs must not be deduplicated.
                if element.tag == W + 'del':
                    return
                if element.tag == W + 'txbxContent':
                    yield (element, anchor)
                    return
                if element.tag == WP + 'anchor':
                    anchor = element
                graphic = graphic or element.tag in {W + 'drawing', W + 'pict'}
                if element.tag == W + 't':
                    if not graphic:
                        yield element.text or ''
                    return
                if element.tag in {W + 'br', W + 'cr'}:
                    if not graphic:
                        yield '\n'
                    return
                if element.tag == W + 'tab':
                    if not graphic:
                        yield '\t'
                    return
                for child in selected_children(element):
                    yield from paragraph_events(child, graphic, anchor)

            def walk(element, location=None):
                nonlocal paragraph, table
                if element.tag == W + 'del':
                    return
                if element.tag == W + 'p':
                    text = []
                    def flush():
                        nonlocal paragraph
                        value = ''.join(text)
                        text.clear()
                        if value.strip():
                            paragraph += 1
                            c.add(value, {'paragraph': paragraph, 'part': part, **(location or {})})
                    events = ordered_events(list(paragraph_events(element)), element, location)
                    for event in events:
                        if isinstance(event, str):
                            text.append(event)
                        else:
                            flush()
                            walk(event[0], location)
                    flush()
                    return
                if element.tag == W + 'tbl':
                    table += 1
                    table_id = table
                    for row_index, row in enumerate(element.findall(W + 'tr'), 1):
                        for column_index, cell in enumerate(row.findall(W + 'tc'), 1):
                            walk(cell, {'table': table_id, 'row': row_index, 'column': column_index})
                    return
                for child in selected_children(element):
                    walk(child, location)
            walk(root)
    return c.finish()


def extract_pdf(raw):
    from pypdf import PdfReader
    c = Collector()
    reader = PdfReader(BytesIO(raw), strict=True)
    if reader.is_encrypted:
        return result('failed', warnings=[{'code': 'encrypted', 'message': 'PDF 已加密，请提供解除加密的副本或粘贴文字；原件已保留。'}])
    count = len(reader.pages)
    if count > MAX_PAGES:
        c.warn('page_limit', f'共 {count} 页，本次最多提取前 {MAX_PAGES} 页，请拆分剩余页面。')
    for index, page in enumerate(reader.pages[:MAX_PAGES], 1):
        try:
            contents = page.get_contents()
            if contents and len(contents.get_data()) > 4 * 1024 * 1024:
                c.warn('page_limit', f'第 {index} 页内容过大，未提取，请拆分或粘贴。')
                continue
            resources = page.get('/Resources', {})
            resources = resources.get_object() if hasattr(resources, 'get_object') else resources
            objects = resources.get('/XObject', {})
            objects = objects.get_object() if hasattr(objects, 'get_object') else objects
            if any(item.get_object().get('/Subtype') in {'/Image', '/Form'} for item in objects.values()):
                c.warn('page_graphics', f'第 {index} 页含图片或嵌入图形，其中的文字可能未提取，请核对。')
            text = page.extract_text(extraction_mode='layout', layout_mode_space_vertically=False, layout_mode_strip_rotated=False) or ''
            if not text.strip():
                c.warn('empty_page', f'第 {index} 页未提取到文字，可能为空白或扫描页，请核对原件。')
                continue
            if '\ufffd' in text or '\x00' in text:
                c.warn('encoding', f'第 {index} 页存在无法识别的字符，请核对数字、日期和职责。')
            if any(re.search(r'\S {4,}\S', line) for line in text.splitlines()):
                c.warn('reading_order', f'第 {index} 页可能含表格或分栏，保留横向间距，请核对阅读顺序。')
            c.add(text, {'page': index, 'lineStart': 1, 'lineEnd': max(1, len(text.splitlines()))})
        except Exception:
            c.warn('page_failed', f'第 {index} 页提取失败，其他页结果保留；请转换该页或粘贴文字。')
    return c.finish(count)


def extract_document(media_type, raw):
    try:
        if media_type == 'application/pdf':
            return extract_pdf(raw)
        return extract_docx(raw)
    except Exception:
        return result('failed', warnings=[{'code': 'unreadable', 'message': '文件损坏、结构不受支持或超过提取限制；原件已保留，请转换后重试或粘贴文字。'}])


def parse_upload(media_type, raw, extracted_text=None):
    if extracted_text is not None:
        return result('parsed', [{'text': extracted_text, 'origin': {'lineStart': 1, 'lineEnd': max(1, len(extracted_text.splitlines()))}}])
    if media_type not in {'application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}:
        return result('pending')
    # Keep malformed parser work out of request/database transactions and bound
    # wall time. The child only receives bytes, no database paths or model API.
    try:
        completed = subprocess.run([sys.executable, '-m', 'app.document_parser_worker'],
            input=json.dumps({'mediaType': media_type, 'raw': base64.b64encode(raw).decode()}),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=6,
            cwd=Path(__file__).resolve().parents[1],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if completed.returncode or len(completed.stdout) > 2 * 1024 * 1024:
            raise ValueError('worker failed')
        return json.loads(completed.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return result('failed', warnings=[{'code': 'parser_limit', 'message': '本次提取未完成或超时，原件已保存；请拆分、转换文件或粘贴文字。'}])
