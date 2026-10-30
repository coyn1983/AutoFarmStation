"""代码风险审计报告(#1–#15)对应的回归用例.

审计报告指出,原来的 46 项自检集中在纯逻辑 / 序列化 / 版本比较上,恰好没覆盖
「跨线程回调、closeEvent 顺序、线程停止与 join、句柄与内存增长、外部输入边界」
这几类真正会出事的点。本文件把这些盲区补上,由 `scripts.selftest.run_all()`
一并调用。

约定与 selftest.py 一致:每项独立 try/except,返回 bool,失败不影响后续项。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from .selftest import _div, _ok, _fail, _skip

REPO_DIR = Path(__file__).resolve().parents[1]


def t_audit_cross_thread_delivery() -> bool:
    """#1:工作线程 → 主线程必须靠 Signal;要真起事件循环才测得出来."""
    _div("审计 #1:跨线程投递(真起事件循环)")
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        import threading
        from PySide6.QtCore import QTimer, QEventLoop
        from PySide6.QtWidgets import QApplication
        from autofarmstation.core.preview_capture import PreviewFrame
        from autofarmstation.ui.preview_widget import PreviewWidget

        app = QApplication.instance() or QApplication([])
        w = PreviewWidget(1234567, fps=1.0)
        w._capture.stop()  # 不真的抓帧,只验证投递通道
        got: list = []
        w._render_frame = lambda frame: got.append(frame)  # type: ignore[assignment]

        frame = PreviewFrame(hwnd=1234567, width=2, height=2, bytes_per_line=8,
                             pixels=b"\0" * 16, captured_at=time.time())
        old_style: list = []

        def _worker() -> None:
            # 旧写法:Python 线程里没有 Qt 事件循环 → 这一句永不触发
            QTimer.singleShot(0, lambda: old_style.append("fired"))
            w._on_frame(frame)

        th = threading.Thread(target=_worker)
        th.start()
        loop = QEventLoop()
        QTimer.singleShot(700, loop.quit)
        loop.exec()
        th.join(timeout=1.0)

        assert len(got) == 1, f"Signal 投递未生效(收到 {len(got)} 帧)"
        assert not old_style, "singleShot 从工作线程竟然触发了(与实测不符)"
        with w._frame_lock:
            assert w._pending_frame is None, "帧取走后应清空,不能长期持有缓冲"
        w.deleteLater()
        app.processEvents()
        _ok("#1 Signal 投递生效;工作线程的 singleShot 确认不触发;待渲染帧已清空")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_cross_thread_delivery", e)
        return False


def t_audit_hidden_window_keeps_running() -> bool:
    """#8:窗口隐藏 / 最小化时连点器只等待,不再静默停止."""
    _div("审计 #8:窗口隐藏时连点器等待而非停止")
    try:
        from autofarmstation.core import window_finder as wf
        from autofarmstation.core.autoclicker import (
            AutoClicker, AutoClickerConfig, ClickPoint)

        state = {"visible": False}

        class _Info:
            pid = 1
            title = "fake"
            rect = (0, 0, 100, 100)

            @property
            def visible(self) -> bool:
                return state["visible"]

            @property
            def is_minimized(self) -> bool:
                return not state["visible"]

        class _Sender:
            def __init__(self) -> None:
                self.clicks = 0

            def click(self, target, button, *, double=False):
                self.clicks += 1
                return (True, "")

        real_info, real_size = wf.get_window_info, wf.client_size
        sender = _Sender()
        try:
            wf.get_window_info = lambda hwnd: _Info()
            wf.client_size = lambda hwnd: (100, 100)
            cfg = AutoClickerConfig(hwnd=12345, points=[ClickPoint(10, 10)],
                                    interval_ms=20, loop=True)
            c = AutoClicker(cfg, sender=sender)
            assert c.start(), "连点器没能启动"
            time.sleep(0.4)
            assert c.is_running, "窗口隐藏时线程不该结束"
            assert c.window_hidden, "应标记为「等待窗口」而不是悄悄停掉"
            assert sender.clicks == 0, "隐藏期间不该发点击"
            state["visible"] = True
            deadline = time.time() + 2.0
            while sender.clicks == 0 and time.time() < deadline:
                time.sleep(0.05)
            assert sender.clicks > 0, "窗口回来后应继续点击"
            assert not c.window_hidden
            c.stop()
        finally:
            wf.get_window_info, wf.client_size = real_info, real_size
        _ok("#8 隐藏→等待→可见→继续点击,线程全程未退出")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_hidden_window_keeps_running", e)
        return False


