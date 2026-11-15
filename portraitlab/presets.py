"""Scoped recipes for the portrait processing pipeline.

SPDX-License-Identifier: AGPL-3.0-only
Recipes describe operations; preset_pipeline resolves them against each photo.
"""
from __future__ import annotations

from copy import deepcopy
import unicodedata

from .engine import DEFAULT_PARAMS, validate_params

MAX_CUSTOM_PRESETS = 100
MAX_PRESET_NAME = 40
MAX_PRESET_DESCRIPTION = 180
PORTRAIT_GROUPS = (
    {"id": "general", "name": "通用", "description": "按照片需要选择修肤、曝光与色调，可先框选再局部应用。"},
    {"id": "senior", "name": "长者人像", "description": "以光线和色彩整理为主，默认保留皱纹与皮肤质感。"},
    {"id": "adult", "name": "成人人像", "description": "日常、职场、妆面与痘点修复，按需要微调强度。"},
    {"id": "youth", "name": "少年人像", "description": "轻调光色，默认不磨皮；需要时单独修复痘点。"},
)
CATEGORIES = (
    {"id": "retouch", "name": "修肤与质感"},
    {"id": "light", "name": "曝光与补脸"},
    {"id": "color", "name": "全图色调"},
    {"id": "body", "name": "身体皮肤"},
    {"id": "basic", "name": "基础"},
    {"id": "mine", "name": "我的预设"},
)
NEUTRAL = {
    **DEFAULT_PARAMS, "smooth": 0.0, "blemishes": 0.0, "even": 0.0, "acne": 0.0,
    "texture": 0.0, "exposure": 0.0, "relight": 0.0, "warmth": 0.0,
    "saturation": 1.0, "opacity": 1.0,
}

PARAMETER_GROUPS = {
    "skin": ("smooth", "blemishes", "even", "acne", "acne_sensitivity", "texture", "region"),
    "light": ("exposure", "relight"),
    "color": ("warmth", "saturation"),
}
GROUP_LABELS = {"skin": "修肤", "light": "明暗", "color": "色彩"}
_ADAPTIVE = {
    "auto_balance": "balance", "dim_scene": "lift", "lower_exposure": "protect_highlights",
    "face_bright": "face_lift", "backlight": "backlight", "face_dim": "face_reduce",
    "senior_soft_fill": "face_lift", "senior_backlit": "backlight",
    "senior_light_balance": "face_reduce", "adult_lowlight": "backlight",
    "adult_outdoor": "face_reduce", "youth_school": "face_lift",
    "youth_outdoor": "face_lift", "youth_backlight": "backlight",
}

# These recipes used to silently change controls from other editing sections.
# Their descriptions now describe only the operations that will be applied.
_SCOPED_DESCRIPTIONS = {
    "face_bright": "按面部明暗自适应补光，保留已有全图曝光；需检测到人脸。",
    "backlight": "按面部亮度补光，限制高光区域增亮；保留修肤和色彩。",
    "dim_scene": "按照片或框选区域的亮度提曝光，高光不足时自动限制增亮。",
    "lower_exposure": "按亮部与中间调降低曝光；纯白区域中丢失的细节无法恢复。",
    "face_dim": "按面部亮度减少局部曝光，保留已有全图曝光。",
    "warm_portrait": "温和暖色与轻度降饱和，保留已有修肤和明暗调整。",
    "cool_clean": "偏冷与轻度降饱和，保留已有修肤和明暗调整。",
    "vivid_color": "提高色彩饱和度，保留已有曝光；请放大检查肤色。",
    "warm_muted": "暖色与低饱和组合，保留已有明暗和肤质。",
    "monochrome": "转换为黑白，保留已有曝光和修肤。",
    "senior_soft_fill": "按面部亮度轻补光，保留皱纹与原有修肤设置。",
    "senior_backlit": "按面部与环境亮度补脸，限制全图增亮幅度。",
    "senior_light_balance": "按亮部与面部明暗减光，保留皮肤纹理。",
    "senior_warm_family": "温和暖色与少量降饱和，保留肤质和曝光。",
    "senior_cold_cast": "为偏冷照片增加温和暖色，保留曝光和修肤。",
    "senior_soft_color": "轻暖低饱和，适合柔和纪实氛围；保留曝光。",
    "senior_monochrome": "转换为纪实黑白，保留皱纹、斑点与明暗。",
    "adult_document": "轻修肤色与独立瑕疵，保留当前明暗和全图色彩。",
    "adult_office": "适度均匀肤色、轻柔肤质，保留已有明暗和色调。",
    "adult_lowlight": "按环境与面部亮度补光，高光不足时限制全图曝光。",
    "adult_outdoor": "按面部亮度轻压过亮区域，保留已有肤色与全图曝光。",
    "adult_monochrome": "转换为沉静黑白，保留已调整的曝光与肤质。",
    "youth_school": "按面部亮度轻补光，保留皮肤纹理与色彩。",
    "youth_outdoor": "按面部和环境亮度轻补光，保留全图色彩。",
    "youth_backlight": "按面部亮度补逆光，保留皮肤纹理与色彩。",
    "youth_warm": "温和暖色与少量增饱和，不改变曝光或肤质。",
    "youth_clean_color": "少量偏冷与降饱和，保留曝光和肤质。",
}


