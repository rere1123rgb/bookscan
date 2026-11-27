"""책 스캔 도우미: 복합기 피더 스캔 → 자동 보정 → 순서·회전 확인 → PDF/PNG 저장."""
import ctypes
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import cv2
import img2pdf
import numpy as np
import pypdf
from PySide6.QtCore import QItemSelectionModel, QObject, QPointF, QSettings, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QDrag, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGraphicsEllipseItem, QInputDialog, QGraphicsItem, QGraphicsPolygonItem,
                               QGraphicsScene, QGraphicsView, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
                               QSplitter, QToolBar, QVBoxLayout, QWidget)

import pagenum
import process as P
import orient
import scanner
import win_scan
import worker
from detect import VERSION as DETECT_VERSION
from worker import imread

SMALL = 0.2          # 썸네일·미리보기 계산용 축소 배율(원본 대비)
THUMB_H = 200
WORK_DIRNAME = '_스캔작업'
IMAGE_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff')

DEFAULT_OPT = dict(
    escl_url=scanner.DEFAULT_URL, device_name='', source='Feeder', duplex=True, dpi=300, auto_place=True, pno_band=None,
    crop=True, edge_mm=0.5, edge_per_page=True, sharpen=1, uniform=True, uniform_mode='pad', spine='auto', margin_mm=0.0,
    auto_orient=True, rot_front=0, rot_back=0,
    skip_blank=True, blank_threshold=0.1,
    save_png=False, save_pdf=True, pdf_quality='q95', name='book',
)
ROT_CHOICES = [(0, '그대로'), (90, '시계 방향 90°'), (180, '180°'), (270, '반시계 방향 90°')]


def imwrite(path, img, params=()):
    ok, buf = cv2.imencode(os.path.splitext(path)[1], img, list(params))
    if not ok:
        raise IOError(f'이미지 저장 실패: {path}')
    buf.tofile(path)


def _missing_text(a, b, n):
    """빠진 쪽 표시: p.47 / p.47~p.48 / (사이에 번호 없는 쪽이 끼어 있으면) p.45~p.50 사이 2쪽."""
    if n == b - a - 1:
        return f'p.{a + 1}' if n == 1 else f'p.{a + 1}~p.{b - 1}'
    return f'p.{a}~p.{b} 사이 {n}쪽'


def _batch_stamp():
    """묶음 이름(스캔 시각). 같은 초에 연달아 스캔해도 원본 파일이 덮어써지지 않게 밀리초까지."""
    t = time.time()
    return time.strftime('%Y%m%d-%H%M%S', time.localtime(t)) + f'{int(t * 1000) % 1000:03d}'