def t_audit_macro_recorder_privacy() -> bool:
    """#2:录制只在目标窗口前台时采集 + 时长/事件数上限;#11:宏文件边界."""
    _div("审计 #2/#11:宏录制隐私边界与宏文件边界")
    try:
        from autofarmstation.core.macro_recorder import (
            MacroRecorder, MacroScript, MacroEvent, MacroEventType,
            MAX_RECORD_SECONDS)

        r = MacroRecorder()
        r._recording = True
        r._start_ts = time.time()
        r._target_hwnd = 111
        r._foreground_of = lambda: 222
        assert r._gate() is False, "目标窗口不在前台时不该采集"
        r._foreground_of = lambda: 111
        assert r._gate() is True
        r._start_ts = time.time() - MAX_RECORD_SECONDS - 5
        assert r._gate() is False and r._recording is False, "超时长应自动停"
        assert r.limit_reason

        r2 = MacroRecorder()
        r2._recording = True
        r2._start_ts = time.time()
        r2._max_events = 3
        for i in range(10):
            r2._record(MacroEvent(type=MacroEventType.MOUSE_CLICK.value, t_ms=i * 100))
        assert r2.event_count == 3
        assert r2._recording is False and "事件数" in r2.limit_reason
        script = r2.take_script()  # 自动停止后结果仍能取走(旧写法会直接丢掉)
        assert script is not None and len(script.events) == 3
        r2._stop_listeners()

        # 宏文件边界:未知事件类型丢弃、t_ms / 坐标夹住、超长名字截断、体积上限
        payload = json.dumps({"name": "x" * 500, "events": [
            {"type": "key_down", "t_ms": -100, "vk": 99999},
            {"type": "bogus_type"},
            {"type": "mouse_click", "t_ms": 10, "x": 10 ** 12, "y": 2, "button": "left"},
        ] * 300})
        s = MacroScript.from_json(payload)
        assert all(e.type != "bogus_type" for e in s.events)
        assert all(e.t_ms >= 0 for e in s.events)
        assert all(abs(e.x) <= 100_000 for e in s.events)
        assert len(s.name) <= 200
        rejected = False
        try:
            MacroScript.from_json(json.dumps({"events": [{"type": "delay"}] * 200}),
                                  max_bytes=64)
        except ValueError:
            rejected = True
        assert rejected, "超过体积上限的宏文件应被拒绝"
        _ok("#2 前台过滤 + 时长/事件数自动停 + 停止后仍可取结果;#11 宏文件边界")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_macro_recorder_privacy", e)
        return False


