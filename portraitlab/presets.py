"""Curated classic recipes; all effects use the existing PortraitLab engine.

SPDX-License-Identifier: AGPL-3.0-only
Recipes are starting points, not promises of a particular face or skin result.
"""
from __future__ import annotations

from copy import deepcopy
import unicodedata

from .engine import DEFAULT_PARAMS, validate_params

MAX_CUSTOM_PRESETS = 100
MAX_PRESET_NAME = 40
MAX_PRESET_DESCRIPTION = 180
CATEGORIES = (
    {"id": "retouch", "name": "修肤与质感"},
    {"id": "light", "name": "曝光与补脸"},
    {"id": "color", "name": "全图色调"},
    {"id": "body", "name": "身体皮肤"},
    {"id": "basic", "name": "基础"},
    {"id": "mine", "name": "我的预设"},
)
NEUTRAL = {
    **DEFAULT_PARAMS, "smooth": 0.0, "blemishes": 0.0, "even": 0.0,
    "texture": 0.0, "exposure": 0.0, "relight": 0.0, "warmth": 0.0,
    "saturation": 1.0, "opacity": 1.0,
}


def _recipe(identifier, name, category, description, tags, *, featured=False, **params):
    return {"id": identifier, "name": name, "category": category,
            "description": description, "tags": tags, "featured": featured,
            "builtin": True, "params": validate_params({**NEUTRAL, **params})}


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
    _recipe("original", "原图", "basic", "关闭当前照片的全部调整，保留保护笔刷记录。", ["原图", "重置", "对比"],
            opacity=0.0),
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
            "tags": ["自定义"], "featured": False}
