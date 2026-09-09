"""Build deterministic unsigned helper archives. No installation or network I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

EXTENSION_FILES = ('manifest.json','helper-core.js','background.js','popup.html','popup.js','popup.css')
SETUP_FILES = ('README.md','Register-DeckPipeAuthHost.ps1','Unregister-DeckPipeAuthHost.ps1')


def archive(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--host-exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    extension = {name: (args.source_root / 'extension' / name).read_bytes() for name in EXTENSION_FILES}
    manifest = json.loads(extension['manifest.json'])
    assert manifest['browser_specific_settings']['gecko']['id'] == 'deckpipe-auth@deckpipe.local'
    args.output.mkdir(parents=True, exist_ok=True)
    xpi = args.output / 'deckpipe-firefox-0.6.0-UNSIGNED.xpi'
    archive(xpi, extension)
    entries = {f'extension/{name}': data for name, data in extension.items()}
    entries.update({name: (args.source_root / 'release' / 'auth-helper' / name).read_bytes() for name in SETUP_FILES})
    entries['deckpipe-auth-host.exe'] = args.host_exe.read_bytes()
    entries[xpi.name] = xpi.read_bytes()
    bundle = args.output / 'deckpipe-auth-helper-0.6.0-UNSIGNED.zip'
    archive(bundle, entries)
    (args.output / 'SHA256SUMS.txt').write_text(''.join(f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in (xpi, bundle)), encoding='ascii')
    print(json.dumps({'unsigned_extension': str(xpi), 'helper_bundle': str(bundle), 'signed': False, 'installed': False}))


if __name__ == '__main__':
    main()
