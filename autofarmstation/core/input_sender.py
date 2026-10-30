"""输入发送.

封装两种发送模式:
- `post` 模式:PostMessage 给指定 hwnd(对窗口化游戏有效,不抢焦点)
- `send` 模式:ctypes SendInput 真实输入(对全屏独占游戏有效,但需抢焦点)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import ctypes as c
import enum
import time
from dataclasses import dataclass

from . import window_finder as wf

# === Windows 输入常量 ===
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

# Mouse event flags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_ABSOLUTE = 0x8000

# 单次 type_text 的字符上限:超过直接截断,避免外部数据把输入队列灌死(#11)
MAX_TYPE_TEXT_LEN = 2000

# Keyboard event flags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004

# 虚拟键码(Virtual-Key Codes) - 完整表
VK = {
    "LBUTTON": 0x01, "RBUTTON": 0x02, "CANCEL": 0x03, "MBUTTON": 0x04,
    "XBUTTON1": 0x05, "XBUTTON2": 0x06, "BACK": 0x08, "TAB": 0x09,
    "CLEAR": 0x0C, "RETURN": 0x0D, "SHIFT": 0x10, "CONTROL": 0x11,
    "MENU": 0x12, "PAUSE": 0x13, "CAPITAL": 0x14,
    "ESCAPE": 0x1B, "SPACE": 0x20, "PRIOR": 0x21, "NEXT": 0x22,
    "END": 0x23, "HOME": 0x24, "LEFT": 0x25, "UP": 0x26,
    "RIGHT": 0x27, "DOWN": 0x28, "SELECT": 0x29, "PRINT": 0x2A,
    "EXECUTE": 0x2B, "SNAPSHOT": 0x2C, "INSERT": 0x2D, "DELETE": 0x2E,
    "HELP": 0x2F,
    **{f"{c}": i for c, i in zip("0123456789", range(0x30, 0x3A))},
    **{c: i for c, i in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", range(0x41, 0x5B))},
    "LWIN": 0x5B, "RWIN": 0x5C, "APPS": 0x5D, "SLEEP": 0x5F,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
    "MULTIPLY": 0x6A, "ADD": 0x6B, "SEPARATOR": 0x6C, "SUBTRACT": 0x6D,
    "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    **{f"F{i}": 0x6F + i for i in range(1, 25)},
    "NUMLOCK": 0x90, "SCROLL": 0x91,
    "LSHIFT": 0xA0, "RSHIFT": 0xA1, "LCONTROL": 0xA2, "RCONTROL": 0xA3,
    "LMENU": 0xA4, "RMENU": 0xA5,
    "OEM_1": 0xBA, "OEM_PLUS": 0xBB, "OEM_COMMA": 0xBC, "OEM_MINUS": 0xBD,
    "OEM_PERIOD": 0xBE, "OEM_2": 0xBF, "OEM_3": 0xC0, "OEM_4": 0xDB,
    "OEM_5": 0xDC, "OEM_6": 0xDD, "OEM_7": 0xDE, "OEM_8": 0xDF,
    "OEM_102": 0xE2,
}
# 反向:码 → 名称
VK_NAME: dict[int, str] = {v: k for k, v in VK.items()}


def parse_vk(name_or_code: str | int) -> int:
    """把 'F1' / 'ctrl' / '0x71' / '113' 都解析为 vk 整数."""
    if isinstance(name_or_code, int):
        return name_or_code & 0xFFFF
    s = str(name_or_code).strip()
    if not s:
        raise ValueError("空按键名")
    # 数字(十进制 / 十六进制)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16) & 0xFFFF
    if s.isdigit():
        return int(s) & 0xFFFF
    # 名称(大小写不敏感,允许 'ctrl' / 'esc' / 'return' 等别名)
    key = s.upper().replace(" ", "_")
    # 常用别名
    aliases = {
        "ENTER": "RETURN", "ESC": "ESCAPE", "DEL": "DELETE", "INS": "INSERT",
        "PGUP": "PRIOR", "PGDN": "NEXT", "CAPSLOCK": "CAPITAL", "CTRL": "CONTROL",
        "ALT": "MENU", "WIN": "LWIN", "ARROWLEFT": "LEFT", "ARROWRIGHT": "RIGHT",
        "ARROWUP": "UP", "ARROWDOWN": "DOWN",
        "N0": "NUMPAD0", "N1": "NUMPAD1", "N2": "NUMPAD2", "N3": "NUMPAD3",
        "N4": "NUMPAD4", "N5": "NUMPAD5", "N6": "NUMPAD6", "N7": "NUMPAD7",
        "N8": "NUMPAD8", "N9": "NUMPAD9",
        "+": "OEM_PLUS", "-": "OEM_MINUS", ",": "OEM_COMMA", ".": "OEM_PERIOD",
        "/": "OEM_2", ";": "OEM_1", "'": "OEM_7", "`": "OEM_3",
        "[": "OEM_4", "]": "OEM_6", "\\": "OEM_5", "=": "OEM_PLUS",
    }
    key = aliases.get(key, key)
    if key in VK:
        return VK[key]
    raise ValueError(f"未知按键: {name_or_code}")


def format_vk(vk: int) -> str:
    return VK_NAME.get(vk & 0xFFFF, f"0x{vk & 0xFFFF:02X}")


# === ctypes 结构体 ===
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", w.DWORD),
        ("wParamL", w.WORD),
        ("wParamH", w.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", w.DWORD),
        ("u", INPUT_UNION),
    ]


# === ctypes 函数原型 ===
_user32 = ctypes.WinDLL("user32", use_last_error=True)  # type: ignore[attr-defined]
_user32.SendInput.argtypes = [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = w.UINT
_user32.GetSystemMetrics.argtypes = [c.c_int]
_user32.GetSystemMetrics.restype = c.c_int  # type: ignore[arg-type]
_user32.SetCursorPos.argtypes = [c.c_int, c.c_int]  # type: ignore[arg-defined]
_user32.SetCursorPos.restype = w.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
_user32.GetCursorPos.restype = w.BOOL
_user32.mouse_event.argtypes = [w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_size_t]
_user32.mouse_event.restype = None

# 修正 SetCursorPos 类型
_user32.SetCursorPos.argtypes = [ctypes.c_long, ctypes.c_long]
_user32.SetCursorPos.restype = w.BOOL


# === 枚举 ===
class InputEventType(str, enum.Enum):
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    KEY_PRESS = "key_press"  # down + up
    DELAY = "delay"


class MouseButton(str, enum.Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


@dataclass
class _InputHelper:
    """对 SendInput 的一层薄封装."""

    @staticmethod
    def screen_size() -> tuple[int, int]:
        SM_CXSCREEN, SM_CYSCREEN = 0, 1
        return (
            _user32.GetSystemMetrics(SM_CXSCREEN),
            _user32.GetSystemMetrics(SM_CYSCREEN),
        )

    @staticmethod
    def move_abs(x: int, y: int) -> None:
        """鼠标绝对移动(0..65535 归一化坐标).

        SendInput 的 ABSOLUTE 归一化基准是**整个虚拟桌面**(所有显示器拼起来),
        不是主显示器 —— 多显示器场景下若按主显示器 sw/sh 归一化,副屏(尤其高 DPI 屏)
        上的点会偏到主屏里或屏幕外,这是 v1.6.0 之前连点器「点了没反应」的主要原因之一。
        """
        vx, vy, vw, vh = wf.virtual_screen()
        nx = int((x - vx) * 65535 / max(1, vw - 1))
        ny = int((y - vy) * 65535 / max(1, vh - 1))
        mi = MOUSEINPUT(nx, ny, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0)
        _send_inputs([INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi))])

    @staticmethod
    def click_at(x: int, y: int, button: MouseButton = MouseButton.LEFT) -> None:
        """绝对坐标点击(对前台窗口)."""
        _InputHelper.move_abs(x, y)
        time.sleep(0.005)
        if button == MouseButton.LEFT:
            down = MOUSEEVENTF_LEFTDOWN
            up = MOUSEEVENTF_LEFTUP
        elif button == MouseButton.RIGHT:
            down = MOUSEEVENTF_RIGHTDOWN
            up = MOUSEEVENTF_RIGHTUP
        else:
            down = MOUSEEVENTF_MIDDLEDOWN
            up = MOUSEEVENTF_MIDDLEUP
        mi_down = MOUSEINPUT(0, 0, 0, down, 0, 0)
        mi_up = MOUSEINPUT(0, 0, 0, up, 0, 0)
        _send_inputs([
            INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi_down)),
            INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi_up)),
        ])

    @staticmethod
    def move_relative(dx: int, dy: int) -> None:
        mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, 0)
        _send_inputs([INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi))])

    @staticmethod
    def scroll(delta: int, *, horizontal: bool = False) -> None:
        flag = MOUSEEVENTF_WHEEL | (MOUSEEVENTF_HWHEEL if horizontal else 0)
        mi = MOUSEINPUT(0, 0, delta & 0xFFFFFFFF, flag, 0, 0)
        _send_inputs([INPUT(INPUT_MOUSE, INPUT_UNION(mi=mi))])

    @staticmethod
    def key_down(vk: int) -> None:
        ki = KEYBDINPUT(vk & 0xFFFF, 0, 0, 0, 0)
        _send_inputs([INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=ki))])

    @staticmethod
    def key_up(vk: int) -> None:
        ki = KEYBDINPUT(vk & 0xFFFF, 0, KEYEVENTF_KEYUP, 0, 0)
        _send_inputs([INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=ki))])

    @staticmethod
    def key_press(vk: int, hold_ms: int = 30) -> None:
        _InputHelper.key_down(vk)
        time.sleep(max(0, hold_ms) / 1000.0)
        _InputHelper.key_up(vk)


def _send_inputs(inputs: list[INPUT]) -> int:
    """底层 SendInput 封装,返回成功发送数."""
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    return int(_user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))


# === 公共入口 ===
def real_input_allowed() -> bool:
    """是否允许「真实输入」路径(SendInput)。全局开关,默认开(#4).

    关掉之后 post 失败就只是失败,绝不会把点击/按键打到用户当前正在用的窗口上。
    """
    try:
        from ..utils.config import instance
        return bool(instance().get("input.allow_real_fallback", True))
    except Exception:  # noqa: BLE001
        return True


def ensure_foreground(hwnd: int, *, timeout: float = 0.3) -> bool:
    """把 ``hwnd`` 提到前台并**确认成功**(审计报告 #4).

    ``SetForegroundWindow`` 会被 Windows 的前台锁定策略拒绝,UIPI 也会挡住
    向提权窗口投递 —— 以前代码忽略返回值就接着 SendInput,结果坐标是游戏
    客户区的坐标,落点却是用户自己的文档 / 浏览器 / 聊天框。确认失败就返回
    False,让调用方放弃这次真实输入。
    """
    hwnd = int(hwnd or 0)
    if not hwnd or not wf._is_window(hwnd):
        return False
    try:
        if int(wf.get_foreground_hwnd() or 0) == hwnd:
            return True
    except Exception:  # noqa: BLE001
        return False
    try:
        wf.set_foreground(hwnd)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if int(wf.get_foreground_hwnd() or 0) == hwnd:
                return True
        except Exception:  # noqa: BLE001
            return False
        time.sleep(0.02)
    # 前台锁定时 SetForegroundWindow 会静默失败,退一步用绕锁方案再确认一次
    try:
        wf.force_foreground(hwnd)
        return int(wf.get_foreground_hwnd() or 0) == hwnd
    except Exception:  # noqa: BLE001
        return False


class InputSender:
    """对外的高级输入发送器."""

    def __init__(self, *, default_mode: str = "auto") -> None:
        """default_mode: 'auto' (默认)= 先后台投递,失败改真实输入;
        'post' = 仅后台投递;'send' = 直接真实输入。"""
        self.default_mode = default_mode if default_mode in ("auto", "post", "send") else "auto"
        # True 时即使提不到前台也照发 SendInput(旧行为)。默认 False:
        # 确认不了前台就不发,避免误打到用户的当前窗口(#4)。
        try:
            from ..utils.config import instance
            self.require_foreground = bool(
                instance().get("input.require_foreground", True))
        except Exception:  # noqa: BLE001
            self.require_foreground = True

    # --- 模式 ---
    def click(
        self,
        target: int | tuple[int, int, int],
        button: MouseButton = MouseButton.LEFT,
        *,
        double: bool = False,
        mode: str | None = None,
    ) -> tuple[bool, str]:
        """点击;返回 (ok, reason)。

        mode:
        - 'auto' (默认) — 先后台投递,PostMessage 失败(独占全屏 / UAC 隔离)时改用真实输入;
        - 'post'        — 只用后台投递;
        - 'send'        — 直接真实输入(需要前台权限 + 可能被目标检测为「模拟输入」)。

        target: int=hwnd(按客户区中心) 或 (hwnd, x, y)=精确客户区坐标。
        """
        m = (mode or self.default_mode).lower()
        if m not in ("auto", "post", "send"):
            m = "auto"
        if isinstance(target, int):
            hwnd = int(target)
            info = wf.get_window_info(hwnd)
            if info is None:
                return (False, "窗口已失效")
            ox, oy = wf.client_origin(hwnd)
            cw, ch = wf.client_size(hwnd)
            x, y = cw // 2, ch // 2
        else:
            hwnd, x, y = int(target[0]), int(target[1]), int(target[2])
            ox, oy = wf.client_origin(hwnd) if hwnd else (0, 0)

        if not hwnd:
            # hwnd=0 = 「绝对屏幕坐标」回放(宏的 absolute 模式):没有可投递的
            # 目标窗口,只能走真实输入,因此**只允许显式 send 模式**,并且
            # 受「禁用真实输入」总开关约束(#4)。
            if m != "send":
                return (False, "没有目标窗口:绝对坐标输入需要显式 send 模式")
            if not real_input_allowed():
                return (False, "真实输入已在设置里关闭")
            try:
                _InputHelper.click_at(x, y, button)
                if double:
                    _InputHelper.click_at(x, y, button)
                return (True, "")
            except Exception as e:
                return (False, f"send 失败:{e}")
        if not wf._is_window(hwnd):
            return (False, "目标窗口无效,已拒绝发送输入")

        if m in ("post", "auto"):
            ok = wf.post_click(hwnd, x, y, button.value, double=double)
            if ok:
                return (True, "")
            if m == "post":
                return (False, "post 返回 0(目标未响应消息,可能是独占全屏或 UAC 隔离)")

        # send 模式 / auto 回退:确认目标窗口真的在前台了才发真实输入(#4)
        if not real_input_allowed():
            return (False, "真实输入已被设置里关闭,post 未生效;请改用 post 模式或重新聚焦窗口")
        if self.require_foreground and not ensure_foreground(hwnd):
            return (
                False,
                "无法把目标窗口提到前台(前台锁定 / 提权窗口 / UAC 隔离),"
                "已放弃真实输入 —— 否则这一枪会打在您当前正在用的窗口上",
            )
        try:
            time.sleep(0.02)
            sx, sy = ox + x, oy + y
            _InputHelper.click_at(sx, sy, button)
            if double:
                _InputHelper.click_at(sx, sy, button)
            reason = "" if m == "send" else "post 失败,已改用真实输入"
            return (True, reason)
        except Exception as e:
            return (False, f"send 失败:{e}")

    def move(
        self,
        target: tuple[int, int, int],
        *,
        mode: str | None = None,
    ) -> tuple[bool, str]:
        """移动鼠标到客户区坐标;确认不了前台就不动光标(#4)."""
        hwnd, x, y = int(target[0]), int(target[1]), int(target[2])
        m = (mode or self.default_mode).lower()
        if m not in ("auto", "post", "send"):
            m = "auto"
        if m == "post":
            if not hwnd:
                return (False, "post 模式需要目标窗口")
            return (wf.post_mouse_move(hwnd, x, y), "")
        if not real_input_allowed():
            return (False, "真实输入已关闭")
        if not hwnd:
            # 绝对屏幕坐标(宏 absolute 模式):无窗口可校验,只允许显式 send
            if m != "send":
                return (False, "没有目标窗口:绝对坐标移动需要显式 send 模式")
            _InputHelper.move_abs(x, y)
            return (True, "")
        if not wf._is_window(hwnd):
            return (False, "目标窗口无效")
        if self.require_foreground and not ensure_foreground(hwnd):
            return (False, "无法确认目标窗口在前台,已放弃移动光标")
        ox, oy = wf.client_origin(hwnd)
        time.sleep(0.005)
        _InputHelper.move_abs(ox + x, oy + y)
        return (True, "")

    def key_down(self, vk: int, *, hwnd: int | None = None, mode: str | None = None) -> bool:
        m = (mode or self.default_mode).lower()
        if m == "post" and hwnd is not None:
            return wf.post_key(hwnd, vk, down=True)
        if not self._allow_real_key(hwnd, m):
            return False
        _InputHelper.key_down(vk)
        return True

    def key_up(self, vk: int, *, hwnd: int | None = None, mode: str | None = None) -> bool:
        m = (mode or self.default_mode).lower()
        if m == "post" and hwnd is not None:
            return wf.post_key(hwnd, vk, down=False)
        if not self._allow_real_key(hwnd, m):
            return False
        _InputHelper.key_up(vk)
        return True

    def key_press(self, vk: int, *, hwnd: int | None = None, hold_ms: int = 30, mode: str | None = None) -> bool:
        m = (mode or self.default_mode).lower()
        if m == "post" and hwnd is not None:
            self.key_down(vk, hwnd=hwnd, mode=m)
            time.sleep(max(0, min(60_000, hold_ms)) / 1000.0)
            self.key_up(vk, hwnd=hwnd, mode=m)
            return True
        if not self._allow_real_key(hwnd, m):
            return False
        _InputHelper.key_press(vk, hold_ms=hold_ms)
        return True

    def type_text(self, text: str, *, hwnd: int | None = None, mode: str | None = None) -> bool:
        """输入纯文本(键盘事件)."""
        m = (mode or self.default_mode).lower()
        if not self._allow_real_key(hwnd, m) and m != "post":
            return False
        if not text:
            return True
        for ch in text[:MAX_TYPE_TEXT_LEN]:
            if m == "post" and hwnd is not None:
                wf.post_key(hwnd, ord(ch.upper()) & 0xFF, down=True)
                wf.post_key(hwnd, ord(ch.upper()) & 0xFF, down=False)
            else:
                _InputHelper.key_press(ord(ch.upper()) & 0xFF)
        return len(text) <= MAX_TYPE_TEXT_LEN

    def _allow_real_key(self, hwnd: int | None, mode: str) -> bool:
        """真实按键前的三道闸:开关开着 / 有目标窗口 / 前台确认得了(#4).

        ``hwnd`` 为空(None 或 0)时旧代码会直接 SendInput —— 按键就落到用户此刻
        正在用的窗口上。现在只有显式 ``mode='send'``(宏的 absolute 回放)才允许。
        """
        if mode == "post":
            # post 模式却没有可用的 hwnd:没有投递目标,只能拒绝
            return False
        if not real_input_allowed():
            return False
        h = int(hwnd or 0)
        if not h:
            return mode == "send"
        if not wf._is_window(h):
            return False
        return (not self.require_foreground) or ensure_foreground(h)

    def scroll(self, delta: int, *, hwnd: int | None = None, mode: str | None = None) -> bool:
        """滚轮:有目标窗口时走 WM_MOUSEWHEEL 后台投递,不再抢前台发真实滚轮(#4)."""
        m = (mode or self.default_mode).lower()
        if m not in ("auto", "post", "send"):
            m = "auto"
        delta = int(max(-14400, min(14400, delta)))
        h = int(hwnd or 0)
        if h and m in ("post", "auto"):
            ox, oy = wf.client_origin(h)
            cw, ch = wf.client_size(h)
            if wf.post_scroll(h, delta, ox + cw // 2, oy + ch // 2):
                return True
            if m == "post":
                return False
        if not real_input_allowed():
            return False
        if not h:
            # 没有目标窗口的真实滚轮只接受显式 send(宏 absolute 回放)
            if m != "send":
                return False
        elif self.require_foreground and not ensure_foreground(h):
            return False
        time.sleep(0.01)
        _InputHelper.scroll(delta)
        return True