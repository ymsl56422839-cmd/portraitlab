# Third-party notices

## RapidRetouch

Copyright Chris Cork. Source: https://github.com/crispin81/RapidRetouch

Pinned commit: `d6f9a848fa5ba5628239b568777c5be3e7851a8e`.
Source archive SHA-256: `a423036bd74c76abe3661846ab9b7190791ccaa7439eff131de728975d909c26`.
License: **AGPL-3.0-only**. The complete upstream license is reproduced in the root `LICENSE`; upstream notices in vendored files are retained.

PortraitLab reuses the necessary image-processing modules and MediaPipe adapters in `portraitlab/vendor/rapidretouch_engine`. Its browser UI, HTTP service, engine wrapper, parameter/batch handling, output-path policy and launch tooling are separate integration changes dated 2026-11-13. This distribution is not an official RapidRetouch or RapidOne release. The original Tauri/React application and unrelated model implementations are not included as active backends.

PortraitLab's own and derived code is distributed under AGPL-3.0-only. The current user's non-commercial research intent adds no non-commercial restriction to the AGPL license. Contributors retain their respective copyrights.

## Runtime dependencies and models

Python packages remain under their respective licenses. The installer obtains dependencies separately; this source release does not relicense their code or claim ownership of their models. `requirements.lock.txt` identifies the validated dependency versions for this release.

MediaPipe framework: Apache-2.0; official source/license: https://github.com/google-ai-edge/mediapipe/blob/master/LICENSE . The two Google-hosted task/model files are separately downloaded, never included in the source release. RapidRetouch's pinned manifests identify them as Apache-2.0, but this distribution has not independently established version-specific weight licensing. Documentation footer licenses for sample code are not treated as proof of weight licensing. See `models/manifest.json` for exact URLs, expected hashes, evidence and status.

The validated dependency set pins MediaPipe 0.10.21. On 2026-11-13, two local portrait samples completed analysis, rendering and adapter shutdown without an observed Clearcut log message. An earlier trial of MediaPipe 0.10.35 emitted `portable_clearcut_uploader.cc:90` / `Failed to send to clearcut` / `Status_ConnectFailed:12029` near shutdown. That failed transmission attempt does not establish its payload or prove a photograph upload. No packet-capture audit was performed; log and binary-string checks do not establish that the pinned SDK is network-silent. Application-layer local processing is not a claim that every third-party SDK is network-silent.

The currently installed FastAPI, Uvicorn, NumPy, Pillow, OpenCV, MediaPipe and transitive dependencies retain their own license files and notices. A binary or bundled-runtime distributor must include the notices required by the exact packages they actually redistribute; this file is not a substitute for those package licenses.

## Not included

No IC-Light, ModelScope Skin Retouching, LBM, DPR or Intrinsic source/weights are incorporated as a PortraitLab backend. Planned registry entries are research references only. In particular, research-only or CC-BY-NC code is not silently relicensed under AGPL.

User photographs, model weights, caches and generated image exports are excluded from the source release. Their rights are separate from this application's code license.
