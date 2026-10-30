"""窗口枚举与信息获取.

只用 ctypes + 标准库,不依赖 pywin32.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Iterable

# === Windows 常量 ===
GW_HWNDNEXT = 2
GA_ROOT = 2

# GetWindowLong / SetWindowLong indices
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZE = 0x20000000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000

# EnumWindowsProc callback
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


# === 数据类 ===
@dataclass
class WindowInfo:
    """窗口信息快照."""

    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    visible: bool = True
    is_tool_window: bool = False
    is_app_window: bool = False
    has_caption: bool = False
    is_minimized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    @property
    def is_valid(self) -> bool:
        """窗口句柄是否仍然有效(用于运行期热检查)."""
        return bool(self.hwnd) and _is_window(self.hwnd)


# === ctypes 函数原型 ===
_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
_user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = wintypes.LONG
_user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
_user32.GetAncestor.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.MoveWindow.argtypes = [
    wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.BOOL,
]
_user32.MoveWindow.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = wintypes.LPARAM


# --- 显示器工作区 / 前置窗口 ---
class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.IsZoomed.argtypes = [wintypes.HWND]
_user32.IsZoomed.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
]
_user32.SystemParametersInfoW.restype = wintypes.BOOL

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

MONITOR_DEFAULTTONEAREST = 2
SPI_GETWORKAREA = 0x0030


# === 公开 API ===
def _is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(_user32.IsWindow(hwnd))


def get_window_info(hwnd: int) -> WindowInfo | None:
    """获取单个窗口的当前信息."""
    if not _is_window(hwnd):
        return None
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, title, 511)
    cls = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls, 255)
    rect = wintypes.RECT(0, 0, 0, 0)
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    visible = bool(_user32.IsWindowVisible(hwnd))
    try:
        ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
    except Exception:
        ex_style, style = 0, 0
    is_minimized = bool(style & WS_MINIMIZE)
    return WindowInfo(
        hwnd=hwnd,
        pid=pid.value,
        title=title.value or "",
        class_name=cls.value or "",
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        visible=visible and not is_minimized,
        is_tool_window=bool(ex_style & WS_EX_TOOLWINDOW),
        is_app_window=bool(ex_style & WS_EX_APPWINDOW),
        has_caption=bool(style & WS_CAPTION),
        is_minimized=is_minimized,
    )


def list_visible_windows(
    *,
    include_tool: bool = False,
    title_filter: str = "",
    require_app_window: bool = False,
) -> list[WindowInfo]:
    """枚举所有可见顶层窗口.

    - 排除:不可见、纯工具窗口(可选包含)、纯子窗口
    """
    out: list[WindowInfo] = []
    filter_lower = title_filter.lower()

    def cb(hwnd: int, _lparam: int) -> bool:
        info = get_window_info(hwnd)
        if info is None:
            return True
        # 排除被最小化的(默认;若 caller 想包含,自己处理)
        if not info.visible:
            return True
        # 排除无标题的窗口(很多后台 helper)
        if not info.title.strip():
            return True
        # 排除纯工具窗口
        if not include_tool and info.is_tool_window and not info.is_app_window:
            return True
        if require_app_window and not info.is_app_window:
            return True
        if filter_lower and filter_lower not in info.title.lower():
            return True
        out.append(info)
        return True

    _user32.EnumWindows(WNDENUMPROC(cb), 0)
    # 排序:按标题
    out.sort(key=lambda w: w.title.lower())
    return out


def find_windows_for_pids(
    pids: Iterable[int],
    *,
    include_hidden: bool = False,
) -> dict[int, list[WindowInfo]]:
    """按 PID 分组返回该进程的所有可见顶层窗口.

    include_hidden=True 时把隐藏 / 无标题窗口也带回来(给「按 PID / 按进程名添加」用)。
    """
    pid_set = set(int(p) for p in pids if p)
    out: dict[int, list[WindowInfo]] = {p: [] for p in pid_set}
    if not pid_set:
        return out

    def cb(hwnd: int, _lparam: int) -> bool:
        info = get_window_info(hwnd)
        if info is None:
            return True
        if not include_hidden and (not info.visible or not info.title.strip()):
            return True
        if info.pid in pid_set:
            out[info.pid].append(info)
        return True

    _user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def list_all_windows_for_pid(
    pid: int,
    *,
    include_hidden: bool = True,
    only_toplevel: bool = True,
) -> list[WindowInfo]:
    """单个 PID 的所有窗口列表(默认含隐藏 / 无标题,排除 WS_CHILD 子窗口).

    给 ProcessManager.add_by_pid_force / add_by_process_name 用:
    即使目标游戏的窗口被隐藏或无标题,也能被枚举出来供用户「强制添加」。
    """
    out: list[WindowInfo] = []
    if not pid:
        return out

    def cb(hwnd: int, _lparam: int) -> bool:
        info = get_window_info(hwnd)
        if info is None or info.pid != pid:
            return True
        if not include_hidden and (not info.visible or not info.title.strip()):
            return True
        if only_toplevel:
            try:
                style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
            except Exception:
                style = 0
            if style & WS_CHILD:
                return True
        out.append(info)
        return True

    _user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def list_all_toplevel_windows(*, include_child: bool = False) -> list[WindowInfo]:
    """一次 EnumWindows 拿到所有顶层窗口(含隐藏 / 最小化 / 无标题).

    这是「列窗口」的正确姿势:整轮枚举只走一遍。旧写法(按每个 PID 各调一次
    `list_all_windows_for_pid`)在 288 进程的机器上等于 288 次全量 EnumWindows,
    勾上「包含隐藏窗口」后界面会僵住一秒起。
    """
    out: list[WindowInfo] = []

    def cb(hwnd: int, _lparam: int) -> bool:
        info = get_window_info(hwnd)
        if info is None:
            return True
        if not include_child:
            try:
                style = _user32.GetWindowLongW(hwnd, GWL_STYLE)
            except Exception:  # noqa: BLE001
                style = 0
            if style & WS_CHILD:
                return True
        out.append(info)
        return True

    _user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


# 这些标题 / 类名永远不是游戏主窗口(是 GDI 辅助窗、输入法代理窗等)
_JUNK_WINDOW_MARKS: tuple[str, ...] = (
    "gdi+ window", "dxgiwatchdogthreadwindow", "default ime", "msctfime ui",
    "ime-ui-default", "ime", "windows live", "gld20",
)


def is_junk_window(info: WindowInfo) -> bool:
    """判断某窗口是不是那种「1x1 的假窗口」(PoE 就有 GDI+ / DXGIWatchdog 两个)."""
    hay = f"{(info.title or '').strip().lower()} {(info.class_name or '').lower()}"
    return any(mark in hay for mark in _JUNK_WINDOW_MARKS)


def window_rank(info: WindowInfo) -> tuple[int, int, int, int]:
    """给「哪个窗口更像游戏主窗口」打分,元组小的优先。

    旧排序只看 `visible + 有标题`,于是最小化的真主窗口会被降到和一堆 1x1 的
    GDI+/DXGI/IME 假窗口同一档,平手后按枚举顺序反而挑中假窗口 —— 表现就是
    「按 PID 添加进去了,但预览是空白、点击没反应」。
    """
    title = (info.title or "").strip()
    junk = 1 if is_junk_window(info) else 0
    cw, ch = (0, 0)
    try:
        if info.hwnd:
            cw, ch = client_size(info.hwnd)
    except Exception:  # noqa: BLE001
        cw, ch = (0, 0)
    area = max(cw * ch, info.width * info.height)
    if cw >= 200 and ch >= 150 or area >= 200 * 150:
        size_rank = 0            # 尺寸像一个真画面
    elif area >= 64 * 64:
        size_rank = 1
    else:
        size_rank = 2            # 1x1 / 10x10 这类辅助窗
    if info.visible:
        state_rank = 0
    elif info.is_minimized:
        state_rank = 1
    else:
        state_rank = 2
    no_title = 1 if not title else 0
    tool = 1 if (info.is_tool_window and not info.is_app_window) else 0
    return (junk, size_rank, state_rank, no_title, tool)


def pick_main_window(candidates: list[WindowInfo]) -> WindowInfo | None:
    """从同一进程的候选里挑最像主窗口的那个(空列表返回 None)."""
    if not candidates:
        return None
    return min(candidates, key=window_rank)


def list_all_windows_across_processes(
    *,
    include_hidden: bool = True,
    only_toplevel: bool = True,
    pid_limit: int = 600,
) -> list[WindowInfo]:
    """所有进程的所有顶层窗口(含隐藏 / 无标题)。「选择窗口」勾选
    「包含隐藏窗口」时用这个。

    实现已改为**一次 EnumWindows**(`list_all_toplevel_windows`),不再按 PID
    反复全量枚举;`pid_limit` 保留只为兼容老调用方签名,现在不再起作用。
    """
    out = list_all_toplevel_windows(include_child=not only_toplevel)
    if not include_hidden:
        out = [w for w in out if w.visible and (w.title or "").strip()]
    return out


def get_foreground_hwnd() -> int:
    """获取前景窗口句柄."""
    return int(_user32.GetForegroundWindow() or 0)


def set_foreground(hwnd: int) -> bool:
    """尝试把窗口切到前台."""
    return bool(_user32.SetForegroundWindow(hwnd))


def client_origin(hwnd: int) -> tuple[int, int]:
    """返回客户区左上角在屏幕坐标系的 (x, y)."""
    pt = wintypes.POINT(0, 0)
    _user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y)


def client_size(hwnd: int) -> tuple[int, int]:
    """返回客户区尺寸 (w, h)."""
    rect = wintypes.RECT(0, 0, 0, 0)
    _user32.GetClientRect(hwnd, ctypes.byref(rect))
    return (max(0, rect.right - rect.left), max(0, rect.bottom - rect.top))


def move_window(hwnd: int, x: int, y: int, w: int, h: int) -> bool:
    return bool(_user32.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True))


# === SW_* 命令用于 ShowWindow ===
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_RESTORE = 9


def show_window(hwnd: int, cmd: int) -> bool:
    return bool(_user32.ShowWindow(hwnd, cmd))


# === SetWindowPos flags ===
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_FRAMECHANGED = 0x0020


def set_window_pos(
    hwnd: int,
    x: int | None = None,
    y: int | None = None,
    w: int | None = None,
    h: int | None = None,
    *,
    after: int = 0,
    flags: int = 0,
) -> bool:
    """通用 SetWindowPos 调用,None 的维度不会被修改."""
    cx = -1 if x is None else int(x)
    cy = -1 if y is None else int(y)
    cw = -1 if w is None else int(w)
    ch = -1 if h is None else int(h)
    f = flags
    if x is None:
        f |= SWP_NOMOVE
    if y is None:
        f |= SWP_NOMOVE
    if w is None or h is None:
        f |= SWP_NOSIZE
    return bool(_user32.SetWindowPos(hwnd, after, cx, cy, cw, ch, f))


# === PostMessage / SendMessage 辅助 ===
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010


def _makelparam(x: int, y: int) -> int:
    """把 (x, y) 合并成 LPARAM(低 16 位 x,高 16 位 y)."""
    return (y & 0xFFFF) << 16 | (x & 0xFFFF)


def post_click(hwnd: int, x: int, y: int, button: str = "left", double: bool = False) -> bool:
    """用 PostMessage 给指定窗口发鼠标点击(客户区坐标)."""
    if button == "left":
        if double:
            ok1 = bool(_user32.PostMessageW(hwnd, WM_LBUTTONDBLCLK, 0, _makelparam(x, y)))
        else:
            ok1 = bool(_user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, _makelparam(x, y)))
            ok2 = bool(_user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, _makelparam(x, y)))
            return ok1 and ok2
        return ok1
    elif button == "right":
        ok1 = bool(_user32.PostMessageW(hwnd, WM_RBUTTONDOWN, MK_RBUTTON, _makelparam(x, y)))
        ok2 = bool(_user32.PostMessageW(hwnd, WM_RBUTTONUP, 0, _makelparam(x, y)))
        return ok1 and ok2
    elif button == "middle":
        ok1 = bool(_user32.PostMessageW(hwnd, WM_MBUTTONDOWN, MK_MBUTTON, _makelparam(x, y)))
        ok2 = bool(_user32.PostMessageW(hwnd, WM_MBUTTONUP, 0, _makelparam(x, y)))
        return ok1 and ok2
    return False


def post_mouse_move(hwnd: int, x: int, y: int) -> bool:
    """发送鼠标移动消息."""
    return bool(_user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, _makelparam(x, y)))


def post_scroll(
    hwnd: int, delta: int, x: int = 0, y: int = 0, *, horizontal: bool = False
) -> bool:
    """后台投递滚轮消息(WM_MOUSEWHEEL / WM_MOUSEHWHEEL),不抢前台(#4).

    WPARAM 低 16 位是按键状态(滚轮恒为 0),高 16 位是**有符号**滚轮增量
    (一格 = 120);LPARAM 是客户区坐标。负数按 16 位补码放进高位。
    """
    d = max(-14400, min(14400, int(delta)))
    wparam = (d & 0xFFFF) << 16
    msg = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
    return bool(_user32.PostMessageW(hwnd, msg, wparam, _makelparam(x, y)))


def post_key(hwnd: int, vk: int, *, down: bool = True) -> bool:
    """发送按键消息(虚拟键码)."""
    msg = WM_KEYDOWN if down else WM_KEYUP
    return bool(_user32.PostMessageW(hwnd, msg, vk, 0))


# === 显示器工作区 / 聚焦 ===
def work_area(hwnd: int = 0) -> tuple[int, int, int, int] | None:
    """窗口所在显示器的工作区(已排除任务栏):(left, top, right, bottom).

    hwnd=0 时取主显示器工作区。
    """
    if hwnd:
        mon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        if mon:
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                r = mi.rcWork
                return (r.left, r.top, r.right, r.bottom)
    rect = wintypes.RECT(0, 0, 0, 0)
    if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return (rect.left, rect.top, rect.right, rect.bottom)
    return None


# GetSystemMetrics 原型(虚拟屏幕范围用于 SendInput ABSOLUTE 坐标归一化)
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def virtual_screen() -> tuple[int, int, int, int]:
    """整个虚拟桌面(所有显示器拼起来)的范围:(vx, vy, vw, vh).

    SendInput ABSOLUTE 坐标是基于虚拟桌面归一化到 0..65535 的,所以必须按这个范围计算,
    不能用主显示器像素数 —— 多显示器场景下主显示器之外的点会被错误归一化。
    失败时回退 (0, 0, 1, 1),保证调用方不会除零。
    """
    vx = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    vy = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    vw = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if vw <= 0 or vh <= 0:
        return (0, 0, 1, 1)
    return (vx, vy, vw, vh)


def is_minimized(hwnd: int) -> bool:
    return bool(_user32.IsIconic(hwnd))


def is_maximized(hwnd: int) -> bool:
    return bool(_user32.IsZoomed(hwnd))


def force_foreground(hwnd: int) -> bool:
    """把窗口提到前台的可靠版本.

    ``SetForegroundWindow`` 在前台锁定时会静默失败,这里退化为
    ``AttachThreadInput`` 到当前前台线程后再试一次。
    """
    if not _is_window(hwnd):
        return False
    _user32.BringWindowToTop(hwnd)
    if _user32.SetForegroundWindow(hwnd):
        return True
    fg = _user32.GetForegroundWindow()
    if not fg:
        return False
    tid_fg = _user32.GetWindowThreadProcessId(fg, None)
    tid_me = _kernel32.GetCurrentThreadId()
    if not tid_fg or tid_fg == tid_me:
        return False
    if not _user32.AttachThreadInput(tid_me, tid_fg, True):
        return False
    try:
        _user32.BringWindowToTop(hwnd)
        return bool(_user32.SetForegroundWindow(hwnd))
    finally:
        _user32.AttachThreadInput(tid_me, tid_fg, False)


def fit_to_work_area(
    hwnd: int,
    *,
    margin: int | tuple[int, int, int, int] | None = None,
    scale: float | None = None,
) -> bool:
    """把窗口调整成所在显示器工作区大小,可按边收缩 / 按比例缩放.

    margin 接受:
    - int           —— 四边各减同样的像素(默认 0 = 完全铺满,行为与旧版一致);
    - (l, t, r, b)  —— 各边独立减多少像素;
    - None          —— 视为 0。
    scale ∈ (0, 2] —— 在裁出来的矩形基础上按比例居中缩放;None 表示不缩。

    已经是最大化状态时保持最大化(游戏全屏更完整)。
    """
    wa = work_area(hwnd)
    if wa is None:
        return False
    left, top, right, bottom = wa
    ml, mt, mr, mb = _resolve_margin(margin)
    left += ml
    top += mt
    right -= mr
    bottom -= mb
    w = max(200, right - left)
    h = max(150, bottom - top)
    if scale is not None and 0 < scale <= 2:
        w = max(160, int(w * scale))
        h = max(120, int(h * scale))
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        left = cx - w // 2
        top = cy - h // 2
        right = left + w
        bottom = top + h
    if is_maximized(hwnd):
        return True
    # SWP_SHOWWINDOW 让被最小化 / 隐藏的窗口显出来;
    # SWP_FRAMECHANGED 让游戏重新计算非客户区 —— 修了部分游戏「改了尺寸但内容
    # 仍按旧 client size 渲染」的毛病。
    return set_window_pos(
        hwnd, left, top, right - left, bottom - top,
        flags=SWP_SHOWWINDOW | SWP_FRAMECHANGED,
    )


def _resolve_margin(
    margin: int | tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int]:
    """把 margin 参数归一为 (left, top, right, bottom)."""
    if margin is None:
        return (0, 0, 0, 0)
    if isinstance(margin, int):
        m = max(0, margin)
        return (m, m, m, m)
    if len(margin) == 4:
        return (
            max(0, int(margin[0])),
            max(0, int(margin[1])),
            max(0, int(margin[2])),
            max(0, int(margin[3])),
        )
    raise ValueError(f"margin 必须是 int 或 4-tuple,得到 {margin!r}")


def focus_window(
    hwnd: int,
    *,
    fit: bool = True,
    margin: int | tuple[int, int, int, int] | None = None,
    scale: float | None = None,
) -> bool:
    """聚焦窗口:最小化则还原 → 可选铺满工作区 → 提到前台.

    返回是否成功提到前台。
    """
    if not _is_window(hwnd):
        return False
    if is_minimized(hwnd):
        show_window(hwnd, SW_RESTORE)
    if fit:
        fit_to_work_area(hwnd, margin=margin, scale=scale)
    return force_foreground(hwnd)


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕坐标系的外接矩形 (left, top, right, bottom)."""
    x, y = client_origin(hwnd)
    w, h = client_size(hwnd)
    return (x, y, x + w, y + h)