def t_audit_input_and_value_bounds() -> bool:
    """#4:真实输入的门禁;#11:连点 / 键盘宏 / 预设的数值边界."""
    _div("审计 #4/#11:输入门禁与数值边界")
    try:
        from autofarmstation.core.input_sender import InputSender, real_input_allowed
        from autofarmstation.core.autoclicker import (
            AutoClickerConfig, MAX_POINTS, MIN_INTERVAL_MS)
        from autofarmstation.core.key_macro import KeyMacroConfig
        from autofarmstation.core.preset_library import (
            autoclicker_dict_to_points, key_macro_dict_to_steps)
        from autofarmstation.utils.config import instance as cfg_instance

        s = InputSender(default_mode="auto")
        # 没有目标 hwnd 的按键:绝不能打到用户此刻的前台窗口
        assert s.key_down(0x41, hwnd=None) is False
        assert s.key_up(0x41, hwnd=None) is False
        assert s.key_press(0x41, hwnd=None) is False
        assert s.key_down(0x41, hwnd=None, mode="post") is False
        # 显式 send 才允许「打前台」(只验门禁,不真的发键)
        assert s._allow_real_key(None, "send") is True
        ok, why = s.click((0, 10, 10))
        assert ok is False and why, "无效 hwnd 的点击必须被拒绝"
        ok, _why = s.click(0)
        assert ok is False
        # 宏的 absolute 回放:没有目标窗口时只允许显式 send,且仍受总开关约束
        ok, _why = s.click((0, 100, 100), mode="auto")
        assert ok is False, "auto 模式不该在没有目标窗口时打前台"
        assert s.move((0, 100, 100), mode="auto")[0] is False
        assert s.scroll(1, hwnd=0, mode="auto") is False
        assert s._allow_real_key(0, "send") is True

        c = cfg_instance()
        old = c.get("input.allow_real_fallback", True)
        c.set("input.allow_real_fallback", False)
        try:
            assert real_input_allowed() is False
            assert s.key_down(0x41, hwnd=None, mode="send") is False
            assert s._allow_real_key(None, "send") is False
        finally:
            c.set("input.allow_real_fallback", old)

        cfg = AutoClickerConfig.from_dict({
            "hwnd": 1, "interval_ms": 0, "jitter_pct": -50, "total_clicks": -9,
            "points": [{"x": -5, "y": 10 ** 12, "rx": 5, "ry": -0.2,
                        "button": "left"}] * (MAX_POINTS + 50),
        })
        assert cfg.interval_ms == MIN_INTERVAL_MS, cfg.interval_ms
        assert cfg.jitter_pct == 0 and cfg.total_clicks == 0
        assert len(cfg.points) == MAX_POINTS
        p0 = cfg.points[0]
        assert p0.x == 0 and p0.y == 100_000
        assert p0.rx == -1.0 and p0.ry == -1.0, "越界比例应退回像素坐标"

        km = KeyMacroConfig.from_dict({"round_delay_ms": -1, "steps": [
            {"key": "a", "vk": 99999, "hold_ms": -10,
             "delay_before_ms": 10 ** 9}] * 3})
        assert km.steps[0].vk <= 0xFFFF
        assert km.steps[0].delay_before_ms == 600_000
        assert km.round_delay_ms >= 0

        pts = autoclicker_dict_to_points({"points": [
            {"x": "abc", "y": 3, "button": "left", "rx": 2.0}]})
        assert pts and pts[0].x == 0 and pts[0].rx == -1.0
        steps = key_macro_dict_to_steps({"steps": [{"key": "F1", "hold_ms": -5}] * 2})
        assert len(steps) == 2 and steps[0].hold_ms >= 0
        _ok("#4 输入门禁(无 hwnd / 全局开关)与 #11 数值边界全部生效")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_input_and_value_bounds", e)
        return False


