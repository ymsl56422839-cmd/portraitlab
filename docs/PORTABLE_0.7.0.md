# 照影 0.7.0 分享包使用说明

## 打开软件

1. 将完整压缩包解压到本机可写文件夹。不要直接在压缩软件里运行，也不要只拿出 PortraitLab.exe。中文目录和空格目录可用；不要放到系统保护目录、只读介质或网络共享路径。
2. 双击 PortraitLab.exe。首次运行会显示第三方运行组件许可，阅读后选择同意并继续。Windows 显卡驱动仍由操作系统维护；包内不安装驱动。
3. 点击“导入照片”，或把照片拖到中央画布。模型和处理环境已在包内，不需要 Python、Git、CUDA Toolkit 或单独下载模型。

面向 Windows 10/11 x64，系统需提供 .NET Framework 4.6.2 或更新版本（现代 Windows 通常已具备；包内不另装 .NET Framework）。CPU 可处理；NVIDIA GPU 模型推理需要兼容驱动。大图占用更多内存和时间。最低显卡/显存尚未经过跨机器测定，不能把本机测试结果当作所有电脑的保证。系统策略、安全软件或不兼容驱动仍可能阻止运行。

## 发给别人

- 本地分享优先发送名称含 All-in-One 的完整 ZIP；收件人完整解压后打开 PortraitLab.exe。
- GitHub 若提供 part01-of-NN、part02-of-NN 等普通 ZIP，必须全部下载，并把每个 ZIP 解压到同一个父文件夹，让同名软件文件夹合并。它们不是任选其一的安装包，也不是只解压第一部分即可的特殊分卷。
- 保留整个目录结构，包括 Data/models、runtime、AI、WebView2、vendor 和 Licenses。模型已经在正确位置，不需要收件人再复制。
- 分享原始发行 ZIP。自己使用后的 Data 和 Cache 可能含照片、处理记录、导出和缓存，请勿把个人工作目录原样再发给别人。

## 常用操作

修片：先在“肤质与色彩”准备精修。使用“瑕疵修补”时，分析只提出候选，不自动改图；检查原尺寸细节后勾选。漏检位置使用“手动标记”，画完后提交。瑕疵候选栏的“清除修补”只清除自动与手动修补选择，保留其他修片、塑形和灯光调整。中央画布顶部“全部恢复原图”则重置当前照片的全部处理；已保留的调整可用“撤销”找回。修片、塑形、灯光也各有自身的重置按钮。

局部：在画布框选后，可切到“框选区域”独立调整。保护笔刷保留涂抹处原貌，恢复工具可擦除保护。塑形的“固定区域”用于防止轮廓移动，与皮肤保护的用途不同。

光影：准备照片后，选择主体、背景或两者，再选择预设或添加光源。拖动光源可以查看草稿。等待最终预览更新并检查，满意后点击“保留调整”；“取消草稿”丢弃尚未保留的参数。

对照：单框和双框都可选择原图、滑动对比、处理结果。鼠标所在视图按 V 轮换，滚轮或滑块缩放；编辑笔刷启用时按住空格拖动画布。Ctrl+Z 撤销，Ctrl+Shift+Z 重做。点击“导出 PNG”保存结果，不会覆盖外部原片。

## 模型与目录

软件右上角“模型与计算设备”显示就绪状态与计算设备。自动模式会检查可用设备；明确选择 GPU 但驱动不兼容时，应查看错误或手动改选 CPU。

Data/models/analysis/face_landmarker/face_landmarker.task：人脸关键点。
Data/models/analysis/selfie_multiclass/selfie_multiclass_256x256.tflite：人物、皮肤与背景粗分区。
Data/models/analysis/lighting/depth_anything_v2_vits.pth：光影深度分析。
Data/models/neural/joint_20210926.pth：神经瑕疵检测和修补。
Data/models/neural/pytorch_model.pt：神经均肤分支。

Data/photos 保存照片和处理资料，Data/state.json 保存工作记录，Data/exports 保存后端生成的导出结果；桌面“另存为”对话框默认打开软件根目录的 Exports 文件夹。Cache 保存可重建的临时数据和桌面组件用户资料。默认均相对软件文件夹。移动软件时请关闭程序，再完整移动整个文件夹。

## 遇到问题

“模型检查失败”：先确认所有 ZIP 均已完整解压、目录未被拆开，再点“重新检查模型与设备”。文件存在不等于完整，发行清单中的 SHA-256 用于核对内容。

“无法使用 GPU”：查看显卡及驱动状态，或切换 CPU。无需重新下载相同模型；包内包含 GPU 所需的用户态运行库，但不包含显卡驱动。

“正在更新”：大图首次分析或最终渲染可能较慢。等待状态结束再保留、导出；报错时保留完整错误文字和软件版本，以便复现。

软件的照片处理服务在本机运行，所需权重已随包提供。Microsoft WebView2 是单独的浏览器组件，可能按其隐私条款连接 Microsoft；本项目未完成整套运行环境的网络抓包审计，离线处理能力不等于所有第三方组件绝不联网。SmartScreen 相关通知及完整许可可在首次运行对话框和 Licenses 文件夹阅读。

应用源代码为 AGPL-3.0-only。模型和 Microsoft、NVIDIA 等运行组件保留各自许可。重新分发时保留 Licenses、源代码获取方式和构建说明，不移除原始归属与许可。模型列表及其固定来源见 Licenses/Models/V2_MODELS_NOTICE.txt。

本版对应源代码与构建脚本：
源码仓库：https://github.com/ymsl56422839-cmd/portraitlab
0.7.0 固定版本：https://github.com/ymsl56422839-cmd/portraitlab/tree/v0.7.0
0.7.0 源码 ZIP：https://github.com/ymsl56422839-cmd/portraitlab/archive/refs/tags/v0.7.0.zip
0.7.0 分享包下载：https://github.com/ymsl56422839-cmd/portraitlab/releases/tag/v0.7.0

软件包 app 目录保留实际运行的应用源文件和启动器构建脚本。GitHub 固定版本提供完整项目、测试和构建说明；源码 ZIP 不包含模型权重，模型在分享包中随许可另行提供。
