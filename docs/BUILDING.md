# 构建 Windows 便携版

构建产物包含原生 `PortraitLab.exe`、WebView2 桌面壳、两个独立的嵌入式 Python 运行时、应用源码和许可证。用户无需安装 Python。目标系统是 Windows 10/11 x64，需要 Microsoft Visual C++ 2015–2022 x64 Runtime 及 Microsoft Edge WebView2 Evergreen Runtime；缺少时程序提供微软官方安装入口。正常运行直接在一个软件窗口内显示操作界面。

下面命令在源码根目录执行，示例把所有构建、临时和缓存文件放到 `F:\Codex\PortraitLab-build`。发行程序本身不限制盘符：默认使用软件旁的 `Data` 和 `Cache`，也支持环境变量和启动参数指定目录。`PORTRAITLAB_STORAGE_ROOT` 是可选的构建或部署边界，设置后会校验写入位置。

## 准备独立依赖环境

需要 64 位 CPython 3.12、网络，以及 Windows .NET Framework 4.x 自带的 `csc.exe`。不要把开发 venv 目录直接作为便携运行时分发。经典处理与 AI 的 NumPy/Pillow 版本不同，必须使用两个环境。

```powershell
$buildRoot = 'F:\Codex\PortraitLab-build'
New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null
$env:PORTRAITLAB_STORAGE_ROOT = 'F:\Codex'
$env:TEMP = Join-Path $buildRoot 'temp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $buildRoot 'cache\pip'
$env:HF_HOME = Join-Path $buildRoot 'cache\huggingface'
$env:TORCH_HOME = Join-Path $buildRoot 'cache\torch'
$env:MPLCONFIGDIR = Join-Path $buildRoot 'cache\matplotlib'
$env:PYTHONDONTWRITEBYTECODE = '1'
New-Item -ItemType Directory -Force -Path $env:TEMP,$env:PIP_CACHE_DIR,$env:HF_HOME,$env:TORCH_HOME,$env:MPLCONFIGDIR | Out-Null

py -3.12 -m venv (Join-Path $buildRoot 'core-env')
py -3.12 -m venv (Join-Path $buildRoot 'ai-env')
$corePython = Join-Path $buildRoot 'core-env\Scripts\python.exe'
$aiPython = Join-Path $buildRoot 'ai-env\Scripts\python.exe'
& $corePython -B -m pip install --no-compile -r requirements.lock.txt
& $aiPython -B -m pip install --no-compile torch==2.7.1+cu128 --index-url https://download.pytorch.org/whl/cu128
& $aiPython -B -m pip install --no-compile -r requirements-relight.lock.txt
```

经典依赖锁定在 `requirements.lock.txt`；AI 使用 `requirements-relight.lock.txt`，其中 Torch 2.7.1+cu128、Diffusers 0.27.2、Transformers 4.36.2、NumPy 1.26.4、Pillow 10.4.0。CUDA 运行库随 Torch wheel 提供，使用 NVIDIA GPU 仍需要兼容驱动；AI 可以选择 CPU 模式。

## 构建完整目录

```powershell
$portable = Join-Path $buildRoot 'PortraitLab-0.3.0-Windows'
$packWork = Join-Path $buildRoot 'packaging-work'
& $corePython -B packaging\build_portable.py `
  --destination $portable --work $packWork `
  --core-env (Join-Path $buildRoot 'core-env') `
  --ai-env (Join-Path $buildRoot 'ai-env') --stage all
```

构建器从 python.org 下载 CPython **3.12.10 Windows x64 embedded ZIP**，核对固定 SHA-256，再把各自 venv 的 `Lib/site-packages` 复制到干净运行时。下载缓存保留在 `packaging-work/downloads`。摘要来自 python.org 对应发布的 Sigstore 文档；本脚本执行摘要校验，没有验证完整 Sigstore 证书链。

运行时使用 `python312._pth` 限定模块路径；不复制 `pyvenv.cfg`，不依赖原 venv 或开发机器的 Python 安装。`runtime-manifest.json` 记录解释器来源。`app` 只复制构建器列明的源码目录和文件，`source-manifest.json` 记录复制后的 SHA-256。所有 wheel 的 dist-info 保留，许可证清单位于 `Licenses/runtime` 和 `Licenses/AI`；wheel 未携带的原文由 `licenses/runtime-extra` 及其来源记录补充。

可分别重建 `--stage core`、`ai`、`app`、`launcher`、`models`。只会重建带 `.portraitlab-generated` 标记的 `runtime`、`AI`、`app` 直接子目录，避免误删外部目录；不会清空 `Data`。`--stage app` 同时刷新文档、许可证清单和源码摘要。

桌面壳使用固定的 Microsoft.Web.WebView2 SDK，来源和 SHA-256 在 `packaging/webview2.lock.json`。构建器从官方 NuGet 获取并验证，复制 Core／WinForms 托管 DLL 和 x64 Loader；原始许可与来源记录在 `Licenses/WebView2`。Evergreen 浏览器 Runtime 是系统前置依赖，不包含在应用包中。WebView 的 profile、cache 与 crash 路径使用配置的 Cache 目录。正常关闭先等待前端保存，再停止自己创建的服务；`--headless --no-browser` 用于自动验证。

## 准备验证用模型

可从工作台下载模型，也可把已按 `models/manifest.json` 和 `models/relight-manifest.json` 核验的模型目录复制进来。`--models` 指向包含以下子目录的父目录：

```text
face_landmarker/face_landmarker.task
selfie_multiclass/selfie_multiclass_256x256.tflite
ic-light/base/{tokenizer,text_encoder,vae,unet}/...
ic-light/iclight_sd15_fc.safetensors
```

```powershell
& $corePython -B packaging\build_portable.py `
  --destination $portable --work $packWork `
  --models (Join-Path $buildRoot 'verified-models') --stage models
