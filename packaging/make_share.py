"""Merge verified ordinary release ZIPs into one model-free sharing ZIP.

Compressed file records are retained; only the ZIP central directory is rebuilt.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path, PurePosixPath
import zipfile

from build_portable import checked, mkdir, sha, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source = args.release.resolve()
    output = mkdir(args.output)
    manifest = json.loads((source / 'release-packages.json').read_text(encoding='utf-8'))
    top = manifest['top_level_directory']
    if PurePosixPath(top).name != top or '\\' in top or top in ('.', '..'):
        raise ValueError('Invalid top-level package directory')
    records = manifest['archives']
    if not any(r['component'] == 'core' for r in records) or not any(r['component'] == 'ai' for r in records):
        raise ValueError('A full sharing package requires Core and AI archives')
    target = checked(output / (top + '-All-in-One.zip'))
    partial = checked(target.with_suffix('.zip.part'))
    if target.exists() or partial.exists():
        raise FileExistsError('Refusing to overwrite an existing sharing archive')
    expected = {}
    with zipfile.ZipFile(partial, 'w', allowZip64=True) as destination:
        for record in records:
            path = (source / record['file']).resolve()
            if path.parent != source or path.stat().st_size != record['bytes'] or sha(path) != record['sha256']:
                raise ValueError('Source release checksum mismatch: ' + record['file'])
            with zipfile.ZipFile(path) as archive:
                members = sorted(archive.infolist(), key=lambda i: i.header_offset)
                for index, member in enumerate(members):
                    parts = PurePosixPath(member.filename).parts
                    if len(parts) < 2 or parts[0] != top or '..' in parts or '\\' in member.filename or parts[1] in ('Data', 'Cache'):
                        raise ValueError('Unexpected release member: ' + member.filename)
                    if member.filename in expected:
                        raise ValueError('Duplicate release member: ' + member.filename)
                    expected[member.filename] = (member.file_size, member.CRC)
                    end = members[index + 1].header_offset if index + 1 < len(members) else archive.start_dir
                    remaining = end - member.header_offset
                    if remaining < member.compress_size:
                        raise ValueError('Invalid compressed file record')
                    archive.fp.seek(member.header_offset)
                    cloned = copy.copy(member)
                    cloned.header_offset = destination.fp.tell()
                    while remaining:
                        block = archive.fp.read(min(8 * 1024 * 1024, remaining))
                        if not block:
                            raise EOFError(member.filename)
                        destination.fp.write(block)
                        remaining -= len(block)
                    destination.filelist.append(cloned)
                    destination.NameToInfo[cloned.filename] = cloned
                    destination.start_dir = destination.fp.tell()
            print('Merged ' + record['file'], flush=True)
    with zipfile.ZipFile(partial) as archive:
        actual = {m.filename: (m.file_size, m.CRC) for m in archive.infolist()}
        if actual != expected or len(archive.infolist()) != len(expected):
            raise ValueError('Merged ZIP differs from verified source archives')
        damaged = archive.testzip()
        if damaged:
            raise ValueError('ZIP CRC failed: ' + damaged)
        readme = archive.read(top + '/PORTABLE-README.txt')
        uncompressed = sum(m.file_size for m in archive.infolist())
    os.replace(partial, target)
    record = {'file': target.name, 'component': 'all', 'bytes': target.stat().st_size,
              'sha256': sha(target), 'files': len(expected), 'uncompressed_bytes': uncompressed,
              'zip_crc_verified': True}
    checked(output / '使用说明.txt').write_bytes(readme)
    checked(output / 'SHA256SUMS.txt').write_text(record['sha256'] + '  ' + target.name + '\n', encoding='ascii')
    write_json(output / 'release-packages.json', {'version': manifest['version'], 'platform': manifest['platform'],
        'top_level_directory': top, 'model_weights_included': False, 'archives': [record],
        'installation': 'Extract this single ZIP completely, then run PortraitLab.exe and prepare models in the app.'})
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
