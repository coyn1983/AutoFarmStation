"""隐私脱敏.

所有日志/反馈/导出文本都需经 Sanitize 处理,确保不含:
- Windows 用户名
- 游戏安装绝对路径
- AppData、TEMP 路径
- 机器名

实现要点(审计报告 #14):
- 大小写不敏感:Windows 路径不区分大小写,`str.replace` 会漏 `C:\\users\\<用户名>`;
- 正斜杠 / 反斜杠两种写法都要覆盖:pathlib 的 ``as_posix()``、``QUrl``、
  PowerShell 输出常用 ``C:/Users/x/...``;
- UNC(``\\\\server\\share``)与 `%USERPROFILE%` 之外的用户目录变体;
- 「游戏安装目录」这类无法靠通用正则识别的路径,由调用方在探测到 exe / Steam
  库时通过 ``register_sensitive_prefix()`` 登记,脱敏时一并替换。
"""

from __future__ import annotations

import getpass
import os
import re
import socket
from typing import Final

# === 通用占位符 ===
PLACEHOLDER_USERNAME: Final[str] = "%USERPROFILE%"
PLACEHOLDER_APPDATA: Final[str] = "%APPDATA%"
PLACEHOLDER_LOCALAPPDATA: Final[str] = "%LOCALAPPDATA%"
PLACEHOLDER_TEMP: Final[str] = "%TEMP%"
PLACEHOLDER_HOSTNAME: Final[str] = "<主机名>"
PLACEHOLDER_GAME: Final[str] = "%GAME_PATH%"
PLACEHOLDER_UNC: Final[str] = "<网络路径>"

# === 正则缓存 ===
_username: str = ""
_appdata: str = ""
_localappdata: str = ""
_temp: str = ""
_hostname: str = ""
_profile: str = ""
# 由调用方登记的敏感前缀(游戏安装目录等):原串 → 占位符
_extra: dict[str, str] = {}
# 编译好的 (pattern, 占位符) 列表;登记新前缀时重建
_patterns: list[tuple[re.Pattern[str], str]] = []

# 兜底:盘符 \Users\xxx\ 这种形式不管用户名是什么都替换为占位符(反斜杠/正斜杠两种)
_USER_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/](?:Users|用户)[\\/][^\s\"'<>|?*:/\\]+", re.IGNORECASE)
# UNC:\\?\server\share 或 \\server\share
_UNC_RE = re.compile(r"\\\\[?\\]?[A-Za-z0-9_.\-]+(?:\\[^\s\"'<>|?]*)?", re.IGNORECASE)


_initialized = False


def _init() -> None:
    """首次使用时初始化本机敏感字段."""
    global _username, _appdata, _localappdata, _temp, _hostname, _profile, _initialized
    if _initialized:
        return
    _initialized = True
    try:
        _username = getpass.getuser()
    except Exception:
        _username = ""
    try:
        _hostname = socket.gethostname()
    except Exception:
        _hostname = ""
    _appdata = os.environ.get("APPDATA", "") or ""
    _localappdata = os.environ.get("LOCALAPPDATA", "") or ""
    _temp = os.environ.get("TEMP", "") or os.environ.get("TMP", "") or ""
    _profile = os.environ.get("USERPROFILE", "") or ""
    if not _profile and _username:
        _profile = f"C:\\Users\\{_username}"
    _rebuild_patterns()


def _esc_variants(path: str) -> list[str]:
    """同一路径的反斜杠 / 正斜杠两种写法."""
    out = [path]
    alt = path.replace("\\", "/")
    if alt != path:
        out.append(alt)
    return out


def _rebuild_patterns() -> None:
    """把已知敏感字段 + 登记的前缀编译成大小写不敏感的正则表(长的先替换)."""
    _init_if_needed()
    pairs: list[tuple[str, str]] = []
    for value, mark in (
        (_appdata, PLACEHOLDER_APPDATA),
        (_localappdata, PLACEHOLDER_LOCALAPPDATA),
        (_temp, PLACEHOLDER_TEMP),
        (_profile, PLACEHOLDER_USERNAME),
    ):
        if value:
            for v in _esc_variants(value):
                pairs.append((v, mark))
    for value, mark in _extra.items():
        for v in _esc_variants(value):
            pairs.append((v, mark))
    # 先长后短,避免 C:\Users\x 先把 C:\Users\x\AppData 截成占位符+尾巴
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
    _patterns.clear()
    for value, mark in pairs:
        _patterns.append((re.compile(re.escape(value), re.IGNORECASE), mark))


def _init_if_needed() -> None:
    _init()


def register_sensitive_prefix(path: str, placeholder: str = PLACEHOLDER_GAME) -> None:
    """登记一个需要脱敏的绝对路径前缀(游戏安装目录 / Steam 库).

    docstring 承诺「不含游戏安装绝对路径」,而这类路径无法靠通用正则识别,
    只能由探测到它的地方(启动信息、库扫描)登记进来(#14)。
    """
    _init_if_needed()
    p = str(path or "").strip().rstrip("\\/")
    if len(p) < 3 or p in _extra:
        return
    _extra[p] = placeholder
    _rebuild_patterns()


def _replace_known(text: str) -> str:
    """替换已知的本机敏感字段(两种分隔符 + 大小写不敏感)."""
    _init()
    out = text
    for pattern, mark in _patterns:
        out = pattern.sub(mark, out)
    # 用户名 / 主机名单独处理:它们可能出现在不带路径上下文的地方。
    # 只在足够长时才做「裸词」替换,否则像 admin / me 这种账号名会把日志搅烂。
    if _username and len(_username) >= 4:
        out = re.sub(re.escape(_username), PLACEHOLDER_USERNAME, out, flags=re.IGNORECASE)
    if _hostname and len(_hostname) >= 3:
        out = re.sub(re.escape(_hostname), PLACEHOLDER_HOSTNAME, out, flags=re.IGNORECASE)
    return out


def sanitize(text: str) -> str:
    """把字符串中的本机隐私字段全部替换为占位符."""
    if not text:
        return text
    out = _replace_known(text)
    # 兜底正则:用户名未知 / 别的账号 / UNC
    out = _USER_PATH_RE.sub(PLACEHOLDER_USERNAME, out)
    out = _UNC_RE.sub(PLACEHOLDER_UNC, out)
    return out


def sanitize_path(path: str) -> str:
    """脱敏路径,优先做精确替换,再走兜底正则."""
    return sanitize(path)


def user_data_dir_display() -> str:
    """UI 显示用:返回 %APPDATA%\\AutoFarmStation(永远不展示真实用户名)."""
    return f"{PLACEHOLDER_APPDATA}\\{'AutoFarmStation'}"


def user_log_dir_display() -> str:
    """UI 显示用:返回 %APPDATA%\\AutoFarmStation\\logs."""
    return f"{user_data_dir_display()}\\logs"


def get_username() -> str:
    """获取当前用户名(返回占位符以避免日志/UI 暴露)."""
    _init()
    return PLACEHOLDER_USERNAME if _username else PLACEHOLDER_USERNAME