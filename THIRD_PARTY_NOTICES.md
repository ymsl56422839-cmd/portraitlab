# Third-party notices

## PortraitLab and RapidRetouch

PortraitLab's integration code and RapidRetouch-derived code are distributed under **AGPL-3.0-only**. This does not relicense separately identified upstream code, model weights, Python, CUDA components or other runtime dependencies. The user's non-commercial research intent adds no non-commercial restriction to the AGPL code.

RapidRetouch: Copyright Chris Cork. [Source](https://github.com/crispin81/RapidRetouch), pinned commit `d6f9a848fa5ba5628239b568777c5be3e7851a8e`. The source archive reviewed for this integration has SHA-256 `a423036bd74c76abe3661846ab9b7190791ccaa7439eff131de728975d909c26`.

The complete upstream license is reproduced in the root [LICENSE](LICENSE). Original notices in `portraitlab/vendor/rapidretouch_engine` are retained. In PortraitLab 0.2, `adapters/mediapipe_face.py` and `adapters/mediapipe_parts.py` were modified to read the model with Python and pass `model_asset_buffer=path.read_bytes()` instead of a native model path. This fixes the observed MediaPipe failure when loading models from Windows Unicode/Chinese directories. Both modified locations carry a PortraitLab change comment; the vendored tree is therefore not an unmodified upstream copy. PortraitLab integrates the necessary processing modules and MediaPipe adapters with a separate browser UI, HTTP service, batch/parameter handling, path policy and launch tooling. The original Tauri/React application is not included. This is not an official RapidRetouch or RapidOne release.

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

MediaPipe framework: [Apache-2.0](https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE). RapidRetouch's pinned manifests label the two Google-hosted task/model weights Apache-2.0, but this distribution has not independently established version-specific weight licensing. Documentation footer licenses for sample code are not proof of weight licensing. See `models/manifest.json` for the exact evidence and status. These weights are not included in the source release.

The classic backend pins MediaPipe 0.10.21. The 2026-11-13 local sample checks completed without an observed Clearcut log. An earlier trial of 0.10.35 emitted `Failed to send to clearcut` / `Status_ConnectFailed:12029` near shutdown. That failed attempt does not establish its payload or prove a photograph upload. No packet-capture audit was performed; neither log checks nor binary-string checks prove network silence.

Python, PyTorch, Diffusers, Transformers, NumPy, Pillow, FastAPI, Uvicorn, OpenCV, MediaPipe, CUDA components and other dependencies retain their own licenses. A portable binary distribution must preserve the actual bundled packages' copyright, license and NOTICE files, including Python's license and relevant third-party GPU runtime terms. Dependency names or a pip freeze are not substitutes for those texts. The source distribution and build instructions accompanying an AGPL executable must provide its corresponding source; see the root license.

The audited wheels for FlatBuffers 25.12.19, SentencePiece 0.2.3, Safetensors 0.4.5 and Tokenizers 0.15.2 omitted license text in their recorded wheel metadata. Matching official release license texts are supplied in [licenses/runtime-extra](licenses/runtime-extra/provenance.json): all four projects' Apache-2.0 licenses, plus SentencePiece's Darts-clone BSD notice and libsais Apache-2.0 text. The provenance file records version tags, available upstream commits, source archives, URLs and SHA-256 values. Safetensors and Tokenizers license bytes were checked against both their official version tag and their hash-verified PyPI source release. These supplements accompany, rather than replace, original package metadata and notices.
## Windows desktop shell and WebView2

The Windows desktop shell uses the official `Microsoft.Web.WebView2` SDK **1.0.4314.35**. Its managed Core and WinForms bindings and x64 `WebView2Loader.dll` are copied from the [fixed official NuGet package](https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.4314.35/microsoft.web.webview2.1.0.4314.35.nupkg). Package and component hashes are recorded in [licenses/webview2/provenance.json](licenses/webview2/provenance.json) and [packaging/webview2.lock.json](packaging/webview2.lock.json); the build verifies them before packaging.

The SDK package's Microsoft copyright and BSD-style redistribution conditions are retained verbatim in [licenses/webview2/LICENSE.txt](licenses/webview2/LICENSE.txt), together with its [third-party NOTICE.txt](licenses/webview2/NOTICE.txt). Binary packages include those original texts under `Licenses/WebView2`. PortraitLab's AGPL license does not replace these component terms.

The **Microsoft Edge WebView2 Evergreen Runtime is a separate browser component**. This portable package detects an existing installation and does not redistribute or install the browser runtime. If absent, the application links to [Microsoft's official runtime download page](https://developer.microsoft.com/en-us/microsoft-edge/webview2/#download). Microsoft's applicable runtime license and its third-party terms govern that separately obtained component; the SDK's BSD-style license must not be read as the license for the entire Edge/WebView2 browser. The desktop shell, Python service and web frontend remain available as corresponding application source.

## Not included and private material

ModelScope Skin Retouching, LBM, DPR and Intrinsic remain unintegrated research references. No research-only or CC-BY-NC code is silently relicensed under AGPL.

User photographs, private generated images, screenshots containing those photographs, account data, caches and session logs are excluded from public source and binary releases. Their rights are separate from the application's code and model licenses.


## Interactive lighting and Depth Anything V2 Small (0.4.0)

The application estimates relative depth with [Depth Anything V2](https://github.com/DepthAnything/Depth-Anything-V2), then derives approximate normals and renders deterministic image-space lights. Existing MediaPipe segmentation supplies the subject mask; PortraitLab applies that mask, its complement, or the entire image for subject-only, background-only, or whole-image lighting. Unselected pixels retain the retouched input values, with weighted blending at feathered boundaries. The optional selection highlight is a UI overlay and is not written into saved or exported images. It does not copy LG_Relight code (no license was identified in the researched repository). The custom lighting renderer and UI are PortraitLab AGPL-3.0-only code.

The vendored PyTorch DPT/DINOv2 subset is pinned to official commit `a561b849ebae10a6f5ef49e26c83cbbcd36c71bf`. Its original Apache-2.0 license, upstream file headers and precise adaptation notices accompany `portraitlab/vendor/depth_anything_v2`. See `licenses/Depth-Anything-V2-Apache-2.0.txt` and the vendor `NOTICE.txt`.

Only **Small** weights are used: official `depth-anything/Depth-Anything-V2-Small`, revision `03876f8651c73a60fe4c2c48294e09fcb6838fcf`, `depth_anything_v2_vits.pth`, 99,218,434 bytes, SHA-256 `715fade13be8f229f8a70cc02066f656f2423a59effd0579197bbf57860e1378`. The model's declared terms are Apache-2.0. Base/Large/Giant have different terms (CC-BY-NC-4.0) and are not substituted. Fixed download and license evidence are recorded in `models/lighting-manifest.json`.
