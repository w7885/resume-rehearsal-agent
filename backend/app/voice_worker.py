"""Disposable offline Vosk worker. Audio is never written to a file."""
import base64
import json
import sys


if __name__ == '__main__':
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    data = json.load(sys.stdin)
    model = Model(data['modelPath'])  # Explicit path: never download by language.
    recognizer = KaldiRecognizer(model, 16000)
    pcm = base64.b64decode(data['pcm'], validate=True)
    texts = []
    for start in range(0, len(pcm), 8000):
        if recognizer.AcceptWaveform(pcm[start:start + 8000]):
            texts.append(json.loads(recognizer.Result()).get('text', ''))
    texts.append(json.loads(recognizer.FinalResult()).get('text', ''))
    sys.stdout.write(json.dumps({'text': ' '.join(t for t in texts if t)}, ensure_ascii=True))
