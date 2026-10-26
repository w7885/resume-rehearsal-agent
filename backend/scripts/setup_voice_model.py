"""Download the official optional Chinese model into this project's ignored data."""
from pathlib import Path
import hashlib
import json
import shutil
import stat
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODELS = (ROOT / '.local/voice-models').resolve()
NAME = 'vosk-model-small-cn-0.22'
URL = f'https://alphacephei.com/vosk/models/{NAME}.zip'


def main():
    assert MODELS.is_relative_to(ROOT)
    MODELS.mkdir(parents=True, exist_ok=True)
    target = (MODELS / NAME).resolve()
    assert target.is_relative_to(MODELS)
    if (target / 'am/final.mdl').is_file():
        print('Chinese voice model already installed.')
        return
    if target.exists():
        raise RuntimeError('Incomplete model directory exists; preserve it and inspect before retrying.')
    archive = MODELS / f'{NAME}.zip'
    if not archive.exists():
        partial = MODELS / f'{NAME}.zip.part'
        with urllib.request.urlopen(URL, timeout=30) as response, partial.open('wb') as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 80 * 1024 * 1024:
                    raise RuntimeError('Model archive exceeds expected size limit.')
                output.write(chunk)
        partial.replace(archive)
    with zipfile.ZipFile(archive) as packed:
        entries = packed.infolist()
        if len(entries) > 1000 or sum(e.file_size for e in entries) > 512 * 1024 * 1024:
            raise RuntimeError('Model archive exceeds extraction limits.')
        if packed.testzip() is not None:
            raise RuntimeError('Model archive CRC check failed.')
        with tempfile.TemporaryDirectory(prefix='voice-model-', dir=MODELS) as temporary:
            stage = Path(temporary).resolve()
            # Bound both extraction and automatic recursive temporary cleanup.
            assert stage.is_relative_to(MODELS)
            for entry in entries:
                name = entry.filename.replace('\\', '/')
                path = (stage / name).resolve()
                if not name.startswith(NAME + '/') or not path.is_relative_to(stage) or stat.S_ISLNK(entry.external_attr >> 16):
                    raise RuntimeError('Unexpected path in model archive.')
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with packed.open(entry) as source, path.open('wb') as output:
                        shutil.copyfileobj(source, output)
            model = (stage / NAME).resolve()
            assert model.is_relative_to(MODELS) and target.is_relative_to(MODELS)
            if not (model / 'am/final.mdl').is_file() or not (model / 'conf/model.conf').is_file():
                raise RuntimeError('Required model files missing.')
            # Python's Windows temporary directories use owner-only ACLs.
            # Copy into a fresh project directory so files inherit project ACLs;
            # renaming the temporary tree would retain its restricted ACLs.
            shutil.copytree(model, target)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (MODELS / 'model-source.json').write_text(json.dumps({'url': URL, 'sha256': digest,
        'note': 'Downloaded via HTTPS; SHA is a local fingerprint, not an upstream checksum claim.'}, indent=2), encoding='utf-8')
    print('Installed official Chinese model; audio recognition is local.')


if __name__ == '__main__':
    main()
