from __future__ import annotations

from pathlib import Path

from .aspect_resize import AspectRatioSizeGrip, aspect_resized_size

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class TruthImageOverlay(QFrame):
    """Collapsible, resizable truth-image window placed above the OpenGL point-cloud view."""

    resized = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self._content_aspect_ratio = 4.0 / 3.0
        self._image_path: Path | None = None
        self._expanded = True
        self._expanded_size = (420, 300)
        self.setObjectName("truthImageOverlay")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "#truthImageOverlay { background-color: rgba(25, 28, 34, 235); border: 1px solid #6e7781; border-radius: 4px; }"
            "#truthImageOverlay QLabel { color: #f0f0f0; }"
        )
        self.setMinimumSize(220, 160)
        self.resize(*self._expanded_size)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 5, 6, 5)
        root.setSpacing(4)
        header = QHBoxLayout()
        self.title_label = QLabel("真值图片")
        self.title_label.setStyleSheet("font-weight: bold;")
        self.file_label = QLabel("未加载")
        self.file_label.setStyleSheet("color: #c7d1db; font-size: 10px;")
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.toggle_button = QPushButton("隐藏")
        self.toggle_button.setFixedWidth(52)
        self.toggle_button.setToolTip("隐藏/展开真值图片")
        self.toggle_button.clicked.connect(self.toggle_collapsed)
        header.addWidget(self.title_label)
        header.addWidget(self.file_label, 1)
        header.addWidget(self.toggle_button)
        root.addLayout(header)

        self.image_label = QLabel("当前真值帧未找到 JPG 图片")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(200, 120)
        self.image_label.setStyleSheet("background-color: #111317; color: #aeb7c2;")
        root.addWidget(self.image_label, 1)

        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        self.size_grip = AspectRatioSizeGrip(self, self)
        self.size_grip.resized.connect(lambda _w, _h: self.resized.emit())
        grip_row.addWidget(self.size_grip)
        root.addLayout(grip_row)

    @property
    def image_path(self) -> Path | None:
        return self._image_path

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def set_image(self, path: Path | None) -> None:
        self._image_path = path
        self._source_pixmap = QPixmap()
        if path is None:
            self.file_label.setText("未找到图片")
            self.file_label.setToolTip("当前真值帧目录中未找到 jpg/jpeg/png 图片")
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("当前真值帧未找到 JPG 图片")
        else:
            pixmap = QPixmap(str(path))
            if pixmap.isNull():
                self.file_label.setText(path.name)
                self.file_label.setToolTip(str(path))
                self.image_label.setPixmap(QPixmap())
                self.image_label.setText("图片加载失败")
            else:
                self._source_pixmap = pixmap
                self._content_aspect_ratio = pixmap.width() / max(1, pixmap.height())
                self.file_label.setText(path.name)
                self.file_label.setToolTip(str(path))
                self.image_label.setText("")
                self._rescale_pixmap()
        self.setToolTip(str(path) if path is not None else "当前真值帧没有可显示图片")

    def toggle_collapsed(self) -> None:
        if self._expanded:
            self._expanded_size = (self.width(), self.height())
            self.image_label.hide()
            self.size_grip.hide()
            self.setMinimumHeight(0)
            self.setMaximumHeight(34)
            self.resize(self.width(), 34)
            self.toggle_button.setText("展开")
            self._expanded = False
        else:
            self.setMaximumHeight(16_777_215)
            self.setMinimumSize(220, 160)
            self.resize(*self._expanded_size)
            self.image_label.show()
            self.size_grip.show()
            self.toggle_button.setText("隐藏")
            self._expanded = True
            self._rescale_pixmap()
        self._clamp_to_parent()
        self.resized.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._expanded:
            self._rescale_pixmap()
        self._clamp_to_parent()

    def aspect_resize(self, dx: int, dy: int, start_width: int, start_height: int) -> tuple[int, int]:
        parent = self.parentWidget()
        max_width = max(220, (parent.width() - 24) if parent is not None else 1600)
        max_height = max(160, (parent.height() - 24) if parent is not None else 1200)
        horizontal_chrome = 12
        header_height = 42
        grip_height = 28
        content_start_width = max(1, start_width - horizontal_chrome)
        content_start_height = max(1, start_height - header_height - grip_height)
        result = aspect_resized_size(
            content_start_width,
            content_start_height,
            dx,
            dy,
            self._content_aspect_ratio,
            208,
            120,
            max(208, max_width - horizontal_chrome),
            max(120, max_height - header_height - grip_height),
        )
        return result.width + horizontal_chrome, result.height + header_height + grip_height

    def _rescale_pixmap(self) -> None:
        if self._source_pixmap.isNull() or not self._expanded:
            return
        available = self.image_label.size()
        if available.width() <= 1 or available.height() <= 1:
            return
        self.image_label.setPixmap(
            self._source_pixmap.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _clamp_to_parent(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        max_width = max(220, parent.width() - 24)
        max_height = max(34, parent.height() - 24)
        if self.width() > max_width or self.height() > max_height:
            self.resize(min(self.width(), max_width), min(self.height(), max_height))
