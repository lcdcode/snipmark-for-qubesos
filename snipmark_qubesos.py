#!/usr/bin/env python3
# SnipMark for QubesOS - offline screenshot annotator for Qubes OS dom0.
# Deps: python3, python3-qt5. Optional (for VM transfer): qubes-core-admin-client.

import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QPoint, QRect, QBuffer, QIODevice
from PyQt5.QtGui import (
    QImage, QPainter, QPen, QBrush, QColor, QFont, QPolygon, QKeySequence, QIcon,
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QToolBar, QAction, QActionGroup,
    QFileDialog, QMessageBox, QColorDialog, QFontDialog, QInputDialog,
    QLabel, QComboBox, QPushButton, QScrollArea, QSpinBox, QStatusBar,
)


TOOL_CROP = "crop"
TOOL_HIGHLIGHT = "highlight"
TOOL_BOX = "box"
TOOL_ARROW = "arrow"
TOOL_BLUR = "blur"
TOOL_TEXT = "text"


@dataclass
class Annotation:
    kind: str
    rect: Optional[QRect] = None
    start: Optional[QPoint] = None
    end: Optional[QPoint] = None
    color: QColor = field(default_factory=lambda: QColor(255, 0, 0))
    width: int = 3
    text: str = ""
    font: Optional[QFont] = None
    point: Optional[QPoint] = None


