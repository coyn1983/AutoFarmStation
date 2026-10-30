"""选择要追踪的进程对话框."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from ..core import window_finder as wf
from ..core.window_finder import WindowInfo


class ProcessSelectorDialog(QDialog):
    """列出所有顶层窗口,让用户选择要添加的.

    默认只列「可见且有标题」的窗口,外加**最小化的有标题窗口**(标「已最小化」)
    —— 游戏挂在最小化状态是很常见的用法,以前它们会被整条过滤掉,用户就只能
    先恢复窗口才能添加。勾「包含隐藏窗口」再额外带出隐藏 / 无标题的窗口。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title_filter: str = "",
        include_hidden: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择要追踪的窗口")
        self.resize(640, 480)
        self.selected: WindowInfo | None = None

        v = QVBoxLayout(self)
        v.addWidget(QLabel("勾选需要挂机管理的窗口(可多选,点击「追踪选中」)"))
        h = QHBoxLayout()
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("按窗口标题筛选...")
        self._filter.textChanged.connect(self._refresh)
        h.addWidget(self._filter, stretch=1)
        self._chk_hidden = QCheckBox("包含隐藏窗口")
        self._chk_hidden.setToolTip(
            "勾选后,主窗口被隐藏 / 无标题的游戏也会出现在列表里"
            "(最小化的游戏窗口不勾也会列出)"
        )
        self._chk_hidden.setChecked(include_hidden)
        self._chk_hidden.toggled.connect(self._refresh)
        h.addWidget(self._chk_hidden)
        v.addLayout(h)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        v.addWidget(self._list)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(self._hint)

        bb = QDialogButtonBox()
        self._btn_ok = bb.addButton("追踪选中", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = bb.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

        self._refresh(title_filter)

    def _refresh(self, title_filter=None) -> None:
        """重建列表.

        两条硬性要求(都是踩过的坑):

        1. 这个槽同时接 `QLineEdit.textChanged`(给 str)和
           `QCheckBox.toggled`(给 **bool**)。以前签名写死 `title_filter: str`,
           一勾选就把 True 当筛选文字送进 `list_visible_windows`,里面
           `True.lower()` 直接抛异常 —— 而此时列表已经 clear 过,用户看到的就是
           「一勾包含隐藏窗口,所有程序都不见了」。
        2. 先把数据算完、再一次性填进控件:任何一步失败都不该清空已有内容。
        """
        text = title_filter.strip().lower() if isinstance(title_filter, str) else ""
        text = self._filter.text().strip().lower() or text
        want_hidden = self._chk_hidden.isChecked()
        try:
            # 一次 EnumWindows 拿全量;旧写法按每个 PID 各枚举一遍(288 进程 = 288 次)
            allwins = wf.list_all_toplevel_windows()
        except Exception as e:  # noqa: BLE001
            self._hint.setText(f"枚举窗口失败:{type(e).__name__}: {e}")
            return

        rows: list[tuple[int, tuple, WindowInfo]] = []
        for w in allwins:
            title = (w.title or "").strip()
            if not title:
                # 无标题窗口只在接受隐藏窗口时才列出来,否则列表全是认不出的行
                if not want_hidden:
                    continue
                bucket = 2
            elif w.visible:
                bucket = 0
            elif w.is_minimized:
                bucket = 1      # 最小化的游戏窗口:不勾也列出来
            else:
                if not want_hidden:
                    continue
                bucket = 2
            if text and text not in title.lower():
                continue
            rows.append((bucket, wf.window_rank(w), w))

        rows.sort(key=lambda r: (r[0], r[1], (r[2].title or "").lower()))

        self._list.clear()
        shown = 0
        for bucket, _rank, w in rows:
            if not w.visible:
                tag = " · 已最小化" if w.is_minimized else " · 隐藏"
            else:
                tag = ""
            warn = "  ⚠不像游戏主窗口" if wf.is_junk_window(w) else ""
            label = f"[{w.pid}] {(w.title or '(无标题)').strip()}{tag}{warn}  ({w.class_name})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, w)
            self._list.addItem(item)
            shown += 1
        if shown:
            self._list.setCurrentRow(0)

        hint = f"共 {shown} 个窗口"
        minimized = sum(1 for _b, _r, w in rows if w.is_minimized)
        if minimized:
            hint += f"(含 {minimized} 个已最小化)"
        if not want_hidden:
            hint += " · 没找到游戏窗口?勾上「包含隐藏窗口」再看一下"
        self._hint.setText(hint)

    def accept(self):
        items = self._list.selectedItems()
        if not items:
            # 允许单选(用户没多选就用当前行)
            if self._list.currentItem():
                items = [self._list.currentItem()]
            else:
                return
        # 取第一个,实际支持多选可以在 main 里循环
        self.selected = items[0].data(Qt.ItemDataRole.UserRole)
        super().accept()

    def selected_all(self) -> list[WindowInfo]:
        """获取所有选中行(支持多选)。"""
        out = []
        for it in self._list.selectedItems():
            w = it.data(Qt.ItemDataRole.UserRole)
            if isinstance(w, WindowInfo):
                out.append(w)
        return out
