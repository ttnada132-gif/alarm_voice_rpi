"""Run before committing a release: python3 build_release.py 1.0.1"""
import hashlib
import json
from pathlib import Path
import sys
from auto_update import FILES, version


def build(root, number):
    version(number)
    (root / 'VERSION').write_text(number + '\n')
    manifest = {'version': number, 'files': {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in FILES
    }}
    (root / 'release.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    build(Path(__file__).resolve().parent, sys.argv[1])