def is_qubes_dom0() -> bool:
    try:
        r = subprocess.run(
            ["qubesdb-read", "/name"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip() == "dom0":
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        r = subprocess.run(
            ["which", "qvm-ls"], capture_output=True, timeout=2,
        )
        if r.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def list_qubes_vms() -> List[str]:
    try:
        r = subprocess.run(
            ["qvm-ls", "--raw-list"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return [
                line.strip() for line in r.stdout.splitlines()
                if line.strip() and line.strip() != "dom0"
            ]
    except (OSError, subprocess.SubprocessError):
        pass
    return []


def send_file_to_vm(vm: str, path: str) -> Tuple[bool, str]:
    filename = os.path.basename(path)
    safe = filename.replace("'", "'\\''")
    try:
        with open(path, "rb") as f:
            data = f.read()
        cmd = [
            "qvm-run", "--pass-io", vm,
            "mkdir -p ~/QubesIncoming/dom0 && "
            f"cat > ~/QubesIncoming/dom0/'{safe}'",
        ]
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=60)
        if r.returncode == 0:
            return True, f"Sent to {vm}:~/QubesIncoming/dom0/{filename}"
        return False, r.stderr.decode("utf-8", errors="replace") or "qvm-run failed"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def send_clipboard_to_vm(vm: str, image: QImage) -> Tuple[bool, str]:
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "PNG")
    data = bytes(buf.data())
    try:
        cmd = [
            "qvm-run", "--pass-io", vm,
            "xclip -selection clipboard -t image/png -i",
        ]
        r = subprocess.run(cmd, input=data, capture_output=True, timeout=30)
        if r.returncode == 0:
            return True, f"Clipboard set on {vm}"
        return False, r.stderr.decode("utf-8", errors="replace") or "qvm-run failed"
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


class Canvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.base_image: Optional[QImage] = None
        self.annotations: List[Annotation] = []
        self.undo_stack: List[Tuple[QImage, List[Annotation]]] = []
        self.redo_stack: List[Tuple[QImage, List[Annotation]]] = []
        self.tool = TOOL_BOX
        self.color = QColor(220, 30, 30)
        self.line_width = 3
        self.font = QFont("Sans", 16)
        self.dragging = False
        self.drag_start: Optional[QPoint] = None
        self.drag_end: Optional[QPoint] = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.resize(900, 600)

    def set_image(self, image: QImage):
        self.push_undo()
        self.base_image = image.convertToFormat(QImage.Format_ARGB32)
        self.annotations = []
        self.redo_stack = []
        self._fit_to_image()
        self.update()

    def _fit_to_image(self):
        if self.base_image is not None:
            sz = self.base_image.size()
            self.setMinimumSize(sz)
            self.resize(sz)

    def push_undo(self):
        if self.base_image is not None:
            self.undo_stack.append((self.base_image.copy(), list(self.annotations)))
            if len(self.undo_stack) > 50:
                self.undo_stack.pop(0)
            self.redo_stack = []

    def undo(self):
        if not self.undo_stack:
            return
        if self.base_image is not None:
            self.redo_stack.append((self.base_image.copy(), list(self.annotations)))
        img, anns = self.undo_stack.pop()
        self.base_image = img
        self.annotations = anns
        self._fit_to_image()
        self.update()

    def redo(self):
        if not self.redo_stack:
            return
        if self.base_image is not None:
            self.undo_stack.append((self.base_image.copy(), list(self.annotations)))
        img, anns = self.redo_stack.pop()
        self.base_image = img
        self.annotations = anns
        self._fit_to_image()
        self.update()

    def render_image(self) -> Optional[QImage]:
        if self.base_image is None:
            return None
        result = self.base_image.copy()
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        for ann in self.annotations:
            self._paint_annotation(painter, ann)
        painter.end()
        return result

    def paintEvent(self, _event):
        painter = QPainter(self)
        if self.base_image is None:
            painter.fillRect(self.rect(), QColor(40, 40, 40))
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(
                self.rect(), Qt.AlignCenter,
                "No image loaded.\nFile -> Paste from Clipboard (Ctrl+V) or Open (Ctrl+O)",
            )
            return
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        painter.drawImage(0, 0, self.base_image)
        for ann in self.annotations:
            self._paint_annotation(painter, ann)
        if self.dragging and self.drag_start is not None and self.drag_end is not None:
            self._paint_preview(painter)

    def _paint_annotation(self, painter: QPainter, ann: Annotation):
        if ann.kind == TOOL_HIGHLIGHT and ann.rect is not None:
            color = QColor(ann.color)
            color.setAlpha(80)
            painter.fillRect(ann.rect, color)
        elif ann.kind == TOOL_BOX and ann.rect is not None:
            painter.setPen(QPen(ann.color, ann.width))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(ann.rect)
        elif ann.kind == TOOL_ARROW and ann.start is not None and ann.end is not None:
            self._draw_arrow(painter, ann.start, ann.end, ann.color, ann.width)
        elif ann.kind == TOOL_TEXT and ann.point is not None and ann.text:
            painter.setPen(QPen(ann.color))
            painter.setFont(ann.font or QFont("Sans", 16))
            painter.drawText(ann.point, ann.text)

    def _paint_preview(self, painter: QPainter):
        rect = QRect(self.drag_start, self.drag_end).normalized()
        if self.tool == TOOL_CROP:
            painter.setPen(QPen(QColor(0, 200, 255), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
        elif self.tool == TOOL_HIGHLIGHT:
            color = QColor(self.color)
            color.setAlpha(80)
            painter.fillRect(rect, color)
        elif self.tool == TOOL_BOX:
            painter.setPen(QPen(self.color, self.line_width))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
        elif self.tool == TOOL_ARROW:
            self._draw_arrow(
                painter, self.drag_start, self.drag_end,
                self.color, self.line_width,
            )
        elif self.tool == TOOL_BLUR:
            painter.setPen(QPen(QColor(255, 200, 0), 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)

    def _draw_arrow(self, painter, start, end, color, width):
        painter.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(QBrush(color))
        painter.drawLine(start, end)
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        if math.hypot(dx, dy) < 1:
            return
        ang = math.atan2(dy, dx)
        head = max(10, width * 4)
        x1 = end.x() - head * math.cos(ang - math.pi / 6)
        y1 = end.y() - head * math.sin(ang - math.pi / 6)
        x2 = end.x() - head * math.cos(ang + math.pi / 6)
        y2 = end.y() - head * math.sin(ang + math.pi / 6)
        poly = QPolygon([end, QPoint(int(x1), int(y1)), QPoint(int(x2), int(y2))])
        painter.drawPolygon(poly)

    def mousePressEvent(self, event):
        if self.base_image is None or event.button() != Qt.LeftButton:
            return
        pos = event.pos()
        if self.tool == TOOL_TEXT:
            text, ok = QInputDialog.getText(self, "Add Text", "Text:")
            if ok and text:
                self.push_undo()
                self.annotations.append(Annotation(
                    kind=TOOL_TEXT, point=QPoint(pos),
                    text=text, color=QColor(self.color),
                    font=QFont(self.font),
                ))
                self.update()
            return
        self.dragging = True
        self.drag_start = QPoint(pos)
        self.drag_end = QPoint(pos)

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.drag_end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.dragging or event.button() != Qt.LeftButton:
            return
        self.dragging = False
        self.drag_end = event.pos()
        rect = QRect(self.drag_start, self.drag_end).normalized()
        if self.tool == TOOL_CROP:
            self._do_crop(rect)
        elif self.tool == TOOL_BLUR:
            if rect.width() > 2 and rect.height() > 2:
                self.push_undo()
                self._apply_blur(rect)
        elif self.tool == TOOL_HIGHLIGHT:
            if rect.width() > 2 and rect.height() > 2:
                self.push_undo()
                self.annotations.append(Annotation(
                    kind=TOOL_HIGHLIGHT, rect=QRect(rect),
                    color=QColor(self.color),
                ))
        elif self.tool == TOOL_BOX:
            if rect.width() > 2 and rect.height() > 2:
                self.push_undo()
                self.annotations.append(Annotation(
                    kind=TOOL_BOX, rect=QRect(rect),
                    color=QColor(self.color), width=self.line_width,
                ))
        elif self.tool == TOOL_ARROW:
            if (self.drag_start - self.drag_end).manhattanLength() > 4:
                self.push_undo()
                self.annotations.append(Annotation(
                    kind=TOOL_ARROW,
                    start=QPoint(self.drag_start), end=QPoint(self.drag_end),
                    color=QColor(self.color), width=self.line_width,
                ))
        self.drag_start = None
        self.drag_end = None
        self.update()

    def _do_crop(self, rect: QRect):
        if rect.width() < 4 or rect.height() < 4 or self.base_image is None:
            return
        rect = rect.intersected(self.base_image.rect())
        if rect.isEmpty():
            return
        self.push_undo()
        self.base_image = self.base_image.copy(rect)
        offset = rect.topLeft()
        new_anns = []
        for ann in self.annotations:
            new_anns.append(Annotation(
                kind=ann.kind,
                rect=ann.rect.translated(-offset.x(), -offset.y()) if ann.rect else None,
                start=(ann.start - offset) if ann.start else None,
                end=(ann.end - offset) if ann.end else None,
                color=QColor(ann.color),
                width=ann.width,
                text=ann.text,
                font=QFont(ann.font) if ann.font else None,
                point=(ann.point - offset) if ann.point else None,
            ))
        self.annotations = new_anns
        self._fit_to_image()

    def _apply_blur(self, rect: QRect):
        rect = rect.intersected(self.base_image.rect())
        if rect.isEmpty():
            return
        region = self.base_image.copy(rect)
        factor = 12
        small = region.scaled(
            max(1, region.width() // factor),
            max(1, region.height() // factor),
            Qt.IgnoreAspectRatio, Qt.FastTransformation,
        )
        blurred = small.scaled(
            region.width(), region.height(),
            Qt.IgnoreAspectRatio, Qt.SmoothTransformation,
        )
        painter = QPainter(self.base_image)
        painter.drawImage(rect.topLeft(), blurred)
        painter.end()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SnipMark for QubesOS")
        self.resize(1100, 750)

        self.canvas = Canvas(self)
        scroll = QScrollArea()
        scroll.setWidget(self.canvas)
        scroll.setAlignment(Qt.AlignCenter)
        scroll.setStyleSheet("background:#2b2b2b;")
        scroll.setWidgetResizable(False)
        self.setCentralWidget(scroll)

        self.is_qubes = is_qubes_dom0()
        self.vm_combo: Optional[QComboBox] = None

        self._build_menu()
        self._build_toolbars()

        self.setStatusBar(QStatusBar())
        if self.is_qubes:
            self.statusBar().showMessage("Qubes dom0 detected - VM transfer enabled")
        else:
            self.statusBar().showMessage("Ready")

    def _build_menu(self):
        m = self.menuBar()
        file_menu = m.addMenu("&File")

        a_paste = QAction("&Paste from Clipboard", self)
        a_paste.setShortcut(QKeySequence.Paste)
        a_paste.triggered.connect(self.action_paste)
        file_menu.addAction(a_paste)

        a_copy = QAction("&Copy to Clipboard", self)
        a_copy.setShortcut(QKeySequence.Copy)
        a_copy.triggered.connect(self.action_copy)
        file_menu.addAction(a_copy)

        a_open = QAction("&Open...", self)
        a_open.setShortcut(QKeySequence.Open)
        a_open.triggered.connect(self.action_open)
        file_menu.addAction(a_open)

        a_save = QAction("&Save As...", self)
        a_save.setShortcut(QKeySequence.Save)
        a_save.triggered.connect(self.action_save)
        file_menu.addAction(a_save)

        file_menu.addSeparator()
        a_quit = QAction("&Quit", self)
        a_quit.setShortcut(QKeySequence.Quit)
        a_quit.triggered.connect(self.close)
        file_menu.addAction(a_quit)

        edit_menu = m.addMenu("&Edit")
        a_undo = QAction("&Undo", self)
        a_undo.setShortcut(QKeySequence.Undo)
        a_undo.triggered.connect(self.canvas.undo)
        edit_menu.addAction(a_undo)
        a_redo = QAction("&Redo", self)
        a_redo.setShortcut(QKeySequence.Redo)
        a_redo.triggered.connect(self.canvas.redo)
        edit_menu.addAction(a_redo)

    def _build_toolbars(self):
        tb = QToolBar("Tools")
        tb.setMovable(False)
        self.addToolBar(Qt.TopToolBarArea, tb)

        tool_group = QActionGroup(self)
        tool_group.setExclusive(True)
        for label, key in [
            ("Crop", TOOL_CROP), ("Highlight", TOOL_HIGHLIGHT),
            ("Box", TOOL_BOX), ("Arrow", TOOL_ARROW),
            ("Blur", TOOL_BLUR), ("Text", TOOL_TEXT),
        ]:
            a = QAction(label, self)
            a.setCheckable(True)
            a.triggered.connect(lambda _checked, k=key: self._set_tool(k))
            tb.addAction(a)
            tool_group.addAction(a)
            if key == TOOL_BOX:
                a.setChecked(True)

        tb.addSeparator()

        self.color_btn = QPushButton("  Color  ")
        self.color_btn.setToolTip("Pick drawing/text color")
        self.color_btn.clicked.connect(self.pick_color)
        self._refresh_color_btn()
        tb.addWidget(self.color_btn)

        tb.addWidget(QLabel(" Width:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 30)
        self.width_spin.setValue(self.canvas.line_width)
        self.width_spin.valueChanged.connect(self._on_width_changed)
        tb.addWidget(self.width_spin)

        self.font_btn = QPushButton("Font...")
        self.font_btn.setToolTip("Pick text font")
        self.font_btn.clicked.connect(self.pick_font)
        tb.addWidget(self.font_btn)

        tb.addSeparator()

        a_paste = QAction("Paste", self)
        a_paste.triggered.connect(self.action_paste)
        tb.addAction(a_paste)
        a_copy = QAction("Copy", self)
        a_copy.triggered.connect(self.action_copy)
        tb.addAction(a_copy)
        a_save = QAction("Save", self)
        a_save.triggered.connect(self.action_save)
        tb.addAction(a_save)

        if self.is_qubes:
            qb = QToolBar("Qubes")
            qb.setMovable(False)
            self.addToolBar(Qt.BottomToolBarArea, qb)
            qb.addWidget(QLabel(" Qubes target VM: "))
            self.vm_combo = QComboBox()
            self.vm_combo.setMinimumWidth(180)
            self._refresh_vms()
            qb.addWidget(self.vm_combo)
            b_refresh = QPushButton("Refresh")
            b_refresh.clicked.connect(self._refresh_vms)
            qb.addWidget(b_refresh)
            qb.addSeparator()
            b_clip = QPushButton("Send Clipboard to VM")
            b_clip.clicked.connect(self.action_send_clipboard_vm)
            qb.addWidget(b_clip)
            b_file = QPushButton("Send File to VM")
            b_file.clicked.connect(self.action_send_file_vm)
            qb.addWidget(b_file)

    def _refresh_vms(self):
        if self.vm_combo is None:
            return
        self.vm_combo.clear()
        vms = list_qubes_vms()
        if vms:
            self.vm_combo.addItems(vms)
        else:
            self.vm_combo.addItem("(no VMs found)")

    def _refresh_color_btn(self):
        c = self.canvas.color
        fg = "#000" if c.lightness() > 128 else "#fff"
        self.color_btn.setStyleSheet(
            f"background:{c.name()};color:{fg};padding:4px 12px;"
        )

    def _set_tool(self, k):
        self.canvas.tool = k
        self.statusBar().showMessage(f"Tool: {k}")

    def _on_width_changed(self, v):
        self.canvas.line_width = v

    def pick_color(self):
        c = QColorDialog.getColor(self.canvas.color, self, "Pick color")
        if c.isValid():
            self.canvas.color = c
            self._refresh_color_btn()

    def pick_font(self):
        f, ok = QFontDialog.getFont(self.canvas.font, self, "Pick font")
        if ok:
            self.canvas.font = f
            self.statusBar().showMessage(
                f"Font: {f.family()} {f.pointSize()}"
            )

    def action_paste(self, *_):
        cb = QApplication.clipboard()
        img = cb.image()
        if img.isNull():
            self.statusBar().showMessage("Clipboard has no image")
            return
        self.canvas.set_image(img)
        self.statusBar().showMessage(
            f"Pasted image {img.width()}x{img.height()}"
        )

    def action_copy(self):
        img = self.canvas.render_image()
        if img is None:
            self.statusBar().showMessage("Nothing to copy")
            return
        QApplication.clipboard().setImage(img)
        self.statusBar().showMessage("Copied to clipboard")

    def action_open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tiff)",
        )
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            QMessageBox.critical(self, "Open failed", f"Could not read {path}")
            return
        self.canvas.set_image(img)
        self.statusBar().showMessage(f"Opened {path}")

    def action_save(self):
        img = self.canvas.render_image()
        if img is None:
            self.statusBar().showMessage("Nothing to save")
            return
        default = f"snipmark_{int(time.time())}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save As", default,
            "PNG (*.png);;JPEG (*.jpg);;BMP (*.bmp)",
        )
        if not path:
            return
        if not img.save(path):
            QMessageBox.critical(self, "Save failed", f"Could not write {path}")
            return
        self.statusBar().showMessage(f"Saved {path}")

    def _selected_vm(self) -> Optional[str]:
        if self.vm_combo is None:
            return None
        v = self.vm_combo.currentText()
        if not v or v.startswith("("):
            return None
        return v

    def action_send_clipboard_vm(self):
        vm = self._selected_vm()
        if not vm:
            QMessageBox.warning(self, "No VM", "Select a target VM first")
            return
        img = self.canvas.render_image()
        if img is None:
            self.statusBar().showMessage("Nothing to send")
            return
        ok, msg = send_clipboard_to_vm(vm, img)
        if not ok:
            QMessageBox.warning(self, "Send failed", msg)
        else:
            self.statusBar().showMessage(msg)

    def action_send_file_vm(self):
        vm = self._selected_vm()
        if not vm:
            QMessageBox.warning(self, "No VM", "Select a target VM first")
            return
        img = self.canvas.render_image()
        if img is None:
            self.statusBar().showMessage("Nothing to send")
            return
        fname = f"snipmark_{int(time.time())}.png"
        tmpdir = tempfile.mkdtemp(prefix="snipmark_")
        tmp = os.path.join(tmpdir, fname)
        try:
            if not img.save(tmp, "PNG"):
                QMessageBox.critical(self, "Error", "Could not encode PNG")
                return
            ok, msg = send_file_to_vm(vm, tmp)
            if not ok:
                QMessageBox.warning(self, "Send failed", msg)
            else:
                self.statusBar().showMessage(msg)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            try:
                os.rmdir(tmpdir)
            except OSError:
                pass


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("SnipMark for QubesOS")
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snipmark.png")
    if os.path.isfile(icon_path):
        icon = QIcon(icon_path)
        app.setWindowIcon(icon)
    win = MainWindow()
    if os.path.isfile(icon_path):
        win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
