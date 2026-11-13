from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from bootstrap import PROJECT, checked, layout, environment

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535: raise ValueError('Port must be 1024..65535')
    paths = layout(); env = environment(paths)
    python = checked(paths['venv'] / 'Scripts/python.exe')
    if not python.exists(): raise RuntimeError('Please run setup.ps1 -DownloadModels first.')
    env['PORTRAITLAB_PORT'] = str(args.port)
    url = f'http://127.0.0.1:{args.port}'
    def open_browser():
        candidates = []
        for variable in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
            if os.environ.get(variable):
                parent = Path(os.environ[variable])
                candidates.extend((parent / 'Microsoft/Edge/Application/msedge.exe',
                                   parent / 'Google/Chrome/Application/chrome.exe'))
        browser = next((p for p in candidates if p.is_file()), None)
        if browser is None:
            print('Open the local application URL in your browser: '+url)
            return
        profile = checked(paths['cache'] / 'browser-profile')
        disk = checked(paths['cache'] / 'browser-cache')
        crashes = checked(paths['cache'] / 'browser-crashes')
        for folder in (profile, disk, crashes): folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([str(browser), '--app='+url, '--user-data-dir='+str(profile),
                          '--disk-cache-dir='+str(disk), '--crash-dumps-dir='+str(crashes),
                          '--no-first-run', '--no-default-browser-check', '--window-size=1440,960'],
                         cwd=PROJECT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def existing():
        try:
            with urllib.request.urlopen(url+'/api/bootstrap', timeout=1) as response:
                data = json.load(response)
            return data.get('version') and Path(data.get('storage', {}).get('data_dir', '')).resolve() == paths['data']
        except Exception:
            return False
    if existing():
        print('PortraitLab is already running: '+url)
        if not args.no_browser: open_browser()
        return 0
    def ready():
        for _ in range(90):
            try:
                with urllib.request.urlopen(url+'/api/bootstrap', timeout=1) as response:
                    data = json.load(response)
                if data.get('version'):
                    if not args.no_browser: open_browser()
                    return
            except Exception: time.sleep(1)
    threading.Thread(target=ready, daemon=True).start()
    try:
        return subprocess.call([str(python), '-B', '-m', 'portraitlab.server', '--port', str(args.port)],
                               cwd=PROJECT, env=env)
    except KeyboardInterrupt: return 0

if __name__ == '__main__': raise SystemExit(main())
