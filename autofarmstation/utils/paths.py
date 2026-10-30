"""路径与用户目录处理.

全部使用 %APPDATA% / %USERPROFILE% / %LOCALAPPDATA% / %TEMP% 占位符,
绝不写死任何盘符或用户名。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Final

# === 应用元数据 ===
APP_DIR_NAME: Final[str] = "AutoFarmStation"


def _env(name: str, default: str) -> str:
    """读取环境变量,空值用 fallback."""
    val = os.environ.get(name)
    if val:
        return val
    return default


def user_data_dir() -> Path:
    """返回 %APPDATA%\\AutoFarmStation,不存在则创建."""
    base = _env("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    p = Path(base) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_log_dir() -> Path:
    """返回 %APPDATA%\\AutoFarmStation\\logs."""
    p = user_data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_cache_dir() -> Path:
    """返回 %APPDATA%\\AutoFarmStation\\cache."""
    p = user_data_dir() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_macro_dir() -> Path:
    """返回 %APPDATA%\\AutoFarmStation\\macros(用户录制的宏)."""
    p = user_data_dir() / "macros"
    p.mkdir(parents=True, exist_ok=True)
    return p


def user_preset_dir() -> Path:
    """返回 %APPDATA%\\AutoFarmStation\\presets(用户自定义预设)."""
    p = user_data_dir() / "presets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def temp_dir() -> Path:
    """返回系统临时目录,确保存在."""
    p = Path(tempfile.gettempdir())
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_dir() -> Path:
    """返回资源目录(PyInstaller 打包后指向 _MEIPASS,源码时指向仓库根)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    # 源码运行时,utils/paths.py 在 autofarmstation/utils/ 下,父级是 autofarmstation/,再父级是仓库根
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """主配置文件路径."""
    return user_data_dir() / "config.json"


def stats_path() -> Path:
    """统计数据文件路径."""
    return user_data_dir() / "stats.json"


def session_path() -> Path:
    """上次会话快照路径(用于「是否恢复上次的窗口」)."""
    return user_data_dir() / "session.json"


def user_bat_dir() -> Path:
    """%APPDATA%\\AutoFarmStation\\bats —— 用户自加的 bat 脚本."""
    p = user_data_dir() / "bats"
    p.mkdir(parents=True, exist_ok=True)
    return p


def schedule_path() -> Path:
    """定时任务列表路径(与主配置分开存,避免和窗口列表一起被覆盖)."""
    return user_data_dir() / "schedules.json"


def bundled_bat_dir() -> Path:
    """内置 bat 库:PyInstaller 时指向 _MEIPASS/bat,源码运行时指向仓库根/bat."""
    return resource_dir() / "bat"


# === 单实例互斥(#9) ===
# 句柄必须一直持有到进程退出,所以存在模块级字典里,不返回给调用方 close。
# 键是互斥体名:同一名字第二次调用必须返回 False(这才是「已有实例」的语义);
# 不同名字互不影响,自检才能反复验证。
_MUTEX_HANDLES: dict[str, object] = {}


