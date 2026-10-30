"""宏录制与回放.

录制:pynput 监听鼠标 + 键盘事件,转成 MacroEvent 列表。
回放:在指定 hwnd 上按时间序列回放。

注意:录制的坐标是屏幕绝对坐标;回放时可选择
- 'absolute':按原始屏幕坐标(用 send 模式)
- 'relative' :相对当前窗口客户区偏移(用 post 模式)

隐私边界(审计报告 #2):pynput 的监听器是**系统级全局钩子**,一旦开录就会
把用户在浏览器 / 密码管理器 / 聊天软件里的每一次按键都收进来。因此录制器
默认只在「目标窗口在前台」时采集事件,并且有时长上限和事件数上限 —— 到点
自动停,不依赖用户记得按停止。
"""

from __future__ import annotations

import enum
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .input_sender import InputSender, parse_vk

# === 录制与回放的安全边界(#2 / #11) ===
MAX_RECORD_SECONDS = 120.0      # 单次录制最长时长,到点自动停止
MAX_RECORD_EVENTS = 20_000      # 单次录制的事件数上限
MAX_EVENTS_PER_SCRIPT = 20_000  # 载入宏 JSON 时保留的事件数上限
MOVE_SAMPLE_MS = 50             # 鼠标移动降采样:最快每 50ms 记一个点
MIN_EVENT_GAP_S = 0.005         # 回放时相邻事件的最小间隔(200 事件/秒封顶)
MAX_T_MS = 24 * 3600 * 1000     # 事件时刻上限(24h)
MAX_COORD = 100_000             # 屏幕坐标绝对值上限
MAX_STRING_LEN = 200            # name/description/note 的长度上限


class MacroEventType(str, enum.Enum):
    MOUSE_MOVE = "mouse_move"
    MOUSE_CLICK = "mouse_click"
    MOUSE_SCROLL = "mouse_scroll"
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    DELAY = "delay"  # 显式延时


@dataclass
class MacroEvent:
    type: str  # MacroEventType 值
    t_ms: int = 0  # 相对录制开始的偏移(毫秒)
    x: int = 0  # 鼠标 x(屏幕绝对坐标)
    y: int = 0
    button: str = ""  # left/right/middle
    vk: int = 0  # 虚拟键码
    scroll: int = 0
    note: str = ""


def _clean_int(raw, default: int, lo: int, hi: int) -> int:
    """把外部 JSON 的整数夹到 [lo, hi];无法解析时用默认值."""
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return int(default)
    return max(lo, min(hi, v))


def _clean_text(raw, default: str) -> str:
    if raw is None:
        return default
    return str(raw)[:MAX_STRING_LEN]


def _clean_event(raw) -> MacroEvent | None:
    """校验并清洗一条事件;结构不对 / 类型不认识 → None(丢弃)."""
    if not isinstance(raw, dict):
        return None
    try:
        etype = MacroEventType(str(raw.get("type", ""))).value
    except ValueError:
        return None
    return MacroEvent(
        type=etype,
        t_ms=_clean_int(raw.get("t_ms", 0), 0, 0, MAX_T_MS),
        x=_clean_int(raw.get("x", 0), 0, -MAX_COORD, MAX_COORD),
        y=_clean_int(raw.get("y", 0), 0, -MAX_COORD, MAX_COORD),
        button=_clean_text(raw.get("button", ""), "") if raw.get("button") else "",
        vk=_clean_int(raw.get("vk", 0), 0, 0, 0xFFFF),
        scroll=_clean_int(raw.get("scroll", 0), 0, -120, 120),
        note=_clean_text(raw.get("note", ""), ""),
    )


