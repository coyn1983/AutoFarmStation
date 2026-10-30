"""键盘宏.

- 按键序列(step 列表)
- 每步可指定:按键 vk / hold_ms / 间隔
- 模式:'post' / 'send'
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .input_sender import InputSender, parse_vk
from .autoclicker import HIDDEN_POLL_S, MAX_STEPS, MIN_KEY_GAP_MS, clamp_int


@dataclass
class KeyStep:
    """一个按键步骤."""

    key: str = ""  # 可读名,如 'F1' / 'space'
    vk: int = 0  # 虚拟键码(由 key 自动解析)
    hold_ms: int = 50  # 按下持续时间
    delay_before_ms: int = 0  # 在这一步之前额外等待
    note: str = ""


@dataclass
class KeyMacroConfig:
    hwnd: int = 0
    steps: list[KeyStep] = field(default_factory=list)
    loop: bool = True
    round_delay_ms: int = 0  # 一轮之间所有延迟
    round_max: int = 0  # 总轮数,0=无限

    # --- 序列化 ---
    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "steps": [
                {
                    "key": s.key,
                    "vk": s.vk,
                    "hold_ms": s.hold_ms,
                    "delay_before_ms": s.delay_before_ms,
                    "note": s.note,
                }
                for s in self.steps
            ],
            "loop": self.loop,
            "round_delay_ms": self.round_delay_ms,
            "round_max": self.round_max,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeyMacroConfig":
        """载入并做边界校验(#11):步骤数量、延时、vk 取值范围都不能信外部 JSON."""
        steps = [
            KeyStep(
                key=str(raw.get("key", ""))[:64],
                vk=clamp_int(raw.get("vk", 0), 0, lo=0, hi=0xFFFF),
                hold_ms=clamp_int(raw.get("hold_ms", 50), 50, lo=0, hi=60_000),
                delay_before_ms=clamp_int(
                    raw.get("delay_before_ms", 0), 0, lo=0, hi=600_000),
                note=str(raw.get("note", ""))[:200],
            )
            for raw in (d.get("steps", []) or [])[:MAX_STEPS]
            if isinstance(raw, dict)
        ]
        return cls(
            hwnd=int(d.get("hwnd", 0) or 0),
            steps=steps,
            loop=bool(d.get("loop", True)),
            round_delay_ms=clamp_int(
                d.get("round_delay_ms", 0), 0, lo=0, hi=24 * 3600 * 1000),
            round_max=clamp_int(d.get("round_max", 0), 0, lo=0, hi=10 ** 9),
        )


class KeyMacro:
    """键盘宏线程."""

    def __init__(
        self,
        config: KeyMacroConfig,
        sender: InputSender | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        # 解析 vk
        for s in config.steps:
            if s.key and not s.vk:
                try:
                    s.vk = parse_vk(s.key)
                except Exception:
                    s.vk = 0
        self._sender = sender or InputSender()
        self._log = logger or logging.getLogger("autofarmstation.keymacro")
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()
        self._lock = threading.Lock()
        self._rounds = 0
        self._keys = 0
        self._started_at = 0.0
        self._window_hidden = False  # 目标窗口暂时不可见(隐藏/最小化)中(#8)
        self.on_start: Callable[[], None] | None = None
        self.on_stop: Callable[[], None] | None = None
        self.on_step: Callable[[int, KeyStep], None] | None = None  # (round, step)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._pause.is_set()

    @property
    def runtime_seconds(self) -> float:
        if not self._started_at:
            return 0.0
        return time.time() - self._started_at

    @property
    def rounds(self) -> int:
        """已完成的轮数."""
        with self._lock:
            return self._rounds

    @property
    def keys(self) -> int:
        """已发送的按键次数."""
        with self._lock:
            return self._keys

    @property
    def window_hidden(self) -> bool:
        """目标窗口当前被隐藏/最小化,宏在原地等待(不是故障,#8)."""
        with self._lock:
            return self._window_hidden

    def start(self) -> bool:
        if self.is_running:
            return False
        if not self.config.hwnd or not self.config.steps:
            self._log.warning("键盘宏缺少目标窗口或步骤")
            return False
        # 至少要有一个有效 vk
        if not any(s.vk for s in self.config.steps):
            self._log.warning("键盘宏所有步骤都无有效 vk")
            return False
        self._stop.clear()
        self._pause.set()
        self._rounds = 0
        self._keys = 0
        self._started_at = time.time()
        self._window_hidden = False
        self._thread = threading.Thread(
            target=self._run, name=f"KeyMacro-{self.config.hwnd}", daemon=True
        )
        self._thread.start()
        if self.on_start:
            try:
                self.on_start()
            except Exception as e:  # noqa: BLE001
                self._log.exception("on_start: %s", e)
        return True

    def stop(self, join_timeout: float = 1.0) -> bool:
        if not self.is_running:
            return False
        self._stop.set()
        self._pause.set()
        if self._thread:
            self._thread.join(timeout=max(0.0, float(join_timeout)))
        self._thread = None
        if self.on_stop:
            try:
                self.on_stop()
            except Exception as e:  # noqa: BLE001
                self._log.exception("on_stop: %s", e)
        return True

    def pause(self, paused: bool = True) -> None:
        if paused:
            self._pause.clear()
        else:
            self._pause.set()

    def _sleep(self, ms: int) -> None:
        # 兜一个最小间隔:全 0 延时的宏在 loop=True 下会打成输入风暴(#11)
        if self._stop.is_set():
            return
        end = time.time() + max(MIN_KEY_GAP_MS, int(ms)) / 1000.0
        while time.time() < end:
            if self._stop.is_set():
                return
            if not self._pause.is_set():
                self._pause.wait(timeout=0.05)
                continue
            time.sleep(min(0.05, end - time.time()))

    def _run(self) -> None:
        round_idx = 0
        try:
            while not self._stop.is_set():
                while not self._pause.is_set() and not self._stop.is_set():
                    self._pause.wait(timeout=0.1)

                # 检查窗口
                if self._stop.is_set():
                    return
                from . import window_finder as wf
                info = wf.get_window_info(self.config.hwnd)
                if info is None:
                    self._log.info("窗口已失效,键盘宏自动停止 (hwnd=%s)", self.config.hwnd)
                    return
                if not info.visible or info.is_minimized:
                    # 隐藏 / 最小化只等待,不结束线程(与连点器一致,#8)
                    with self._lock:
                        first_time = not self._window_hidden
                        self._window_hidden = True
                    if first_time:
                        self._log.info(
                            "窗口不可见(隐藏/最小化),键盘宏等待中 (hwnd=%s)",
                            self.config.hwnd,
                        )
                    self._stop.wait(HIDDEN_POLL_S)
                    continue
                with self._lock:
                    was_hidden = self._window_hidden
                    self._window_hidden = False
                if was_hidden:
                    self._log.info("窗口重新可见,键盘宏继续 (hwnd=%s)", self.config.hwnd)

                if self.config.round_max and round_idx >= self.config.round_max:
                    return

                for s in self.config.steps:
                    if self._stop.is_set():
                        return
                    if not s.vk:
                        continue
                    # 每步都走一次 _sleep(其内有最小间隔兜底):
                    # 旧写法 `if s.delay_before_ms:` 让全 0 延时的宏在一轮里
                    # 空转打键,#11 的合成输入风暴就是这么来的。
                    self._sleep(s.delay_before_ms)
                    try:
                        self._sender.key_press(s.vk, hwnd=self.config.hwnd, hold_ms=s.hold_ms)
                    except Exception as e:  # noqa: BLE001
                        self._log.warning("按键失败 %s: %s", s.key, e)
                    with self._lock:
                        self._keys += 1
                    if self.on_step:
                        try:
                            self.on_step(round_idx, s)
                        except Exception as e:  # noqa: BLE001
                            self._log.exception("on_step: %s", e)

                round_idx += 1
                with self._lock:
                    self._rounds = round_idx

                # 无条件走一次轮间等待(内部有最小间隔):round_delay_ms=0 且
                # 本轮没发出任何按键(步骤全无效)时,旧写法会 100% CPU 空转(#11)
                self._sleep(self.config.round_delay_ms)

                if not self.config.loop and round_idx >= 1:
                    return
        finally:
            self._log.debug("键盘宏线程退出")