def acquire_single_instance(name: str = "AutoFarmStation") -> bool:
    """尝试取得命名互斥体;已有实例在运行时返回 False.

    没有互斥时双开就是 last-writer-wins:两份进程各自保存配置,后写的把前一份
    的窗口列表 / 预设整个覆盖掉。
    """
    if name in _MUTEX_HANDLES:
        return True
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [
                wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(
                None, False, f"Local\\{name}.single.instance")
            err = ctypes.get_last_error()
            if not handle:
                # 连句柄都拿不到(异常环境):不拦用户启动
                return True
            if err == 183:  # ERROR_ALREADY_EXISTS
                # 已经有人在跑:关掉这个新句柄,也别登记
                try:
                    kernel32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
                return False
            _MUTEX_HANDLES[name] = handle
            return True
        except Exception:  # noqa: BLE001
            return True
    # 非 Windows 兜底:O_EXCL 的 pid 锁文件(本软件面向 Windows,够用即可)
    try:
        lock = user_data_dir() / f"{name}.lock"
        if lock.exists():
            try:
                pid = int(lock.read_text(encoding="utf-8").strip() or "0")
                if pid > 0 and os.path.exists(f"/proc/{pid}"):
                    return False
            except (ValueError, OSError):
                pass
            try:
                lock.unlink()
            except OSError:
                pass
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        _MUTEX_HANDLES[name] = lock
        return True
    except OSError:
        return True


def steam_config_path() -> Path:
    """Steam 的 steam.cfg 路径(平台 store 进程读取)."""
    base = _env("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return Path(base) / "Steam" / "steam.cfg"


def steam_userdata_dir() -> Path:
    """Steam 用户数据根目录(localconfig.vdf 等)."""
    base = _env("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    return Path(base) / "Steam" / "htmlcache"  # 占位,实际路径见具体子模块调用方


# === 工具函数 ===
def safe_filename(name: str) -> str:
    """把任意字符串规整为合法文件名."""
    bad = '<>:"/\\|?*\x00'
    out = "".join("_" if ch in bad else ch for ch in name)
    out = out.strip(" .")
    return out or "unnamed"


def contained_path(base: Path | str, name: str | Path, *, must_be_file: bool = True) -> Path:
    """把外部给的 ``name`` 解析成 ``base`` 目录**内**的路径(#10 路径穿越).

    预设 id、bat manifest 的 ``file`` 字段这些都来自可编辑的 JSON:原样拼接时
    ``..\\..\\x``(Windows 的 ntpath 认反斜杠)就能写到目标目录之外,bat 那条
    路径甚至会被当作脚本执行。所以:只取最后一段 → 过滤非法字符 → resolve
    之后必须仍在 base 里,否则抛 ValueError,由调用方丢弃这条记录。
    """
    base_r = Path(base).resolve()
    raw = str(name or "")
    leaf_name = Path(raw.replace("\\", "/")).name
    leaf = safe_filename(leaf_name)
    if must_be_file and (not leaf or leaf in (".", "..")):
        raise ValueError(f"非法名称:{raw!r}")
    target = (base_r / leaf).resolve()
    try:
        target.relative_to(base_r)
    except ValueError:
        raise ValueError(f"路径越界:{raw!r}") from None
    return target


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 6) -> None:
    """``os.replace`` 的 Windows 重试包装.

    Windows 上 rename 会因「目标正被另一个 replace 处理中」或「杀软刚打开源文件
    扫描」而短暂返回 ACCESS_DENIED / PERMISSION。重试几次即可过去 —— 不重试的话
    多线程并发保存就会偶发丢一次写入。
    """
    delay = 0.01
    last: OSError | None = None
    for _ in range(attempts):
        try:
            os.replace(str(src), str(dst))
            return
        except PermissionError as e:  # noqa: PERF203
            last = e
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        except OSError as e:
            raise
    if last is not None:
        raise last


def atomic_write(path: Path, content: bytes | str) -> None:
    """原子写入:同目录唯一临时文件 + ``os.replace``(#9).

    旧实现先 ``unlink()`` 再 ``shutil.move()``,中间有一个「目标文件不存在」
    的窗口期(此时崩溃 = 配置丢失),而且固定名 ``xxx.tmp`` 会让两个进程/两次
    写入互相覆盖。``os.replace`` 在 Windows 上是原子的(同卷 rename),
    读方要么看到旧内容要么看到新内容,不会看到半截或空文件。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_bytes = isinstance(content, (bytes, bytearray))
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp.new", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    adopted = False  # fd 是否已交给文件对象(决定失败时要不要自己 close)
    try:
        # 注意位置参数顺序:os.fdopen(fd, mode, buffering, encoding) ——
        # encoding 必须用关键字传,否则会被当成 buffering 而抛 TypeError
        f = os.fdopen(fd, "wb" if is_bytes else "w",
                      encoding=None if is_bytes else "utf-8")
        adopted = True
        with f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except Exception:
        if not adopted:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise