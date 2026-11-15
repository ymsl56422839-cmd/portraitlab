"""Exercise a staged V2 desktop package without changing its normal photo library.

Uses an isolated data/cache directory and local ports; never downloads a model.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


def run(package: Path, work: Path, port: int, desktop: bool) -> dict:
    package, work = package.resolve(), work.resolve()
    if not package.is_dir() or not (package / 'PortraitLab.exe').is_file():
        raise ValueError('Expected a staged package containing PortraitLab.exe')
    boundary = os.environ.get('PORTRAITLAB_STORAGE_ROOT')
    if boundary and not work.is_relative_to(Path(boundary).resolve()):
        raise ValueError('Smoke output is outside PORTRAITLAB_STORAGE_ROOT')
    work.mkdir(parents=True, exist_ok=True)
    data, cache = work / '测试资料 data', work / '测试缓存 cache'
    temp = work / 'temp'
    temp.mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE='1')
    # The package's ordinary Data remains untouched, including its photo library.
    base = [str(package / 'PortraitLab.exe'), '--data', str(data), '--cache', str(cache), '--port', str(port)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    process = None
    report = {'package': str(package), 'work': str(work), 'ok': False}
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def request(url, token=None):
        headers = {'X-PortraitLab-Token': token} if token else {}
        message = urllib.request.Request(url, data=b'' if token else None, headers=headers)
        with opener.open(message, timeout=4) as response:
            return json.load(response)

    def wait_exit(child, timeout=25):
        code = child.wait(timeout=timeout)
        if code != 0:
            raise AssertionError('Launcher exited with code ' + str(code))

    try:
        # A listening foreign service must never be mistaken for our backend.
        occupied.bind(('127.0.0.1', port))
        occupied.listen(8)
        process = subprocess.Popen(base + ['--headless'], cwd=package, env=environment, creationflags=flags)
        status_file = data / 'logs/launcher-status.json'
        deadline = time.monotonic() + 75
        status = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('Launcher exited before backend became ready')
            try:
                candidate = json.loads(status_file.read_text(encoding='utf-8'))
                if candidate.get('ready') and candidate.get('launcher_pid') == process.pid:
                    status = candidate
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        if status is None:
            raise TimeoutError('Launcher did not write a ready status; inspect ' + str(data / 'logs/launcher.log'))
        assert status['port'] == port + 1, status
        assert Path(status['data_dir']).resolve() == data.resolve(), status
        assert Path(status['cache_dir']).resolve() == cache.resolve(), status
        assert Path(status['runtime']).resolve() == (package / 'runtime/python.exe').resolve(), status
        assert status['owns_server'], status
        bootstrap = request(status['url'] + '/api/bootstrap')
        assert bootstrap['application'] == 'portraitlab-v2', bootstrap
        assert Path(bootstrap['storage']['data_dir']).resolve() == data.resolve(), bootstrap
        duplicate = subprocess.Popen(base + ['--headless'], cwd=package, env=environment, creationflags=flags)
        try:
            wait_exit(duplicate, 8)
        finally:
            if duplicate.poll() is None:
                duplicate.kill()
        current = json.loads(status_file.read_text(encoding='utf-8'))
        assert current['launcher_pid'] == process.pid, current
        assert current['server_pid'] == status['server_pid'], current
        report.update(port_collision_skipped=True, single_instance=True, package_relative_runtime=True,
                      chinese_space_data_paths=True, headless_status=status)
        request(status['url'] + '/api/shutdown', bootstrap['token'])
        wait_exit(process)
        process = None
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            assert probe.connect_ex(('127.0.0.1', status['port'])) != 0, 'Backend remains listening after exit'
        finally:
            probe.close()
        report['backend_shutdown'] = True
        occupied.close()
        if desktop:
            process = subprocess.Popen(base + ['--webview-smoke-test'], cwd=package, env=environment, creationflags=flags)
            wait_exit(process, 90)
            process = None
            smoke = json.loads((data / 'logs/desktop-smoke.json').read_text(encoding='utf-8'))
            assert smoke['ok'] and smoke['embedded'] and smoke['async_save_waited'], smoke
            report['desktop'] = smoke
        report['ok'] = True
        return report
    finally:
        occupied.close()
        if process is not None and process.poll() is None:
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], cwd=work,
                               env=environment, creationflags=flags, check=False, stdout=subprocess.DEVNULL)
            else:
                process.kill()
            process.wait(timeout=10)
        (work / 'launcher-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18960)
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.package, args.work, args.port, args.desktop), ensure_ascii=False))
