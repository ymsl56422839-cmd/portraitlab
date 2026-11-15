# Third-party notices

## PortraitLab and RapidRetouch

PortraitLab's integration code and RapidRetouch-derived code are distributed under **AGPL-3.0-only**. This does not relicense separately identified upstream code, model weights, Python, CUDA components or other runtime dependencies. The user's non-commercial research intent adds no non-commercial restriction to the AGPL code.

RapidRetouch: Copyright Chris Cork. [Source](https://github.com/crispin81/RapidRetouch), pinned commit `d6f9a848fa5ba5628239b568777c5be3e7851a8e`. The source archive reviewed for this integration has SHA-256 `a423036bd74c76abe3661846ab9b7190791ccaa7439eff131de728975d909c26`.

The complete upstream license is reproduced in the root [LICENSE](LICENSE). Original notices in `portraitlab/vendor/rapidretouch_engine` are retained. In PortraitLab 0.2, `adapters/mediapipe_face.py` and `adapters/mediapipe_parts.py` were modified to read the model with Python and pass `model_asset_buffer=path.read_bytes()` instead of a native model path. This fixes the observed MediaPipe failure when loading models from Windows Unicode/Chinese directories. Both modified locations carry a PortraitLab change comment; the vendored tree is therefore not an unmodified upstream copy. PortraitLab integrates the necessary processing modules and MediaPipe adapters with a separate browser UI, HTTP service, batch/parameter handling, path policy and launch tooling. The original Tauri/React application is not included. This is not an official RapidRetouch or RapidOne release.

PortraitLab 0.5.0 adds manual donor spot healing in `portraitlab/spot_repair.py`, adapted from RapidRetouch's `engine/src/rapidretouch_engine/tools/patch.py` at the same pinned commit. It retains Chris Cork's attribution and **AGPL-3.0-only** terms. The reviewed upstream file has SHA-256 `fd6d51b6b839abccea1c9fcc49f12c47562e4bcf9033f8ef4ab5357abe32ae8e`. The adapted operation combines surrounding target tone with explicitly selected donor texture in linear light; PortraitLab adds circular inward feathering, exact exterior preservation, immutable donor sampling, bounded tone fields, tiled processing and validation. Full provenance and modification details are in [licenses/SPOT_REPAIR_NOTICE.txt](licenses/SPOT_REPAIR_NOTICE.txt).

This legacy spot-healing operation requires no new model weights and is not an OpenCV inpainting model or generative face reconstruction. The 0.5.0 photo-aware preset resolver used measured luminance and existing face masks. Starting with the independent V2 editor in 0.7.0, ModelScope neural skin repair is included as described below; it is separate from the legacy RapidRetouch-derived detector and donor-texture path. Dependency licenses remain applicable as described below.

## ModelScope Skin Retouching (V2 / 0.7.0)

The V2 editor uses the official `damo/cv_unet_skin_retouching_torch` model at revision `30f890aab89b787aae93c3f396dbf32714bd950e`. The original model card fetched at that fixed revision explicitly declares **Apache License 2.0**. Its two unmodified checkpoints are `joint_20210926.pth` (227,618,450 bytes, SHA-256 `c9e1388b19d9334c90db08308c86de208f535920ee381ae8a9cb63484a78351a`) and `pytorch_model.pt` (53,646,276 bytes, SHA-256 `760d0c0b95c4e0a692165677fe2b82dbdf8fbbd4a3780ca966599c627b082993`). They serve blemish detection / local neural repair and the skin-evening blend layer respectively. Candidate detection does not establish that a mark is a temporary blemish; V2 asks the user to confirm repairs.

Network code is pinned to [ModelScope](https://github.com/modelscope/modelscope) commit `34844f9977a33f541cb5d8959c40a57cd6155b3b`, under Apache-2.0. Original module headers and the full license are preserved. PortraitLab supplies its own cropped-inference orchestration, manual confirmation, masks, history and composition. Source attribution: ModelScope / Alibaba and contributors. Research attribution: Lei et al., “ABPN: Adaptive Blend Pyramid Network for Real-Time Local Retouching of Ultra High-Resolution Photo”, CVPR 2022. This is not an official ModelScope or Alibaba release.

The full license, fixed-revision model card, exact URLs and SHA-256 values are in [licenses/modelscope](licenses/modelscope/provenance.json). A complete overview of the five V2 model files is in [V2_MODELS_NOTICE.txt](licenses/V2_MODELS_NOTICE.txt). Source archives do not contain weight binaries; the 0.7.0 all-in-one binary sharing package includes those identified weights with their own terms.

## IC-Light source code

[IC-Light](https://github.com/lllyasviel/IC-Light) by Lvmin Zhang and contributors. Research attribution: Lvmin Zhang, Anyi Rao and Maneesh Agrawala, “Scaling In-the-Wild Training for Diffusion-based Illumination Harmonization and Editing by Imposing Consistent Light Transport,” ICLR 2025.

The integration is based on `gradio_demo.py` at commit `bcf3f29ca85be8a4686215f477b546f5030be8b7`. Upstream code is licensed under **Apache-2.0**; the complete original text is included as [IC-Light-Apache-2.0.txt](licenses/IC-Light-Apache-2.0.txt). No upstream `NOTICE` file was present in that pinned tree. Upstream attribution and this license remain applicable to reused/adapted code.

PortraitLab's adaptations replace the Gradio application and BRIA preprocessing with its own local job interface and existing MediaPipe foreground mask, use fixed model revisions and checked local storage, and keep generated results separate from imported originals. The underlying foreground conditioning, 8-channel UNet offset merge, directional latent initialization and two-stage diffusion follow the official example. Modified source files identify the adaptation.

The official demo uses BRIA RMBG-1.4, whose non-commercial restrictions are explicitly identified by IC-Light's README. PortraitLab does **not** include or download BRIA RMBG-1.4, its weights or `briarmbg.py` as part of this integration.

## IC-Light and base-model weights

These are separate model artifacts, not AGPL application source:

| Artifact | Fixed revision | Declared terms |
|---|---|---|
| `lllyasviel/ic-light`, `iclight_sd15_fc.safetensors` | `9cad1878695f546a7fb9eaca14e2a89131ba5ffe` | CreativeML OpenRAIL-M |
| `stablediffusionapi/realistic-vision-v51`, the base used by the official demo | `19e3643d7d963c156d01537188ec08f0b79a514a` | CreativeML OpenRAIL-M |

The exact source declarations are preserved in [IC-Light's model card](licenses/IC-Light-MODEL-CARD.md) and [the selected base model's card](licenses/Realistic-Vision-v51-MODEL-CARD.md). The [original Realistic Vision author's card](licenses/Realistic-Vision-v51-AUTHOR-CARD.md), pinned at `1e9f017a7b1eaefb63a1900ea6c5953d2739fd21`, also declares CreativeML OpenRAIL-M. The selected base is the ModelsLab/StableDiffusionAPI conversion used by the official example; it is not represented as a checkpoint uploaded by the original author.

The full [CreativeML OpenRAIL-M text](licenses/CreativeML-OpenRAIL-M.txt), dated August 22, 2022, is preserved from the pinned CompVis reference. Source URLs and file hashes are in [licenses/provenance.json](licenses/provenance.json). The IC-Light model card puts its license declaration after its prose; missing parsed Hugging Face `cardData` does not erase that declaration.

OpenRAIL-M permits use and conditional redistribution, including commercial use; it is not an unrestricted license. Distribution of weights or model derivatives must carry the license, preserve applicable notices, mark modified files, and pass on Section 5 / Attachment A use restrictions as enforceable terms. Hosted model access is also covered by its definition of distribution. The model's output clause does not assign all possible rights in user photographs or other third-party material. See [RELIGHTING.md](docs/RELIGHTING.md) for the integration and distribution boundaries; its explanatory summary does not replace the original terms.

Source archives exclude model binaries. The public software can obtain fixed weights separately; a local complete portable bundle may include weights with their original terms. Separating the download does not remove the model user's obligations. Merged, quantized or converted weights remain subject to the applicable upstream model terms.

## MediaPipe and runtime dependencies

MediaPipe framework: [Apache-2.0](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE). On 2026-11-15 the model-weight licensing was independently checked using Google's official task-page tables and their linked model cards: BlazeFace, Face Mesh V2, Blendshape V2 and Multiclass Segmentation each state “LICENSED UNDER Apache License, Version 2.0” on page 1. The two `latest` weight downloads linked by those tables were SHA-256 checked and match the project's fixed version-1 artifacts exactly. This evidence is independent of the documentation footer's code-sample license. The original PDFs, full Apache text, URLs and hashes are preserved in [licenses/mediapipe](licenses/mediapipe/provenance.json). Weight binaries remain excluded from source archives and are included with these notices in the 0.7.0 sharing package.

The classic backend pins MediaPipe 0.10.21. The 2026-11-13 local sample checks completed without an observed Clearcut log. An earlier trial of 0.10.35 emitted `Failed to send to clearcut` / `Status_ConnectFailed:12029` near shutdown. That failed attempt does not establish its payload or prove a photograph upload. No packet-capture audit was performed; neither log checks nor binary-string checks prove network silence.

Python, PyTorch, Diffusers, Transformers, NumPy, Pillow, FastAPI, Uvicorn, OpenCV, MediaPipe, CUDA components and other dependencies retain their own licenses. A portable binary distribution must preserve the actual bundled packages' copyright, license and NOTICE files, including Python's license and relevant third-party GPU runtime terms. Dependency names or a pip freeze are not substitutes for those texts. The source distribution and build instructions accompanying an AGPL executable must provide its corresponding source; see the root license.

The audited wheels for FlatBuffers 25.12.19, SentencePiece 0.2.3, Safetensors 0.4.5 and Tokenizers 0.15.2 omitted license text in their recorded wheel metadata. Matching official release license texts are supplied in [licenses/runtime-extra](licenses/runtime-extra/provenance.json): all four projects' Apache-2.0 licenses, plus SentencePiece's Darts-clone BSD notice and libsais Apache-2.0 text. The provenance file records version tags, available upstream commits, source archives, URLs and SHA-256 values. Safetensors and Tokenizers license bytes were checked against both their official version tag and their hash-verified PyPI source release. These supplements accompany, rather than replace, original package metadata and notices.
## Windows desktop shell and WebView2

The Windows desktop shell uses the official `Microsoft.Web.WebView2` SDK **1.0.4314.35**. Its managed Core and WinForms bindings and x64 `WebView2Loader.dll` are copied from the [fixed official NuGet package](https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4314.35/microsoft.web.webview2.1.0.4314.35.nupkg). Package and component hashes are recorded in [licenses/webview2/provenance.json](licenses/webview2/provenance.json) and [packaging/webview2.lock.json](packaging/webview2.lock.json); the build verifies them before packaging.

The SDK package's Microsoft copyright and BSD-style redistribution conditions are retained verbatim in [licenses/webview2/LICENSE.txt](licenses/webview2/LICENSE.txt), together with its [third-party NOTICE.txt](licenses/webview2/NOTICE.txt). Binary packages include those original texts under `Licenses/WebView2`. PortraitLab's AGPL license does not replace these component terms.

The **Microsoft Edge WebView2 Runtime is a separate browser component**. Earlier releases detected a separately installed Evergreen Runtime. The 0.7.0 sharing package instead includes the official x64 Fixed Version Runtime 157.0.4341.60, with the original [Microsoft runtime terms](licenses/webview2-runtime/LICENSE.html), readable plain-text copy and [provenance](licenses/webview2-runtime/provenance.json). The first-run component-license dialog appears before loading the browser runtime. Its terms, original browser third-party notices and required SmartScreen privacy notice are preserved. The fixed runtime is serviced by subsequent application bundles; it is not the automatically updating Evergreen Runtime. The SDK's BSD-style license is not the license for the entire browser. The desktop shell, Python service and web frontend remain available as corresponding application source.

The app-local Microsoft Visual C++ 2015–2022 x64 runtime is taken unmodified from the official 14.44.35211 redistributable. Developer redistribution is governed by the applicable Visual Studio Community 2022 Distributable Code grant and REDIST list, separately from the runtime's end-user installation license. The official Community terms, REDIST evidence, original runtime RTF and readable runtime license accompany [licenses/microsoft-vc](licenses/microsoft-vc/provenance.json). The build uses an existing Community installation for this AGPL open-source project. These Microsoft components remain separate proprietary runtime components and are not relicensed under AGPL.

The GPU worker includes unmodified runtime libraries delivered with PyTorch 2.7.1+cu128. NVIDIA CUDA 12.8 and cuDNN 9.7.1 retain their separate SDK/runtime license and redistribution requirements. Complete official terms are archived in [licenses/nvidia](licenses/nvidia/provenance.json) and included in the first-run component-license review. NVIDIA components are provided only for the application's use, not as a standalone toolkit. The package preserves PyTorch's full LICENSE and NOTICE as well. No NVIDIA driver is included or installed; a compatible driver belongs to the recipient's system. The application license does not impose source-disclosure or modification rights on these independently identified runtime libraries.

## Not included and private material

LBM, DPR, Intrinsic, Marigold, AdaInt and RetouchFormer remain research references for V2. No research-only or CC-BY-NC code is silently relicensed under AGPL. Historical IC-Light integration notices remain relevant to its source, but the V2 0.7.0 sharing package does not include IC-Light, Stable Diffusion, Realistic Vision or BRIA model weights.

User photographs, private generated images, screenshots containing those photographs, account data, caches and session logs are excluded from public source and binary releases. Their rights are separate from the application's code and model licenses.


## Interactive lighting and Depth Anything V2 Small (0.4.0)

The application estimates relative depth with [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2), then derives approximate normals and renders deterministic image-space lights. Existing MediaPipe segmentation supplies the subject mask; PortraitLab applies that mask, its complement, or the entire image for subject-only, background-only, or whole-image lighting. Unselected pixels retain the retouched input values, with weighted blending at feathered boundaries. The optional selection highlight is a UI overlay and is not written into saved or exported images. It does not copy LG_Relight code (no license was identified in the researched repository). The custom lighting renderer and UI are PortraitLab AGPL-3.0-only code.

The vendored PyTorch DPT/DINOv2 subset is pinned to official commit `a561b849ebae10a6f5ef49e26c83cbbcd36c71bf`. Its original Apache-2.0 license, upstream file headers and precise adaptation notices accompany `portraitlab/vendor/depth_anything_v2`. See `licenses/Depth-Anything-V2-Apache-2.0.txt` and the vendor `NOTICE.txt`.

Only **Small** weights are used: official `depth-anything/Depth-Anything-V2-Small`, revision `03876f8651c73a60fe4c2c48294e09fcb6838fcf`, `depth_anything_v2_vits.pth`, 99,218,434 bytes, SHA-256 `715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378`. The model's declared terms are Apache-2.0. Base/Large/Giant have different terms (CC-BY-NC-4.0) and are not substituted. Fixed download and license evidence are recorded in `models/lighting-manifest.json`.
