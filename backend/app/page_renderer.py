"""Local raster preparation only. No text extraction or model calls."""
import base64
from io import BytesIO
import json
import math
import os
from pathlib import Path
import subprocess
import sys

RENDER_VERSION = 'pdfium-5.14.0-pillow-12.2.0-edge1600-v1'
MAX_PAGES = 40
MAX_EDGE = 1600
MAX_OUTPUT = 6 * 1024 * 1024


def failure(code, message, page_count=None):
    return {'status': 'failed', 'pageCount': page_count, 'errorCode': code, 'errorMessage': message}


def render_page(media_type, raw, number):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 25_000_000
    count = None
    try:
        if media_type == 'application/pdf':
            from pypdf import PdfReader
            if PdfReader(BytesIO(raw), strict=True).is_encrypted:
                return failure('encrypted', 'PDF 已加密，请提供解除加密的副本；原件仍保留。')
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(raw) as document:
                count = len(document)
                if number > count or number > MAX_PAGES:
                    return failure('invalid_page', '页码不存在或超过本次前 40 页限制。', count)
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
                        return failure('page_size', '该页尺寸异常，无法准备预览。', count)
                    scale = min(120 / 72, MAX_EDGE / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            output = BytesIO(); image.save(output, format='PNG')
                            data, size = output.getvalue(), image.size
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        else:
            count = 1
            if number != 1:
                return failure('invalid_page', '单张图片只有第 1 页。', count)
            with Image.open(BytesIO(raw)) as original:
                if original.format not in {'PNG', 'JPEG'} or original.width * original.height > 25_000_000 or getattr(original, 'n_frames', 1) > 1:
                    return failure('image_limit', '请使用不超过 2500 万像素的静态 PNG／JPEG；原件已保留。', count)
                image = ImageOps.exif_transpose(original)
                try:
                    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                    output = BytesIO(); image.save(output, format='PNG')
                    data, size = output.getvalue(), image.size
                finally:
                    image.close()
        if len(data) > MAX_OUTPUT:
            return failure('output_limit', '该页图像过大，请拆分或降低图片尺寸后重传。', count)
        return {'status': 'ready', 'pageCount': count, 'width': size[0], 'height': size[1], 'image': base64.b64encode(data).decode('ascii')}
    except Exception:
        return failure('render_failed', '图像损坏、格式不受支持或该页无法渲染；原件已保留，可转换后重传。', count)


def prepare_page(media_type, raw, number):
    try:
        completed = subprocess.run([sys.executable, '-m', 'app.page_renderer_worker'],
            input=json.dumps({'mediaType': media_type, 'raw': base64.b64encode(raw).decode('ascii'), 'page': number}),
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=6,
            cwd=Path(__file__).resolve().parents[1], creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if completed.returncode or len(completed.stdout) > 9 * 1024 * 1024:
            raise ValueError('worker output')
        value = json.loads(completed.stdout)
        if value['status'] == 'ready':
            value['imageData'] = base64.b64decode(value.pop('image'), validate=True)
        return value
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        return failure('render_timeout', '本页准备未完成或超时，原件已保留，请重试或转换文件。')
