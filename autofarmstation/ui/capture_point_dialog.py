"""可视化选点对话框.

弹出一个全屏透明覆盖层,用户点击目标窗口内的某个位置即可捕获坐标.

像素口径(审计报告 #13):Win32 的 GetWindowRect / client_origin 都是**物理**像素,
而 Qt 的 setGeometry / globalPosition 都是**逻辑**像素。缩放不等于 100% 时两者
混用会导致覆盖层位置和尺寸都错、捕获到的客户区坐标也偏 —— 所以这里显式做一次
物理 ↔ 逻辑换算,并且**不再**把落在客户区外的点击静默夹成 (0, 0)。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QWidget, QLabel, QVBoxLayout, QPushButton, QHBoxLayout,
)

from ..core import window_finder as wf


def _dpr_at_physical(phys_x: int, phys_y: int) -> float:
    """目标点(物理像素)所在显示器的缩放倍率;拿不到就返回 1.0."""
    try:
        primary = QGuiApplication.primaryScreen()
        pdpr = float(primary.devicePixelRatio() if primary else 1.0) or 1.0
        # screenAt 收的是逻辑坐标:先用主屏倍率粗估,再找该点所在屏幕
        guess = QPoint(int(phys_x / pdpr), int(phys_y / pdpr))
        scr = QGuiApplication.screenAt(guess) or primary
        if scr is not None:
            return float(scr.devicePixelRatio()) or 1.0
    except Exception:  # noqa: BLE001
        pass
    return 1.0


class _OverlayWidget(QWidget):
    """覆盖在目标窗口上的透明 widget,捕获点击坐标."""

    def __init__(self, target_hwnd: int) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self._target_hwnd = int(target_hwnd)
        self.captured: tuple[int, int] | None = None
        self.reject_reason: str = ""
        self._dpr = 1.0
        info = wf.get_window_info(target_hwnd)
        if info is None:
            self.close()
            return
        left, top, right, bottom = info.rect
        w, h = right - left, bottom - top
        # 物理 → 逻辑:setGeometry 要的是逻辑像素(#13)
        self._dpr = _dpr_at_physical(left, top)
        self.setGeometry(
            int(round(left / self._dpr)),
            int(round(top / self._dpr)),
            max(1, int(round(w / self._dpr))),
            max(1, int(round(h / self._dpr))),
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowOpacity(0.20)
        self.activateWindow()
        self.raise_()
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 80))
        pen = QPen(QColor(74, 158, 255))
        pen.setWidth(3)
        p.setPen(pen)
        p.drawRect(self.rect().adjusted(2, 2, -3, -3))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            # 客户区坐标 = 物理屏幕坐标 - 物理客户区原点。
            # globalPosition 是逻辑值,必须先乘回物理倍率再相减(#13)。
            ox, oy = wf.client_origin(self._target_hwnd)
            cw, ch = wf.client_size(self._target_hwnd)
            gp = ev.globalPosition().toPoint()
            gx = int(round(gp.x() * self._dpr))
            gy = int(round(gp.y() * self._dpr))
            x, y = gx - int(ox), gy - int(oy)
            if cw <= 0 or ch <= 0:
                self.reject_reason = "目标窗口客户区尺寸取不到"
                self.close()
                return
            if x < 0 or y < 0 or x >= cw or y >= ch:
                # 旧写法 max(0, …) 会把点在标题栏/边框上的结果静默夹成 (0, 0),
                # 用户以为选中的是画面位置。这里明确拒绝并说明原因。
                self.reject_reason = (
                    f"点击位置不在客户区内(偏移 {x},{y};客户区 {cw}×{ch})。\n"
                    "请点在游戏画面内部,或先取消窗口最大化后重试。"
                )
                self.close()
                return
            self.captured = (int(x), int(y))
            self.close()
        elif ev.button() == Qt.MouseButton.RightButton:
            self.close()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.close()


class CapturePointDialog(QDialog):
    """选点对话框封装."""

    def __init__(self, target_hwnd: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择点击位置")
        self.setModal(True)
        self.captured: tuple[int, int] | None = None
        # 未取到点时的原因("点不在客户区内" 之类),供调用方明确提示(#13)
        self.reject_reason: str = ""
        self._target_hwnd = int(target_hwnd)

        v = QVBoxLayout(self)
        lab = QLabel("游戏窗口已置顶,点击目标位置 / 右键取消 / ESC 取消")
        lab.setStyleSheet("color:#a0cfff;")
        v.addWidget(lab)
        b = QHBoxLayout()
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        b.addStretch(); b.addWidget(btn_cancel)
        v.addLayout(b)

    def exec(self):
        # 先把目标窗口置顶
        wf.set_foreground(self._target_hwnd)
        # 弹出覆盖层
        self._overlay = _OverlayWidget(self._target_hwnd)
        self._overlay.show()
        # 模态循环
        from PySide6.QtCore import QEventLoop
        loop = QEventLoop()
        self._overlay.destroyed.connect(loop.quit)
        loop.exec()
        self.captured = self._overlay.captured
        self.reject_reason = str(getattr(self._overlay, "reject_reason", "") or "")
        return self.captured is not None