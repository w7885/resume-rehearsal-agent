"""Offline build smoke check: load the real recognizer using synthetic silence."""
from io import BytesIO
from pathlib import Path
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import voice


def main():
    if not voice.status()['available']:
        raise SystemExit('Voice dependency or model is not ready.')
    audio = BytesIO()
    with wave.open(audio, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(voice.RATE)
        output.writeframes(b'\0' * voice.RATE * 2)
    try:
        voice.transcribe(audio.getvalue())
    except voice.VoiceError as error:
        if error.code != 'voice_no_speech':
            raise SystemExit('Voice model smoke check failed: ' + error.code) from None
    print('VOICE_MODEL_CHECK: ready (synthetic silence, no external provider)')


if __name__ == '__main__':
    main()
