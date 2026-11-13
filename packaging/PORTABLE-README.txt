映肤工作台 · PortraitLab 0.2.0 Windows 便携版

启动
双击 PortraitLab.exe。软件自带 Python 运行时，无需安装 Python 或使用终端。
启动窗口会打开本地工作台；在窗口内可停止处理、重新启动或查看日志。
支持 Windows 10/11 x64；需要本机的 Edge 或 Chrome 打开工作台。
系统须有 Microsoft Visual C++ 2015–2022 x64 运行库。缺少时启动器会提示，
并提供微软官方页面按钮，请选择 x64 版本：
https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170

下载包怎么解压
1. 解压 PortraitLab-0.2.0-Windows-Core.zip。
2. 需要 AI 重打光时，把所有 AI-xx-of-xx.zip 全部解压到与 Core 相同的位置。
   每个 ZIP 都是普通独立压缩包。让内容合入同一个 PortraitLab-0.2.0-Windows
   文件夹即可，不需要拼接文件或使用特殊分卷工具。
3. 双击这个文件夹中的 PortraitLab.exe。
4. GitHub 发行 ZIP 不含模型权重。第一次使用时，在工作台中下载并校验
   经典模型；进入“AI 重打光”后下载相应 AI 模型。
   已另行准备好模型的本地完整目录可直接使用，无需重复下载。

保存位置
默认照片、工程状态和模型保存在软件旁的 Data 文件夹，缓存保存在 Cache。
整个软件目录可以移动；请先正常退出程序，再搬到有写权限的新位置。
支持中文与空格路径。无需固定盘符或原开发机器的 Python 安装目录。
如需自选数据位置，可设置 PORTRAITLAB_DATA / PORTRAITLAB_CACHE 环境变量，
或在快捷方式目标末尾添加 --data "数据目录" --cache "缓存目录"。
程序只会停止当前启动器所创建的处理服务。

AI 重打光
经典修肤可使用 CPU。AI 也支持较慢的 CPU 模式。
NVIDIA GPU 建议从 8 GB 显存、较小图像和低显存模式开始；12 GB 更宽裕。
这些是部署建议，实际占用受图像尺寸、其他程序和驱动影响。
IC-Light 会生成一张新照片，可能改变人脸细节、衣物或背景；原片会保留。

源码与许可
app 文件夹包含应用源码、构建脚本、模型清单及说明。
应用代码使用 AGPL-3.0-only。Python、依赖库和模型各自保留独立许可；
详情见 LICENSE、THIRD_PARTY_NOTICES.md 和 Licenses 文件夹。
模型下载来源、固定版本和 SHA-256 记录在 app/models 中。

完整文档：app/README.md、app/docs/RELIGHTING.md、app/docs/REPRODUCING.md。
