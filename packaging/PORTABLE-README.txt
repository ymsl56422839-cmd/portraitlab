照影 · PortraitLab 0.3.0 · Windows 人像修图

启动
完整解压后双击 PortraitLab.exe，直接进入软件操作界面。
运行日志在设置中查看。关闭软件会结束该窗口启动的服务；运行任务时会提示。
软件自带 Python，无需安装 Python、Git 或 CUDA Toolkit。
系统要求 Windows 10/11 x64、Microsoft Edge WebView2 Evergreen Runtime、
Microsoft Visual C++ 2015–2022 x64 Runtime。缺少时按程序提示从微软安装：
https://developer.microsoft.com/en-us/microsoft-edge/webview2/
https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170

下载与解压
单包版：只需完整解压 All-in-One.zip，不要只发送或移动 EXE。
GitHub 分包版：把 Core 和所有 AI-xx-of-xx.zip 解压到同一位置，
合并成一个 PortraitLab-0.3.0-Windows 文件夹。均为普通 ZIP，无需拼接工具。
公开 ZIP 不含权重。经典模型在设置中下载；AI 模型在重打光面板准备。
本地已有模型可直接校验，不必重复下载。AI 模型约 3.59 GiB。

常用操作
22 个经典预设支持搜索、分类、收藏、自定义保存和整体强度调整。
照片可通过顶部、空白画布或重打光面板导入，也可拖入文件或文件夹。
重打光默认使用当前照片；多图生成要明确开启批量并选择目标。
批量同步保留每张照片的保护笔刷，支持撤销上次批量。

设备
经典处理支持自动、CPU、NVIDIA GPU 合成；GPU 需要 AI 运行组件和兼容驱动。
GPU 执行曝光、色彩及混合，识别、修肤和文件压缩仍使用 CPU。
生成式重打光有独立设备选择。建议 NVIDIA 12 GB 或以上；
8 GB / 512 档 / 低显存模式是兼容目标，尚未在所有显卡上验证。
AMD/Intel GPU 加速尚未接入；无兼容 CUDA 设备可选择较慢的 CPU。
生成结果可能改变面部、衣物和背景；结果作为新照片保存，原片保留。

保存位置与升级
软件可放在任意可写盘符，支持中文路径，不依赖原开发电脑。
默认 Data 保存原片副本、编辑记录、模型和输出，Cache 保存运行缓存。
可用 --data / --cache 或 PORTRAITLAB_DATA / PORTRAITLAB_CACHE 自选位置。
移动目录前先正常退出。升级旧版时保留 Data，勿用空目录覆盖它。

源码与许可
app 内包含源码、构建脚本、方法与验证记录。应用采用 AGPL-3.0-only；
Python、WebView2、依赖库和模型各自保留独立许可。
详见 LICENSE、THIRD_PARTY_NOTICES.md 和 Licenses。
模型固定来源和 SHA-256 位于 app/models；下载通过后才能用于推理。
https://github.com/ymsl56422839-cmd/portraitlab
