import base64
import json
import sys
from app.page_renderer import render_page

if __name__ == '__main__':
    payload = json.load(sys.stdin)
    value = render_page(payload['mediaType'], base64.b64decode(payload['raw']), payload['page'])
    sys.stdout.write(json.dumps(value, ensure_ascii=True))
