"""统计.

- 总运行时长
- 总点击次数
- 总按键次数
- 累计日期

数据持久化在 stats.json,与 config.json 分离.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..utils.paths import atomic_write, stats_path


@dataclass
class Stats:
    """统计快照."""

    total_runtime_ms: int = 0
    total_clicks: int = 0
    total_keys: int = 0
    first_run_iso: str = ""
    last_run_iso: str = ""
    run_count: int = 0
    # 每进程维度
    per_process: dict[str, dict] = field(default_factory=dict)


class Statistics:
    """线程安全的统计管理器."""

    # per_process 以 hwnd 为键,而 hwnd 每开一次游戏就是一个新值:不回收的话
    # stats.json 会随挂机周数单调增长(#15)。
    MAX_PER_PROCESS = 256
    PER_PROCESS_MAX_AGE_SEC = 30 * 24 * 3600

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path or stats_path()
        self._data = Stats()
        self._start_time = time.time()
        self.load()

    # --- 加载 ---
    def load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._data = Stats()
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                self._data = Stats()
                return
            try:
                self._data = Stats(
                    total_runtime_ms=int(raw.get("total_runtime_ms", 0)),
                    total_clicks=int(raw.get("total_clicks", 0)),
                    total_keys=int(raw.get("total_keys", 0)),
                    first_run_iso=str(raw.get("first_run_iso", "")),
                    last_run_iso=str(raw.get("last_run_iso", "")),
                    run_count=int(raw.get("run_count", 0)),
                    per_process=dict(raw.get("per_process", {}) or {}),
                )
            except Exception:
                self._data = Stats()

    def save(self) -> None:
        with self._lock:
            self._prune_per_process()
            atomic_write(self._path, json.dumps(asdict(self._data), indent=2, ensure_ascii=False))

    # --- 每进程记录的回收(#15)---
    def _bump(self, hwnd: int) -> dict:
        k = str(int(hwnd))
        rec = self._data.per_process.setdefault(
            k, {"clicks": 0, "keys": 0, "runtime_ms": 0, "last_seen": 0.0})
        rec["last_seen"] = time.time()
        return rec

    def forget(self, hwnd: int) -> None:
        """窗口被移除时清掉它的每进程统计."""
        with self._lock:
            self._data.per_process.pop(str(int(hwnd)), None)

    def _prune_per_process(self) -> None:
        """丢掉长期没再出现过的窗口记录,并给总量封顶."""
        now = time.time()
        items = self._data.per_process
        stale = [
            k for k, v in items.items()
            if now - float(v.get("last_seen", 0.0) or 0.0) > self.PER_PROCESS_MAX_AGE_SEC
        ]
        for k in stale:
            items.pop(k, None)
        if len(items) <= self.MAX_PER_PROCESS:
            return
        # 还超上限:按最近使用时间保留前 MAX_PER_PROCESS 个
        ordered = sorted(
            items.items(),
            key=lambda kv: float(kv[1].get("last_seen", 0.0) or 0.0),
            reverse=True,
        )
        keep = dict(ordered[: self.MAX_PER_PROCESS])
        self._data.per_process = keep

    # --- 累加 ---
    def on_session_start(self) -> None:
        with self._lock:
            self._data.run_count += 1
            self._data.last_run_iso = time.strftime("%Y-%m-%d %H:%M:%S")
            if not self._data.first_run_iso:
                self._data.first_run_iso = self._data.last_run_iso

    def on_session_end(self) -> None:
        with self._lock:
            dur = int((time.time() - self._start_time) * 1000)
            self._data.total_runtime_ms += dur

    def add_click(self, count: int = 1, hwnd: int = 0) -> None:
        with self._lock:
            self._data.total_clicks += max(0, count)
            if hwnd:
                rec = self._bump(hwnd)
                rec["clicks"] = rec.get("clicks", 0) + max(0, count)

    def add_key(self, count: int = 1, hwnd: int = 0) -> None:
        with self._lock:
            self._data.total_keys += max(0, count)
            if hwnd:
                rec = self._bump(hwnd)
                rec["keys"] = rec.get("keys", 0) + max(0, count)

    def add_runtime(self, ms: int, hwnd: int = 0) -> None:
        with self._lock:
            self._data.total_runtime_ms += max(0, ms)
            if hwnd:
                rec = self._bump(hwnd)
                rec["runtime_ms"] = rec.get("runtime_ms", 0) + max(0, ms)

    # --- 访问 ---
    def snapshot(self) -> Stats:
        with self._lock:
            return Stats(
                total_runtime_ms=self._data.total_runtime_ms,
                total_clicks=self._data.total_clicks,
                total_keys=self._data.total_keys,
                first_run_iso=self._data.first_run_iso,
                last_run_iso=self._data.last_run_iso,
                run_count=self._data.run_count,
                per_process=dict(self._data.per_process),
            )