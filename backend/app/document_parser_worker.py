"""Disposable local parser process. No business data or network operations."""
import base64
import json
import sys
from app.document_parser import extract_document

if __name__ == '__main__':
    payload = json.load(sys.stdin)
    value = extract_document(payload['mediaType'], base64.b64decode(payload['raw']))
    # ASCII JSON avoids Windows console code-page differences in redirected IO.
    sys.stdout.write(json.dumps(value, ensure_ascii=True))
