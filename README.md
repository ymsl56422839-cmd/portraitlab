# PortraitLab · 映肤工作台

本地人像处理的开源 Windows 软件：批量修肤、逐图调整、保护笔刷、原尺寸导出，以及 **IC-Light 生成式重打光**。0.2.0 提供自带 Python 的便携运行时与双击启动入口。

修肤使用 MediaPipe 定位／分割和 RapidRetouch 非生成式算法；重打光使用 IC-Light v1 FC 与 Realistic Vision v5.1 基座。AI 结果是一张独立的新照片，可能改变五官、头发、衣服和背景，不保证身份或细节完全保真。

## 下载与启动

从 [Releases](https://github.com/ymsl56422839-cmd/portraitlab/releases) 下载 Windows 软件包。Core 包包含界面、经典修肤及其独立解释器；AI 运行组件包含 PyTorch、CUDA 库及推理依赖。若 AI 组件分为多个普通 ZIP，全部解压到同一位置，合并同名 PortraitLab-0.2.0-Windows 文件夹。

双击 **PortraitLab.exe** 即可启动并打开本机工作台，无需另装 Python、Git 或运行终端命令。启动器提供重新打开界面、查看日志和停止服务；关闭网页不代表服务已退出。

系统要求 Windows 10／11 x64。AI 运行库需要 [Microsoft Visual C++ 2015–2022 x64 Runtime](https://aka.ms/vs/17/release/vc_redist.x64.exe)；许多电脑已安装，缺少时按启动器提示安装微软官方运行库。已验证中文目录迁移，尚未在全新 Windows 虚拟机验证全部系统前置条件。

软件可放在可写的任意盘符与中文目录。默认 Data 保存模型、原片副本、编辑记录与输出，Cache 保存运行缓存。可使用启动参数 --data、--cache 或环境变量配置位置。程序只监听 127.0.0.1，不作为公网服务部署。

公开软件包不包含模型权重。首次使用：

1. 打开左下角“处理模型”，点击“下载并校验经典模型”。
2. 使用重打光时，打开“AI 重打光”，点击“下载并校验模型”。IC-Light 与基座共约 3.59 GiB，另需要 AI 运行组件。
3. 模型准备后导入照片。下载来自清单固定的上游地址，逐文件校验 SHA-256；中断后可重试。

模型下载需要联网，照片处理使用本机模型。推理不会为了补齐模型而自动联网。应用没有实现照片上传服务；第三方 SDK 的网络行为仍受其实现影响，验证边界见文档。

## 硬件与通用性

| 使用方式 | 建议 |
|---|---|
| 经典修肤 | Windows x64、CPU；无需 NVIDIA 显卡 |
| AI 入门目标 | NVIDIA 8 GB 显存，512 像素档，低显存模式 |
| AI 推荐 | NVIDIA 12 GB 或以上；先试 512／768 档 |
| 无兼容 CUDA 设备 | 可选择 CPU；已验证 256×256 小图，较大尺寸明显更慢 |
| 内存与磁盘 | 建议 32 GB 内存；完整运行时和模型约 11 GB，另为解压、原片和输出预留空间 |

当前实测硬件是 RTX 5060 Ti 16 GB。8 GB 是保守兼容目标，不是对所有 GPU、驱动及尺寸的保证；4070 Ti 12 GB 符合目标显存档，但未在该具体型号上实测。低显存模式将模型分阶段移到内存，降低峰值显存；不支持 BF16 的设备使用相应回退类型。批量 AI 任务逐张运行。

AMD／Intel GPU 加速尚未接入，仍可使用 CPU。首批发行与验证面向 Windows x64；其他平台尚未完成同等打包验证。驱动需要支持随 AI 组件提供的 CUDA 12.8 运行时，无需单独安装 CUDA Toolkit。

## 使用流程

- **修肤：**导入照片，选择预设并检查自动选区。保护笔刷排除经典修肤与面部局部提亮，不能阻止全局曝光／色彩调整。
- **批量：**同步数值参数后逐张复查。每张图保留自己的保护笔刷；导出使用各自保存的参数。
- **重打光：**选择照片，点击“AI 重打光”，选择光线预设、方向、设备、尺寸与种子。输入取提交时的当前修肤快照；想从未修肤图开始可先选“原图”预设。
- **检查生成结果：**新图进入照片库，初始不再叠加第二遍修肤。可以继续调整并导出；生成提示词、种子、模型哈希和原片关系随导出旁车 JSON 保存。

AI 会按目标比例缩放并中心裁切，输出为指定的生成尺寸，**不是原片全尺寸重建**。经典保护笔刷不直接约束扩散模型。不同尺寸、设备或依赖可能得到不同结果；相同种子不保证跨平台逐像素一致。

## 格式与数据

导入支持单帧 JPEG、PNG、TIFF、WebP；不支持 RAW、动画和多页图像。每批导入最多 50 张，单文件最多 100 MiB，单图最多 80 MP。程序保留原始文件副本，工作图按 EXIF 转正并转换到 8-bit sRGB。

JPEG、PNG、TIFF 导出均为 **8-bit sRGB**，嵌入 ICC；不复制原图 EXIF。经典修肤可按原尺寸重算，AI 图按其实际生成尺寸导出。输出不覆盖原片，附带参数与来源 JSON。

便携默认数据位于软件目录，也可通过 PORTRAITLAB_DATA、PORTRAITLAB_CACHE 配置。PORTRAITLAB_AI_PYTHON 指向独立 AI 解释器，PORTRAITLAB_RELIGHT_MODELS 指向 AI 模型目录。PORTRAITLAB_STORAGE_ROOT 是可选的写入根限制；默认不绑定固定盘符。源码检出的布局详见复现文档。

## 从源码运行和构建

准备 64 位 Python 3.12，在源码目录执行：

    .\setup.ps1 -PythonPath <python.exe路径> -DownloadModels
    .\Launch.cmd

经典环境使用 requirements.lock.txt。AI 使用独立环境，CUDA Torch 固定为 2.7.1+cu128，其余依赖及模型清单随源码提供。安装 AI 的具体命令和便携构建步骤见 [构建说明](docs/BUILDING.md)，实验记录方式见 [复现说明](docs/REPRODUCING.md)；直接下载便携包不需要执行这些命令。

## 来源、许可与验证

PortraitLab 与 RapidRetouch 衍生代码采用 **AGPL-3.0-only**，保留 Chris Cork 的上游版权；IC-Light 参考代码为 **Apache-2.0**。IC-Light 和基座权重使用 **CreativeML OpenRAIL-M**，其使用限制单独适用。个人非商业研究计划不会把 AGPL 改成禁止商用的许可证。

MediaPipe 框架 Apache-2.0 已核验，具体下载权重的版本许可仍单独记录为未独立核实，不以框架许可替代权重条款。公开包不携带用户照片、凭据、会话状态或权重。二进制附带依赖许可证，对应源码与构建脚本同步公开。

- [方法与参数](docs/METHODS.md) · [重打光原理与边界](docs/RELIGHTING.md)
- [来源与固定版本](docs/SOURCES.md) · [第三方许可说明](THIRD_PARTY_NOTICES.md)
- [复现和构建](docs/REPRODUCING.md) · [验证记录](docs/VALIDATION.md)
- [已知限制](docs/LIMITATIONS.md) · [AGPL 完整许可证](LICENSE)