def t_audit_update_integrity() -> bool:
    """#3:更新来源白名单 + SHA-256 校验;校验失败绝不落到最终路径."""
    _div("审计 #3:自动更新完整性")
    try:
        import hashlib
        from autofarmstation.utils import update_checker as uc
        from autofarmstation.utils import updater
        from autofarmstation.utils.config import Config

        repo = uc.DEFAULT_REPO
        assert uc.is_allowed_download_url(
            f"https://github.com/{repo}/releases/download/v1/x.exe")
        for bad in (
            "http://github.com/x/y/releases/download/a.exe",
            f"https://evil.example/{repo}/releases/download/a.exe",
            "https://github.com/other/repo/releases/download/v1/x.exe",
            "",
        ):
            assert not uc.is_allowed_download_url(bad), bad

        assets = [
            {"name": "Sneaky.exe", "size": 1,
             "browser_download_url": "https://evil.example/Sneaky.exe"},
            {"name": "AutoFarmStation.exe", "size": 70_000_000,
             "browser_download_url":
                 f"https://github.com/{repo}/releases/download/v2/AutoFarmStation.exe"},
            {"name": "AutoFarmStation.exe.sha256", "size": 64,
             "browser_download_url":
                 f"https://github.com/{repo}/releases/download/v2/AutoFarmStation.exe.sha256"},
        ]
        _url, name, size, sha_url = uc._pick_exe_asset(assets, repo)
        assert name == "AutoFarmStation.exe" and size == 70_000_000
        assert sha_url.endswith(".sha256"), sha_url

        # 来源不合法:下载前就拒绝(不联网)
        info = updater.build_fake_release()
        info.download_url = "https://evil.example/AutoFarmStation.exe"
        res = updater.download_and_apply_release(info)
        assert res.ok is False and "不属于本仓库" in res.error

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            blob = b"MZ\x90\x00" + b"payload" * 10
            fake = d / "new.exe"
            fake.write_bytes(blob)
            good = hashlib.sha256(blob).hexdigest()
            assert updater.sha256_of_file(fake) == good

            cfg = Config(path=d / "config.json")
            cfg.set("settings.pending_update_path", str(fake))
            cfg.set("settings.pending_update_target_exe", str(d / "target.exe"))
            cfg.set("settings.pending_update_sha256", "0" * 64)
            assert updater.apply_pending_update(cfg, None) is False
            assert not fake.exists(), "校验失败的下载文件应被删除"
            again = Config(path=d / "config.json")
            assert again.get("settings.pending_update_path") == ""
            assert "AFS_UPDATER_SHA256" in updater.SWAP_HELPER_PS1
            assert ".bak" in updater.SWAP_HELPER_PS1, "助手应有旧文件备份 / 回滚"
        _ok("#3 URL 白名单、sidecar 识别、哈希校验与失败清理均生效")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_update_integrity", e)
        return False


def t_audit_scheduler_cancellation() -> bool:
    """#6:延时定时任务在 stop() 之后不再触发."""
    _div("审计 #6:延时任务可取消")
    try:
        import threading
        from autofarmstation.core.scheduler import Scheduler, ScheduledTask, TaskFreq

        with tempfile.TemporaryDirectory() as td:
            calls: list = []
            s = Scheduler(path=Path(td) / "schedules.json", autosave=False)
            s.register_handler("start_all", lambda t: calls.append(t.name))
            task = ScheduledTask(name="延时停", action="start_all",
                                 freq=TaskFreq.INTERVAL, interval_sec=3600,
                                 delay_sec=60)
            s.add(task, persist=False)
            fired: list = []
            timer = threading.Timer(30.0, lambda: fired.append(1))
            timer.daemon = True
            with s._lock:
                s._timers.add(timer)
            timer.start()
            s.stop()
            time.sleep(0.3)
            assert not fired, "stop() 之后未触发的延时任务仍执行了"
            s._invoke_safe(task)
            assert not calls, "调度器已停止时延时任务不该动手"
            # 未停止时仍要正常执行
            s._stop.clear()
            s._invoke_safe(task)
            assert calls == ["延时停"], calls
        _ok("#6 延时任务可取消 + 停止后不动手,未停止时正常执行")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_scheduler_cancellation", e)
        return False