def _recipe(identifier, name, category, description, tags, *, featured=False, portrait_group="general", **params):
    if identifier == "original":
        groups, keys = list(PARAMETER_GROUPS), list(NEUTRAL)
        resolved = dict(NEUTRAL)
    else:
        group = "skin" if category in ("retouch", "body") else "light" if category == "light" else "color"
        groups = [group]
        # A face-light operation must not erase an existing global exposure.
        keys = [key for key in PARAMETER_GROUPS[group] if group != "light" or key in params]
        resolved = {**NEUTRAL, **{key: value for key, value in params.items() if key in keys},
                    "opacity": params.get("opacity", 1.0)}
    adaptive = _ADAPTIVE.get(identifier, False)
    return {"id": identifier, "name": name, "category": category,
            "portrait_group": portrait_group,
            "description": _SCOPED_DESCRIPTIONS.get(identifier, description), "tags": tags, "featured": featured,
            "builtin": True, "params": validate_params(resolved), "apply_mode": "scoped",
            "alias_of": "monochrome" if identifier in ("senior_monochrome", "adult_monochrome") else None,
            "apply_groups": groups, "apply_keys": keys, "adaptive": adaptive,
            "operations": [{"id": group, "label": GROUP_LABELS[group],
                            "keys": [key for key in keys if key in PARAMETER_GROUPS[group]],
                            "adaptive": adaptive if group == "light" else False} for group in groups]}