def save_icon():
    """납작한 저장 아이콘: 파란 둥근 사각형 안에 흰 '아래로 내려 담기' 화살표."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(30, 110, 230))
    p.drawRoundedRect(4, 4, 56, 56, 13, 13)
    pen = QPen(QColor(255, 255, 255), 6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.drawLine(32, 15, 32, 37)                                     # 화살표 몸통
    p.drawPolyline(QPolygonF([QPointF(22, 28), QPointF(32, 38), QPointF(42, 28)]))  # 화살촉
    p.drawPolyline(QPolygonF([QPointF(17, 41), QPointF(17, 48), QPointF(47, 48), QPointF(47, 41)]))  # 받침
    p.end()
    return QIcon(pm)


def to_qimage(img):
    h, w = img.shape[:2]
    return QImage(np.ascontiguousarray(img).data, w, h, 3 * w, QImage.Format_BGR888).copy()


class Bus(QObject):
    """작업 스레드 → 화면 스레드 신호."""
    analyzed = Signal(str, object)       # page id, worker.analyze 결과
    thumb = Signal(str, int, object)     # page id, generation, 이미지
    preview = Signal(str, int, object)
    scanned = Signal(str, str)           # 파일 경로, side
    scan_done = Signal(int, str)         # 장수, 오류 메시지('' 이면 정상)
    progress = Signal(int, int, str)
    export_done = Signal(str)
    caps = Signal(object)                # 복합기가 받는 해상도 {'Feeder': [...], 'Platen': [...]}
    devices = Signal(object)             # 찾은 스캐너 목록
    notice = Signal(str)                 # 스캔 중 알림(양면 → 단면 등)
    rescan_done = Signal(object)
    analyze_failed = Signal(str, str)    # page id, 오류 설명


class CornerEditor(QDialog):
    """감지가 틀린 페이지의 네 모서리를 손으로 옮기는 창.
    스캔 원본은 A4 라 종이는 위쪽에 있고 아래는 비어 있다 → 열 때 종이 영역에 맞춰 확대해 가운데 둔다."""
    SCALE = 0.5   # 확대해서 맞출 수 있게 넉넉한 해상도

    def __init__(self, parent, raw_path, quad, auto_quad):
        super().__init__(parent)
        self.setWindowTitle('모서리 직접 조정 — 빨간 점을 끌어서 종이 모서리에 맞추세요')
        self.auto_quad = auto_quad
        img = cv2.resize(imread(raw_path), None, fx=self.SCALE, fy=self.SCALE, interpolation=cv2.INTER_AREA)
        scene = QGraphicsScene(self)
        scene.addPixmap(QPixmap.fromImage(to_qimage(img)))
        self.poly = QGraphicsPolygonItem()
        self.poly.setPen(QPen(QColor(255, 0, 0), 2))
        scene.addItem(self.poly)
        self.handles = []
        for x, y in quad:
            hd = _Handle(self._update)
            hd.setPos(x * self.SCALE, y * self.SCALE)
            scene.addItem(hd)
            self.handles.append(hd)
        self._update()
        self.view = view = ZoomView(scene)
        self.paper_rect = self.poly.boundingRect()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText('적용')
        buttons.button(QDialogButtonBox.Cancel).setText('취소')
        reset = buttons.addButton('자동 감지값으로', QDialogButtonBox.ResetRole)
        reset.clicked.connect(self._reset)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(view)
        hint = QLabel('Ctrl+휠: 확대·축소   휠: 위아래 이동   빨간 점을 끌어 모서리에 맞추세요')
        hint.setStyleSheet('color: gray;')
        lay.addWidget(hint)
        lay.addWidget(buttons)
        self.resize(1100, 850)

    def showEvent(self, e):
        super().showEvent(e)
        # 창 크기가 정해진 뒤에 종이 영역(+여유 6%)에 맞춘다
        r = self.paper_rect
        m = max(r.width(), r.height()) * 0.06
        QTimer.singleShot(0, lambda: self.view.fitInView(r.adjusted(-m, -m, m, m), Qt.KeepAspectRatio))

    def _update(self):
        self.poly.setPolygon(QPolygonF([h.pos() for h in self.handles]))

    def _reset(self):
        for hd, (x, y) in zip(self.handles, self.auto_quad):
            hd.setPos(x * self.SCALE, y * self.SCALE)

    def quad(self):
        return [[h.pos().x() / self.SCALE, h.pos().y() / self.SCALE] for h in self.handles]


class PageList(QListWidget):
    """썸네일 격자. 끌어다 놓으면 놓은 자리로 '순서'를 바꾼다.
    Qt 기본 격자 보기(IconMode)의 끌어 놓기는 아이콘을 화면의 다른 칸으로 옮기기만 하고
    목록 순서는 그대로 두는 경우가 있어, 놓은 위치를 직접 계산해 항목을 옮긴다."""
    reordered = Signal()
    before_change = Signal()  # 실행 취소용: 바뀌기 직전
    caret_changed = Signal()

    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Snap)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSpacing(6)
        self.setWordWrap(True)
        self.drop_row = None  # 끄는 중: 놓으면 들어갈 자리(그 자리에 파란 막대를 그린다)
        self.caret_row = None  # 페이지 사이를 클릭해 정한 붙여넣을 자리

    # --- 끄는 동안 '여기에 들어갑니다' 표시 ---
    def startDrag(self, actions):
        """끄는 이미지: 첫 장 썸네일 + 몇 장인지."""
        items = self.selectedItems()
        if not items:
            return
        pm = items[0].icon().pixmap(self.iconSize() * 0.6)
        if len(items) > 1:
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor(30, 110, 230))
            p.setPen(Qt.NoPen)
            p.drawEllipse(pm.width() - 34, 4, 30, 30)
            p.setPen(QColor(255, 255, 255))
            p.drawText(pm.width() - 34, 4, 30, 30, Qt.AlignCenter, str(len(items)))
            p.end()
        drag = QDrag(self)
        drag.setMimeData(self.model().mimeData(self.selectedIndexes()))
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        drag.exec(Qt.MoveAction)
        self.drop_row = None
        self.viewport().update()

    def dragEnterEvent(self, e):
        if e.source() is self:
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is not self:
            e.ignore()
            return
        self.drop_row = self.insert_row_at(e.position().toPoint())
        e.setDropAction(Qt.MoveAction)
        e.accept()
        self.viewport().update()

    def dragLeaveEvent(self, e):
        self.drop_row = None  # 목록 밖으로 나가면 표시를 지운다(여기서 놓으면 아무 일도 없다)
        self.viewport().update()
        super().dragLeaveEvent(e)

    # --- 페이지 '사이' 클릭: 붙여넣을 자리(커서) ---
    def gap_row_at(self, pos):
        """pos 가 썸네일 사이(또는 썸네일 양옆 여백·빈 곳)면 그 자리(0..count), 썸네일 위면 None.
        간격이 좁아 클릭하기 어려우므로 그림 바깥 여백까지 '사이'로 친다."""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.insert_row_at(pos) if self.count() else 0
        r = self.visualRect(idx)
        half = self.iconSize().width() // 2
        if pos.x() < r.center().x() - half:
            return idx.row()
        if pos.x() > r.center().x() + half:
            return idx.row() + 1
        return None

    def mousePressEvent(self, e):
        row = self.gap_row_at(e.position().toPoint()) if e.button() in (Qt.LeftButton, Qt.RightButton) else None
        if row is not None:
            self.clearSelection()
            self.set_caret(row)
            return
        self.set_caret(None)
        super().mousePressEvent(e)

    def set_caret(self, row):
        if row != self.caret_row:
            self.caret_row = row
            self.caret_changed.emit()
            self.viewport().update()

    def _bar_x(self, row):
        if row < self.count():
            r = self.visualItemRect(self.item(row))
            return r.left() - self.spacing() // 2 - 2, r
        r = self.visualItemRect(self.item(self.count() - 1))
        return r.right() + self.spacing() // 2 + 1, r

    def paintEvent(self, e):
        super().paintEvent(e)
        row = self.drop_row if self.drop_row is not None else self.caret_row
        if row is None or not self.count():
            return
        x, r = self._bar_x(row)
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(30, 110, 230))
        p.drawRoundedRect(x - 2, r.top(), 5, r.height(), 2, 2)
        p.end()

    def insert_row_at(self, pos):
        """놓은 위치 → 끼워 넣을 자리(0..count). 가장 가까운 썸네일의 왼쪽/오른쪽 절반으로 앞뒤를 정한다."""
        best, best_d = None, None
        for i in range(self.count()):
            r = self.visualItemRect(self.item(i))
            dx = 0 if r.left() <= pos.x() <= r.right() else min(abs(pos.x() - r.left()), abs(pos.x() - r.right()))
            dy = 0 if r.top() <= pos.y() <= r.bottom() else min(abs(pos.y() - r.top()), abs(pos.y() - r.bottom()))
            d = dy * 4 + dx  # 같은 줄을 우선
            if best_d is None or d < best_d:
                best, best_d = i, d
        if best is None:
            return 0
        r = self.visualItemRect(self.item(best))
        return best + (1 if pos.x() > r.center().x() else 0)

    def move_rows(self, rows, target):
        """rows(여러 장 가능)를 순서를 유지한 채 target 자리로."""
        rows = sorted(set(rows))
        target -= sum(1 for r in rows if r < target)  # 앞에서 빠지는 만큼 당겨진다
        items = [self.takeItem(r) for r in reversed(rows)][::-1]
        for k, it in enumerate(items):
            self.insertItem(target + k, it)
        self.clearSelection()
        for it in items:
            it.setSelected(True)
        self.setCurrentItem(items[0])
        self.reordered.emit()

    def dropEvent(self, e):
        self.drop_row = None
        self.viewport().update()
        if e.source() is not self:
            e.ignore()
            return
        rows = [self.row(it) for it in self.selectedItems()]
        if rows:
            self.before_change.emit()
            self.move_rows(rows, self.insert_row_at(e.position().toPoint()))
        # Qt 가 끌기를 '이동'으로 끝내면 원본 항목을 지우려 하므로, 이미 옮긴 뒤엔 복사로 끝낸 척한다
        e.setDropAction(Qt.CopyAction)
        e.accept()


class PnoDialog(QDialog):
    """쪽 번호를 직접 입력하는 창. 페이지를 하나씩 크게 보여 주고 주변 번호와 추측값을 알려 준다.
    ask(pid) → (이미지, 주변 설명, 추측 번호, 지금 번호, 표지 표시 'front'/'back'/None) 을 돌려주는 함수
    (입력할 때마다 다시 계산). 결과는 on_answer(pid, 값) 로 바로 알린다.
    값: 숫자(0 이면 표지) / 'front'(표지) / 'back'(뒷표지) / 'none'(번호 없는 쪽). 건너뛰면 부르지 않는다."""

    def __init__(self, parent, pids, ask, on_answer):
        super().__init__(parent)
        self.pids, self.ask, self.on_answer, self.i = pids, ask, on_answer, 0
        self.setWindowTitle('쪽 번호 입력')
        self.img = QLabel()
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setMinimumSize(520, 720)
        self.head = QLabel()
        self.head.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.around = QLabel()
        self.around.setWordWrap(True)
        self.spin = QSpinBox()
        self.spin.setRange(0, 9999)  # 0 = 표지
        self.spin.setStyleSheet('font-size: 22px;')
        self.spin.setMinimumHeight(44)
        self.chk_front = QCheckBox('표지 (0쪽)')
        self.chk_back = QCheckBox('뒷표지 (마지막 쪽, 자동)')
        self.chk_front.toggled.connect(lambda on: self._cover('front', on))
        self.chk_back.toggled.connect(lambda on: self._cover('back', on))
        btn_ok = QPushButton('이 번호로 (Enter)')
        btn_ok.setDefault(True)
        btn_none = QPushButton('번호 없는 쪽 (면지 등)')
        btn_skip = QPushButton('건너뛰기')
        btn_done = QPushButton('끝내기')
        btn_ok.clicked.connect(self._ok)
        btn_none.clicked.connect(self._none)
        btn_skip.clicked.connect(self._next)
        btn_done.clicked.connect(self.accept)
        side = QVBoxLayout()
        side.addWidget(self.head)
        side.addWidget(self.around)
        side.addSpacing(12)
        side.addWidget(QLabel('이 페이지의 쪽 번호'))
        side.addWidget(self.spin)
        side.addWidget(self.chk_front)
        side.addWidget(self.chk_back)
        side.addWidget(btn_ok)
        side.addSpacing(12)
        side.addWidget(btn_none)
        side.addWidget(btn_skip)
        side.addStretch(1)
        side.addWidget(btn_done)
        lay = QHBoxLayout(self)
        lay.addWidget(self.img, 1)
        lay.addLayout(side)
        self.resize(1000, 860)
        self._show()

    def _cover(self, kind, on):
        """표지·뒷표지는 둘 중 하나만. 고르면 번호 칸은 쓰지 않는다(표지 0쪽, 뒷표지는 자동)."""
        other = self.chk_back if kind == 'front' else self.chk_front
        if on:
            other.blockSignals(True)
            other.setChecked(False)
            other.blockSignals(False)
            if kind == 'front':
                self.spin.setValue(0)
        self.spin.setEnabled(not (self.chk_front.isChecked() or self.chk_back.isChecked()))

    def _show(self):
        pid = self.pids[self.i]
        img, around, guess, current, cover = self.ask(pid)
        pm = QPixmap.fromImage(to_qimage(img))
        self.img.setPixmap(pm.scaled(520, 760, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.head.setText(f'{self.i + 1} / {len(self.pids)}')
        now = {'front': '표지', 'back': '뒷표지'}.get(cover) or (f'p.{current}' if current is not None else '없음(못 읽음)')
        self.around.setText(around + f'\n지금 번호: {now}')
        for chk, kind in ((self.chk_front, 'front'), (self.chk_back, 'back')):
            chk.blockSignals(True)
            chk.setChecked(cover == kind)
            chk.blockSignals(False)
        self.spin.setValue(current if current is not None else guess)
        self.spin.setEnabled(cover is None)
        self.spin.setFocus()
        self.spin.selectAll()

    def _next(self):
        if self.i + 1 < len(self.pids):
            self.i += 1
            self._show()
        else:
            self.accept()

    def _ok(self):
        if self.chk_front.isChecked():
            value = 'front'
        elif self.chk_back.isChecked():
            value = 'back'
        else:
            value = self.spin.value()
        self.on_answer(self.pids[self.i], value)
        self._next()

    def _none(self):
        self.on_answer(self.pids[self.i], 'none')
        self._next()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._ok()
        else:
            super().keyPressEvent(e)


class ReviewDialog(QDialog):
    """'확인 필요' 페이지를 하나씩 보며 처리하는 창. 실제 처리는 본 창(win)의 기능을 그대로 쓴다
    (그래서 Ctrl+Z 로 되돌릴 수 있고, 왼쪽 목록도 지금 보는 페이지를 따라간다)."""

    def __init__(self, win, pids):
        super().__init__(win)
        self.win, self.pids, self.i = win, pids, 0
        self.rescan = []           # 재스캔할 페이지로 표시한 것
        self.setWindowTitle('문제 페이지 검토')
        self.head = QLabel()
        self.head.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.reason = QLabel()
        self.reason.setStyleSheet('color: #c62828; font-size: 14px;')
        self.reason.setWordWrap(True)
        # 기본은 결과물 한 장을 크게. 결과만으로 애매할 때만 원본과 감지 영역을 옆에 띄운다
        self.out_img, self.raw_img = QLabel(), QLabel()
        for lb in (self.out_img, self.raw_img):
            lb.setAlignment(Qt.AlignCenter)
            lb.setMinimumSize(420, 600)
        self.raw_img.hide()
        imgs = QHBoxLayout()
        imgs.addWidget(self.out_img, 1)
        imgs.addWidget(self.raw_img, 1)
        self.chk_raw = QCheckBox('원본과 감지 영역도 보기')
        self.chk_raw.toggled.connect(lambda on: (self.raw_img.setVisible(on), self._show()))

        def button(text, fn, tip=''):
            b = QPushButton(text)
            b.clicked.connect(fn)
            b.setToolTip(tip)
            return b
        self.btn_ok = button('문제없음 (Enter)', self._ok, '경고를 끄고 다음 페이지로')
        self.btn_ok.setDefault(True)
        rot = QHBoxLayout()
        for text, deg in (('↺', -90), ('↻', 90), ('180°', 180)):
            rot.addWidget(button(text, lambda _=False, d=deg: self._act(lambda: self.win._rotate_sel(d))))
        self.sp_edge = QDoubleSpinBox(suffix=' mm', maximum=8, singleStep=0.5, decimals=1)
        self.edge_timer = QTimer(self, singleShot=True, interval=350, timeout=self._apply_edge)
        self.sp_edge.valueChanged.connect(lambda *_: self.edge_timer.start())
        erow = QHBoxLayout()
        erow.addWidget(QLabel('가장자리 정리'))
        erow.addWidget(self.sp_edge)
        self.btn_rescan = button('재스캔할 페이지로 표시', self._mark_rescan, '창을 닫을 때 표시한 페이지를 한꺼번에 재스캔할지 묻습니다')
        side = QVBoxLayout()
        side.addWidget(self.btn_ok)
        side.addSpacing(10)
        side.addWidget(button('모서리 조정…', self._corners, '종이 영역을 손으로 맞추기'))
        side.addLayout(rot)
        side.addLayout(erow)
        side.addSpacing(10)
        side.addWidget(button('제외 (저장에서 빼기)', self._exclude, '목록엔 두고 저장에서만 뺍니다. 다음 페이지로'))
        side.addWidget(self.btn_rescan)
        side.addSpacing(10)
        side.addWidget(self.chk_raw)
        side.addStretch(1)
        side.addWidget(QLabel('Ctrl+Z 로 되돌릴 수 있습니다'))
        body = QHBoxLayout()
        body.addLayout(imgs, 1)
        body.addLayout(side)
        nav = QHBoxLayout()
        self.btn_prev = button('← 이전', self._prev)
        self.btn_next = button('다음 →', self._next)
        nav.addWidget(self.btn_prev)
        nav.addStretch(1)
        nav.addWidget(self.btn_next)
        nav.addWidget(button('닫기', self.accept))
        lay = QVBoxLayout(self)
        lay.addWidget(self.head)
        lay.addWidget(self.reason)
        lay.addLayout(body, 1)
        lay.addLayout(nav)
        self.resize(1150, 900)
        self._show()

    # --- 표시 ---
    def _pid(self):
        return self.pids[self.i]

    def _show(self):
        pid = self._pid()
        w = self.win
        if pid not in w.pages:            # 그사이 목록에서 빠진 페이지
            self.head.setText(f'{self.i + 1} / {len(self.pids)}')
            self.reason.setText('목록에서 빠진 페이지입니다')
            self.out_img.clear()
            self.raw_img.clear()
            return
        w._select_only([pid])
        p = w.pages[pid]
        n = w._order().index(pid) + 1
        pno = {'front': '표지', 'back': '뒷표지'}.get(p.get('cover')) or (f'p.{p["pno"]}' if p.get('pno') is not None else '')
        self.head.setText(f'{self.i + 1} / {len(self.pids)}   ·   목록 {n}번째 {pno}'
                          + ('   ·   재스캔 표시됨' if pid in self.rescan else ''))
        target = w._target()
        reasons = P.warn_reasons(p, w.opt, target)
        if P.is_excluded(p, w.opt):
            self.reason.setText('✓ 제외됨 (저장에서 빠짐)')
        else:
            self.reason.setText('⚠ ' + ' / '.join(reasons) if reasons else '✓ 해결됨')
        sc = 0.3
        raw = cv2.resize(imread(w._raw_path(p)), None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        out = P.render(raw, p, w.opt, target, sc)
        q = np.array(p['quad']) * sc
        frame = raw.copy()
        cv2.polylines(frame, [q.astype(np.int32)], True, (0, 0, 255), 2)
        m = int(max(np.ptp(q[:, 0]), np.ptp(q[:, 1])) * 0.06)
        x0, y0 = np.maximum(q.min(0) - m, 0).astype(int)
        x1, y1 = (q.max(0) + m).astype(int)
        frame = P.rotate(frame[y0:y1, x0:x1], P.page_rotation(p, w.opt))  # 결과물과 같은 방향으로 세워 비교
        both = self.chk_raw.isChecked()
        for lb, img in ((self.out_img, out), (self.raw_img, frame)):
            if lb is self.raw_img and not both:
                continue
            pm = QPixmap.fromImage(to_qimage(img))
            lb.setPixmap(pm.scaled(560 if both else 900, 780, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.sp_edge.blockSignals(True)
        self.sp_edge.setValue(p.get('edge_mm', w.opt['edge_mm']))
        self.sp_edge.blockSignals(False)
        self.btn_rescan.setText('재스캔 표시 취소' if pid in self.rescan else '재스캔할 페이지로 표시')
        self.btn_prev.setEnabled(self.i > 0)
        self.btn_next.setEnabled(self.i + 1 < len(self.pids))

    # --- 처리 (본 창 기능을 지금 페이지에 대해 실행) ---
    def _act(self, fn, then_next=False):
        if self._pid() not in self.win.pages:
            return
        self.win._select_only([self._pid()])
        fn()
        if then_next and self.i + 1 < len(self.pids):
            self.i += 1
        self._show()

    def _ok(self):
        self._act(self.win._warn_ok, then_next=True)

    def _exclude(self):
        p = self.win.pages.get(self._pid())
        if p is not None and not P.is_excluded(p, self.win.opt):
            self._act(self.win._toggle_exclude_sel, then_next=True)

    def _corners(self):
        before = self.win.pages.get(self._pid(), {}).get('quad')
        self._act(self.win._edit_corners)
        if self.win.pages.get(self._pid(), {}).get('quad') != before and self.i + 1 < len(self.pids):
            self.i += 1           # 조정을 적용했으면 다음 페이지로
            self._show()

    def _apply_edge(self):
        self._act(lambda: (self.win.sp_page_edge.setValue(self.sp_edge.value()), self.win._apply_page_edge()))

    def _mark_rescan(self):
        pid = self._pid()
        if pid in self.rescan:
            self.rescan.remove(pid)
        else:
            self.rescan.append(pid)
        self._show()

    def _prev(self):
        if self.i > 0:
            self.i -= 1
            self._show()

    def _next(self):
        if self.i + 1 < len(self.pids):
            self.i += 1
            self._show()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._ok()
        elif e.key() == Qt.Key_Right:
            self._next()
        elif e.key() == Qt.Key_Left:
            self._prev()
        elif e.matches(QKeySequence.Undo):
            self.win._undo()
            self._show()
        else:
            super().keyPressEvent(e)


class DupDialog(QDialog):
    """같은 쪽 번호가 여러 장일 때(재스캔을 통째로 한 경우 등) 나란히 보고 하나만 남기는 창.
    groups: [(쪽 번호, [(페이지 id, 이미지, 설명), ...]), ...]  각 묶음은 2~4장(넘으면 앞 4장).
    결과: self.choices = {쪽 번호: 남길 페이지 id}  (건너뛴 번호는 없음)"""

    def __init__(self, parent, groups, default_pick):
        super().__init__(parent)
        self.groups, self.default_pick = groups, default_pick
        self.choices, self.i = {}, 0
        self.title = QLabel()
        self.title.setStyleSheet('font-size: 15px; font-weight: bold;')
        self.grid = QHBoxLayout()
        self.cells_box = QWidget()
        self.cells_box.setLayout(self.grid)
        hint = QLabel('남길 페이지를 클릭하세요 (더블클릭: 바로 남기고 다음).  ← → 로 고르고 Enter 로 남기기')
        hint.setStyleSheet('color: gray;')
        self.btn_keep = QPushButton('이것만 남기기 →')
        self.btn_keep.setDefault(True)
        self.btn_skip = QPushButton('건너뛰기 (모두 둠)')
        self.btn_prev = QPushButton('← 이전')
        self.btn_done = QPushButton('끝내기')
        self.btn_keep.clicked.connect(self._keep)
        self.btn_skip.clicked.connect(self._skip)
        self.btn_prev.clicked.connect(self._prev)
        self.btn_done.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addWidget(self.btn_prev)
        row.addStretch(1)
        row.addWidget(self.btn_skip)
        row.addWidget(self.btn_keep)
        row.addWidget(self.btn_done)
        lay = QVBoxLayout(self)
        lay.addWidget(self.title)
        lay.addWidget(self.cells_box, 1)
        lay.addWidget(hint)
        lay.addLayout(row)
        self.setWindowTitle('중복 쪽 정리')
        self.resize(1300, 900)
        self._show()

    def _show(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        pno, cands = self.groups[self.i]
        cands = cands[:4]
        self.title.setText(f'p.{pno} — 같은 쪽이 {len(cands)}장 있습니다   ({self.i + 1} / {len(self.groups)})')
        cols = 2 if len(cands) > 2 else len(cands)
        # 3~4장은 2×2: 세로 두 줄을 가로 칸 안에 넣는다
        columns = [QVBoxLayout() for _ in range(cols)]
        self.cells = []
        avail_h = 760 if len(cands) <= 2 else 370
        for k, (pid, img, desc) in enumerate(cands):
            cell = QLabel()
            cell.setAlignment(Qt.AlignCenter)
            pm = QPixmap.fromImage(to_qimage(img))
            cell.setPixmap(pm.scaled(600, avail_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            cell.setCursor(Qt.PointingHandCursor)
            cell.mousePressEvent = lambda e, k=k: self._select(k)
            cell.mouseDoubleClickEvent = lambda e, k=k: (self._select(k), self._keep())
            cap = QLabel(desc)
            cap.setAlignment(Qt.AlignCenter)
            cap.setWordWrap(True)
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.addWidget(cell, 1)
            bl.addWidget(cap)
            columns[k % cols].addWidget(box)
            self.cells.append((pid, box))
        for c in columns:
            holder = QWidget()
            holder.setLayout(c)
            self.grid.addWidget(holder)
        pick = self.choices.get(pno, self.default_pick(pno, [c[0] for c in cands]))
        self._select(next((k for k, (pid, _) in enumerate(self.cells) if pid == pick), 0))
        self.btn_prev.setEnabled(self.i > 0)

    def _select(self, k):
        self.sel = k
        for j, (_, box) in enumerate(self.cells):
            box.setObjectName('cell')
            box.setStyleSheet('#cell { border: 4px solid %s; border-radius: 6px; }'
                              % ('#1e6ee6' if j == k else 'transparent'))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Left, Qt.Key_Right) and self.cells:
            self._select((self.sel + (1 if e.key() == Qt.Key_Right else -1)) % len(self.cells))
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._keep()
        else:
            super().keyPressEvent(e)

    def _next(self):
        if self.i + 1 < len(self.groups):
            self.i += 1
            self._show()
        else:
            self.accept()

    def _keep(self):
        pno = self.groups[self.i][0]
        self.choices[pno] = self.cells[self.sel][0]
        self._next()

    def _skip(self):
        self.choices.pop(self.groups[self.i][0], None)
        self._next()

    def _prev(self):
        if self.i > 0:
            self.i -= 1
            self._show()


class ZoomView(QGraphicsView):
    """Ctrl+휠로 마우스 위치를 중심으로 확대·축소."""

    def __init__(self, scene):
        super().__init__(scene)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setRenderHint(QPainter.SmoothPixmapTransform)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            f = 1.25 if e.angleDelta().y() > 0 else 0.8
            self.scale(f, f)
        else:
            super().wheelEvent(e)


class _Handle(QGraphicsEllipseItem):
    def __init__(self, on_move):
        super().__init__(-7, -7, 14, 14)
        self.on_move = on_move
        self.setBrush(QColor(255, 0, 0))
        # 확대해도 점 크기는 그대로(화면 기준 14px)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemSendsGeometryChanges
                      | QGraphicsItem.ItemIgnoresTransformations)
        self.setZValue(1)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.on_move()
        return super().itemChange(change, value)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('책 스캔 도우미')
        self.settings = QSettings('bookscan', 'bookscan-selftest' if os.environ.get('BOOKSCAN_SELFTEST') else 'bookscan')
        self.bus = Bus()
        cpu = os.cpu_count() or 4
        self.pool = ThreadPoolExecutor(max_workers=cpu)
        # 분석 프로세스는 각자 모델을 올리므로(약 200MB씩) 코어 하나는 남기고 최대 8개까지
        self.procs = ProcessPoolExecutor(max_workers=max(1, min(cpu - 1, 8)))
        self.pages = {}        # id → 페이지 정보(dict)
        self.small = {}        # id → 축소 원본(np)
        self.gen = 0           # 썸네일 세대: 옵션이 바뀌면 이전 결과는 버린다
        self.next_id = 1
        self.scanning = False
        self.stop_scan = False
        self.opt = dict(DEFAULT_OPT)
        self.undo_stack, self.redo_stack = [], []
        self.analyzing = set()      # 분석 결과를 기다리는 페이지
        self.analyze_info = {}      # page id → (시작 시각, quad 인자, 재시도 횟수)
        self.pending_place = []     # 새로 스캔해서 쪽 번호 자리를 찾아 줄 페이지(스캔 순서)
        self.clip = None  # {'mode': 'cut'|'copy', 'pids': [...]}
        self.out_dir = self.settings.value('out_dir', '') or ''

        self._build_ui()
        self.bus.analyzed.connect(self._on_analyzed)
        self.bus.thumb.connect(self._on_thumb)
        self.bus.preview.connect(self._on_preview)
        self.bus.scanned.connect(lambda path, side: self._add_page(path, side))
        self.bus.scan_done.connect(self._on_scan_done)
        self.bus.progress.connect(self._on_progress)
        self.bus.export_done.connect(self._on_export_done)
        self.bus.caps.connect(self._on_caps)
        self.bus.devices.connect(self._on_devices)
        self.bus.notice.connect(lambda m: self.status.setText(m))
        self.bus.rescan_done.connect(self._on_rescan_done)
        self.bus.analyze_failed.connect(self._on_analyze_failed)
        # 결과가 너무 오래 안 오는 분석(보조 프로세스가 멈춤 등)을 찾아 다시 시도한다
        self.watchdog = QTimer(self, interval=15000, timeout=self._check_stuck_analysis)
        self.watchdog.start()

        self.refresh_timer = QTimer(self, singleShot=True, interval=400, timeout=self._refresh_all)
        self.save_timer = QTimer(self, singleShot=True, interval=1000, timeout=self._save_project)
        if self.out_dir and os.path.isdir(self.out_dir):
            self._load_project()
        self._update_status()
        self._startup_scanner()

    # ---------- 화면 구성 ----------
    def _build_ui(self):
        tb = QToolBar('작업')
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        self.act_scan = QAction('스캔 시작', self, triggered=self._start_scan)
        self.act_stop = QAction('스캔 중지', self, triggered=self._stop_scan, enabled=False)
        tb.addAction(self.act_scan)
        tb.addAction(self.act_stop)
        tb.addSeparator()
        tb.addAction(QAction('파일 불러오기', self, triggered=self._import_files))
        tb.addSeparator()
        # 자주 쓰는 것만. 드물게 쓰는 것(순서 교정·목록에서 빼기 등)은 오른쪽 클릭 메뉴에
        act = QAction('문제 페이지 검토', self, triggered=lambda: self._review_problems())
        act.setToolTip('확인 필요 페이지를 모아 하나씩 보며 처리')
        tb.addAction(act)
        tb.addSeparator()
        for text, tip, fn in (('↺', '왼쪽으로 90° 회전', lambda: self._rotate_sel(-90)),
                              ('↻', '오른쪽으로 90° 회전', lambda: self._rotate_sel(90)),
                              ('180°', '180° 회전', lambda: self._rotate_sel(180)),
                              ('제외/포함', '목록엔 두고 저장에서만 빼기(다시 누르면 포함)', self._toggle_exclude_sel),
                              ('모서리 조정', '종이 영역을 손으로 맞추기(더블클릭과 같음)', self._edit_corners),
                              ('선택 페이지 재스캔', '고른 페이지를 다시 스캔한 것으로 바꾸기', self._rescan_selected),
                              ('순서 뒤집기', '목록 전체 순서를 거꾸로', self._reverse_order),
                              ('중복 쪽 정리', '같은 쪽 번호가 여러 장이면 비교해서 하나만 남기기',
                               lambda: self._resolve_duplicates()),
                              ('쪽 번호 입력', '번호가 없거나 못 읽은 페이지에 쪽 번호를 직접 넣기 (고른 페이지가 있으면 그것만)',
                               lambda: self._enter_pnos())):
            act = QAction(text, self, triggered=fn)
            act.setToolTip(tip)
            tb.addAction(act)
        tb.addSeparator()
        self.act_export = QAction(save_icon(), '저장하기', self, triggered=self._export)
        tb.addAction(self.act_export)
        tb.setIconSize(QSize(18, 18))
        tb.widgetForAction(self.act_export).setToolButtonStyle(Qt.ToolButtonTextBesideIcon)

        # 페이지 목록: 끌어서 순서 바꾸기
        self.list = PageList()
        self.list.setIconSize(self._icon_size())
        self.list.reordered.connect(lambda: (self._renumber(), self._schedule_save()))
        self.list.before_change.connect(self._checkpoint)
        self._build_edit_actions()
        self.list.currentItemChanged.connect(lambda *a: self._request_preview())
        self.list.caret_changed.connect(self._on_caret)
        self.list.itemDoubleClicked.connect(lambda *a: self._edit_corners())
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)

        # 미리보기
        self.preview = QLabel(
            '<div style="font-size:15px; line-height:150%">'
            '<b>사용 순서</b><br><br>'
            '1. 오른쪽 [스캔]에서 스캐너를 고릅니다 (자동으로 찾아 둡니다)<br>'
            '2. 책 종이를 피더에 넣고 위의 [스캔 시작]을 누릅니다<br>'
            '&nbsp;&nbsp;&nbsp;&nbsp;이미 스캔한 이미지가 있으면 [파일 불러오기]<br>'
            '3. 빨간 테두리가 붙은 쪽은 [문제 페이지 검토]로 확인합니다<br>'
            '4. 오른쪽 아래 [저장하기]를 누르면 PDF 한 권이 만들어집니다<br><br>'
            '<span style="color:gray">페이지를 고르면 여기에 결과가 보입니다</span></div>')
        self.preview.setWordWrap(True)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(300)
        self.chk_raw = QCheckBox('원본과 감지 영역 보기')
        self.chk_raw.toggled.connect(lambda *a: self._request_preview())
        self.preview_info = QLabel('')
        self.warn_info = QLabel('')  # 확인 표시가 뜬 이유
        self.warn_info.setStyleSheet('color: #c62828;')
        self.warn_info.setWordWrap(True)
        pv = QWidget()
        pvl = QVBoxLayout(pv)
        pvl.addWidget(self.preview, 1)
        pvl.addWidget(self.preview_info)
        pvl.addWidget(self.chk_raw)
        self.btn_warn_ok = QPushButton('문제없음')
        self.btn_warn_ok.setToolTip('이 페이지의 확인 필요 표시를 끕니다(고른 페이지 모두)')
        self.btn_warn_ok.clicked.connect(self._warn_ok)
        self.btn_warn_ok.hide()
        wrow = QHBoxLayout()
        wrow.addWidget(self.warn_info, 1)
        wrow.addWidget(self.btn_warn_ok)
        pvl.addLayout(wrow)
        erow = QHBoxLayout()
        erow.addWidget(QLabel('이 페이지 가장자리 정리'))
        self.sp_page_edge = QDoubleSpinBox(suffix=' mm', maximum=8, singleStep=0.5, decimals=1)
        self.sp_page_edge.setToolTip('고른 페이지들에만 적용됩니다')
        self.lb_edge_src = QLabel('')
        self.lb_edge_src.setStyleSheet('color: gray;')
        btn_edge_def = QPushButton('기본값으로')
        btn_edge_def.setToolTip('따로 정한 값을 지우고 오른쪽 옵션의 기본값을 따릅니다')
        erow.addWidget(self.sp_page_edge)
        erow.addWidget(self.lb_edge_src)
        erow.addStretch(1)
        erow.addWidget(btn_edge_def)
        pvl.addLayout(erow)
        # 화살표를 연달아 누를 때마다 실행 취소가 쌓이지 않게 잠깐 모았다가 한 번에 적용
        self.edge_timer = QTimer(self, singleShot=True, interval=350, timeout=self._apply_page_edge)
        self.sp_page_edge.valueChanged.connect(lambda *_: self.edge_timer.start())
        btn_edge_def.clicked.connect(lambda: self._apply_page_edge(clear=True))

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(pv)
        split.addWidget(self._build_options())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setStretchFactor(2, 0)
        self.setCentralWidget(split)

        self.status = QLabel()
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(250)
        self.bar.hide()
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.bar)
        # 기본 창 크기: 화면 작업 영역에 맞춰(오른쪽 옵션 칸에 스크롤이 생기지 않을 높이)
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1600, int(avail.width() * 0.92)), int(avail.height() * 0.94))
        self.move(avail.center() - self.rect().center())

    def _icon_size(self):
        return QSize(int(THUMB_H * 0.75), THUMB_H)

    def _build_options(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(6)
        lay.setContentsMargins(6, 6, 6, 6)

        def pair(*items):
            """짝이 되는 항목을 한 줄에(세로 공간 아끼기)."""
            row = QHBoxLayout()
            row.setSpacing(6)
            for it in items:
                if isinstance(it, str):
                    row.addWidget(QLabel(it))
                else:
                    row.addWidget(it)
            row.addStretch(1)
            return row

        g = QGroupBox('스캔')
        f = QFormLayout(g)
        self.devices = []           # 찾은 스캐너 [{'dev','name','kind'}]
        self.cb_dev = QComboBox()
        self.cb_dev.setMinimumContentsLength(10)
        self.cb_dev.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cb_dev.setToolTip('네트워크: 공유기로 연결된 복합기(드라이버 불필요)\n드라이버: USB 등 Windows 드라이버로 연결된 스캐너')
        btn_find = QPushButton('찾기')
        btn_find.setToolTip('네트워크와 이 PC에 연결된 스캐너를 다시 찾습니다')
        btn_find.clicked.connect(lambda: self._find_scanner(True))
        row = QHBoxLayout()
        row.addWidget(self.cb_dev, 1)
        row.addWidget(btn_find)
        f.addRow('스캐너', row)
        self.cb_source = QComboBox()
        self.cb_source.addItem('피더 (여러 장 자동)', 'Feeder')
        self.cb_source.addItem('평판 (유리판, 한 장씩)', 'Platen')
        f.addRow('스캔 방식', self.cb_source)
        self.chk_duplex = QCheckBox('양면')
        self.chk_place = QCheckBox('새로 스캔한 빠진 쪽을 쪽 번호 자리에 자동으로 넣기')
        self.chk_place.setToolTip('쪽 번호를 읽어, 목록에서 비어 있는 번호면 앞뒤 번호 사이로 옮깁니다')
        f.addRow(self.chk_place)
        self.cb_dpi = QComboBox()
        self.scan_caps = {'Feeder': [300, 150], 'Platen': [300, 150]}  # 복합기에 물어보면 바뀐다
        self._fill_dpi(300)
        f.addRow(pair(self.chk_duplex, '   해상도', self.cb_dpi))
        lay.addWidget(g)

        g = QGroupBox('보정')
        f = QFormLayout(g)
        self.chk_crop = QCheckBox('기울기 보정 + 종이 영역만 잘라내기')
        self.chk_uniform = QCheckBox('모든 페이지 같은 크기로 (큰 쪽 기준)')
        self.cb_mode = QComboBox()
        self.cb_mode.addItem('제본 쪽에 종이 덧대기 (내용 크기 유지)', 'pad')
        self.cb_mode.addItem('이미지 늘리기 (내용도 늘어남)', 'stretch')
        self.cb_spine = QComboBox()
        self.cb_spine.addItem('자동 (앞면은 왼쪽, 뒷면은 오른쪽)', 'auto')
        self.cb_spine.addItem('반대로 (앞면은 오른쪽)', 'swap')
        self.cb_spine.addItem('양쪽 똑같이', 'center')
        self.sp_margin = QDoubleSpinBox(suffix=' mm', maximum=30, singleStep=1, decimals=1)
        f.addRow(self.chk_crop)
        f.addRow(self.chk_uniform)
        f.addRow('맞추는 방식', self.cb_mode)
        f.addRow('제본 쪽', self.cb_spine)
        self.sp_edge = QDoubleSpinBox(suffix=' mm', maximum=8, singleStep=0.5, decimals=1)
        self.sp_edge.setToolTip('감지 오차로 남는 배경판 띠·그림자 선을 가리려고 가장자리를 바탕색으로 덮습니다')
        self.cb_sharpen = QComboBox()
        for lv, text in ((0, '끔'), (1, '약하게'), (2, '보통'), (3, '강하게')):
            self.cb_sharpen.addItem(text, lv)
        f.addRow('가장자리 정리', pair(self.sp_edge, ' 선명도', self.cb_sharpen))
        self.sp_edge.setToolTip('가장자리 정리 기본값(페이지별로는 미리보기 아래에서)\n'
                                '감지 오차로 남는 배경판 띠·그림자 선을 바탕색으로 덮습니다')
        f.addRow('여백 추가', self.sp_margin)
        lay.addWidget(g)

        g = QGroupBox('방향')
        f = QFormLayout(g)
        self.chk_auto_ori = QCheckBox('자동 판별 (앞면·뒷면별 다수결)')
        self.chk_auto_ori.setEnabled(orient.available())
        f.addRow(self.chk_auto_ori)
        self.cb_rot_front, self.cb_rot_back = QComboBox(), QComboBox()
        for cb in (self.cb_rot_front, self.cb_rot_back):
            for deg, text in ROT_CHOICES:
                cb.addItem(text, deg)
            cb.setToolTip('자동 판별을 끄면 이 값을 씁니다(양면 스캔은 앞뒷면 방향이 반대)')
        f.addRow(pair('앞면', self.cb_rot_front, ' 뒷면', self.cb_rot_back))
        lay.addWidget(g)

        g = QGroupBox('빈 페이지')
        f = QFormLayout(g)
        self.chk_blank = QCheckBox('자동으로 제외 — 내용이')
        self.sp_blank = QDoubleSpinBox(suffix=' %', minimum=0.01, maximum=5, singleStep=0.05, decimals=2)
        f.addRow(pair(self.chk_blank, self.sp_blank, '미만'))
        lay.addWidget(g)

        g = QGroupBox('저장')
        f = QFormLayout(g)
        self.lb_out = QLabel()
        self.lb_out.setWordWrap(True)
        btn_out = QPushButton('저장 폴더 변경')
        btn_out.clicked.connect(self._choose_out_dir)
        btn_open = QPushButton('저장 폴더 열기')
        btn_open.clicked.connect(self._open_out_dir)
        f.addRow(self.lb_out)
        row = QHBoxLayout()
        row.addWidget(btn_out)
        row.addWidget(btn_open)
        f.addRow(row)
        self.ed_name = QLineEdit()
        f.addRow('파일 이름', self.ed_name)
        self.chk_pdf, self.chk_png = QCheckBox('PDF'), QCheckBox('PNG (페이지별)')
        self.cb_pdfq = QComboBox()
        for text, key in (('기본', 'q95'), ('최고', 'q100'), ('보통', 'q85'), ('작게', 'q75'), ('무손실', 'png')):
            self.cb_pdfq.addItem(text, key)
        self.cb_pdfq.setToolTip('PDF 용량·화질 (300dpi 컬러 210쪽 책 예시)\n기본: JPEG 품질 95 (약 120MB)\n'
                                '최고: 100 (약 250MB)\n보통: 85 (약 70MB)\n작게: 75 (약 50MB)\n무손실: PNG (훨씬 큼)')
        self.cb_pdfq.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        f.addRow(pair(self.chk_pdf, self.cb_pdfq, ' ', self.chk_png))
        self.btn_save = QPushButton(save_icon(), ' 저장하기')
        self.btn_save.setIconSize(QSize(16, 16))
        self.btn_save.clicked.connect(lambda: self._export())
        f.addRow(self.btn_save)
        lay.addWidget(g)
        lay.addStretch(1)

        self._opt_to_ui()
        for wdg in (self.chk_place, self.chk_auto_ori, self.chk_crop, self.chk_uniform, self.chk_blank, self.chk_duplex, self.chk_pdf, self.chk_png):
            wdg.toggled.connect(self._ui_to_opt)
        self.cb_source.currentIndexChanged.connect(lambda *_: self._fill_dpi())  # 해상도 목록 먼저 바꾼 뒤 옵션 저장
        for wdg in (self.cb_source, self.cb_mode, self.cb_sharpen, self.cb_spine, self.cb_rot_front, self.cb_rot_back, self.cb_dpi, self.cb_pdfq):
            wdg.currentIndexChanged.connect(self._ui_to_opt)
        for wdg in (self.sp_margin, self.sp_edge, self.sp_blank):
            wdg.valueChanged.connect(self._ui_to_opt)
        self.ed_name.editingFinished.connect(self._ui_to_opt)
        self.cb_dev.activated.connect(self._on_dev_chosen)

        sc = QScrollArea()
        sc.setWidget(w)
        sc.setWidgetResizable(True)
        sc.setMinimumWidth(400)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return sc

    def _opt_to_ui(self):
        o = self.opt
        self._fill_devices()
        self.cb_source.setCurrentIndex(max(self.cb_source.findData(o['source']), 0))
        self.chk_duplex.setChecked(o['duplex'])
        self.chk_place.setChecked(o['auto_place'])
        self._fill_dpi(o['dpi'])
        self.cb_dpi.setCurrentIndex(max(self.cb_dpi.findData(o['dpi']), 0))
        self.chk_crop.setChecked(o['crop'])
        self.chk_uniform.setChecked(o['uniform'])
        self.cb_mode.setCurrentIndex(max(self.cb_mode.findData(o['uniform_mode']), 0))
        self.cb_spine.setCurrentIndex(max(self.cb_spine.findData(o['spine']), 0))
        self.sp_margin.setValue(o['margin_mm'])
        self.sp_edge.setValue(o['edge_mm'])
        self.cb_sharpen.setCurrentIndex(max(self.cb_sharpen.findData(o['sharpen']), 0))
        self.chk_auto_ori.setChecked(o['auto_orient'] and orient.available())
        self.cb_rot_front.setCurrentIndex(max(self.cb_rot_front.findData(o['rot_front']), 0))
        self.cb_rot_back.setCurrentIndex(max(self.cb_rot_back.findData(o['rot_back']), 0))
        self.chk_blank.setChecked(o['skip_blank'])
        self.sp_blank.setValue(o['blank_threshold'])
        self.ed_name.setText(o['name'])
        self.chk_pdf.setChecked(o['save_pdf'])
        self.chk_png.setChecked(o['save_png'])
        # 예전 값('jpeg' = 품질 95)은 목록에 없으니 첫 항목(기본, 95)으로
        self.cb_pdfq.setCurrentIndex(max(self.cb_pdfq.findData(o['pdf_quality']), 0))
        self.lb_out.setText(f'저장 폴더: {self.out_dir or "(아직 안 정함)"}')

    def _ui_to_opt(self, *_):
        old = dict(self.opt)
        self.opt.update(
            escl_url=self._cur_dev(), device_name=self._cur_dev_name(), source=self.cb_source.currentData(), duplex=self.chk_duplex.isChecked(), auto_place=self.chk_place.isChecked(),
            dpi=self.cb_dpi.currentData(), crop=self.chk_crop.isChecked(), uniform=self.chk_uniform.isChecked(),
            uniform_mode=self.cb_mode.currentData(), spine=self.cb_spine.currentData(), margin_mm=self.sp_margin.value(), edge_mm=self.sp_edge.value(), sharpen=self.cb_sharpen.currentData(),
            auto_orient=self.chk_auto_ori.isChecked(), rot_front=self.cb_rot_front.currentData(), rot_back=self.cb_rot_back.currentData(),
            skip_blank=self.chk_blank.isChecked(), blank_threshold=self.sp_blank.value(),
            name=self.ed_name.text().strip() or 'book', save_pdf=self.chk_pdf.isChecked(),
            save_png=self.chk_png.isChecked(), pdf_quality=self.cb_pdfq.currentData())
        look = ('crop', 'edge_mm', 'sharpen', 'uniform', 'uniform_mode', 'spine', 'margin_mm', 'auto_orient', 'rot_front', 'rot_back', 'skip_blank', 'blank_threshold')
        if any(old[k] != self.opt[k] for k in look):
            self.refresh_timer.start()
        self._schedule_save()

    # ---------- 프로젝트(작업 폴더) ----------
    def _work_dir(self):
        return os.path.join(self.out_dir, WORK_DIRNAME)

    def _ensure_out_dir(self):
        if self.out_dir and os.path.isdir(self.out_dir):
            return True
        QMessageBox.information(self, '저장 폴더', '먼저 결과를 저장할 폴더를 정해 주세요.\n스캔 원본도 그 안의 "_스캔작업" 폴더에 보관됩니다.')
        return self._choose_out_dir()

    def _choose_out_dir(self):
        start = self.out_dir or QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation) or os.path.expanduser('~')
        d = QFileDialog.getExistingDirectory(self, '저장 폴더 선택 (책마다 폴더를 따로 만들면 편합니다)', start)
        if not d:
            return False
        if self.pages and os.path.normcase(d) != os.path.normcase(self.out_dir):
            if QMessageBox.question(self, '저장 폴더 변경',
                                    '지금 작업 중인 페이지를 새 폴더로 옮길까요?\n'
                                    '"아니요"를 누르면 새 폴더의 작업을 엽니다(없으면 빈 목록).') == QMessageBox.Yes:
                os.makedirs(os.path.join(d, WORK_DIRNAME, 'raw'), exist_ok=True)
                for p in self.pages.values():
                    dst = os.path.join(d, WORK_DIRNAME, 'raw', os.path.basename(p['raw']))
                    shutil.copy2(self._raw_path(p), dst)
                self.out_dir = d
                self.settings.setValue('out_dir', d)
                self._opt_to_ui()
                self._save_project()
                return True
        self.out_dir = d
        self.settings.setValue('out_dir', d)
        self._clear()
        self._load_project()
        self._opt_to_ui()
        return True

    def _open_out_dir(self):
        """탐색기로 저장 폴더 열기."""
        if self.out_dir and os.path.isdir(self.out_dir):
            os.startfile(self.out_dir)
        else:
            QMessageBox.information(self, '저장 폴더', '아직 저장 폴더를 정하지 않았습니다.')

    def _raw_path(self, p):
        return os.path.join(self._work_dir(), 'raw', p['raw'])

    def _clear(self):
        self.list.clear()
        self.pages.clear()
        self.small.clear()
        self.gen += 1

    def _load_project(self):
        path = os.path.join(self._work_dir(), 'project.json')
        if not os.path.exists(path):
            return
        try:
            data = json.load(open(path, encoding='utf-8'))
        except Exception as e:
            QMessageBox.warning(self, '불러오기 실패', f'작업 파일을 읽지 못했습니다:\n{e}')
            return
        self.opt.update({k: v for k, v in data.get('opt', {}).items() if k in DEFAULT_OPT})
        if not data.get('opt', {}).get('edge_per_page'):  # 페이지별 설정 이전 작업: 기본값 0.5mm 로
            self.opt['edge_mm'] = DEFAULT_OPT['edge_mm']
        self.opt['edge_per_page'] = True
        self.next_id = data.get('next_id', 1)
        for p in data.get('pages', []):
            if not os.path.exists(self._raw_path(p)):
                continue
            self.pages[p['id']] = p
            self._new_item(p['id'])
            # 감지 방식이 바뀌었으면 다시 감지(손으로 조정한 모서리는 유지)
            keep = p.get('manual_quad') or p.get('det_v') == DETECT_VERSION
            self._analyze(p['id'], quad=p.get('quad') if keep else None)
        self._opt_to_ui()
        self._renumber()

    def _schedule_save(self):
        if self.out_dir:
            self.save_timer.start()

    def _save_project(self):
        if not self.out_dir:
            return
        os.makedirs(self._work_dir(), exist_ok=True)
        data = dict(opt=self.opt, next_id=self.next_id, pages=[self.pages[i] for i in self._order()])
        tmp = os.path.join(self._work_dir(), 'project.json.tmp')
        json.dump(data, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self._work_dir(), 'project.json'))

    # ---------- 페이지 추가 / 분석 ----------
    def _order(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def _new_item(self, pid):
        it = QListWidgetItem('…')
        it.setData(Qt.UserRole, pid)
        it.setTextAlignment(Qt.AlignHCenter)
        it.setSizeHint(self._icon_size() + QSize(16, 30))
        self.list.addItem(it)
        return it

    def _item(self, pid):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == pid:
                return self.list.item(i)
        return None

    def _add_page(self, path, side):
        """path 는 이미 작업 폴더 raw/ 안에 있는 파일."""
        pid = f'p{self.next_id:05d}'
        self.next_id += 1
        self.pages[pid] = dict(id=pid, raw=os.path.basename(path), side=side, rot=0, excluded=None,
                               dpi=self.opt['dpi'], quad=None, auto_quad=None, conf=0.0, ink=1.0)
        self._new_item(pid)
        if self.opt.get('auto_place'):
            self.pending_place.append(pid)
        self._analyze(pid)
        self._renumber()
        self._schedule_save()

    def _analyze(self, pid, quad=None, retry=0):
        """분석은 프로세스 풀에서, 결과는 화면 스레드로. quad 를 주면 감지는 건너뛴다.
        실패하면(파일 손상, 보조 프로세스가 죽음 등) 두 번까지 다시 하고, 그래도 안 되면 '분석 실패'로 표시한다.
        (예전엔 오류가 로그에만 남아 그 페이지가 영원히 '분석 중'으로 남고 저장도 막혔다)"""
        p = self.pages[pid]
        self.analyzing.add(pid)
        self.analyze_info[pid] = (time.time(), quad, retry)
        args = (worker.analyze, self._raw_path(p), p['dpi'], SMALL, quad, p.get('side') != 'flat',
                self.opt.get('pno_band'))
        try:
            fut = self.procs.submit(*args)
        except Exception:                 # 풀이 망가졌다(보조 프로세스가 죽음) → 새로 만들고 한 번 더
            traceback.print_exc()
            self._restart_procs()
            try:
                fut = self.procs.submit(*args)
            except Exception as e:
                msg = f'{type(e).__name__}: {e}'
                QTimer.singleShot(0, lambda: self._on_analyze_failed(pid, msg))
                return

        def done(f):
            try:
                self.bus.analyzed.emit(pid, f.result())
            except BaseException as e:
                traceback.print_exc()
                self.bus.analyze_failed.emit(pid, f'{type(e).__name__}: {e}')
        fut.add_done_callback(done)

    def _restart_procs(self):
        """분석 프로세스 풀을 새로 만든다(짧은 시간에 여러 번 부르면 한 번만)."""
        if time.time() - getattr(self, '_procs_restarted', 0) < 5:
            return
        self._procs_restarted = time.time()
        old = self.procs
        cpu = os.cpu_count() or 4
        self.procs = ProcessPoolExecutor(max_workers=max(1, min(cpu - 1, 8)))
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _on_analyze_failed(self, pid, err):
        if pid not in self.analyzing:
            return                      # 이미 처리됨(늦게 온 결과 등)
        self.analyzing.discard(pid)
        p = self.pages.get(pid)
        if not p:
            return
        _, quad, retry = self.analyze_info.get(pid, (0, None, 0))
        if 'BrokenProcessPool' in err or 'terminated abruptly' in err or '응답 없음' in err:
            self._restart_procs()
        if retry < 2:
            QTimer.singleShot(1000 * (retry + 1),
                              lambda: pid in self.pages and not self.pages[pid].get('quad') and pid not in self.analyzing
                              and self._analyze(pid, quad, retry + 1))
            return
        print(f'분석 실패: {p.get("raw")} — {err}', flush=True)
        p['analysis_error'] = err[:300]
        self._update_status()

    def _check_stuck_analysis(self):
        now = time.time()
        for pid in list(self.analyzing):
            started = self.analyze_info.get(pid, (now,))[0]
            if now - started > 180:      # 한 장 분석은 보통 몇 초. 3분이면 멈춘 것
                self._on_analyze_failed(pid, '응답 없음(분석이 3분 넘게 끝나지 않음)')

    def _on_analyzed(self, pid, res):
        self.analyzing.discard(pid)
        self.analyze_info.pop(pid, None)
        p = self.pages.get(pid)
        if not p:
            return
        p.pop('analysis_error', None)
        if 'conf' in res:
            p['quad'] = p['auto_quad'] = res['quad']
            p['conf'], p['top_ok'], p['ink'] = res['conf'], res['top_ok'], res['ink']
            p['det_v'], p['side_conf'] = res['det_v'], res['side_conf']
            p.update(auto_conf=res['conf'], auto_side_conf=res['side_conf'], auto_top_ok=res['top_ok'], from_mate=False)
        if 'img_w' in res:
            p['img_w'] = res['img_w']
            p.setdefault('auto_conf', p.get('conf', 0.0))
            p.setdefault('auto_side_conf', p.get('side_conf'))
            p.setdefault('auto_top_ok', p.get('top_ok', True))
        # 양면: 한 면만으로 가장자리를 알 수 없으면(흰 배경판 위 흰 종이 등) 같은 종이 다른 면의 영역을 쓴다
        P.reconcile_sheets(list(self.pages.values()))
        if 'pno_cands' in res:
            p['pno_cands'], p['pno_rot'] = res['pno_cands'], res['pno_rot']
        if 'ori' in res:
            p['ori'] = res['ori']
            P.assign_auto_rotation(list(self.pages.values()))  # 다수결이 바뀌면 다른 페이지도 영향
        self.small[pid] = res['small']
        self._render_thumb(pid)
        self.refresh_timer.start()  # 통일 크기가 바뀌었을 수 있으니 전체를 한 번 다시
        self._schedule_save()
        self._update_status()

    # ---------- 썸네일 / 미리보기 ----------
    def _target(self):
        ready = [p for p in self.pages.values() if p.get('quad')]
        return P.target_size(ready, self.opt)

    def _render_thumb(self, pid, target=None):
        if pid in self.small:
            target = target if target is not None else self._target()
            self.pool.submit(self._job_thumb, pid, self.gen, dict(self.pages[pid]), dict(self.opt), target)

    def _pages_changed(self, pids):
        """몇 페이지만 바뀌었을 때. 통일 크기가 그대로면 그 페이지들만 다시 그리고 번호·상태만 고친다.
        (예전엔 매번 썸네일 전체를 다시 그려 '문제없음' 한 번에 화면이 1초 넘게 멈칫했다)
        통일 크기가 바뀌면 모든 페이지 모양이 바뀌므로 전체를 다시 그린다."""
        target = self._target()
        last = getattr(self, 'last_target', None)
        same = (target is None and last is None) or (
            target is not None and last is not None and all(abs(a - b) < 0.5 for a, b in zip(target, last)))
        if not same:
            self.refresh_timer.start()
            return
        for pid in pids:
            if pid in self.pages:
                self._render_thumb(pid, target)
        self._assign_pnos()
        self._renumber()
        self._request_preview()
        self._update_status()

    def _job_thumb(self, pid, gen, page, opt, target):
        try:
            img = P.render(self.small[pid], page, opt, target, SMALL)
            h, w = img.shape[:2]
            s = THUMB_H / max(h, w / 0.75)
            img = cv2.resize(img, (max(int(w * s), 1), max(int(h * s), 1)), interpolation=cv2.INTER_AREA)
            warn = bool(P.warn_reasons(page, opt, target))
            if P.is_excluded(page, opt):
                img = cv2.addWeighted(cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR), .35,
                                      np.full_like(img, 255), .65, 0)
            elif warn:
                cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), (0, 0, 230), 4)
            self.bus.thumb.emit(pid, gen, img)
        except Exception:
            traceback.print_exc()

    def _on_thumb(self, pid, gen, img):
        if gen != self.gen:
            return
        it = self._item(pid)
        if it:
            it.setIcon(QIcon(QPixmap.fromImage(to_qimage(img))))
        if self.list.currentItem() is it:
            self._request_preview()

    def _assign_pnos(self):
        order = [self.pages[pid] for pid in self._order() if pid in self.pages]
        band, support = pagenum.assign(order, [P.page_rotation(p, self.opt) for p in order])
        # 쪽 번호 자리가 충분히(10쪽 이상) 확인되면 책에 기억해 두고, 이후 분석은 그 띠만 읽는다
        if band and support >= 10 and band != self.opt.get('pno_band'):
            self.opt['pno_band'] = band
            self._schedule_save()

    def _refresh_all(self):
        self._assign_pnos()
        if self.pending_place and not (self.analyzing & set(self.pending_place)) and not self.scanning:
            self._place_pending()
        self.gen += 1
        target = self.last_target = self._target()   # 210장마다 다시 계산하지 않게 한 번만
        for pid in self._order():
            self._render_thumb(pid, target)
        self._renumber()
        self._request_preview()
        self._update_status()

    def _renumber(self):
        n = 0
        target = self._target()
        for i in range(self.list.count()):
            it = self.list.item(i)
            p = self.pages[it.data(Qt.UserRole)]
            if P.is_excluded(p, self.opt):
                it.setText('제외' + (' (빈 페이지)' if p.get('excluded') is None else ''))
            else:
                n += 1
                warn = bool(P.warn_reasons(p, self.opt, target))
                cover = {'front': ' · 표지', 'back': ' · 뒷표지'}.get(p.get('cover'))
                it.setText(f'{n}' + (cover or (f' · p.{p["pno"]}' if p.get('pno') is not None else ''))
                           + ('  ⚠ 확인' if warn else ''))

    def _request_preview(self):
        it = self.list.currentItem()
        if not it:
            return
        pid = it.data(Qt.UserRole)
        if pid not in self.small or not self.pages[pid].get('quad'):
            return
        self.pool.submit(self._job_preview, pid, self.gen, dict(self.pages[pid]), dict(self.opt), self._target(),
                         self.chk_raw.isChecked())

    def _job_preview(self, pid, gen, page, opt, target, show_raw):
        try:
            sc = 0.35
            raw = cv2.resize(imread(self._raw_path(page)), None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            if show_raw:
                img = raw.copy()
                q = np.array(page['quad']) * sc
                cv2.polylines(img, [q.astype(np.int32)], True, (0, 0, 255), 2)
                # 아래쪽 빈 공간은 빼고 종이 주변(+여유 6%)만 보여 준다
                m = int(max(np.ptp(q[:, 0]), np.ptp(q[:, 1])) * 0.06)
                x0, y0 = (np.maximum(q.min(0) - m, 0)).astype(int)
                x1, y1 = (q.max(0) + m).astype(int)
                img = img[y0:y1, x0:x1]
            else:
                img = P.render(raw, page, opt, target, sc)
            self.bus.preview.emit(pid, gen, img)
        except Exception:
            traceback.print_exc()

    def _on_preview(self, pid, gen, img):
        it = self.list.currentItem()
        if not it or it.data(Qt.UserRole) != pid:
            return
        pm = QPixmap.fromImage(to_qimage(img))
        self.preview.setPixmap(pm.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        p = self.pages[pid]
        w, h = P.quad_size(P.effective_quad(p, self.opt, self._target()))
        mm = 25.4 / p['dpi']
        self.preview_info.setText(f'종이 {w * mm:.1f} × {h * mm:.1f} mm   감지 신뢰도 {p["conf"]:.2f}   '
                                  f'내용 {p["ink"] * 100:.2f}%   {"앞면" if p["side"] == "front" else "뒷면" if p["side"] == "back" else ""}')
        reasons = P.warn_reasons(p, self.opt, self._target())
        self._show_page_edge(p)
        show = bool(reasons) and not P.is_excluded(p, self.opt)
        self.warn_info.setText('⚠ ' + ' / '.join(reasons) if show else '')
        self.btn_warn_ok.setVisible(show)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._request_preview()

    # ---------- 페이지 편집 ----------
    def _selected(self):
        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    # 실행 취소: 바꾸기 직전의 (페이지 정보, 순서)를 쌓아 둔다. 썸네일 원본(self.small)은 지우지 않아
    # 되살린 페이지도 바로 다시 그릴 수 있다.
    UNDO_DEPTH = 50

    def _snapshot(self):
        return json.loads(json.dumps(self.pages)), self._order()

    def _checkpoint(self):
        self.undo_stack.append(self._snapshot())
        del self.undo_stack[:-self.UNDO_DEPTH]
        self.redo_stack.clear()

    def _restore(self, snap):
        pages, order = snap
        cur = self.list.currentItem().data(Qt.UserRole) if self.list.currentItem() else None
        sel = self._selected()
        self._clear_cut()
        self.pages = pages
        self.list.clear()
        for pid in order:
            self._new_item(pid)
        for pid in order:
            if pid not in self.small and pid in self.pages:
                self._analyze(pid, quad=self.pages[pid].get('quad'))
        # 보고 있던 페이지·선택을 그대로(되살아난 페이지면 그 페이지를)
        keep = [pid for pid in sel if pid in self.pages]
        if keep:
            self._select_only(keep)
        if cur in self.pages:
            self.list.setCurrentItem(self._item(cur), QItemSelectionModel.NoUpdate)
        self._refresh_all()
        self._schedule_save()

    def _undo(self):
        if self.undo_stack:
            self.redo_stack.append(self._snapshot())
            self._restore(self.undo_stack.pop())

    def _redo_edit(self):
        if self.redo_stack:
            self.undo_stack.append(self._snapshot())
            self._restore(self.redo_stack.pop())

    def _rotate_sel(self, deg):
        pids = self._selected()
        if not pids:
            return
        self._checkpoint()
        for pid in self._selected():
            self.pages[pid]['rot'] = (self.pages[pid].get('rot', 0) + deg) % 360
        self._pages_changed(pids)
        self._schedule_save()

    def _show_page_edge(self, p):
        own = p.get('edge_mm')
        self.sp_page_edge.blockSignals(True)
        self.sp_page_edge.setValue(own if own is not None else self.opt['edge_mm'])
        self.sp_page_edge.blockSignals(False)
        self.lb_edge_src.setText('(이 페이지만)' if own is not None else '(기본값)')

    def _apply_page_edge(self, clear=False):
        """고른 페이지(없으면 지금 페이지)의 가장자리 정리 값을 바꾼다."""
        pids = self._selected() or ([self.list.currentItem().data(Qt.UserRole)] if self.list.currentItem() else [])
        if not pids:
            return
        value = None if clear else round(self.sp_page_edge.value(), 1)
        if all(self.pages[pid].get('edge_mm') == value for pid in pids):
            return
        self._checkpoint()
        for pid in pids:
            if value is None:
                self.pages[pid].pop('edge_mm', None)
            else:
                self.pages[pid]['edge_mm'] = value
            self._render_thumb(pid)
        self._request_preview()
        self._schedule_save()

    def _warn_ok(self):
        """'문제없음': 고른 페이지(없으면 지금 페이지)의 확인 필요 표시를 끈다. 재스캔하면 다시 판단한다."""
        pids = self._selected() or ([self.list.currentItem().data(Qt.UserRole)] if self.list.currentItem() else [])
        if not pids:
            return
        self._checkpoint()
        for pid in pids:
            self.pages[pid]['warn_ok'] = True
        self._pages_changed(pids)
        self._schedule_save()

    def _toggle_exclude_sel(self):
        pids = self._selected()
        if not pids:
            return
        self._checkpoint()
        for pid in self._selected():
            p = self.pages[pid]
            p['excluded'] = not P.is_excluded(p, self.opt)
        self._pages_changed(pids)
        self._schedule_save()

    def _auto_exclude_sel(self):
        pids = self._selected()
        self._checkpoint()
        for pid in self._selected():
            self.pages[pid]['excluded'] = None
        self._pages_changed(pids)
        self._schedule_save()

    def _remove_sel(self):
        """목록에서 빼기. Ctrl+Z 로 되돌릴 수 있어 따로 묻지 않는다(원본 파일은 남는다)."""
        sel = self._selected()
        if not sel:
            return
        self._checkpoint()
        row = self.list.row(self._item(sel[0]))
        for pid in sel:
            self.list.takeItem(self.list.row(self._item(pid)))
            self.pages.pop(pid, None)
        if self.list.count():
            self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._pages_changed([])
        self._schedule_save()
        self._update_status(f'{len(sel)}장을 목록에서 뺐습니다 (Ctrl+Z 로 되돌리기)')

    def _apply_order(self, order):
        """목록을 order(페이지 id 순서)로 다시 배치."""
        items = {self.list.item(i).data(Qt.UserRole): self.list.item(i) for i in range(self.list.count())}
        cur = [pid for pid in self._order()]
        if cur == order:
            return
        taken = [self.list.takeItem(0) for _ in range(self.list.count())]
        for pid in order:
            self.list.addItem(items[pid])
        self._renumber()
        self._schedule_save()

    def _place_pages(self, pids, gaps_only):
        """pids 를 쪽 번호에 맞는 자리로 옮긴다. 옮기지 못한 페이지는 지금 자리 그대로.
        - 같은 종이의 번호 없는 면(빈 뒷면 등)은 번호 있는 면을 따라간다.
        - 다른 페이지에 이미 같은 번호가 있으면 옮기지 않는다(중복).
        - gaps_only: 기존 번호 범위 '안쪽'의 빈 번호만(새 스캔 자동 배치용 — 책 뒷부분을 이어서
          스캔한 경우는 스캔한 자리 그대로 둔다).
        반환: (옮긴 번호들, 중복 번호들, 번호를 못 읽은 장수)"""
        order = self._order()
        chosen = [pid for pid in order if pid in set(pids)]
        groups = []
        for pid in chosen:
            key = P._sheet_key(self.pages[pid])
            if (self.pages[pid].get('pno') is None and groups and key is not None
                    and P._sheet_key(self.pages[groups[-1][0]]) == key):
                groups[-1].append(pid)
            else:
                groups.append([pid])
        others = [pid for pid in order if pid not in set(chosen)]
        have = {self.pages[pid]['pno'] for pid in others if self.pages[pid].get('pno') is not None}
        self.place_already = []
        if not have:
            return [], [], 0  # 기준이 될 번호가 없다(처음 스캔 등)
        lo, hi = min(have), max(have)
        moving, dup, unread = [], [], 0
        for g in groups:
            pno = self.pages[g[0]].get('pno')
            if pno is None:
                unread += len(g)
            elif pno in have:
                dup.append(pno)
            elif not gaps_only or lo < pno < hi:
                moving.append(g)
                have.add(pno)
        if not moving:
            return [], dup, unread
        movers = {pid for g in moving for pid in g}
        new = [pid for pid in order if pid not in movers]
        for g in sorted(moving, key=lambda g: self.pages[g[0]]['pno']):
            at = pagenum.place_index([self.pages[x] for x in new], self.pages[g[0]]['pno'])
            new[at:at] = g
        # 실제로 자리가 바뀐 것만 '옮겼다'고 한다: 함께 옮긴 페이지는 건너뛰고, 바로 앞의 '안 옮긴'
        # 페이지가 그대로면 이미 제자리였던 것
        def prev(seq, pid):
            i = seq.index(pid) - 1
            while i >= 0 and seq[i] in movers:
                i -= 1
            return seq[i] if i >= 0 else None
        changed = [g for g in moving if prev(order, g[0]) != prev(new, g[0])]
        self.place_already = sorted(self.pages[g[0]]['pno'] for g in moving if g not in changed)
        if not changed:
            return [], dup, unread
        self._checkpoint()
        self._apply_order(new)
        self._select_only(sorted({pid for g in changed for pid in g}, key=new.index))
        return sorted(self.pages[g[0]]['pno'] for g in changed), dup, unread

    def _place_pending(self):
        """새로 스캔·불러온 페이지 중 기존 목록에 빠져 있던 번호면 제자리로. 이미 있는 번호면
        재스캔을 통째로 한 것으로 보고, 나란히 비교해 하나만 남길지 묻는다."""
        pending = [pid for pid in self.pending_place if pid in self.pages]
        self.pending_place = []
        moved, dup, _ = self._place_pages(pending, gaps_only=True)
        if moved:
            QMessageBox.information(self, '쪽 번호로 자리 찾기',
                                    '새로 스캔한 쪽을 제자리에 넣었습니다: ' + ', '.join(f'p.{v}' for v in moved)
                                    + '\n\n(Ctrl+Z 로 되돌릴 수 있습니다)')
        if dup:
            shown = ', '.join(f'p.{v}' for v in dup[:8]) + (' …' if len(dup) > 8 else '')
            if QMessageBox.question(self, '중복 쪽', f'새로 스캔한 쪽 중 {len(dup)}쪽은 이미 있는 번호입니다.\n{shown}\n\n'
                                    "나란히 비교해서 하나만 남길까요?\n(나중에 툴바의 '중복 쪽 정리'로도 할 수 있습니다)"
                                    ) == QMessageBox.Yes:
                self._resolve_duplicates(set(dup))

    def _fix_selected_order(self):
        """고른 페이지만 쪽 번호에 맞는 자리로 옮긴다. 나머지 페이지는 그대로."""
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, '선택 페이지 순서 교정', '옮길 페이지를 먼저 고르세요(여러 장 가능).')
            return
        moved, dup, unread = self._place_pages(sel, gaps_only=False)
        lines = []
        if moved:
            lines.append('쪽 번호 자리로 옮겼습니다: ' + ', '.join(f'p.{v}' for v in moved))
        if dup:
            lines.append('다른 페이지에 이미 같은 번호가 있어 그대로 둔 쪽: ' + ', '.join(f'p.{v}' for v in dup))
        if self.place_already:
            lines.append('이미 제자리에 있는 쪽: ' + ', '.join(f'p.{v}' for v in self.place_already))
        if unread:
            lines.append(f'쪽 번호를 읽지 못해 그대로 둔 페이지: {unread}장')
        if not lines:
            lines.append('옮길 페이지가 없습니다(쪽 번호를 읽은 다른 페이지가 없음).')
        QMessageBox.information(self, '선택 페이지 순서 교정',
                                '\n'.join(lines) + ('\n\n(Ctrl+Z 로 되돌릴 수 있습니다)' if moved else ''))

    # ---------- 중복 쪽 정리: 같은 쪽 번호가 여러 장이면 나란히 보고 하나만 남긴다 ----------
    def _dup_groups(self, only=None):
        """[(쪽 번호, [페이지 id...], 본문 자리 페이지 id)] — 본문 자리 = 앞뒤 번호가 이어지는 쪽."""
        order = [pid for pid in self._order() if pid in self.pages]
        nums = [(i, self.pages[pid]['pno']) for i, pid in enumerate(order) if self.pages[pid].get('pno') is not None]
        run = dict(zip((i for i, _ in nums), pagenum._runs(nums)))
        by = {}
        for i, v in nums:
            by.setdefault(v, []).append(i)
        out = []
        for v in sorted(by):
            if len(by[v]) > 1 and (only is None or v in only):
                body = max(by[v], key=lambda i: run[i])
                out.append((v, [order[i] for i in by[v]], order[body]))
        return out

    def _resolve_duplicates(self, only=None):
        if not isinstance(only, (set, list, tuple)):  # 버튼에서 오면 Qt 가 눌림 상태(bool)를 넘긴다
            only = None
        groups = self._dup_groups(only)
        if not groups:
            QMessageBox.information(self, '중복 쪽 정리', '같은 쪽 번호가 여러 장인 곳이 없습니다.')
            return
        target = self._target()
        sc = 0.3

        def render(pid):
            p = self.pages[pid]
            raw = cv2.resize(imread(self._raw_path(p)), None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
            return P.render(raw, p, self.opt, target, sc)

        pids = [pid for _, members, _ in groups for pid in members]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            imgs = dict(zip(pids, self.pool.map(render, pids)))
        finally:
            QApplication.restoreOverrideCursor()
        stamp = lambda pid: re.sub(r'^[A-Z]+', '', self.pages[pid]['raw'])  # 스캔 시각(재스캔 R·평판 P 표시 빼고)

        def desc(pid, body):
            t = stamp(pid)
            when = f'{t[9:11]}:{t[11:13]}:{t[13:15]} 스캔' if len(t) >= 15 else ''
            where = '본문 자리' if pid == body else '추가로 스캔한 것'
            reasons = P.warn_reasons(self.pages[pid], self.opt, target)
            return f'{where} · {when}' + (f'\n⚠ {" / ".join(reasons)}' if reasons else '')

        data = [(v, [(pid, imgs[pid], desc(pid, body)) for pid in members]) for v, members, body in groups]

        def default_pick(v, members):
            # 확인 필요 표시가 없는 것 중 가장 최근 스캔(다시 스캔한 게 보통 더 낫다), 없으면 가장 최근
            ok = [pid for pid in members if not P.warn_reasons(self.pages[pid], self.opt, target)]
            return max(ok or members, key=stamp)

        dlg = DupDialog(self, data, default_pick)
        dlg.exec()
        if not dlg.choices:
            return
        self._checkpoint()
        order = self._order()
        bodies = {v: body for v, _, body in groups}
        members = {v: m for v, m, _ in groups}
        removed = set()
        for v, keep in dlg.choices.items():
            # 고른 장을 본문 자리에 두고 나머지는 목록에서 뺀다(원본 파일은 남는다)
            i, j = order.index(bodies[v]), order.index(keep)
            order[i], order[j] = order[j], order[i]
            removed |= {pid for pid in members[v] if pid != keep}
        order = [pid for pid in order if pid not in removed]
        self._apply_order_removing(order, removed)
        self._update_status(f'중복 {len(dlg.choices)}쪽 정리: {len(removed)}장을 목록에서 뺐습니다 (Ctrl+Z 로 되돌리기)')

    def _apply_order_removing(self, order, removed):
        for pid in removed:
            it = self._item(pid)
            if it:
                self.list.takeItem(self.list.row(it))
            self.pages.pop(pid, None)
        self._apply_order(order)
        self._pages_changed([])
        self._schedule_save()

    def _reverse_order(self):
        self._checkpoint()
        items = [self.list.takeItem(0) for _ in range(self.list.count())]
        for it in reversed(items):
            self.list.addItem(it)
        self._renumber()
        self._schedule_save()

    # --- 잘라내기·복사·붙여넣기·복제 (붙여넣기는 선택한 페이지 바로 뒤에) ---
    def _clear_cut(self):
        if self.clip and self.clip['mode'] == 'cut':
            for pid in self.clip['pids']:
                it = self._item(pid)
                if it:
                    it.setFlags(it.flags() | Qt.ItemIsEnabled)
        self.clip = None

    def _sel_in_order(self):
        sel = set(self._selected())
        return [pid for pid in self._order() if pid in sel]

    def _cut(self):
        sel = self._sel_in_order()
        if not sel:
            return
        self._clear_cut()
        self.clip = dict(mode='cut', pids=sel)
        for pid in sel:  # 붙여넣기 전까지 흐리게(선택도 안 되게)
            it = self._item(pid)
            it.setSelected(False)
            it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
        self._update_status(f'{len(sel)}장 잘라냄 — 넣을 자리(페이지 사이)를 클릭하고 Ctrl+V (취소: Esc)')

    def _copy(self):
        sel = self._sel_in_order()
        if not sel:
            return
        self._clear_cut()
        self.clip = dict(mode='copy', pids=sel)
        self._update_status(f'{len(sel)}장 복사함 — 넣을 자리(페이지 사이)를 클릭하고 Ctrl+V')

    def _paste_row(self):
        """페이지 사이를 클릭해 둔 자리 → 아니면 선택한 페이지 바로 뒤 → 아니면 맨 끝."""
        if self.list.caret_row is not None:
            return min(self.list.caret_row, self.list.count())
        rows = [self.list.row(it) for it in self.list.selectedItems()]
        return max(rows) + 1 if rows else self.list.count()

    def _on_caret(self):
        row = self.list.caret_row
        if row is None:
            return
        where = '맨 앞' if row == 0 else '맨 끝' if row >= self.list.count() else f'{row}번째와 {row + 1}번째 사이'
        what = f'{len(self.clip["pids"])}장을 ' if self.clip else ''
        self._update_status(f'붙여넣을 자리: {where} — {what}Ctrl+V 로 여기에 넣기')

    def _clone_pages(self, pids, row):
        """pids 의 복제본을 row 자리에 끼워 넣는다(원본 스캔 파일은 함께 쓴다)."""
        new_ids = []
        for k, pid in enumerate(pids):
            nid = f'p{self.next_id:05d}'
            self.next_id += 1
            p = json.loads(json.dumps(self.pages[pid]))
            p['id'] = nid
            self.pages[nid] = p
            if pid in self.small:
                self.small[nid] = self.small[pid]
            it = QListWidgetItem('…')
            it.setData(Qt.UserRole, nid)
            it.setTextAlignment(Qt.AlignHCenter)
            it.setSizeHint(self._icon_size() + QSize(16, 30))
            src = self._item(pid)
            if src:
                it.setIcon(src.icon())
            self.list.insertItem(row + k, it)
            new_ids.append(nid)
            self._render_thumb(nid)
        return new_ids

    def _select_only(self, pids):
        self.list.clearSelection()
        for pid in pids:
            it = self._item(pid)
            if it:
                it.setSelected(True)
        if pids and self._item(pids[0]):
            # 기준 페이지만 바꾸고 선택은 그대로(그냥 바꾸면 여러 장 선택이 한 장으로 풀린다)
            self.list.setCurrentItem(self._item(pids[0]), QItemSelectionModel.NoUpdate)
            self.list.scrollToItem(self._item(pids[0]))

    def _paste(self):
        if not self.clip:
            return
        self._checkpoint()
        row = self._paste_row()
        self.list.set_caret(None)
        if self.clip['mode'] == 'cut':
            pids = [pid for pid in self.clip['pids'] if self._item(pid)]
            self._clear_cut()
            self.list.move_rows([self.list.row(self._item(pid)) for pid in pids], row)
            self._select_only(pids)
        else:
            pids = self._clone_pages([pid for pid in self.clip['pids'] if pid in self.pages], row)
            self._select_only(pids)
            self._renumber()
            self._pages_changed([])
        self._schedule_save()

    def _duplicate(self):
        sel = self._sel_in_order()
        if not sel:
            return
        self._checkpoint()
        row = max(self.list.row(self._item(pid)) for pid in sel) + 1
        self._select_only(self._clone_pages(sel, row))
        self._renumber()
        self._pages_changed([])
        self._schedule_save()

    def _edit_corners(self):
        it = self.list.currentItem()
        if not it:
            return
        p = self.pages[it.data(Qt.UserRole)]
        if not p.get('quad'):
            return
        dlg = CornerEditor(self, self._raw_path(p), p['quad'], p['auto_quad'] or p['quad'])
        if dlg.exec() == QDialog.Accepted:
            self._checkpoint()
            p['quad'] = dlg.quad()
            p['conf'], p['top_ok'], p['manual_quad'] = 1.0, True, True  # 사람이 확인한 값
            p['ink'] = P.ink_ratio(P.warp(self.small[p['id']], p['quad'], SMALL))
            self._pages_changed([p['id']])
            self._schedule_save()

    def _build_edit_actions(self):
        """목록에서 쓰는 단축키. 목록에 포커스가 있을 때만 동작(오른쪽 칸 입력란의 Ctrl+C 등과 안 겹치게)."""
        defs = [('Ctrl+X', '잘라내기', self._cut), ('Ctrl+C', '복사', self._copy),
                ('Ctrl+V', '붙여넣기', self._paste), ('Ctrl+D', '복제', self._duplicate),
                ('Delete', '목록에서 빼기', self._remove_sel),
                ('Ctrl+Z', '실행 취소', self._undo), ('Ctrl+Y', '다시 실행', self._redo_edit),
                ('Escape', '잘라내기 취소', self._clear_cut)]
        self.edit_actions = {}
        for key, text, fn in defs:
            act = QAction(text, self.list)
            act.setShortcut(QKeySequence(key))
            act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            act.triggered.connect(fn)
            self.list.addAction(act)
            self.edit_actions[key] = act
        self.edit_actions['Ctrl+Y'].setShortcuts([QKeySequence('Ctrl+Y'), QKeySequence('Ctrl+Shift+Z')])

    def _context_menu(self, pos):
        m = QMenu(self)
        a = self.edit_actions
        if self.list.caret_row is not None and self.list.gap_row_at(pos) is not None:
            a['Ctrl+V'].setEnabled(bool(self.clip))  # 페이지 사이에서 오른쪽 클릭: 그 자리에 붙여넣기
            m.addAction(a['Ctrl+V'])
            m.exec(self.list.mapToGlobal(pos))
            return
        a['Ctrl+V'].setEnabled(bool(self.clip))
        a['Ctrl+Z'].setEnabled(bool(self.undo_stack))
        a['Ctrl+Y'].setEnabled(bool(self.redo_stack))
        for key in ('Ctrl+X', 'Ctrl+C', 'Ctrl+V', 'Ctrl+D'):
            m.addAction(a[key])
        m.addSeparator()
        m.addAction('↺ 왼쪽 회전', lambda: self._rotate_sel(-90))
        m.addAction('↻ 오른쪽 회전', lambda: self._rotate_sel(90))
        m.addAction('180°', lambda: self._rotate_sel(180))
        m.addAction('선택 페이지 순서 교정 (쪽 번호 자리로)', self._fix_selected_order)
        m.addAction('쪽 번호 입력', lambda: self._enter_pnos())
        m.addAction('문제 페이지 검토', lambda: self._review_problems())
        m.addAction('중복 쪽 정리', lambda: self._resolve_duplicates())
        m.addAction('순서 뒤집기 (전체)', self._reverse_order)
        m.addSeparator()
        m.addAction('제외/포함', self._toggle_exclude_sel)
        m.addAction('빈 페이지 자동 판정으로 되돌리기', self._auto_exclude_sel)
        m.addAction('모서리 조정', self._edit_corners)
        m.addAction('선택 페이지 재스캔', self._rescan_selected)
        m.addAction('재스캔 되돌리기', self._undo_rescan)
        m.addSeparator()
        m.addAction(a['Delete'])
        m.addAction(a['Ctrl+Z'])
        m.addAction(a['Ctrl+Y'])
        m.exec(self.list.mapToGlobal(pos))

    # ---------- 스캔 ----------
    def _fill_dpi(self, want=None):
        """지금 고른 스캔 방식에서 복합기가 받는 해상도로 목록을 채운다(높은 값 먼저)."""
        source = self.cb_source.currentData() if hasattr(self, 'cb_source') else 'Feeder'
        want = want or self.cb_dpi.currentData() or 300
        self.cb_dpi.blockSignals(True)
        self.cb_dpi.clear()
        for d in self.scan_caps.get(source) or [300, 150]:
            self.cb_dpi.addItem(f'{d} dpi', d)
        idx = self.cb_dpi.findData(want)
        self.cb_dpi.setCurrentIndex(idx if idx >= 0 else 0)  # 없으면 가장 높은 해상도
        self.cb_dpi.blockSignals(False)
        if hasattr(self, 'chk_duplex'):
            # 평판은 한 면씩, 양면 없는 피더도 끈다
            self.chk_duplex.setEnabled(source == 'Feeder' and self.scan_caps.get('duplex', True))

    # 스캐너 문자열: eSCL 은 주소(http…), Windows 드라이버는 'win:장치ID'
    def _cur_dev(self):
        d = self.cb_dev.currentData() if hasattr(self, 'cb_dev') else None
        return d if d and not d.startswith('__') else ''

    def _cur_dev_name(self):
        dev = self._cur_dev()
        return next((d['name'] for d in self.devices if d['dev'] == dev), self.opt.get('device_name') or '')

    def _fill_devices(self):
        cur = self.opt.get('escl_url') or ''
        items = [(d['dev'], f"{d['name']}  ({'네트워크' if d['kind'] == 'escl' else '드라이버'})") for d in self.devices]
        if cur and cur not in [d for d, _ in items]:
            items.insert(0, (cur, self.opt.get('device_name') or cur))  # 지난번에 쓴 스캐너
        self.cb_dev.blockSignals(True)
        self.cb_dev.clear()
        if not items:
            self.cb_dev.addItem('(찾은 스캐너 없음 — [찾기])', '__none__')
        for dev, label in items:
            self.cb_dev.addItem(label, dev)
        self.cb_dev.addItem('네트워크 주소 직접 입력…', '__manual__')
        idx = self.cb_dev.findData(cur) if cur else -1
        self.cb_dev.setCurrentIndex(idx if idx >= 0 else 0)
        self.cb_dev.blockSignals(False)

    def _on_dev_chosen(self, *_):
        dev = self.cb_dev.currentData()
        if dev == '__manual__':
            text, ok = QInputDialog.getText(
                self, '스캐너 주소', '복합기의 네트워크 주소(IP)를 입력하세요.\n'
                                  '복합기 화면의 [네트워크 설정]이나 설정 보고서에서 볼 수 있습니다.\n예: 192.168.0.10',
                text=self.opt.get('escl_url', '') if self.opt.get('escl_url', '').startswith('http') else '')
            text = text.strip()
            if ok and text:
                if not text.startswith('http'):
                    text = 'http://' + text
                text = text.rstrip('/')
                if text.count('/') < 3:
                    text += '/eSCL'
                self.opt.update(escl_url=text, device_name=text)
            self._fill_devices()
        elif dev == '__none__':
            self._find_scanner(True)
            return
        self._ui_to_opt()
        self._ask_caps()

    def _startup_scanner(self):
        if self.opt.get('escl_url', '').startswith('wia:'):   # 2026.11.27 판의 드라이버 장치 표기 → 다시 고르게
            self.opt.update(escl_url='', device_name='')
        if self.opt.get('escl_url'):
            self._ask_caps()
        self._find_scanner(False)   # 목록은 늘 새로 찾아 둔다(고른 스캐너는 그대로)

    def _find_scanner(self, interactive=False):
        if interactive:
            self.status.setText('스캐너를 찾는 중… (몇 초 걸립니다)')

        def run():
            try:
                escl = [dict(dev=d['url'], name=d['name'], kind='escl') for d in scanner.discover()]
            except Exception:
                traceback.print_exc()
                escl = []
            wia = []
            for d in win_scan.list_devices():
                # Windows 가 네트워크 eSCL 스캐너를 드라이버 장치로도 보여 주므로, 네트워크로 찾았으면 뺀다
                if escl and '\\ESCL\\' in d['id'].upper():
                    continue
                wia.append(dict(dev='win:' + d['id'], name=d['name'], kind='wia'))
            self.bus.devices.emit(dict(list=escl + wia, interactive=interactive))
        threading.Thread(target=run, daemon=True).start()

    def _on_devices(self, res):
        self.devices = res['list']
        if not self.opt.get('escl_url') and self.devices:
            self.opt.update(escl_url=self.devices[0]['dev'], device_name=self.devices[0]['name'])
            self._ask_caps()
        self._fill_devices()
        self._ui_to_opt()
        if not res['interactive']:
            return
        if self.devices:
            self.status.setText(f'스캐너 {len(self.devices)}대를 찾았습니다. 목록에서 고르세요.')
        else:
            self._update_status()
            QMessageBox.information(
                self, '스캐너 찾기',
                '스캐너를 찾지 못했습니다.\n\n'
                '· 공유기(와이파이)로 연결한 복합기: 켜져 있고 이 PC와 같은 공유기에 연결돼 있는지 확인하세요. '
                '절전 중이면 복합기 화면을 한 번 눌러 깨운 뒤 [찾기]를 다시 누르세요.\n'
                '· USB로 연결한 복합기: 제조사 홈페이지의 스캐너 드라이버를 설치해야 보입니다.\n'
                '· 주소(IP)를 알면 목록의 "네트워크 주소 직접 입력…"을 고르세요.\n\n'
                '다른 프로그램으로 스캔해 둔 이미지는 [파일 불러오기]로 정리할 수 있습니다.')

    def _ask_caps(self):
        dev = self.opt.get('escl_url')
        if getattr(self, '_caps_for', None) != dev:   # 다른 기기: 이전 기기 정보를 기본값으로 되돌린다
            self._caps_for = dev
            self.scan_caps = {'Feeder': [300, 150], 'Platen': [300, 150]}
            for i in range(self.cb_source.count()):
                self.cb_source.model().item(i).setEnabled(True)
            self._fill_dpi()
        if not dev:
            return

        def run():
            try:
                caps = win_scan.capabilities(dev[4:]) if dev.startswith('win:') else scanner.capabilities(dev)
                if caps:
                    caps['_dev'] = dev
                    self.bus.caps.emit(caps)
            except Exception:
                pass  # 스캐너가 꺼져 있으면 기본값(300/150) 그대로
        threading.Thread(target=run, daemon=True).start()

    def _on_caps(self, caps):
        if not caps or caps.pop('_dev', None) != self.opt.get('escl_url'):
            return   # 그사이 다른 스캐너를 골랐다
        self.scan_caps = {'Feeder': caps.get('Feeder') or [], 'Platen': caps.get('Platen') or [],
                          'duplex': caps.get('duplex', True)}
        src = self.cb_source.currentData()
        for i in range(self.cb_source.count()):   # 없는 방식은 고를 수 없게
            avail = self.cb_source.itemData(i) in caps
            self.cb_source.model().item(i).setEnabled(avail)
        if src not in caps:
            other = 'Platen' if src == 'Feeder' else 'Feeder'
            if other in caps:
                self.cb_source.setCurrentIndex(self.cb_source.findData(other))
        for k in ('Feeder', 'Platen'):
            self.scan_caps[k] = self.scan_caps[k] or [300, 150]
        self._fill_dpi()
        self._ui_to_opt()

    def _scan_with(self, dev, on_page, duplex, dpi, source, notice):
        """고른 스캐너 종류에 맞게 스캔. 받은 장수."""
        if not dev:
            raise scanner.ScanError('스캐너를 고르지 않았습니다. 오른쪽 [스캔]의 [찾기]를 눌러 스캐너를 고르세요.')
        if dev.startswith('win:'):
            return win_scan.scan(dev[4:], on_page, duplex=duplex, dpi=dpi, should_stop=lambda: self.stop_scan,
                                 source=source, notice=notice)
        return scanner.scan(dev, on_page, duplex=duplex, dpi=dpi, should_stop=lambda: self.stop_scan,
                            source=source, notice=notice)

    def _start_scan(self):
        if self.scanning or not self._ensure_out_dir():
            return
        self._ui_to_opt()
        raw_dir = os.path.join(self._work_dir(), 'raw')
        os.makedirs(raw_dir, exist_ok=True)
        self.scanning, self.stop_scan = True, False
        self.act_scan.setEnabled(False)
        self.act_stop.setEnabled(True)
        self.status.setText('스캔 중…')
        url, duplex, dpi, source = self.opt['escl_url'], self.opt['duplex'], self.opt['dpi'], self.opt['source']
        flat = source == 'Platen'
        batch = ('P' if flat else '') + _batch_stamp()

        eff = {'duplex': duplex}

        def notice(msg):
            if '한 면씩' in msg:
                eff['duplex'] = False
            self.bus.notice.emit(msg)

        def on_page(data, i):
            path = os.path.join(raw_dir, f'{batch}-{i + 1:04d}.jpg')
            with open(path, 'wb') as fh:
                fh.write(data)
            side = 'flat' if flat else ('front' if i % 2 == 0 else 'back') if eff['duplex'] else 'front'
            self.bus.scanned.emit(path, side)
            self.bus.progress.emit(i + 1, 0, f'스캔 중… {i + 1}면 받음')

        def run():
            try:
                n = self._scan_with(url, on_page, duplex, dpi, source, notice)
                self.bus.scan_done.emit(n, '')
            except scanner.ScanError as e:
                self.bus.scan_done.emit(0, str(e))
            except Exception as e:
                traceback.print_exc()
                self.bus.scan_done.emit(0, f'예상치 못한 오류: {e}')

        threading.Thread(target=run, daemon=True).start()

    def _stop_scan(self):
        self.stop_scan = True
        self.status.setText('중지하는 중… (지금 넘어가는 장까지 받고 멈춥니다)')

    def _on_scan_done(self, n, err):
        self.scanning = False
        self.act_scan.setEnabled(True)
        self.act_stop.setEnabled(False)
        self.bar.hide()
        if err:
            QMessageBox.warning(self, '스캔', err)
        if err:
            self._update_status()
        elif self.opt['source'] == 'Platen':
            self._update_status(f'평판 스캔 완료 — 유리판에 다음 장을 올리고 [스캔 시작]을 누르세요')
        else:
            self._update_status(f'스캔 완료: {n}면')

    # ---------- 재스캔: 같은 묶음을 통째로 다시 넣고, 고른 페이지만 갈아끼운다 ----------
    def _batch_of(self, p):
        """(묶음 이름, 묶음 안 순번 0부터, 묶음 면 수). 파일 이름이 규칙과 다르면 None."""
        m = P.RAW_NAME.match(p['raw'])
        if not m:
            return None
        batch = m.group(1)
        n = sum(1 for f in os.listdir(os.path.join(self._work_dir(), 'raw'))
                if (mm := P.RAW_NAME.match(f)) and mm.group(1) == batch)
        return batch, int(m.group(2)) - 1, n

    def _rescan_selected(self):
        if self.scanning:
            return
        sel = self._selected()
        if not sel:
            QMessageBox.information(self, '재스캔', '다시 스캔할 페이지를 먼저 고르세요(여러 장 가능).')
            return
        info = {pid: self._batch_of(self.pages[pid]) for pid in sel}
        batches = {v[0] for v in info.values() if v}
        if None in info.values() or len(batches) != 1:
            QMessageBox.information(self, '재스캔', '한 번에 스캔한 묶음 안의 페이지만 함께 고를 수 있습니다.\n'
                                                   '(묶음이 다르면 묶음별로 나눠서 해 주세요)')
            return
        old_len = next(iter(info.values()))[2]
        sheets = f'종이 {(old_len + 1) // 2}장, ' if self.opt['duplex'] else ''
        if QMessageBox.question(
                self, '재스캔',
                f'고른 {len(sel)}쪽이 들어 있는 종이를 피더에 넣고 [예]를 누르세요.\n'
                f'처음 묶음 전체({sheets}{old_len}면)를 다시 넣어도 되고, 그 종이를 포함한 일부만 넣어도 됩니다.\n\n'
                f'다시 스캔한 것 중 내용이 확실히 같은 면만 바꿉니다. 애매하면 바꾸지 않고 알려 드립니다.\n'
                f"(바뀐 페이지는 오른쪽 클릭 → '재스캔 되돌리기'로 원래대로 돌릴 수 있습니다)") != QMessageBox.Yes:
            return
        self._ui_to_opt()
        self.scanning, self.stop_scan = True, False
        self.act_scan.setEnabled(False)
        self.act_stop.setEnabled(True)
        raw_dir = os.path.join(self._work_dir(), 'raw')
        batch = 'R' + _batch_stamp()
        url, duplex, dpi = self.opt['escl_url'], self.opt['duplex'], self.opt['dpi']
        targets = {pid: (info[pid][1], self._raw_path(self.pages[pid])) for pid in sel}
        feeder = self.opt['source'] == 'Feeder'

        def run():
            paths = []

            def on_page(data, i):
                path = os.path.join(raw_dir, f'{batch}-{i + 1:04d}.jpg')
                with open(path, 'wb') as fh:
                    fh.write(data)
                paths.append(path)
                self.bus.progress.emit(i + 1, 0, f'재스캔 중… {i + 1}면 받음')

            eff = {'duplex': duplex}

            def notice(msg):
                if '한 면씩' in msg:
                    eff['duplex'] = False
                self.bus.notice.emit(msg)

            try:
                self._scan_with(url, on_page, duplex, dpi, self.opt['source'], notice)
                self.bus.rescan_done.emit(self._match_rescan(paths, targets, old_len, eff['duplex'] and feeder, feeder))
            except scanner.ScanError as e:
                self.bus.rescan_done.emit(dict(error=str(e), paths=paths))
            except Exception as e:
                traceback.print_exc()
                self.bus.rescan_done.emit(dict(error=f'예상치 못한 오류: {e}', paths=paths))

        threading.Thread(target=run, daemon=True).start()

    def _match_rescan(self, paths, targets, old_len, duplex, feeder):
        """작업 스레드. 고른 페이지마다 재스캔 중 같은 종이를 찾는다. 확실할 때만 바꾼다.
        - 장수가 같다(묶음 통째로): 같은 순번 한 장만 후보 — 그래도 내용이 같은지 확인
        - 비슷하다(한두 장 겹치거나 빠짐): 비례 위치 앞뒤 2장
        - 훨씬 적다(일부만 넣음): 받은 면 전부 (양면이면 앞면은 앞면끼리)"""
        new_len = len(paths)
        jobs = []
        for pid, (idx, target) in targets.items():
            if new_len == old_len:
                cands = [idx]
            elif new_len >= old_len * 0.8:
                cands = P.rescan_window(idx, old_len, new_len)
            else:
                cands = list(range(new_len))
            if duplex:
                cands = [j for j in cands if j % 2 == idx % 2]
            jobs.append((pid, idx, target, cands))
        futs = [self.procs.submit(worker.find_same_page, target, [paths[j] for j in cands],
                                  self.opt['dpi'], feeder) for pid, idx, target, cands in jobs]
        mapping, notes = {}, []
        for (pid, idx, target, cands), fut in zip(jobs, futs):
            k, diff = fut.result() if cands else (None, 1.0)
            if k is not None:
                mapping[pid] = paths[cands[k]]
            else:
                notes.append(f'· 처음 묶음의 {idx + 1}번째 면: 같은 종이를 확실히 찾지 못해 그대로 둠 '
                             f'(가장 비슷한 면도 내용이 {diff * 100:.0f}% 다름)')
        return dict(mapping=mapping, paths=paths, notes=notes)

    def _on_rescan_done(self, res):
        self.scanning = False
        self.act_scan.setEnabled(True)
        self.act_stop.setEnabled(False)
        self.bar.hide()
        mapping = res.get('mapping', {})
        for pid, path in mapping.items():
            p = self.pages.get(pid)
            if not p:
                continue
            # 새 원본으로 처음부터 다시 분석. 손으로 돌린 회전은 같은 종이이니 유지
            p.setdefault('prev_raw', []).append(p['raw'])  # 되돌리기용 (원본 파일은 지우지 않는다)
            p.update(raw=os.path.basename(path), excluded=None)
            self._reanalyze(pid)
        used = set(mapping.values())
        for path in res.get('paths', []):  # 안 쓴 재스캔 면은 지운다
            if path not in used:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._schedule_save()
        if res.get('error'):
            QMessageBox.warning(self, '재스캔', res['error'] + ('\n\n바꾼 페이지가 없습니다.' if not mapping else ''))
        else:
            QMessageBox.information(self, '재스캔', '\n'.join(
                [f'{len(mapping)}쪽을 새로 스캔한 것으로 바꿨습니다.'] +
                (['', '바꾸지 않은 페이지:'] + res['notes'] +
                 ['', '그 종이가 들어 있었는지 확인하고 다시 재스캔해 주세요.'] if res['notes'] else [])))
        self._update_status()

    # ---------- 문제 페이지 검토: '확인 필요' 페이지를 모아서 하나씩 처리 ----------
    def _review_problems(self):
        target = self._target()
        pids = [pid for pid in self._order() if pid in self.pages and not P.is_excluded(self.pages[pid], self.opt)
                and P.warn_reasons(self.pages[pid], self.opt, target)]
        if not pids:
            QMessageBox.information(self, '문제 페이지 검토', '확인이 필요한 페이지가 없습니다.')
            return
        dlg = ReviewDialog(self, pids)
        dlg.exec()
        self._refresh_all()
        marked = [pid for pid in dlg.rescan if pid in self.pages]
        if marked and QMessageBox.question(
                self, '재스캔', f'재스캔할 페이지로 표시한 {len(marked)}장을 지금 재스캔할까요?\n'
                               '(그 종이들을 피더에 넣은 뒤 [예])') == QMessageBox.Yes:
            self._select_only(marked)
            self._rescan_selected()

    # ---------- 쪽 번호 직접 입력: 번호가 없거나 못 읽은 페이지 ----------
    def _enter_pnos(self):
        """고른 페이지가 있으면 그 페이지들, 없으면 번호를 못 읽은 페이지 전부(번호 없는 쪽 표시한 건 빼고)."""
        order = [pid for pid in self._order() if pid in self.pages]
        sel = set(self._selected())
        pids = [pid for pid in order if pid in sel] if sel else \
            [pid for pid in order if self.pages[pid].get('pno') is None and not self.pages[pid].get('no_pno')
             and not self.pages[pid].get('cover')
             and not P.is_excluded(self.pages[pid], self.opt)]
        if not pids:
            QMessageBox.information(self, '쪽 번호 입력', '번호를 못 읽은 페이지가 없습니다.\n'
                                                     '(번호를 고치려면 그 페이지를 고르고 다시 누르세요)')
            return
        target = self._target()
        self._checkpoint()  # 입력 전체를 Ctrl+Z 한 번으로 되돌릴 수 있게

        def ask(pid):
            order = [x for x in self._order() if x in self.pages]
            pages = [self.pages[x] for x in order]
            i = order.index(pid)
            prev = next((pages[j]['pno'] for j in range(i - 1, -1, -1) if pages[j].get('pno') is not None), None)
            nxt = next((pages[j]['pno'] for j in range(i + 1, len(pages)) if pages[j].get('pno') is not None), None)
            around = (f'목록 {i + 1}번째 페이지\n앞쪽 가장 가까운 번호: {f"p.{prev}" if prev is not None else "없음"}'
                      f'\n뒤쪽 가장 가까운 번호: {f"p.{nxt}" if nxt is not None else "없음"}')
            p = self.pages[pid]
            raw = cv2.resize(imread(self._raw_path(p)), None, fx=0.3, fy=0.3, interpolation=cv2.INTER_AREA)
            return (P.render(raw, p, self.opt, target, 0.3), around, pagenum.suggest(pages, i),
                    p.get('pno'), p.get('cover'))

        def on_answer(pid, value):
            p = self.pages[pid]
            if value == 'none':
                p.update(no_pno=True, pno_manual=None, cover=None)
            elif value in ('front', 'back') or value == 0:     # 0쪽 = 표지
                p.update(no_pno=False, pno_manual=None, cover='front' if value in ('front', 0) else 'back')
            else:
                p.update(no_pno=False, pno_manual=int(value), cover=None)
            self._assign_pnos()   # 다음 페이지 추측에 바로 반영
            self._renumber()

        PnoDialog(self, pids, ask, on_answer).exec()
        self._schedule_save()
        self._update_status()

    # ---------- 다시 분석: 재스캔으로 원본이 바뀌었을 때 감지·방향·빈 페이지 판정을 처음부터 ----------
    def _reanalyze(self, pid):
        p = self.pages[pid]
        p.update(quad=None, auto_quad=None, conf=0.0, ink=1.0)
        for k in ('top_ok', 'ori', 'auto_rot', 'manual_quad', 'det_v', 'warn_ok'):
            p.pop(k, None)
        self.small.pop(pid, None)
        self._analyze(pid)

    def _undo_rescan(self):
        done = 0
        for pid in self._selected():
            p = self.pages[pid]
            if p.get('prev_raw'):
                p['raw'] = p['prev_raw'].pop()
                self._reanalyze(pid)
                done += 1
        if not done:
            QMessageBox.information(self, '재스캔 되돌리기', '고른 페이지 중 재스캔으로 바뀐 페이지가 없습니다.')
        self._schedule_save()

    # ---------- 불러오기 ----------
    def _import_files(self):
        if not self._ensure_out_dir():
            return
        files, _ = QFileDialog.getOpenFileNames(self, '스캔 이미지 불러오기', '',
                                                '이미지 (*.jpg *.jpeg *.png *.bmp *.tif *.tiff)')
        if not files:
            return
        raw_dir = os.path.join(self._work_dir(), 'raw')
        os.makedirs(raw_dir, exist_ok=True)
        batch = _batch_stamp()
        for i, f in enumerate(sorted(files)):
            dst = os.path.join(raw_dir, f'{batch}-imp{i + 1:04d}{os.path.splitext(f)[1].lower()}')
            shutil.copy2(f, dst)
            self._add_page(dst, 'front')

    # ---------- 저장 ----------
    def _export(self):
        if not self._ensure_out_dir():
            return
        self._ui_to_opt()
        if not (self.opt['save_pdf'] or self.opt['save_png']):
            QMessageBox.information(self, '저장하기', 'PDF 나 PNG 중 하나 이상을 골라 주세요.')
            return
        failed = [pid for pid in self._order() if not self.pages[pid].get('quad') and self.pages[pid].get('analysis_error')]
        pending = [pid for pid in self._order() if not self.pages[pid].get('quad') and pid not in failed]
        if pending:
            QMessageBox.information(self, '저장하기', f'아직 분석 중인 페이지가 {len(pending)}장 있습니다. 잠시 후 다시 눌러 주세요.')
            return
        if failed:
            box = QMessageBox(QMessageBox.Warning, '저장하기',
                              f'분석하지 못한 페이지가 {len(failed)}장 있습니다(스캔 파일이 손상됐을 수 있습니다).\n'
                              '이 페이지들을 빼고 저장할까요?', parent=self)
            b_skip = box.addButton('빼고 저장', QMessageBox.AcceptRole)
            b_retry = box.addButton('다시 분석', QMessageBox.ActionRole)
            box.addButton('취소', QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_retry:
                for pid in failed:
                    self.pages[pid].pop('analysis_error', None)
                    self._analyze(pid)
                self._update_status()
                return
            if box.clickedButton() is not b_skip:
                return
        order = [pid for pid in self._order() if pid not in failed and not P.is_excluded(self.pages[pid], self.opt)]
        if not order:
            QMessageBox.information(self, '저장하기', '저장할 페이지가 없습니다.')
            return
        pages = [dict(self.pages[pid]) for pid in order]
        opt, target = dict(self.opt), self._target()

        # 같은 이름이 이미 있으면 = 합쳐 달라는 뜻일 수 있으니 먼저 묻는다
        name = opt['name']
        pdf_path = os.path.join(self.out_dir, f'{name}.pdf')
        png_dir = os.path.join(self.out_dir, f'{name}_png')
        old_pngs = self._existing_pngs(png_dir, name) if opt['save_png'] else []
        has_pdf = opt['save_pdf'] and os.path.exists(pdf_path)
        merge = False
        if has_pdf or old_pngs:
            what = '\n'.join(([f'· {name}.pdf'] if has_pdf else []) +
                             ([f'· {name}_png 폴더 ({len(old_pngs)}장)'] if old_pngs else []))
            box = QMessageBox(QMessageBox.Question, '같은 이름이 있습니다',
                              f'같은 이름의 결과물이 이미 있습니다.\n{what}\n\n'
                              f'기존 파일 뒤에 이번 {len(pages)}쪽을 이어 붙여 합칠까요?', parent=self)
            b_merge = box.addButton('합치기', QMessageBox.YesRole)
            b_over = box.addButton('덮어쓰기', QMessageBox.DestructiveRole)
            box.addButton('취소', QMessageBox.RejectRole)
            box.setDefaultButton(b_merge)
            box.exec()
            if box.clickedButton() not in (b_merge, b_over):
                return
            merge = box.clickedButton() is b_merge
            if merge and opt['uniform'] and opt['crop']:
                # 한 권으로 합치는 것이니 새 페이지를 기존 책 크기에 맞춘다
                target = self._existing_book_size(pdf_path if has_pdf else None, png_dir, old_pngs, opt['dpi']) or target
        self.act_export.setEnabled(False)
        self.btn_save.setEnabled(False)
        threading.Thread(target=self._job_export, args=(pages, opt, target, merge), daemon=True).start()

    @staticmethod
    def _existing_pngs(png_dir, name):
        if not os.path.isdir(png_dir):
            return []
        pat = re.compile(re.escape(name) + r'_(\d+)\.png$', re.I)
        return sorted((int(m.group(1)), f) for f in os.listdir(png_dir) if (m := pat.match(f)))

    @staticmethod
    def _existing_book_size(pdf_path, png_dir, old_pngs, dpi):
        """기존 결과물의 페이지 크기(px). PDF 는 첫 쪽 크기(pt → px), 없으면 첫 PNG 크기."""
        try:
            if pdf_path:
                box = pypdf.PdfReader(pdf_path).pages[0].mediabox
                return float(box.width) / 72 * dpi, float(box.height) / 72 * dpi
            if old_pngs:
                h, w = imread(os.path.join(png_dir, old_pngs[0][1])).shape[:2]
                return float(w), float(h)
        except Exception:
            traceback.print_exc()
        return None

    def _job_export(self, pages, opt, target, merge=False):
        try:
            name = opt['name']
            png_dir = os.path.join(self.out_dir, f'{name}_png')
            tmp_dir = os.path.join(self._work_dir(), '_pdf_tmp')
            png_start = 0
            if opt['save_png']:
                os.makedirs(png_dir, exist_ok=True)
                old = self._existing_pngs(png_dir, name)
                if merge:
                    png_start = old[-1][0] if old else 0   # 기존 마지막 번호 다음부터
                else:
                    for _, f in old:                        # 덮어쓰기: 예전 번호가 남지 않게
                        os.remove(os.path.join(png_dir, f))
            if opt['save_pdf']:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                os.makedirs(tmp_dir)

            def one(i, p):
                img = P.render(imread(self._raw_path(p)), p, opt, target, 1.0)
                if opt['save_png']:
                    imwrite(os.path.join(png_dir, f'{name}_{png_start + i + 1:04d}.png'), img)
                if not opt['save_pdf']:
                    return None
                if opt['pdf_quality'] == 'png':
                    part = os.path.join(tmp_dir, f'{i:05d}.png')
                    imwrite(part, img)
                else:
                    part = os.path.join(tmp_dir, f'{i:05d}.jpg')
                    quality = {'q100': 100, 'q95': 95, 'q85': 85, 'q75': 75}.get(opt['pdf_quality'], 95)
                    imwrite(part, img, (cv2.IMWRITE_JPEG_QUALITY, quality))
                return part

            pdf_parts = [None] * len(pages)
            with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
                futs = {ex.submit(one, i, p): i for i, p in enumerate(pages)}
                for done, fut in enumerate(as_completed(futs), 1):
                    pdf_parts[futs[fut]] = fut.result()
                    self.bus.progress.emit(done, len(pages), f'저장 중… {done}/{len(pages)}')
            msg = []
            if opt['save_pdf']:
                self.bus.progress.emit(len(pages), len(pages), 'PDF 묶는 중…')
                pdf_path = os.path.join(self.out_dir, f'{name}.pdf')
                new_pdf = os.path.join(tmp_dir, 'new.pdf')
                layout = img2pdf.get_fixed_dpi_layout_fun((opt['dpi'], opt['dpi']))  # 실제 책 크기로 기록
                with open(new_pdf, 'wb') as fh:
                    img2pdf.convert(pdf_parts, layout_fun=layout, outputstream=fh)
                out = os.path.join(tmp_dir, 'out.pdf')
                if merge and os.path.exists(pdf_path):
                    w = pypdf.PdfWriter()
                    w.append(pdf_path)
                    old_n = len(w.pages)
                    w.append(new_pdf)
                    with open(out, 'wb') as fh:
                        w.write(fh)
                    msg.append(f'{pdf_path}\n  (기존 {old_n}쪽 + 이번 {len(pages)}쪽 = 총 {old_n + len(pages)}쪽)')
                else:
                    os.replace(new_pdf, out)
                    msg.append(pdf_path)
                try:
                    os.replace(out, pdf_path)  # 다 만든 뒤 한 번에 바꿔치기: 중간에 실패해도 기존 PDF 는 안전
                except PermissionError:
                    raise RuntimeError(f'{name}.pdf 가 다른 프로그램(PDF 뷰어 등)에서 열려 있어 바꿀 수 없습니다. 닫고 다시 저장하세요.')
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if opt['save_png']:
                msg.append(png_dir + (f'\n  ({png_start + 1}번부터 이어서 저장)' if png_start else ''))
            self.bus.export_done.emit(f'{len(pages)}쪽 저장 완료' + (' (기존 파일에 합침)' if merge else '') +
                                      '\n' + '\n'.join(msg))
        except Exception as e:
            traceback.print_exc()
            self.bus.export_done.emit(f'!저장 실패: {e}')

    def _on_export_done(self, msg):
        self.act_export.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.bar.hide()
        if msg.startswith('!'):
            QMessageBox.warning(self, '저장하기', msg[1:])
        else:
            QMessageBox.information(self, '저장하기', msg)
        self._update_status()

    # ---------- 상태 표시 ----------
    def _on_progress(self, i, total, text):
        self.status.setText(text)
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(i)
            self.bar.show()

    def _update_status(self, prefix=None):
        if self.scanning:
            return
        target = self._target()
        ps = list(self.pages.values())
        excluded = sum(P.is_excluded(p, self.opt) for p in ps)
        warn = sum(1 for p in ps if not P.is_excluded(p, self.opt) and P.warn_reasons(p, self.opt, target))
        failed = sum(1 for p in ps if not p.get('quad') and p.get('analysis_error'))
        analyzing = sum(1 for p in ps if not p.get('quad') and not p.get('analysis_error'))
        parts = [prefix] if prefix else []
        parts.append(f'전체 {len(ps)}장 · 저장될 쪽 {len(ps) - excluded} · 제외 {excluded}')
        if failed:
            parts.append(f'⚠ 분석 실패 {failed}장 ([저장하기]에서 다시 분석하거나 빼고 저장)')
        if warn:
            parts.append(f'⚠ 확인 필요 {warn}장')
        if analyzing:
            parts.append(f'분석 중 {analyzing}장')
        else:
            missing, dup = pagenum.check([self.pages[pid] for pid in self._order() if pid in self.pages])
            if missing:
                parts.append('빠진 쪽: ' + ', '.join(_missing_text(a, b, n) for a, b, n in missing[:4])
                             + (' …' if len(missing) > 4 else ''))
            if dup:
                parts.append('쪽 번호 중복: ' + ', '.join(f'p.{v}' for v in dup[:6]) + (' …' if len(dup) > 6 else ''))
        if target:
            mm = 25.4 / self.opt['dpi']
            parts.append(f'통일 크기 {target[0] * mm:.1f} × {target[1] * mm:.1f} mm')
        self.status.setText('   |   '.join(parts))

    def closeEvent(self, e):
        if self.scanning:
            if QMessageBox.question(self, '종료', '스캔 중입니다. 그래도 닫을까요?') != QMessageBox.Yes:
                e.ignore()
                return
            self.stop_scan = True
        self._save_project()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.procs.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(e)


INSTANCE_KEY = 'bookscan-single-instance'


def _bring_to_front(w):
    if w.isMinimized():
        w.showNormal()
    w.show()
    w.raise_()
    w.activateWindow()


def _selftest(w, src, out):
    """이미지 폴더를 양면 스캔처럼 불러와 분석·저장까지 돌리고 결과를 selftest.json 에 남긴 뒤 종료."""
    raw_dir = os.path.join(w._work_dir(), 'raw')
    os.makedirs(raw_dir, exist_ok=True)
    t0 = time.time()
    files = sorted(f for f in os.listdir(src) if f.lower().endswith(IMAGE_EXT))
    for i, f in enumerate(files):
        dst = os.path.join(raw_dir, f'T-{i + 1:04d}{os.path.splitext(f)[1].lower()}')
        shutil.copy2(os.path.join(src, f), dst)
        w._add_page(dst, 'front' if i % 2 == 0 else 'back')
    state = {'saved': False}

    def tick():
        if w.analyzing or any(not p.get('quad') for p in w.pages.values()):
            return
        if not state['saved']:
            state['saved'] = True
            w._export()
            return
        pdf = os.path.join(w.out_dir, f"{w.opt['name']}.pdf")
        if not (w.act_export.isEnabled() and os.path.exists(pdf)):
            return
        timer.stop()
        order = w._order()
        rep = dict(pages=len(order), seconds=round(time.time() - t0, 1),
                   excluded=[i for i, pid in enumerate(order, 1) if P.is_excluded(w.pages[pid], w.opt)],
                   pdf_pages=len(pypdf.PdfReader(pdf).pages), target=[round(v) for v in w._target()],
                   status=w.status.text(), win_devices=len(win_scan.list_devices()))
        with open(os.path.join(out, 'selftest.json'), 'w', encoding='utf-8') as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1)
        QApplication.quit()
    timer = QTimer(w, interval=500, timeout=tick)
    timer.start()


def main():
    multiprocessing.freeze_support()  # exe 로 묶었을 때 프로세스 풀이 동작하도록
    if getattr(sys, 'frozen', False) or os.path.basename(sys.executable).lower() == 'pythonw.exe' or sys.stderr is None:
        # 콘솔 없이 실행하면 오류를 로그 파일로 (%LOCALAPPDATA%\bookscan\bookscan.log)
        log_dir = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'bookscan')
        os.makedirs(log_dir, exist_ok=True)
        log = open(os.path.join(log_dir, 'bookscan.log'), 'a', encoding='utf-8', buffering=1)
        sys.stdout = sys.stderr = log
        print(f'--- {time.strftime("%Y-%m-%d %H:%M:%S")} 시작')
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    selftest = os.environ.get('BOOKSCAN_SELFTEST')  # 빌드 확인용: '이미지폴더|결과폴더'
    if selftest:
        QSettings('bookscan', 'bookscan-selftest').setValue('out_dir', selftest.split('|')[1])
        w = MainWindow()
        QTimer.singleShot(0, lambda: _selftest(w, *selftest.split('|')))
        w.show()
        sys.exit(app.exec())

    # 이미 떠 있으면 새로 만들지 않고 기존 창을 앞으로. 지금 프로세스는 사용자가 직접 연 것이라
    # 앞으로 가져올 권한이 있으니, 그 권한을 기존 창에 넘겨준 뒤 신호를 보낸다.
    sock = QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if sock.waitForConnected(500):
        if sys.platform == 'win32':
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        sock.write(b'show')
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return
    QLocalServer.removeServer(INSTANCE_KEY)  # 비정상 종료로 남은 찌꺼기 정리
    server = QLocalServer()
    server.listen(INSTANCE_KEY)

    w = MainWindow()
    server.newConnection.connect(lambda: (server.nextPendingConnection(), _bring_to_front(w)))
    _bring_to_front(w)
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