def t_audit_write_atomicity_and_identity() -> bool:
    """#9 原子写 / 单实例;#10 路径穿越;#12 进程身份;#15 统计回收."""
    _div("审计 #9/#10/#12/#15:写入原子性、路径边界、进程身份、统计回收")
    try:
        import threading
        from autofarmstation.utils import paths
        from autofarmstation.core.game_launcher import process_identity, identity_matches
        from autofarmstation.core.statistics import Statistics

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            tgt = d / "config.json"
            errs: list = []

            def _writer(i: int) -> None:
                for k in range(15):
                    try:
                        paths.atomic_write(
                            tgt, json.dumps({"i": i, "k": k, "pad": "x" * 300}))
                    except Exception as e:  # noqa: BLE001
                        errs.append(e)

            threads = [threading.Thread(target=_writer, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errs, errs
            data = json.loads(tgt.read_text(encoding="utf-8"))
            assert set(data) == {"i", "k", "pad"}, data
            leftovers = [p.name for p in d.iterdir() if ".tmp.new" in p.name]
            assert not leftovers, f"临时文件残留:{leftovers}"

            # 单实例互斥:本进程内重复调用应幂等 True;别的进程持有时必须 False
            name = f"AFSSelftest{os.getpid()}"
            assert paths.acquire_single_instance(name) is True
            assert paths.acquire_single_instance(name) is True, "同一进程内不该自撞"
            import subprocess
            import sys
            held = name + ".held"
            holder = subprocess.Popen(
                [sys.executable, "-c",
                 "import time,sys;sys.path.insert(0,r'%s');"
                 "from autofarmstation.utils.paths import acquire_single_instance;"
                 "assert acquire_single_instance(r'%s');time.sleep(8)" % (str(REPO_DIR), held)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(2.5)  # 等子进程把互斥体拿到手
                assert paths.acquire_single_instance(held) is False, \
                    "另一个进程已持有互斥体,本进程应判定为「已有实例」"
            finally:
                holder.kill()
                holder.wait(timeout=5)

            # 路径穿越:只允许落在 base 目录内
            base = d / "presets"
            base.mkdir()
            for raw in ("../../evil.json", "..\\..\\evil.json", "a/b/c.json",
                        "C:/Windows/x.json"):
                got = paths.contained_path(base, raw)
                assert got.parent == base.resolve(), (raw, got)

            # 进程身份:PID 被复用后创建时间对不上 → 拒绝
            pid = os.getpid()
            ct, pname = process_identity(pid)
            assert ct > 0 and pname
            ok, _why = identity_matches(pid, expect_name=pname,
                                        expect_create_time=ct)
            assert ok is True
            ok, why = identity_matches(pid, expect_create_time=ct + 5000)
            assert ok is False and "复用" in why
            ok, _why = identity_matches(pid, expect_name="not-this.exe")
            assert ok is False

            # 每进程统计按窗口回收
            st = Statistics(path=d / "stats.json")
            for h in range(600):
                st.add_click(1, hwnd=h)
            st._data.per_process["999"] = {
                "clicks": 1, "keys": 0, "runtime_ms": 0,
                "last_seen": time.time() - 40 * 86400}
            st.save()
            reloaded = Statistics(path=d / "stats.json")
            per = reloaded.snapshot().per_process
            assert len(per) <= reloaded.MAX_PER_PROCESS, len(per)
            assert "999" not in per, "30 天没出现的窗口应被回收"
            reloaded.forget(1)
            assert "1" not in reloaded.snapshot().per_process
        _ok("#9/#10/#12/#15 均按预期工作")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_write_atomicity_and_identity", e)
        return False


def t_audit_cleanup_chain() -> bool:
    """#7:退出清理链每一步独立容错且幂等(用桩对象驱动真实方法)."""
    _div("审计 #7:退出清理链逐步容错 + 幂等")
    try:
        from types import SimpleNamespace
        from autofarmstation.ui.main_window import MainWindow

        done: list = []

        def _mk(name: str, boom: bool = False):
            def _f() -> None:
                if boom:
                    raise RuntimeError(f"{name} 模拟失败")
                done.append(name)
            return _f

        stub = SimpleNamespace(
            _cleaned=False,
            _log=logging.getLogger("selftest"),
            _save_state=_mk("save", True),
            _restore_volume_snapshot=_mk("volume"),
            _steam=SimpleNamespace(shutdown=_mk("steam", True)),
            _hub=SimpleNamespace(stop_all=_mk("hub", True)),
            _monitor=SimpleNamespace(stop=_mk("monitor")),
            _sched=SimpleNamespace(stop=_mk("sched", True)),
            _hotkeys=SimpleNamespace(stop=_mk("hotkeys")),
            _stats_finish=_mk("stats", True),
            _release_embedded_windows=_mk("release", True),
        )
        MainWindow._run_cleanup_once(stub)
        assert "monitor" in done and "hotkeys" in done, done
        assert "save" not in done
        n = len(done)
        MainWindow._run_cleanup_once(stub)
        assert len(done) == n, "清理链不该跑第二遍"
        assert stub._cleaned is True
        _ok("#7 前序步骤全炸时后续清理仍执行,且整链幂等")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_cleanup_chain", e)
        return False


def t_audit_embed_state_and_capture_resources() -> bool:
    """#7:release 失败保留状态 + journal 可回放;#15:mss 被关闭."""
    _div("审计 #7/#15:嵌入状态与截图资源")
    try:
        from autofarmstation.core import window_host as wh
        from autofarmstation.core.preview_capture import PreviewCapture

        st = wh._EmbedState(hwnd=987654, container=1, orig_parent=0, orig_style=0,
                            orig_ex_style=0, orig_rect=(0, 0, 10, 10),
                            orig_minimized=False, ts=time.time())
        real_is_window = wh.wf._is_window
        real_set_parent = wh._user32.SetParent
        try:
            # (a) 句柄已消失 → 状态清掉(确实无可还原)
            wh._states[st.hwnd] = st
            wh._journal_write()
            assert wh.release_window(st.hwnd) is False
            assert not wh.is_embedded(st.hwnd)
            # (b) 窗口在、但还原中途失败 → 状态必须保留,并留在 journal 里等回放
            wh.wf._is_window = lambda hwnd: True

            def _boom(*a, **k):
                raise OSError("模拟 SetParent 失败")

            wh._user32.SetParent = _boom
            wh._states[st.hwnd] = st
            wh._journal_write()
            assert wh.release_window(st.hwnd) is False
            assert wh.is_embedded(st.hwnd), "还原失败却丢了状态 → 窗口将永久无人还原"
            rows = json.loads(wh._journal_path().read_text(encoding="utf-8"))
            assert any(int(r.get("hwnd", 0)) == st.hwnd for r in rows), rows
            # 回放:该句柄并非真窗口时安全跳过,不崩
            wh.wf._is_window = real_is_window
            assert wh.recover_embedded_windows(logging.getLogger("selftest")) == 0
        finally:
            wh.wf._is_window = real_is_window
            wh._user32.SetParent = real_set_parent
            wh._states.pop(st.hwnd, None)
            wh._journal_write()

        closed: list = []

        class _FakeMss:
            def close(self) -> None:
                closed.append(1)

        pc = PreviewCapture(1)
        pc._mss = _FakeMss()
        pc.stop()
        assert pc._mss is None and closed, "mss 句柄没被释放(#15)"
        _ok("#7 还原失败保留状态并可回放;#15 mss 已释放")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_embed_state_and_capture_resources", e)
        return False


def t_audit_log_sanitization() -> bool:
    """#14:脱敏覆盖 traceback、正斜杠与大小写变体."""
    _div("审计 #14:日志与路径脱敏")
    try:
        import getpass
        from autofarmstation.utils.sanitize import sanitize
        from autofarmstation.utils.logger import get as get_log, log_file_path

        user = getpass.getuser()
        if not user:
            _skip("audit_log_sanitization", "拿不到用户名")
            return True
        samples = [
            f"C:\\Users\\{user}\\games\\x.exe",
            f"C:/Users/{user}/AppData/Roaming/y",
            f"c:\\users\\{user.upper()}\\desktop",
            f"{user} 出现在纯文本里",
        ]
        for s in samples:
            out = sanitize(s)
            assert user.lower() not in out.lower(), (s, out)

        lg = get_log()
        try:
            raise ValueError(str(Path.home() / "secret" / "a.txt"))
        except ValueError:
            lg.exception("自检异常路径(应被脱敏)")
        text = log_file_path().read_text(encoding="utf-8", errors="replace")
        assert user.lower() not in text.lower(), "日志(含 traceback)里仍有真实用户名"
        assert "Traceback" in text, "traceback 应当被完整写入(只是脱敏)"
        _ok("#14 traceback / 正斜杠 / 大小写变体均已脱敏")
        return True
    except Exception as e:  # noqa: BLE001
        _fail("audit_log_sanitization", e)
        return False


# 供 scripts.selftest.run_all() 追加调用
TESTS = (
    t_audit_cross_thread_delivery,
    t_audit_hidden_window_keeps_running,
    t_audit_macro_recorder_privacy,
    t_audit_input_and_value_bounds,
    t_audit_update_integrity,
    t_audit_scheduler_cancellation,
    t_audit_write_atomicity_and_identity,
    t_audit_cleanup_chain,
    t_audit_embed_state_and_capture_resources,
    t_audit_log_sanitization,
)
