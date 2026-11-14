"""Build a model-free application update for an existing 0.2/0.3/0.4 portable folder.

ZIP entries start at the application root; runtime, AI, Data and Cache are never
included. The recipient closes PortraitLab before extracting into its folder.
SPDX-License-Identifier: AGPL-3.0-only
"""
from __future__ import annotations
import sys
sys.dont_write_bytecode = True
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import zipfile

from build_portable import checked, mkdir, sha, write_json

ROOT_FILES = (
    'PortraitLab.exe', 'Microsoft.Web.WebView2.Core.dll',
    'Microsoft.Web.WebView2.WinForms.dll', 'WebView2Loader.dll',
    'webview2-manifest.json', 'portable-build.json', 'source-manifest.json',
    'PORTABLE-README.txt', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md',
    'MODEL_LICENSES.md', 'REPRODUCING.md',
)
REQUIRED = tuple(name for name in ROOT_FILES if name not in ('NOTICE', 'MODEL_LICENSES.md', 'REPRODUCING.md'))
EXCLUDED = {'runtime', 'ai', 'data', 'cache'}


def member_name(path, package):
    if not path.resolve().is_relative_to(package):
        raise ValueError('Refusing an upgrade member outside the package: ' + str(path))
    name = path.relative_to(package).as_posix()
    parts = PurePosixPath(name).parts
    if not parts or parts[0].lower() in EXCLUDED or '..' in parts:
        raise ValueError('Disallowed upgrade member: ' + name)
    if path.resolve() != path.absolute():
        raise ValueError('Upgrade members must not traverse symlinks or directory junctions: ' + name)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    package, output = args.package.resolve(), mkdir(args.output)
    if package == output or output.is_relative_to(package):
        raise ValueError('Upgrade output must be outside the portable folder')
    for name in REQUIRED:
        if not (package / name).is_file():
            raise ValueError('Incomplete desktop package: ' + name)
    build = json.loads((package / 'portable-build.json').read_text(encoding='utf-8'))
    version = build['version']
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', version):
        raise ValueError('Invalid package version')
    if build.get('platform') != 'Windows x64':
        raise ValueError('Only Windows x64 portable packages are supported')
    files = []
    for folder in ('app', 'Licenses'):
        if not (package / folder).is_dir():
            raise ValueError('Missing upgrade component: ' + folder)
        files.extend(p for p in (package / folder).rglob('*') if p.is_file())
    files.extend(package / name for name in ROOT_FILES if (package / name).is_file())
    members = {member_name(path, package): path for path in files}
    if len(members) != len(files):
        raise ValueError('Duplicate upgrade archive member')
    expected_source = json.loads((package / 'source-manifest.json').read_text(encoding='utf-8'))
    actual_source = {name[4:]: sha(path) for name, path in members.items() if name.startswith('app/')}
    if actual_source != expected_source:
        raise ValueError('Application files differ from source-manifest.json; restage the app before packaging')
    inventory = {name: {'bytes': path.stat().st_size, 'sha256': sha(path)} for name, path in sorted(members.items())}
    webview = json.loads((package / 'webview2-manifest.json').read_text(encoding='utf-8'))
    for component in webview['files']:
        if inventory.get(component['file']) != {'bytes': component['bytes'], 'sha256': component['sha256']}:
            raise ValueError('WebView2 files differ from their verified manifest: ' + component['file'])
    upgrade = {'version': version, 'base_versions': ['0.2.0', '0.3.0', '0.4.0'], 'platform': build['platform'],
               'layout': 'application-root', 'preserved_directories': ['runtime', 'AI', 'Data', 'Cache'],
               'model_weights_included': False, 'files': inventory}
    readme = (
        f'照影 / PortraitLab {version} 小升级包\n\n'
        '适用：已完整解压并可运行的 PortraitLab 0.4.0 Windows x64 软件目录；0.2.0 / 0.3.0 也列入兼容运行时基线。\n'
        '已有 0.4.0 完整包只需本补丁；新用户只需 0.4.0 完整包加本补丁，无需重新下载大运行时包。\n\n'
        '本次更新：矩形局部调整；通用、长者人像、成人人像、少年人像四类 52 个预设；\n'
        '独立去痘强度；更醒目的模型下载与设备入口；鼠标悬停图像视窗时按 V 轮换原图、滑动对比、处理结果。\n'
        '去痘修复检测到的面部独立痘点，可能漏检或误判；可先框选，并用保护笔刷保留痣、雀斑与妆面。\n'
        '分类由用户选择，不自动识别年龄；长者方案默认不磨皮。批量只同步全局参数，保留各张局部与笔刷。\n\n'
        '1. 先保存当前操作并关闭所有照影 / PortraitLab 窗口，确认处理与模型下载任务已停止。只再开一个窗口会复用旧后台。\n'
        '2. 建议先备份原软件目录中的 Data（照片、参数、设置及模型）。\n'
        '3. 将本 ZIP 的全部内容直接解压到原软件目录，与 PortraitLab.exe 放在同一级，选择覆盖程序文件。\n'
        '   ZIP 内没有额外的顶层文件夹，请勿解压成原目录下的新子目录。\n'
        '4. 双击 PortraitLab.exe，打开「照影」单窗口界面。\n\n'
        '本升级包仅更新 app、启动器、WebView2 SDK 组件、许可和说明；不包含也不覆盖 runtime、AI、Data 或 Cache。\n'
        '原版 Python / AI 运行环境继续使用，已有照片、编辑参数、收藏和模型保留。补丁不包含新模型。\n'
        '旧预览缓存会按新版处理重算；原尺寸处理后缩图与旧版缩图修片可能有细节差异，不承诺旧预览像素完全相同。\n'
        '桌面界面需要 Microsoft Edge WebView2 Evergreen Runtime。缺少时程序会提供微软官方链接；该浏览器组件不包含在升级包内，适用微软自己的许可条款。\n'
        'WebView2 SDK 原始许可与来源见 Licenses/WebView2；应用与模型的其他许可见 THIRD_PARTY_NOTICES.md。\n'
    )
    target = checked(output / f'PortraitLab-{version}-Windows-Upgrade.zip')
    partial = checked(target.with_suffix('.zip.part'))
    if target.exists() or partial.exists():
        raise FileExistsError('Upgrade archives are never overwritten: ' + str(target))
    with zipfile.ZipFile(partial, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for name, path in sorted(members.items()):
            archive.write(path, name)
        archive.writestr('UPGRADE-README.txt', readme.encode('utf-8-sig'))
        archive.writestr('upgrade-manifest.json', json.dumps(upgrade, ensure_ascii=False, indent=2).encode('utf-8'))
    with zipfile.ZipFile(partial) as archive:
        if archive.testzip():
            raise ValueError('Upgrade ZIP CRC validation failed')
        names = archive.namelist()
        if any(PurePosixPath(name).parts[0].lower() in EXCLUDED for name in names):
            raise ValueError('A preserved directory unexpectedly entered the upgrade')
        if set(names) != set(members) | {'UPGRADE-README.txt', 'upgrade-manifest.json'}:
            raise ValueError('Upgrade archive inventory mismatch')
    os.replace(partial, target)
    record = {'file': target.name, 'version': version, 'bytes': target.stat().st_size,
              'sha256': sha(target), 'files': len(names), 'zip_crc_verified': True,
              'preserved_directories': upgrade['preserved_directories'], 'base_versions': upgrade['base_versions']}
    write_json(output / 'upgrade-package.json', record)
    checked(output / 'UPGRADE-README.txt').write_text(readme, encoding='utf-8-sig')
    checked(output / 'SHA256SUMS.txt').write_text(record['sha256'] + '  ' + target.name + '\n', encoding='ascii')
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