```

模型可以保留在个人使用的完整目录中；公开发行 ZIP 不带任何模型权重。不要把导入照片、运行状态、日志或缓存放进发行目录。

## 实际移动与运行验证

```powershell
& $corePython -B packaging\verify_portable.py `
  --package $portable `
  --copy-to (Join-Path $buildRoot '移动验证\PortraitLab 中文路径') `
  --report (Join-Path $buildRoot 'portable-verification.json') `
  --port 18875 --ai-generate
```

目标必须不存在。验证器复制整包后故意设置无效的 `PYTHONHOME` 和 `PYTHONPATH`，确认核心和 AI 的导入、`sys.path`、`sys.base_prefix` 均来自新目录；启动原生 EXE，实际导入测试 PNG、进行 MediaPipe 预览，再通过带 token 的本地 API 关闭并检查进程退出。

`--ai-generate` 额外使用新目录下的 AI Python、模型和中文文件名执行 **256×256、10 步、CPU** 真实生成，并核对输出尺寸。默认完整验证还会执行 AI CUDA/CPU 能力探测；只验证经典处理时可改用 `--core-only`，此时不复制 AI 运行时和 IC-Light 模型。

验证发生在复制目录中，原发行目录不会收到测试照片、缓存或状态。现有 Windows 机器上的移动测试不能替代干净 Windows 虚拟机或多种 GPU 的兼容性测试。

## 制作公开发行 ZIP

确认代码和两套运行时冻结后执行：

```powershell
& $corePython -B packaging\make_release.py `
  --package $portable --output (Join-Path $buildRoot 'release') --jobs 2
```

输出一个 `Core.zip` 和若干 `AI-01-of-NN.zip`。每个文件都是可单独打开的普通 ZIP，所有 AI 包包含不同文件，顶层目录相同；用户全部解压到同一位置即可，不需要拼接程序。单个 ZIP 必须小于 **1.8 GiB**，生成后进行 CRC 校验并记录 SHA-256。目录中提供 `release-packages.json` 和 `SHA256SUMS.txt`。

打包清单只包含源码、运行时、启动器、说明和许可证，排除 `Data`、`Cache`、模型权重和处理记录。可用 `--component ai` 先压缩冻结的 AI 运行时，再用 `--component core` 制作最终核心包；后一步会核对并合并已有发行清单。脚本拒绝覆盖已存在的 ZIP，请使用新输出目录进行新一轮发行构建。

发行包生成后，可在移动验证命令中增加 `--archives (Join-Path $buildRoot 'release')`，并选择一个新的 `--copy-to` 目录。验证器先检查发行 ZIP 的 SHA-256，再把所有 ZIP 解压合并到新中文目录；仅从 `--package` 复制已核验模型作为离线测试数据。此模式中的应用源码和两套运行时全部来自实际发行 ZIP。

这是固定依赖与来源的重建流程，不承诺不同构建机器产生逐字节相同的 EXE/ZIP：编译器和文件时间戳仍可能不同。应用、依赖库和模型分别保留各自许可，不能把整个二进制包统一描述为 AGPL。

## 单包转发与升级包

完整发行 ZIP 通过后，可合为一个普通 ZIP，便于直接转发。合并器先验证所有来源 SHA-256，保留已压缩的数据，重新生成目录，再逐项检查 CRC；不添加 Data、Cache 或模型。

```powershell
& $corePython -B packaging\make_share.py `
  --release (Join-Path $buildRoot 'release') --output (Join-Path $buildRoot 'share')
& $corePython -B packaging\make_upgrade.py `
  --package $portable --output (Join-Path $buildRoot 'upgrade')
```

0.3.0 沿用 0.2.0 的两套 Python 依赖。升级包提供应用源码、新 EXE、WebView2 DLL、文档和许可，不带原有运行时与用户数据。用户需先退出旧版，再将升级内容解压到已有软件目录；保留 Data，不能只复制 EXE。新安装应使用完整包。升级包生成后必须在旧版隔离副本验证启动与数据保留。
