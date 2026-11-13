# 来源与固定版本

核验日期：2026-11-13。源码可访问、权重可下载、明确允许的使用范围是三个不同事实。

## 本版实际复用

- 项目：[crispin81/RapidRetouch](https://github.com/crispin81/RapidRetouch)，作者 Chris Cork。
- 固定提交：[`d6f9a848fa5ba5628239b568777c5be3e7851a8e`](https://github.com/crispin81/RapidRetouch/tree/d6f9a848fa5ba5628239b568777c5be3e7851a8e)。
- 本次获取的源码归档 SHA-256：`a423036bd74c76abe3661846ab9b7190791ccaa7439eff131de728975d909c26`。
- 许可证：上游 `package.json` 与 README 声明 `AGPL-3.0-only`；根 `LICENSE` 完整复制上游文本。归档校验用于追踪本次输入，不代表将来 GitHub 重生成的 ZIP 必然字节相同。
- 复用范围：`portraitlab/vendor/rapidretouch_engine` 的修肤工具及必要数值、掩膜和 MediaPipe 适配依赖。保留上游文件中的版权与注释。
- PortraitLab 改动：独立的中文浏览器界面、FastAPI 本机服务、批处理与单图参数管理、保护笔刷集成、路径限制、模型准备和启动脚本；原 RapidRetouch 的 Tauri／React 应用不作为本版前端分发。

上游独立应用目前暂停开发，功能转入 RapidOne；本版使用固定提交，后续修复需要单独评估与合并。

## 本版实际模型

两个下载地址、期望哈希和对应上游清单见 [manifest.json](../models/manifest.json)。地址固定到 Google 存储中的版本 `1`，不使用不受控的 `latest`。预期哈希首先来自上述固定提交；实际下载后的校验状态由安装结果另行记录。

- [MediaPipe Face Landmarker 官方说明](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker#models)
- [MediaPipe Image Segmenter 官方模型说明](https://ai.google.dev/edge/mediapipe/solutions/vision/image_segmenter#models)
- [MediaPipe 框架 Apache-2.0](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE)

Google 开发文档页脚对示例代码的许可，不作为具体权重授权的替代证据。本版模型清单明确记录该核验边界。

## 研究候选，未集成

- [IC-Light](https://github.com/lllyasviel/IC-Light)：代码 Apache-2.0；[官方 v1 权重卡](https://huggingface.co/lllyasviel/ic-light/raw/main/README.md) 声明 CreativeML OpenRAIL-M。默认背景分割器另有许可限制。
- [ModelScope Skin Retouching](https://modelscope.cn/models/iic/cv_unet_skin_retouching_torch)：后续候选；本版未下载、未运行、未完成分发许可核验。
- [LBM 官方代码](https://github.com/gojasper/LBM) 与 [权重卡](https://huggingface.co/jasperai/LBM_relighting)：代码 README 与模型卡声明 CC-BY-NC-4.0，SDXL 基座另有 OpenRAIL++ 条款。没有把其源码混入 AGPL 分发代码。

这些候选的效果或论文指标不作为 PortraitLab 第一版的性能与质量证明。
