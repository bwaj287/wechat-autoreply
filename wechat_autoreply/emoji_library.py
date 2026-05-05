from __future__ import annotations

from pathlib import Path, PurePosixPath
import zipfile


DEFAULT_WECHAT_EMOJI_NAMES: list[str] = [
    "666",
    "Emm",
    "亲亲",
    "偷笑",
    "傲慢",
    "再见",
    "加油",
    "加油加油",
    "发呆",
    "发怒",
    "可怜",
    "右哼哼",
    "叹气",
    "吃瓜",
    "吐",
    "吐舌",
    "呲牙",
    "咒骂",
    "哇",
    "嘘",
    "嘿哈",
    "囧",
    "困",
    "坏笑",
    "大哭",
    "天啊",
    "失望",
    "奸笑",
    "好的",
    "委屈",
    "害羞",
    "尴尬",
    "得意",
    "微笑",
    "快哭了",
    "恐惧",
    "悠闲",
    "惊恐",
    "惊讶",
    "愉快",
    "憨笑",
    "打脸",
    "抓狂",
    "抠鼻",
    "拥抱",
    "捂脸",
    "撇嘴",
    "擦汗",
    "敲打",
    "无语",
    "旺柴",
    "晕",
    "机智",
    "汗",
    "流泪",
    "猪头",
    "生病",
    "疑问",
    "白眼",
    "皱眉",
    "睡",
    "破涕为笑",
    "社会社会",
    "笑脸",
    "翻白眼",
    "耶",
    "脸红",
    "色",
    "苦涩",
    "衰",
    "裂开",
    "让我看看",
    "调皮",
    "鄙视",
    "闭嘴",
    "阴险",
    "难过",
    "骷髅",
    "鼓掌",
]

_EMOJI_EXTS = {".png", ".webp", ".gif", ".jpg", ".jpeg"}
_SOURCE_HINTS = ("emojipedia.org", "mp.weixin.qq.com")


def _decode_zip_name(raw_name: str) -> str:
    try:
        return raw_name.encode("cp437").decode("gbk")
    except Exception:
        return raw_name


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def load_wechat_emoji_names(zip_path: str = "") -> list[str]:
    if not zip_path:
        return list(DEFAULT_WECHAT_EMOJI_NAMES)
    path = Path(zip_path).expanduser()
    if not path.exists():
        return list(DEFAULT_WECHAT_EMOJI_NAMES)
    try:
        with zipfile.ZipFile(path) as archive:
            names: list[str] = []
            for raw_name in archive.namelist():
                decoded = _decode_zip_name(raw_name)
                normalized = decoded.lower()
                if not any(hint in normalized for hint in _SOURCE_HINTS):
                    continue
                suffix = PurePosixPath(decoded).suffix.lower()
                if suffix not in _EMOJI_EXTS:
                    continue
                stem = PurePosixPath(decoded).stem.strip()
                if not stem or "." in stem:
                    continue
                names.append(stem)
            deduped = _dedupe_preserve_order(names)
            if deduped:
                return deduped
    except Exception:
        pass
    return list(DEFAULT_WECHAT_EMOJI_NAMES)


def build_wechat_emoji_codes(names: list[str]) -> list[str]:
    return [f"[{name}]" for name in names if name]

