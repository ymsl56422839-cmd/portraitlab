"""Fetch the pinned official WebView2 SDK and stage its x64 desktop bindings.

The Evergreen browser runtime remains a separately installed Microsoft component.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
LOCK = Path(__file__).with_name('webview2.lock.json')


def checked(path):
    path = Path(path).expanduser().resolve()
    boundary = os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not path.is_relative_to(Path(boundary).expanduser().resolve()):
        raise ValueError('WebView2 output is outside PORTRAITLAB_STORAGE_ROOT')
    return path


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prepare(work, destination):
    work, destination = checked(work), checked(destination)
    work.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    spec = json.loads(LOCK.read_text(encoding='utf-8'))
    package = checked(work / ('microsoft.web.webview2.' + spec['version'] + '.nupkg'))
    if not package.is_file() or digest(package) != spec['sha256']:
        partial = checked(package.with_suffix('.nupkg.part'))
        with urllib.request.urlopen(spec['url'], timeout=45) as incoming, partial.open('wb') as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        if partial.stat().st_size != spec['package_bytes'] or digest(partial) != spec['sha256']:
            partial.unlink()
            raise ValueError('The official WebView2 package does not match the pinned SHA256')
        os.replace(partial, package)
    license_dir = checked(destination / 'Licenses/WebView2')
    license_dir.mkdir(parents=True, exist_ok=True)
    deployed = []
    with zipfile.ZipFile(package) as archive:
        for entry in spec['files']:
            contents = archive.read(entry['path'])
            if len(contents) != entry['bytes'] or hashlib.sha256(contents).hexdigest() != entry['sha256']:
                raise ValueError('WebView2 component checksum mismatch: ' + entry['path'])
            if entry['destination'].endswith('.dll'):
                target = checked(destination / entry['destination'])
            else:
                target = checked(license_dir / entry['destination'])
                upstream = PROJECT / 'licenses/webview2' / entry['destination']
                if upstream.read_bytes() != contents:
                    raise ValueError('The retained SDK license differs from the pinned NuGet package')
            target.write_bytes(contents)
            deployed.append({'file': str(target.relative_to(destination)).replace('\\', '/'),
                             'sha256': entry['sha256'], 'bytes': entry['bytes']})
    shutil.copy2(LOCK, checked(license_dir / 'provenance.json'))
    report = {'sdk_package': spec['package'], 'sdk_version': spec['version'],
              'package_url': spec['url'], 'package_sha256': spec['sha256'], 'files': deployed,
              'runtime_policy': spec['runtime']}
    checked(destination / 'webview2-manifest.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.work, args.destination)))
