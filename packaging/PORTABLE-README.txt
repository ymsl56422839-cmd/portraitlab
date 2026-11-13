照影 · PortraitLab 0.4.0 · Windows 人像修图

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
GitHub 分包版：下载 Core 和全部 AI-xx-of-xx.zip，先解压 Core，
再把全部 AI 分包解压到同一位置，合并为 PortraitLab-0.4.0-Windows 文件夹。
均为普通 ZIP，无需拼接工具；缺少任一 AI 分包可能导致照片分析不可用。
公开 ZIP 不含权重。经典模型在设置中下载；布光分析模型在“光影重塑”准备。
本地已有模型可直接校验，不必重复下载。Depth Anything V2 Small 约 99 MB。
新交互布光不需下载旧 IC-Light 和扩散基座；分析需要完整 AI 运行组件。

常用操作
22 个经典预设支持搜索、分类、收藏、自定义保存和整体强度调整。
照片可通过顶部、空白画布或布光面板导入，也可拖入文件或文件夹。
光影重塑分析一次照片后，可在画布拖动光源并实时预览，提供十个布光预设。
可选“仅主体 · 保留背景”“仅背景 · 保留主体”或“整张照片”。
“显示作用选区”只高亮查看范围，不写入照片；羽化边缘按分割权重过渡。
拖灯或调灯光参数会自动关闭选区叠加，显示实际效果；只有原图视图时会显示结果。
中央状态区区分正在预览、尚未保留与已保留，预览和保存共用修片底图。
“保留调整”写入当前照片，支持撤销和重启恢复；照片库移除也可撤销。
批量同步保留每张照片的保护笔刷，支持撤销上次批量。

设备
经典处理支持自动、CPU、NVIDIA GPU 合成；GPU 需要 AI 运行组件和兼容驱动。
GPU 执行曝光、色彩及混合，识别、修肤和文件压缩仍使用 CPU。
布光分析独立选择 CPU、自动或 NVIDIA GPU，画布预览优先使用 WebGL2，
不可用时降为较小尺寸的 CPU 预览；原尺寸导出使用 CPU 分块计算。
AMD/Intel 专用分析后端尚未接入，可使用 CPU；WebGL2 依赖浏览器和驱动。
原有阴影已保留在照片中，近似布光不能保证消除旧阴影或准确投射新阴影。
未选中像素保留修片底图数值；精确检查使用 PNG，JPEG 压缩可能改变像素。

保存位置与升级
软件可放在任意可写盘符，支持中文路径，不依赖原开发电脑。
默认 Data 保存原片副本、编辑记录、模型和输出，Cache 保存运行缓存。
可用 --data / --cache 或 PORTRAITLAB_DATA / PORTRAITLAB_CACHE 自选位置。
移动或升级前，先在每个窗口保留或放弃草稿，再关闭所有照影窗口。
完整 0.2.0／0.3.0 可使用 Upgrade 包，合并全部升级内容，保留 Data 和 Cache。
升级包不含运行时，新安装应使用完整包；不能只复制 EXE。
重新开一个窗口会复用仍在运行的后台，更新后须完整退出再打开。
若出现“软件更新尚未生效”，关闭所有照影窗口后重新打开即可加载新版。

源码与许可
app 内包含源码、构建脚本、方法与验证记录。应用采用 AGPL-3.0-only；
Python、WebView2、依赖库和模型各自保留独立许可。
详见 LICENSE、THIRD_PARTY_NOTICES.md 和 Licenses。
模型固定来源和 SHA-256 位于 app/models；下载通过后才能用于推理。
https://github.com/ymsl56422839-cmd/portraitlab