BUILTIN_PRESETS = (
    _recipe("natural", "自然日常", "retouch", "轻度柔肤与均匀肤色，适合日常人像的起点。", ["自然", "通用", "日常"],
            featured=True, smooth=.25, blemishes=.20, even=.15, texture=.10, opacity=.75),
    _recipe("texture_first", "保留纹理", "retouch", "减少柔化，适合希望保留毛孔和皮肤起伏的人像。", ["保纹", "毛孔", "真实"],
            featured=True, smooth=.08, blemishes=.10, even=.12, texture=0, opacity=.65),
    _recipe("clean_portrait", "轻度净肤", "retouch", "以淡化局部瑕疵为主，保持较轻的整体柔肤。", ["瑕疵", "净肤", "轻修"],
            smooth=.18, blemishes=.40, even=.16, texture=.06, opacity=.75),
    _recipe("soft_portrait", "细腻写真", "retouch", "更明显的柔肤与肤色整理，适合写真；建议放大检查细节。", ["写真", "细腻", "柔肤"],
            featured=True, smooth=.45, blemishes=.40, even=.25, texture=.25, opacity=.85),
    _recipe("studio_finish", "棚拍精修", "retouch", "加强瑕疵淡化和肤色均匀，保留适度纹理。", ["棚拍", "精修", "肤色"],
            smooth=.32, blemishes=.48, even=.36, texture=.12, opacity=.85),
    _recipe("light_makeup", "淡妆柔肤", "retouch", "轻柔皮肤纹理，适合保留已有妆容的淡妆照片；不添加妆容。", ["淡妆", "柔肤", "妆面"],
            smooth=.22, blemishes=.16, even=.18, texture=.07, opacity=.70),
    _recipe("even_skin", "均匀肤色", "retouch", "主要整理皮肤区域的色彩起伏，少做纹理柔化。", ["肤色", "色差", "均匀"],
            featured=True, smooth=.08, blemishes=.08, even=.45, texture=.02, opacity=.80),
    _recipe("gentle_texture", "轻柔纹理", "retouch", "主要减弱较粗的皮肤纹理，保留原有曝光与色调。", ["纹理", "柔化", "细节"],
            smooth=.12, blemishes=.08, even=.08, texture=.30, opacity=.65),
    _recipe("face_bright", "暗脸提亮", "light", "只做面部局部提亮，适合脸部偏暗；需检测到人脸。", ["暗脸", "面部", "补光"],
            featured=True, relight=.38, opacity=.85),
    _recipe("backlight", "逆光补脸", "light", "加强面部局部提亮并轻修皮肤，不重建已经丢失的暗部细节。", ["逆光", "补脸", "户外"],
            smooth=.12, blemishes=.10, even=.10, relight=.62, opacity=.85),
    _recipe("dim_scene", "暗片提亮", "light", "提高整张照片曝光；背景也会变亮，请检查高光。", ["暗片", "曝光", "全图"],
            exposure=.45, opacity=.85),
    _recipe("lower_exposure", "亮片压暗", "light", "降低整张照片曝光，不恢复已过曝成纯白的细节。", ["亮片", "曝光", "全图"],
            exposure=-.40, opacity=.85),
    _recipe("face_dim", "面部减亮", "light", "轻压面部中央亮度，适合脸部比环境过亮的照片。", ["面部", "减亮", "亮度"],
            relight=-.25, opacity=.80),
    _recipe("warm_portrait", "暖调人像", "color", "全图偏暖并轻微柔肤，适合温暖日常氛围；背景也会变色。", ["暖色", "氛围", "全图"],
            featured=True, smooth=.12, blemishes=.10, even=.10, warmth=.35, saturation=.95, opacity=.80),
    _recipe("cool_clean", "冷调清透", "color", "全图偏冷并略提曝光，适合清爽色调；不会自动校正白平衡。", ["清透", "冷色", "全图"],
            warmth=-.25, exposure=.10, saturation=.92, opacity=.80),
    _recipe("muted_color", "低饱和柔色", "color", "降低整张图的饱和度，让颜色更柔和；不添加颗粒。", ["低饱和", "柔色", "全图"],
            saturation=.70, warmth=.10, opacity=.85),
    _recipe("vivid_color", "色彩鲜明", "color", "提高全图饱和度，适合偏灰的照片；请检查肤色。", ["鲜明", "饱和度", "全图"],
            saturation=1.18, exposure=.05, opacity=.80),
    _recipe("warm_muted", "暖灰氛围", "color", "全图低饱和与暖色组合，适合柔和的日常氛围。", ["暖灰", "低饱和", "全图"],
            saturation=.45, warmth=.48, exposure=-.06, opacity=.90),
    _recipe("monochrome", "黑白人像", "color", "将全图转换为黑白，并稍提曝光；不改变人物五官。", ["黑白", "单色", "全图"],
            saturation=0.0, exposure=.08, opacity=1.0),
    _recipe("body_natural", "全身自然修肤", "body", "轻修检测到的人脸、颈部和裸露皮肤；请检查自动选区。", ["全身", "颈部", "皮肤"],
            smooth=.22, blemishes=.18, even=.18, texture=.08, region="skin", opacity=.75),
    _recipe("body_even", "裸露皮肤均匀", "body", "重点均匀面部与身体皮肤色彩，不改变衣服或背景的色调。", ["身体", "肤色", "均匀"],
            smooth=.12, blemishes=.12, even=.42, texture=.04, region="skin", opacity=.80),
    _recipe("original", "重置修片", "basic", "清空当前范围的修肤、明暗与色彩，之后拖动滑块立即生效。", ["原图", "重置", "对比"]),
    _recipe("acne_gentle", "去痘·轻度", "retouch", "轻修识别到的独立面部痘点；可先框选，保护痣和雀斑。", ["去痘", "痘点", "局部", "轻修"],
            featured=True, acne=.20, acne_sensitivity=.35),
    _recipe("acne_standard", "去痘·标准", "retouch", "标准检测灵敏度与适度修复强度，不增加磨皮；建议放大检查。", ["去痘", "痘点", "局部"],
            featured=True, acne=.50, acne_sensitivity=.50),
    _recipe("acne_strong", "去痘·加强", "retouch", "提高痘点检测灵敏度与修复强度；可框选限制处理，注意保护痣与雀斑。", ["去痘", "痘点", "局部", "加强"],
            acne=.80, acne_sensitivity=.70),
    _recipe("local_even_skin", "局部肤色整理", "retouch", "建议先框选色差区域再应用；未框选时整理自动面部皮肤选区。", ["局部", "肤色", "色差", "框选"],
            even=.28, opacity=.90),
    _recipe("senior_true", "长者真实肤质", "retouch", "仅轻整肤色，不柔化皱纹、毛孔或自动修复斑点。", ["长者", "真实", "肤色"],
            portrait_group="senior", featured=True, even=.08, opacity=.75),
    _recipe("senior_soft_fill", "长者柔和补脸", "light", "轻补面部曝光并少量均匀肤色，保留年龄特征。", ["长者", "补脸", "保纹"],
            portrait_group="senior", featured=True, even=.08, relight=.24, opacity=.80),
    _recipe("senior_backlit", "长者逆光肖像", "light", "补亮偏暗面部并略提全图曝光，保留原有皮肤纹理。", ["长者", "逆光", "曝光"],
            portrait_group="senior", even=.06, exposure=.10, relight=.48, opacity=.80),
    _recipe("senior_light_balance", "长者亮脸平衡", "light", "轻压过亮面部与全图曝光；不能恢复已丢失的高光。", ["长者", "亮脸", "减亮"],
            portrait_group="senior", exposure=-.08, relight=-.18, opacity=.85),
    _recipe("senior_warm_family", "长者暖调合影", "color", "温和全图暖调与少量肤色整理，不磨平皮肤起伏。", ["长者", "合影", "暖色", "全图"],
            portrait_group="senior", even=.06, warmth=.20, saturation=.96, opacity=.75),
    _recipe("senior_cold_cast", "长者偏冷回暖", "color", "手动为偏冷照片增加全图暖色并略提曝光，保留肤质。", ["长者", "偏冷", "回暖", "全图"],
            portrait_group="senior", exposure=.06, warmth=.18, opacity=.80),
    _recipe("senior_soft_color", "长者柔和色彩", "color", "降低全图饱和度、轻提曝光，适合柔和纪实氛围。", ["长者", "柔色", "纪实", "全图"],
            portrait_group="senior", exposure=.04, warmth=.04, saturation=.82, opacity=.90),
    _recipe("senior_monochrome", "长者纪实黑白", "color", "全图沉静黑白与极轻肤色整理，保留皱纹和斑点。", ["长者", "黑白", "纪实", "全图"],
            portrait_group="senior", even=.05, exposure=-.12, saturation=0.0),
    _recipe("adult_document", "成人证件自然", "retouch", "轻修肤色和瑕疵、温和补脸；不改变五官或背景颜色。", ["成人", "证件", "自然"],
            portrait_group="adult", featured=True, smooth=.10, blemishes=.12, even=.16, relight=.12, opacity=.85),
    _recipe("adult_office", "成人职场肖像", "retouch", "适度均匀肤色与轻柔肤质，全图略降饱和并偏冷。", ["成人", "职场", "肤色", "全图"],
            portrait_group="adult", featured=True, smooth=.16, blemishes=.18, even=.24, warmth=-.06, saturation=.96, opacity=.80),
    _recipe("adult_lowlight", "成人室内暗光", "light", "增加全图曝光与面部补光、轻修肤色；请检查暗部噪点。", ["成人", "室内", "暗光", "曝光"],
            portrait_group="adult", smooth=.08, blemishes=.10, even=.10, exposure=.30, relight=.18, saturation=.94, opacity=.80),
    _recipe("adult_outdoor", "成人户外亮脸", "light", "轻压偏亮面部、整理肤色，全图略降暖色。", ["成人", "户外", "亮脸"],
            portrait_group="adult", smooth=.12, blemishes=.08, even=.20, relight=-.10, warmth=-.10, opacity=.75),
    _recipe("adult_makeup_detail", "成人妆面保细节", "retouch", "极轻肤质和肤色整理，不增加妆容；可用保护笔刷保留妆面。", ["成人", "妆面", "细节"],
            portrait_group="adult", smooth=.06, blemishes=.06, even=.08, texture=.02, opacity=.75),
    _recipe("adult_acne_light", "成人轻度去痘", "retouch", "轻修识别到的独立面部痘点，建议先框选需处理位置。", ["成人", "去痘", "痘点", "局部"],
            portrait_group="adult", acne=.30),
    _recipe("adult_acne_medium", "成人加强去痘", "retouch", "加强已识别痘点的修复，不叠加磨皮；放大检查痣和雀斑。", ["成人", "去痘", "痘点", "加强"],
            portrait_group="adult", acne=.60),
    _recipe("adult_acne_even", "成人去痘匀肤", "retouch", "修复独立痘点并轻整肤色，可框选限制作用位置。", ["成人", "去痘", "肤色", "局部"],
            portrait_group="adult", acne=.45, even=.16, opacity=.90),
    _recipe("adult_monochrome", "成人沉静黑白", "color", "全图黑白、略压曝光并轻修肤质，适合克制的肖像氛围。", ["成人", "黑白", "全图"],
            portrait_group="adult", smooth=.06, even=.08, exposure=-.06, saturation=0.0),
    _recipe("adult_body_even", "成人全身轻匀肤", "body", "轻整面部、颈部和裸露皮肤色差；请检查自动皮肤选区。", ["成人", "全身", "肤色"],
            portrait_group="adult", smooth=.08, blemishes=.06, even=.28, region="skin", opacity=.80),
    _recipe("youth_true", "少年真实肤质", "retouch", "只做极轻肤色整理，默认不磨皮、不修复斑点。", ["少年", "真实", "保纹"],
            portrait_group="youth", featured=True, even=.04, opacity=.80),
    _recipe("youth_school", "少年校园肖像", "light", "轻补面部、少量均匀肤色，全图略增色彩。", ["少年", "校园", "补脸"],
            portrait_group="youth", featured=True, even=.08, relight=.16, saturation=1.02, opacity=.85),
    _recipe("youth_outdoor", "少年户外清爽", "light", "轻补面部并略提全图曝光，全图少量偏冷。", ["少年", "户外", "清爽"],
            portrait_group="youth", exposure=.08, relight=.22, warmth=-.06, opacity=.80),
    _recipe("youth_backlight", "少年逆光补脸", "light", "补亮逆光面部并极轻整理肤色，保留真实皮肤纹理。", ["少年", "逆光", "补脸"],
            portrait_group="youth", even=.04, relight=.44, opacity=.80),
    _recipe("youth_warm", "少年暖调日常", "color", "全图温和暖色、略增饱和并轻补面部，不磨皮。", ["少年", "日常", "暖色", "全图"],
            portrait_group="youth", relight=.08, warmth=.18, saturation=1.03, opacity=.80),
    _recipe("youth_clean_color", "少年清淡色彩", "color", "轻整肤色，全图少量偏冷并降低饱和度。", ["少年", "清淡", "肤色", "全图"],
            portrait_group="youth", even=.10, warmth=-.08, saturation=.97, opacity=.80),
    _recipe("youth_acne_light", "少年轻度去痘", "retouch", "仅轻修识别到的独立痘点，保留毛孔；建议先框选。", ["少年", "去痘", "局部", "保纹"],
            portrait_group="youth", acne=.25),
    _recipe("youth_acne_even", "少年痘点轻匀肤", "retouch", "适度修复独立痘点并轻整肤色，不叠加磨皮。", ["少年", "去痘", "肤色", "局部"],
            portrait_group="youth", acne=.40, even=.08, opacity=.90),
    _recipe("auto_balance", "自然明暗", "light", "按原片中间调与高光余量整理曝光；框选后只分析选区。", ["自动", "曝光", "明暗", "自适应"],
            featured=True, exposure=0.0),
)
BUILTIN_IDS = frozenset(preset["id"] for preset in BUILTIN_PRESETS)