@dataclass
class MacroScript:
    """一个宏脚本."""

    name: str = "未命名宏"
    description: str = ""
    created_at: str = ""
    duration_ms: int = 0
    events: list[MacroEvent] = field(default_factory=list)
    loop: bool = False
    # 录制时目标窗口的客户区尺寸。回放时按「当前尺寸 / 基准尺寸」等比缩放
    # 鼠标偏移量,这样窗口被聚焦铺满或手动缩放后,宏依然打在同一个位置。
    # 0 表示没有基准(按原样播放,和旧版本行为一致)。
    base_w: int = 0
    base_h: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str, *, max_bytes: int = 8 * 1024 * 1024) -> "MacroScript":
        """从磁盘文本载入并做边界校验(#11).

        宏文件是用户可任意编辑的 JSON:`events` 不设上限时一个几百 MB 的文件
        会把内存吃光,`t_ms` 为 0/负数配合 loop 回放就是无停顿的输入风暴。
        """
        if max_bytes and len(text.encode("utf-8", "ignore")) > max_bytes:
            raise ValueError(f"宏文件过大(> {max_bytes // 1024 // 1024} MB),已拒绝载入")
        d = json.loads(text)
        if not isinstance(d, dict):
            raise ValueError("宏文件格式不正确:根节点应为对象")
        evs: list[MacroEvent] = []
        dropped = 0
        for raw in (d.get("events", []) or [])[:MAX_EVENTS_PER_SCRIPT]:
            ev = _clean_event(raw)
            if ev is None:
                dropped += 1
                continue
            evs.append(ev)
        if dropped:
            logging.getLogger("autofarmstation.macro").warning(
                "宏 %s 有 %d 条事件非法,已丢弃", str(d.get("name", ""))[:60], dropped)
        return cls(
            name=_clean_text(d.get("name", "未命名宏"), "未命名宏"),
            description=_clean_text(d.get("description", ""), ""),
            created_at=_clean_text(d.get("created_at", ""), ""),
            duration_ms=_clean_int(d.get("duration_ms", 0), 0, 0, MAX_T_MS),
            events=evs,
            loop=bool(d.get("loop", False)),
            base_w=_clean_int(d.get("base_w", 0), 0, 0, MAX_COORD),
            base_h=_clean_int(d.get("base_h", 0), 0, 0, MAX_COORD),
        )


