"""按扩展名枚举本机可打开该文件的应用（macOS 应用清单）。

原理：读取各 .app 包内 Info.plist 的 CFBundleDocumentTypes，
匹配 CFBundleTypeExtensions（精确扩展名优先，声明 "*" 的应用排后）。
结果按扩展名进程内缓存——应用安装/卸载极少见，首次扫描后复用。
"""

from __future__ import annotations

import plistlib
import threading
from pathlib import Path

_EXACT_CAP = 12   # 精确匹配应用上限
_WILDCARD_CAP = 6  # 声明可打开任意文件的应用上限（避免刷屏）

_cache: dict[str, list[dict]] = {}
_cache_lock = threading.Lock()


def _default_app_dirs() -> list[Path]:
    return [
        Path("/Applications"),
        Path.home() / "Applications",
        Path("/System/Applications"),
    ]


def _scan(app_dirs: list[Path], ext: str) -> list[dict]:
    exact: list[Path] = []
    wildcard: list[Path] = []
    seen: set[Path] = set()
    for base in app_dirs:
        if not base.is_dir():
            continue
        # 覆盖一级目录与一级子目录（如 /Applications/Utilities）
        for app in sorted(list(base.glob("*.app")) + list(base.glob("*/*.app"))):
            if app in seen:
                continue
            seen.add(app)
            try:
                info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            for doc in info.get("CFBundleDocumentTypes") or []:
                exts = [str(e).lower() for e in doc.get("CFBundleTypeExtensions") or []]
                if ext in exts:
                    exact.append(app)
                    break
                if "*" in exts:
                    wildcard.append(app)
                    break

    def item(app: Path) -> dict:
        return {"name": app.stem, "path": str(app)}

    out = [item(a) for a in exact[:_EXACT_CAP]]
    out += [item(a) for a in wildcard[:_WILDCARD_CAP]]
    return out


def list_apps_for_extension(ext: str, app_dirs: list[Path] | None = None) -> list[dict]:
    """返回声明支持该扩展名的应用列表 [{name, path}]，精确匹配在前。"""
    key = (ext or "").lstrip(".").lower()
    if not key:
        return []
    if app_dirs is not None:
        return _scan(app_dirs, key)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    apps = _scan(_default_app_dirs(), key)
    with _cache_lock:
        _cache[key] = apps
    return apps