def builtin_presets():
    """Return independent recipes so callers cannot mutate the shared catalog."""
    return deepcopy(list(BUILTIN_PRESETS))


def _text(value, label, maximum, *, optional=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{label}须为不超过 {maximum} 字的文本。")
    value = value.strip()
    if not value and not optional:
        raise ValueError(f"请填写{label}。")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise ValueError(f"{label}不能包含控制字符。")
    return value


def custom_payload(data):
    if not isinstance(data, dict) or set(data) - {"name", "description", "params"}:
        raise ValueError("预设仅支持 name、description 和 params。")
    if not isinstance(data.get("params"), dict):
        raise ValueError("预设 params 必须是参数对象。")
    return {"name": _text(data.get("name"), "预设名称", MAX_PRESET_NAME),
            "description": _text(data.get("description", ""), "预设说明", MAX_PRESET_DESCRIPTION, optional=True),
            "params": validate_params(data["params"])}


def custom_record(identifier, data):
    return {"id": identifier, **custom_payload(data), "category": "mine", "builtin": False,
            "tags": ["自定义"], "featured": False, "apply_mode": "snapshot",
            "apply_groups": list(PARAMETER_GROUPS), "apply_keys": list(NEUTRAL), "adaptive": False,
            "operations": [{"id": group, "label": GROUP_LABELS[group], "keys": list(keys), "adaptive": False}
                           for group, keys in PARAMETER_GROUPS.items()]}