class MacroRecorder:
    """基于 pynput 的录制器.

    注:在 PyInstaller 打包后,pynput 的 keyboard hook 依赖
    `pyHook` 或系统级钩子,部分杀软会拦截。代码用 try-import 隔离,
    失败时给出明确错误。
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("autofarmstation.recorder")
        self._listeners = []
        self._mouse_listener = None
        self._key_listener = None
        self._recording = False
        self._start_ts = 0.0
        self._events: list[MacroEvent] = []
        self._lock = threading.Lock()
        # 隐私/资源限制(#2):目标窗口在前台才采集;超时 / 超量自动停
        self._target_hwnd = 0
        self._max_seconds = MAX_RECORD_SECONDS
        self._max_events = MAX_RECORD_EVENTS
        self._last_move_ms = -10_000.0
        self._foreground_of = None
        self._key_events = 0
        self._dropped_moves = 0
        self._limit_reason = ""

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def key_event_count(self) -> int:
        """本次录制采到的按键数(用于 UI 明示隐私影响)."""
        with self._lock:
            return self._key_events

    @property
    def limit_reason(self) -> str:
        """自动停止的原因('' 表示是用户手动停的)."""
        return self._limit_reason

    @property
    def elapsed_sec(self) -> float:
        if not self._recording:
            return 0.0
        return max(0.0, time.time() - self._start_ts)

    def remaining_sec(self) -> float:
        """距离自动停止还剩多少秒(未录制时返回 0)."""
        if not self._recording:
            return 0.0
        return max(0.0, self._max_seconds - (time.time() - self._start_ts))

    def start(
        self,
        target_hwnd: int = 0,
        *,
        max_seconds: float = MAX_RECORD_SECONDS,
        max_events: int = MAX_RECORD_EVENTS,
    ) -> bool:
        """开始录制.

        ``target_hwnd`` 非 0 时(推荐)只采集「该窗口在前台」期间的输入,
        用户在其它程序里的按键不会被记录;为 0 时是全系统采集,调用方
        **必须**向用户明示这一点。
        """
        if self._recording:
            return False
        try:
            from pynput import keyboard, mouse  # type: ignore
        except Exception as e:  # noqa: BLE001
            self._log.error("pynput 不可用,录制失败: %s", e)
            return False
        try:
            from . import window_finder as wf
            self._foreground_of = wf.get_foreground_hwnd
        except Exception:  # noqa: BLE001
            self._foreground_of = None

        self._events = []
        self._key_events = 0
        self._dropped_moves = 0
        self._limit_reason = ""
        self._start_ts = time.time()
        self._last_move_ms = -10_000.0
        self._target_hwnd = int(target_hwnd or 0)
        self._max_seconds = max(5.0, float(max_seconds))
        self._max_events = max(16, int(max_events))
        self._recording = True

        def on_move(x, y):
            if not self._gate():
                return
            # 降采样:移动事件量最大,50ms 一个点足够还原轨迹(#2)
            now_ms = (time.time() - self._start_ts) * 1000.0
            if now_ms - self._last_move_ms < MOVE_SAMPLE_MS:
                self._dropped_moves += 1
                return
            self._last_move_ms = now_ms
            self._record(MacroEvent(
                type=MacroEventType.MOUSE_MOVE.value,
                t_ms=int(now_ms), x=int(x), y=int(y),
            ))

        def on_click(x, y, button, pressed):
            if not pressed or not self._gate():
                return
            from pynput.mouse import Button  # type: ignore
            name = {Button.left: "left", Button.right: "right", Button.middle: "middle"}.get(
                button, "left"
            )
            self._record(MacroEvent(
                type=MacroEventType.MOUSE_CLICK.value,
                t_ms=int((time.time() - self._start_ts) * 1000),
                x=int(x), y=int(y),
                button=name,
            ))

        def on_scroll(x, y, dx, dy):
            if not self._gate():
                return
            self._record(MacroEvent(
                type=MacroEventType.MOUSE_SCROLL.value,
                t_ms=int((time.time() - self._start_ts) * 1000),
                x=int(x), y=int(y),
                scroll=int(dy or dx),
            ))

        def on_key_press(k):
            if not self._recording:
                return False
            if not self._gate():
                return  # 非目标窗口前台:忽略但不退出监听器
            vk = self._vk_from_key(k)
            self._record(MacroEvent(
                type=MacroEventType.KEY_DOWN.value,
                t_ms=int((time.time() - self._start_ts) * 1000),
                vk=vk,
            ), is_key=True)

        def on_key_release(k):
            if not self._recording:
                return False
            if not self._gate():
                return
            vk = self._vk_from_key(k)
            self._record(MacroEvent(
                type=MacroEventType.KEY_UP.value,
                t_ms=int((time.time() - self._start_ts) * 1000),
                vk=vk,
            ), is_key=True)

        try:
            self._mouse_listener = mouse.Listener(  # type: ignore
                on_move=on_move, on_click=on_click, on_scroll=on_scroll
            )
            self._key_listener = keyboard.Listener(  # type: ignore
                on_press=on_key_press, on_release=on_key_release
            )
            self._mouse_listener.start()
            self._key_listener.start()
            if self._target_hwnd:
                self._log.info(
                    "开始录制(仅目标窗口前台期间,最长 %.0fs / 最多 %d 事件) hwnd=%s",
                    self._max_seconds, self._max_events, self._target_hwnd)
            else:
                self._log.warning(
                    "开始录制(未指定目标窗口 → 采集全系统输入,最长 %.0fs)", self._max_seconds)
            return True
        except Exception as e:  # noqa: BLE001
            self._log.error("启动监听器失败: %s", e)
            self._recording = False
            return False

    # --- 采集门控与落账 ---
    def _gate(self) -> bool:
        """此刻是否应该采集输入(#2 的核心开关).

        - 时长超限 → 自动停止
        - 指定了目标窗口 → 只有它在前台时才采(否则就是在录键盘记录器)
        """
        if not self._recording:
            return False
        if time.time() - self._start_ts > self._max_seconds:
            self._auto_stop("达到录制时长上限")
            return False
        if self._target_hwnd and self._foreground_of is not None:
            try:
                if int(self._foreground_of() or 0) != self._target_hwnd:
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def _record(self, ev: MacroEvent, *, is_key: bool = False) -> None:
        with self._lock:
            if len(self._events) >= self._max_events:
                over = True
            else:
                over = False
                self._events.append(ev)
                if is_key:
                    self._key_events += 1
        if over:
            self._auto_stop("事件数达到上限")

    def _auto_stop(self, reason: str) -> None:
        """到上限:停止采集,但不在钩子线程里 stop 监听器(pynput 的坑),
        交给后台线程拆;脚本留在内存里等 UI 用 take_script() 取走(#2)。
        """
        if not self._recording:
            return
        self._recording = False
        self._limit_reason = reason
        self._log.warning("录制自动停止:%s", reason)
        threading.Thread(
            target=self._stop_listeners, name="RecorderStop", daemon=True
        ).start()

    def _stop_listeners(self) -> None:
        for lst in (self._mouse_listener, self._key_listener):
            try:
                if lst:
                    lst.stop()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _vk_from_key(k) -> int:
        try:
            from pynput.keyboard import Key, KeyCode  # type: ignore
        except Exception:
            return 0
        if isinstance(k, KeyCode):
            return int(k.vk or 0) or (ord(k.char.upper()) if k.char and len(k.char) == 1 else 0)
        # 特殊键映射
        special = {
            Key.space: 0x20, Key.enter: 0x0D, Key.tab: 0x09, Key.esc: 0x1B,
            Key.backspace: 0x08, Key.delete: 0x2E, Key.insert: 0x2D,
            Key.home: 0x24, Key.end: 0x23, Key.page_up: 0x21, Key.page_down: 0x22,
            Key.up: 0x26, Key.down: 0x28, Key.left: 0x25, Key.right: 0x27,
            Key.shift: 0x10, Key.shift_l: 0xA0, Key.shift_r: 0xA1,
            Key.ctrl: 0x11, Key.ctrl_l: 0xA2, Key.ctrl_r: 0xA3,
            Key.alt: 0x12, Key.alt_l: 0xA4, Key.alt_r: 0xA5,
            Key.caps_lock: 0x14,
            Key.f1: 0x70, Key.f2: 0x71, Key.f3: 0x72, Key.f4: 0x73,
            Key.f5: 0x74, Key.f6: 0x75, Key.f7: 0x76, Key.f8: 0x77,
            Key.f9: 0x78, Key.f10: 0x79, Key.f11: 0x7A, Key.f12: 0x7B,
        }
        return int(special.get(k, 0))

    def stop(self) -> MacroScript | None:
        """结束录制并交出脚本.

        自动停止(超时长 / 超事件数)之后仍然可以调用本方法(或 take_script)
        把已经采到的事件取走 —— 旧实现这时返回 None,事件被静默丢弃。
        """
        if not self._recording and not self._events:
            return None
        self._recording = False
        self._stop_listeners()
        self._mouse_listener = None
        self._key_listener = None
        with self._lock:
            events = sorted(self._events, key=lambda e: e.t_ms)
            self._events = []
        if not events:
            return None
        # 录制时长:以最后一条事件为准,再夹到上限内,避免 _start_ts 异常时
        # 算出跨天的 t_ms。
        duration = max(events[-1].t_ms, 0)
        duration = min(duration, int(self._max_seconds * 1000))
        return MacroScript(
            name=f"宏-{time.strftime('%Y%m%d-%H%M%S')}",
            duration_ms=duration,
            events=events,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    def take_script(self) -> MacroScript | None:
        """录制已被自动停止时,由 UI 取走结果(等价于 stop)."""
        return self.stop()


class MacroPlayer:
    """宏回放器.

    target_mode:
    - 'absolute':用 send 模式按原始屏幕坐标播放(对前台窗口,需要前台)
    - 'window'  :用 post 模式,坐标变换为当前目标 hwnd 客户区坐标
    """

    def __init__(
        self,
        script: MacroScript,
        hwnd: int,
        *,
        target_mode: str = "window",
        speed: float = 1.0,
        loop: bool = False,
        sender: InputSender | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.script = script
        self.hwnd = hwnd
        self.target_mode = target_mode
        self.speed = max(0.05, min(10.0, speed))
        self.loop = loop
        self._sender = sender or InputSender(default_mode="post" if target_mode == "window" else "send")
        self._log = logger or logging.getLogger("autofarmstation.macroplayer")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()
        self._started_at = 0.0
        self.on_finish: Callable[[], None] | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._pause.is_set()

    def start(self) -> bool:
        if self.is_running:
            return False
        if not self.script.events:
            return False
        self._stop.clear()
        self._pause.set()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name=f"MacroPlayer-{self.hwnd}", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not self.is_running:
            return False
        self._stop.set()
        self._pause.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        return True

    def pause(self, paused: bool = True) -> None:
        if paused:
            self._pause.clear()
        else:
            self._pause.set()

    def _sleep_until(self, deadline: float) -> None:
        while time.time() < deadline:
            if self._stop.is_set():
                return
            if not self._pause.is_set():
                self._pause.wait(timeout=0.05)
                continue
            time.sleep(min(0.05, deadline - time.time()))

    def _run(self) -> None:
        from . import window_finder as wf
        # 录制时的初始屏幕位置 → 当前 hwnd 客户区偏移
        sx, sy = 1.0, 1.0
        if self.target_mode == "window":
            base_event = next(
                (e for e in self.script.events
                 if e.type in (MacroEventType.MOUSE_CLICK.value, MacroEventType.MOUSE_MOVE.value)),
                None,
            )
            ox, oy = wf.client_origin(self.hwnd) if self.hwnd else (0, 0)
            base_x, base_y = (base_event.x, base_event.y) if base_event else (0, 0)
            dx, dy = ox - base_x, oy - base_y
            # 录的时候窗口是多大,现在又是多大 —— 等比缩放偏移量,
            # 否则窗口一被聚焦铺满,宏就整体打偏。
            bw, bh = int(self.script.base_w or 0), int(self.script.base_h or 0)
            if bw > 0 and bh > 0 and self.hwnd:
                cw, ch = wf.client_size(self.hwnd)
                if cw > 0 and ch > 0:
                    sx, sy = cw / float(bw), ch / float(bh)
        else:
            dx, dy = 0, 0
            base_x = base_y = 0

        def _map(ex: int, ey: int) -> tuple[int, int]:
            """把录制坐标映射到当前窗口的客户区."""
            if self.target_mode == "window":
                return (int(round(dx + (ex - base_x) * sx)),
                        int(round(dy + (ey - base_y) * sy)))
            return (ex, ey)

        try:
            # 事件时刻必须单调不减,并给相邻事件留一个最小间隔:t_ms 全 0
            # (或被外部改坏)时,loop 回放会退化成无停顿的输入风暴(#11)。
            schedule: list[float] = []
            prev_s = -MIN_EVENT_GAP_S
            for ev in self.script.events:
                when = max(prev_s + MIN_EVENT_GAP_S, max(0, int(ev.t_ms)) / 1000.0)
                schedule.append(when / self.speed)
                prev_s = when
            while not self._stop.is_set():
                if self._stop.is_set():
                    return
                if not wf._is_window(self.hwnd):  # type: ignore[attr-defined]
                    self._log.warning("目标窗口已失效")
                    return
                t0 = time.time()
                for ev, when_s in zip(self.script.events, schedule):
                    if self._stop.is_set():
                        return
                    # 计算该事件的实际 deadline
                    self._sleep_until(t0 + when_s)
                    try:
                        if ev.type == MacroEventType.MOUSE_MOVE.value:
                            mx, my = _map(ev.x, ev.y)
                            ok_move, why_move = self._sender.move((self.hwnd, mx, my))
                            if not ok_move:
                                self._log.warning("移动失败:%s", why_move or "未知原因")
                        elif ev.type == MacroEventType.MOUSE_CLICK.value:
                            from .input_sender import MouseButton
                            btn = MouseButton(ev.button or "left")
                            mx, my = _map(ev.x, ev.y)
                            self._sender.click((self.hwnd, mx, my), btn)
                        elif ev.type == MacroEventType.MOUSE_SCROLL.value:
                            self._sender.scroll(ev.scroll, hwnd=self.hwnd)
                        elif ev.type == MacroEventType.KEY_DOWN.value:
                            self._sender.key_down(ev.vk, hwnd=self.hwnd)
                        elif ev.type == MacroEventType.KEY_UP.value:
                            self._sender.key_up(ev.vk, hwnd=self.hwnd)
                    except Exception as e:  # noqa: BLE001
                        self._log.warning("回放事件失败: %s", e)
                if not self.loop:
                    break
        finally:
            self._log.debug("宏回放线程退出")
            if self.on_finish:
                try:
                    self.on_finish()
                except Exception:
                    pass