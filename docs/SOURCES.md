# 来源与固定版本

核验日期：2026-11-13。下列版本用于追踪 0.4.0 的实现；模型的下载、运行验证和许可核验分别记录。公开发行源码保留固定来源、完整许可和 vendor 修改说明，公开二进制包不含模型权重。

## RapidRetouch 与经典引擎

- [crispin81/RapidRetouch](https://github.com/crispin81/RapidRetouch)，作者 Chris Cork，固定提交 [`d6f9a848fa5ba5628239b568777c5be3e7851a8e`](https://github.com/crispin81/RapidRetouch/tree/d6f9a848fa5ba5628239b568777c5be3e7851a8e)。
- 本次源码归档 SHA-256：`a423036bd74c76abe3661846ab9b7190791ccaa7439eff131de728975d909c26`。GitHub 后续重新生成的 ZIP 不一定字节相同。
- 代码采用 AGPL-3.0-only；根 `LICENSE` 完整复制上游文本。复用目录为 `portraitlab/vendor/rapidretouch_engine`，保留原版权与注释。0.2 修改了 `adapters/mediapipe_face.py` 与 `adapters/mediapipe_parts.py`：Python 读取模型字节后传入 `BaseOptions.model_asset_buffer`，修复本次中文目录实测中 native 路径加载失败；修改处有明确注释，不能再把整个 vendor 目录描述为未经修改的上游副本。
- PortraitLab 提供独立的中文界面、本机服务、参数与批处理管理、保护笔刷集成、路径限制、模型准备及启动程序。原 Tauri／React 应用不作为本版前端分发。

上游独立应用已暂停开发，功能转入 RapidOne；本版固定提交，后续修复需要单独评估。

## MediaPipe 区域模型

两个 Google 模型的来源、期望哈希与核验状态见 [manifest.json](../models/manifest.json)。下载地址固定到版本 `1`；实际下载另校验 SHA-256。框架的 Apache-2.0 不能自动证明具体权重授权，清单保留这一核验边界。

- [Face Landmarker 官方说明](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker#models)
- [Image Segmenter 官方模型说明](https://ai.google.dev/edge/mediapipe/solutions/vision/image_segmenter#models)
- [MediaPipe 框架许可证](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE)

## Depth Anything V2 Small 与交互布光

- 官方代码固定到 [`a561b849ebae10a6f5ef49e26c83cbbcd36c71bf`](https://github.com/DepthAnything/Depth-Anything-V2/tree/a561b849ebae10a6f5ef49e26c83cbbcd36c71bf)，DPT／DINOv2 所需子集位于 `portraitlab/vendor/depth_anything_v2`；原 Apache-2.0 许可、上游文件头及修改说明一同保留。
- 使用 [Depth-Anything-V2-Small 固定 revision](https://huggingface.co/depth-anything/Depth-Anything-V2-Small/tree/03876f8651c73a60fe4c2c48294e09fcb6838fcf) 的 `depth_anything_v2_vits.pth`，99,218,434 字节、SHA-256 与下载来源见 [lighting-manifest.json](../models/lighting-manifest.json)。仅 Small 权重声明 Apache-2.0；Base／Large／Giant 的 CC-BY-NC-4.0 不适用于本项目使用的 Small，也不能反向套用 Small 条款替换其他规格。
- PortraitLab 从相对深度估计法线，并使用现有 MediaPipe 人物分割控制仅主体、仅背景、整张照片三种范围。灯光公式、选区预览与界面为本项目实现，不采用生成新图流程；来源、许可与方法的详细边界见 [INTERACTIVE_LIGHTING.md](INTERACTIVE_LIGHTING.md)。

## IC-Light 生成式重打光（旧流程兼容）

| 组成 | 固定来源 | 许可 |
|---|---|---|
| IC-Light 参考实现 | [官方仓库提交 `bcf3f29`](https://github.com/lllyasviel/IC-Light/tree/bcf3f29ca85be8a4686215f477b546f5030be8b7)，`gradio_demo.py` | Apache-2.0 |
| 前景条件 checkpoint | [官方权重提交 `9cad187`](https://huggingface.co/lllyasviel/ic-light/tree/9cad1878695f546a7fb9eaca14e2a89131ba5ffe)，`iclight_sd15_fc.safetensors` | CreativeML OpenRAIL-M |
| 官方演示使用的基座 | [stablediffusionapi/realistic-vision-v51 提交 `19e3643`](https://huggingface.co/stablediffusionapi/realistic-vision-v51/tree/19e3643d7d963c156d01537188ec08f0b79a514a) | CreativeML OpenRAIL-M |

基座是官方演示采用的第三方转换仓库；[原作者 SG161222 的 V5.1 模型卡](https://huggingface.co/SG161222/Realistic_Vision_V5.1_noVAE/blob/1e9f017a7b1eaefb63a1900ea6c5953d2739fd21/README.md)也声明 CreativeML OpenRAIL-M。各模型与文件的下载核验由重打光清单单独管理，不用源码许可代替权重许可。

本版替换官方演示的 BRIA RMBG-1.4，使用现有 MediaPipe 的 `1 − 背景概率` 作为前景条件。没有引入 BRIA 权重或其独立的非商用限制。推理适配保留官方的前景条件、UNet offset 合并、方向初始潜变量和两阶段扩散；具体参数及输出边界见 [RELIGHTING.md](RELIGHTING.md)。

完整许可、固定模型卡和各文本 SHA-256 存于 [licenses](../licenses/provenance.json)。CreativeML OpenRAIL-M 完整文本来自 [CompVis 固定版本](https://github.com/CompVis/stable-diffusion/blob/21f890f9da3cfbeaba8e2ac3c425ee9e998d5229/LICENSE)。IC-Light 模型卡的许可声明位于正文末尾，不能因自动解析的 cardData 为空而误判为未声明。

论文：[Scaling In-the-Wild Training for Diffusion-based Illumination Harmonization and Editing by Imposing Consistent Light Transport](https://openreview.net/forum?id=u1cQYxRI1H)，Lvmin Zhang、Anyi Rao、Maneesh Agrawala，ICLR 2025。论文与上游示例结果不作为本版速度或质量实测。

## 未集成的研究候选

- [ModelScope Skin Retouching](https://modelscope.cn/models/iic/cv_unet_skin_retouching_torch)：未下载、未运行，分发许可未完成核验。
- [LBM 官方代码](https://github.com/gojasper/LBM)与[权重卡](https://huggingface.co/jasperai/LBM_relighting)：声明 CC-BY-NC-4.0，SDXL 基座另有 OpenRAIL++ 条款。源码与权重均未混入本版。

模型来源与许可可追踪，并不代表已完成跨人群质量评估；实际验证范围以验证记录为准。
