"""Build the V2 native desktop entry point using the pinned WebView2 SDK.

The browser itself must be staged separately in destination/WebView2.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
# Embedded CPython does not automatically put a script's directory on sys.path.
sys.path.insert(0, str(HERE))
from webview2 import checked, prepare


def build_launcher(destination: Path, work: Path, sdk_source: Path | None = None) -> dict:
    destination, work = checked(destination), checked(work)
    destination.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    temp = checked(work / 'temp')
    temp.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1')
    spec = json.loads((HERE / 'webview2.lock.json').read_text(encoding='utf-8'))
    if sdk_source is None:
        sdk_report = prepare(work, destination)
    else:
        sdk_source = Path(sdk_source).resolve()
        license_dir = checked(destination / 'Licenses/WebView2')
        license_dir.mkdir(parents=True, exist_ok=True)
        deployed = []
        for entry in spec['files']:
            is_dll = entry['destination'].endswith('.dll')
            source = sdk_source / entry['destination'] if is_dll else HERE.parent / 'licenses/webview2' / entry['destination']
            target = checked((destination if is_dll else license_dir) / entry['destination'])
            contents = source.read_bytes()
            if len(contents) != entry['bytes'] or hashlib.sha256(contents).hexdigest() != entry['sha256']:
                raise ValueError('Pinned WebView2 SDK checksum mismatch: ' + str(source))
            if source.resolve() != target.resolve():
                target.write_bytes(contents)
            deployed.append({'file': target.relative_to(destination).as_posix(), 'sha256': entry['sha256'], 'bytes': entry['bytes']})
        shutil.copy2(HERE / 'webview2.lock.json', checked(license_dir / 'provenance.json'))
        sdk_report = {'sdk_package': spec['package'], 'sdk_version': spec['version'],
                      'package_url': spec['url'], 'package_sha256': spec['sha256'],
                      'files': deployed, 'runtime_policy': 'Fixed Version runtime bundled in WebView2/'}
    sdk_report['runtime_policy'] = 'Fixed Version runtime bundled in WebView2/'
    checked(destination / 'webview2-manifest.json').write_text(json.dumps(sdk_report, indent=2) + '\n', encoding='utf-8')
    windows = Path(os.environ.get('WINDIR', 'C:/Windows'))
    compiler = next((windows / f'Microsoft.NET/{folder}/v4.0.30319/csc.exe' for folder in ('Framework64', 'Framework')
                     if (windows / f'Microsoft.NET/{folder}/v4.0.30319/csc.exe').is_file()), None)
    if compiler is None:
        raise RuntimeError('Windows .NET Framework C# compiler is required to build the launcher')
    manifest = checked(work / 'PortraitLabV2.manifest')
    manifest.write_text('''<?xml version="1.0" encoding="utf-8"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
 <assemblyIdentity version="0.7.0.0" name="PortraitLab.Desktop.V2"/>
 <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
 <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings><longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware><dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware></windowsSettings></application>
</assembly>''', encoding='utf-8')
    executable = checked(destination / 'PortraitLab.exe')
    command = [str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
               '/out:' + str(executable), '/win32manifest:' + str(manifest),
               '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Web.Extensions.dll', '/r:System.Core.dll',
               '/r:' + str(destination / 'Microsoft.Web.WebView2.Core.dll'),
               '/r:' + str(destination / 'Microsoft.Web.WebView2.WinForms.dll'), str(HERE / 'V2Launcher.cs')]
    subprocess.run(command, cwd=work, env=environment, check=True)
    return {'launcher': str(executable), 'bytes': executable.stat().st_size,
            'sha256': hashlib.sha256(executable.read_bytes()).hexdigest(), 'webview2_sdk': sdk_report['sdk_version']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sdk-source', type=Path)
    args = parser.parse_args()
    print(json.dumps(build_launcher(args.destination, args.work, args.sdk_source)))
