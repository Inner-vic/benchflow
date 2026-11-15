from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph.opengl as gl
from PySide6.QtCore import QEvent, QPointF, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedLayout,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .axis_overlay import AdaptiveAxisOverlay
from .image_overlay import TruthImageOverlay
from .interaction import PickCandidate, PickingGLViewWidget, nearest_screen_candidate, project_points_to_screen
from .loaders import PointCloudLoadError, load_point_cloud
from .models import PointCloudData, SourceKind
from .palette import all_known_labels, label_style
from .scene_index import (
    FramePair,
    SceneDirectoryPair,
    MatchedScenePair,
    SceneIndexError,
    SceneScanResult,
    build_frame_pairs,
    find_matched_scene_pairs,
)
from .settings import load_last_scene_state, save_scene_state
from .view_presets import VIEW_PRESETS, apply_view_preset
from .renderer import PointCloudRenderer, reset_camera

_SOURCE_NAMES = {"ground_truth": "真值", "prediction": "预测"}
_WHEEL_NAMES = ("FL", "FR", "RL", "RR")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("uroad 点云与预测数据可视化")
        self.resize(1580, 960)

        self.clouds: dict[str, PointCloudData | None] = {"ground_truth": None, "prediction": None}
        self.label_checks: dict[int, QCheckBox] = {}
        self._selected_points: list[tuple[str, int]] = []
        self.matched_scene_pairs: tuple[MatchedScenePair, ...] = ()
        self.current_matched_scene_index = -1
        self.gt_scene_dir: Path | None = None
        self.pred_scene_dir: Path | None = None
        self.scene_scan: SceneScanResult | None = None
        self.frame_pairs: tuple[FramePair, ...] = ()
        self._reset_view_on_next_frame_load = False
        self._restore_timestamp_ms: int | None = None
        self._scene_scan_in_progress = False
        self.current_view_preset_key = "bird"
        self._view_actions: dict[str, QAction] = {}

        self.view = PickingGLViewWidget()
        self.view.setBackgroundColor("#16181d")
        self.view.opts["distance"] = 35
        self.view.addItem(gl.GLGridItem())
        self.view.addItem(gl.GLAxisItem())
        self.renderers = {source_id: PointCloudRenderer(self.view, source_id) for source_id in self.clouds}
        self.axis_overlay = AdaptiveAxisOverlay(self.view)
        self.selection_marker_a = gl.GLScatterPlotItem(pxMode=True)
        self.selection_marker_b = gl.GLScatterPlotItem(pxMode=True)
        self.view.addItem(self.selection_marker_a)
        self.view.addItem(self.selection_marker_b)
        self._clear_selection()
        self.view.pointClicked.connect(self._pick_point)
        self._axis_timer = QTimer(self)
        self._axis_timer.setSingleShot(True)
        self._axis_timer.setInterval(150)
        self._axis_timer.timeout.connect(self._update_axes)
        self.view.cameraInteractionFinished.connect(self._schedule_axis_update)
        self.truth_image_overlay = TruthImageOverlay()
        self.truth_image_overlay.resized.connect(self._position_truth_image_overlay)

        self._build_toolbar()
        self._build_layout()
        self._rebuild_label_filters()
        self._restore_last_scene_state()
        self.statusBar().showMessage("加载真值和预测文件后开始比较。左键短点击选择点；左键拖动旋转。")

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("工具")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        toolbar.addAction(self._action("加载真值文件", lambda: self._choose_file("ground_truth")))
        toolbar.addAction(self._action("加载预测文件", lambda: self._choose_file("prediction")))
        toolbar.addSeparator()
        view_group = QActionGroup(self)
        view_group.setExclusive(True)
        for key, shortcut in (("yx", "1"), ("xz", "2"), ("yz", "3"), ("bird", "4")):
            preset = VIEW_PRESETS[key]
            action = QAction(preset.display_name, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.setToolTip(f"切换到 {preset.display_name} 视角（{shortcut}）")
            action.triggered.connect(lambda checked=False, preset_key=key: self._set_view_preset(preset_key))
            view_group.addAction(action)
            toolbar.addAction(action)
            self._view_actions[key] = action
        self._view_actions[self.current_view_preset_key].setChecked(True)
        toolbar.addSeparator()
        toolbar.addAction(self._action("重置视角", self._reset_camera, QKeySequence("R")))
        toolbar.addAction(self._action("截图", self._save_screenshot, QKeySequence("S")))

    def _set_view_preset(self, preset_key: str) -> None:
        if preset_key not in VIEW_PRESETS:
            return
        self.current_view_preset_key = preset_key
        if preset_key in self._view_actions:
            self._view_actions[preset_key].setChecked(True)
        apply_view_preset(self.view, VIEW_PRESETS[preset_key])
        self._schedule_axis_update()
        self.statusBar().showMessage(f"已切换到 {VIEW_PRESETS[preset_key].display_name} 视角")

    def _action(self, text: str, callback, shortcut: QKeySequence | None = None) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(callback)
        if shortcut is not None:
            action.setShortcut(shortcut)
        return action

    def _build_layout(self) -> None:
        top_controls = QWidget()
        top_controls.setMinimumWidth(460)
        top_controls.setMaximumWidth(620)
        top_layout = QVBoxLayout(top_controls)
        top_layout.setContentsMargins(8, 8, 8, 8)
        top_layout.setSpacing(6)
        top_layout.addWidget(self._scene_browser_group())
        top_layout.addWidget(self._source_group("ground_truth"))
        top_layout.addWidget(self._source_group("prediction"))
        top_layout.addWidget(self._selection_group())
        top_layout.addWidget(QLabel("左键短点击：选择点　|　左键拖动：旋转　|　中键拖动：平移　|　滚轮：缩放　|　R：重置　|　S：截图"))
        top_layout.addStretch(1)

        controls_splitter = QSplitter(Qt.Orientation.Vertical)
        controls_splitter.setChildrenCollapsible(False)
        controls_splitter.addWidget(top_controls)
        controls_splitter.addWidget(self._filter_group())
        controls_splitter.setStretchFactor(1, 1)
        controls_splitter.setSizes([390, 570])

        left_panel = QWidget()
        left_panel.setMinimumWidth(460)
        left_panel.setMaximumWidth(620)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(controls_splitter)

        view_container = QWidget()
        view_container.installEventFilter(self)
        self._view_container = view_container
        view_stack = QStackedLayout(view_container)
        view_stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        view_stack.setContentsMargins(0, 0, 0, 0)
        view_stack.addWidget(self.view)

        self.truth_image_overlay.setParent(view_container)
        self.truth_image_overlay.show()
        QTimer.singleShot(0, self._position_truth_image_overlay)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left_panel)
        splitter.addWidget(view_container)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([540, 1040])
        self.setCentralWidget(splitter)

    def eventFilter(self, watched, event) -> bool:
        if watched is getattr(self, "_view_container", None) and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._clamp_truth_image_overlay)
        return super().eventFilter(watched, event)

    def _clamp_truth_image_overlay(self) -> None:
        if hasattr(self, "truth_image_overlay"):
            self.truth_image_overlay._clamp_to_parent()
            self._position_truth_image_overlay()

    def _position_truth_image_overlay(self) -> None:
        if not hasattr(self, "_view_container"):
            return
        overlay = self.truth_image_overlay
        x = max(12, self._view_container.width() - overlay.width() - 12)
        overlay.move(x, 12)
        overlay.raise_()

    def _scene_browser_group(self) -> QGroupBox:
        group = QGroupBox("场景与帧浏览（dense 真值 ↔ pred_traj 预测）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(4)

        gt_row = QHBoxLayout()
        gt_button = QPushButton("选择真值场景")
        gt_button.clicked.connect(lambda: self._choose_scene_directory("ground_truth"))
        self.gt_scene_label = QLabel("未选择")
        self.gt_scene_label.setStyleSheet("color: #555;")
        self.gt_scene_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gt_row.addWidget(gt_button)
        gt_row.addWidget(self.gt_scene_label, 1)
        layout.addLayout(gt_row)

        pred_row = QHBoxLayout()
        pred_button = QPushButton("选择预测场景")
        pred_button.clicked.connect(lambda: self._choose_scene_directory("prediction"))
        self.pred_scene_label = QLabel("未选择")
        self.pred_scene_label.setStyleSheet("color: #555;")
        self.pred_scene_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pred_row.addWidget(pred_button)
        pred_row.addWidget(self.pred_scene_label, 1)
        layout.addLayout(pred_row)

        scene_navigation = QHBoxLayout()
        self.previous_scene_button = QPushButton("◀ 上一场景")
        self.previous_scene_button.clicked.connect(lambda: self._move_scene(-1))
        self.scene_position_label = QLabel("场景 0 / 0")
        self.scene_position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_scene_button = QPushButton("下一场景 ▶")
        self.next_scene_button.clicked.connect(lambda: self._move_scene(1))
        scene_navigation.addWidget(self.previous_scene_button)
        scene_navigation.addWidget(self.scene_position_label, 1)
        scene_navigation.addWidget(self.next_scene_button)
        layout.addLayout(scene_navigation)

        scan_row = QHBoxLayout()
        self.scan_frames_button = QPushButton("扫描共同帧")
        self.scan_frames_button.clicked.connect(self._scan_common_frames)
        self.scene_summary_label = QLabel("请先选择两侧场景目录")
        self.scene_summary_label.setStyleSheet("color: #555;")
        scan_row.addWidget(self.scan_frames_button)
        scan_row.addWidget(self.scene_summary_label, 1)
        layout.addLayout(scan_row)

        navigation = QHBoxLayout()
        self.previous_frame_button = QPushButton("◀ 上一帧")
        self.previous_frame_button.clicked.connect(lambda: self._move_frame(-1))
        self.frame_position_label = QLabel("第 0 / 0 帧")
        self.frame_position_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_frame_button = QPushButton("下一帧 ▶")
        self.next_frame_button.clicked.connect(lambda: self._move_frame(1))
        navigation.addWidget(self.previous_frame_button)
        navigation.addWidget(self.frame_position_label, 1)
        navigation.addWidget(self.next_frame_button)
        layout.addLayout(navigation)

        self.frame_list = QListWidget()
        self.frame_list.setMinimumHeight(150)
        self.frame_list.currentRowChanged.connect(self._load_selected_frame)
        layout.addWidget(self.frame_list)
        self._update_scene_navigation_controls()
        self._update_navigation_controls()
        return group

    def _source_group(self, source_id: str) -> QGroupBox:
        group = QGroupBox(f"{_SOURCE_NAMES[source_id]}数据")
        form = QFormLayout(group)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setVerticalSpacing(3)

        button = QPushButton(f"加载{_SOURCE_NAMES[source_id]}文件")
        button.clicked.connect(lambda: self._choose_file(source_id))
        path_label = QLabel("未加载")
        path_label.setWordWrap(False)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setStyleSheet("color: #555;")
        self.__dict__[f"{source_id}_path_label"] = path_label

        visible = QCheckBox("显示")
        visible.setChecked(True)
        visible.toggled.connect(lambda checked, key=source_id: self._set_visibility(key, checked))
        self.__dict__[f"{source_id}_visible"] = visible

        point_size = QSlider(Qt.Orientation.Horizontal)
        point_size.setRange(1, 12)
        point_size.setValue(4 if source_id == "prediction" else 3)
        point_size.valueChanged.connect(lambda value, key=source_id: self._set_point_size(key, value))

        opacity = QSlider(Qt.Orientation.Horizontal)
        opacity.setRange(30, 255)
        opacity.setValue(230 if source_id == "prediction" else 180)
        opacity.valueChanged.connect(lambda value, key=source_id: self._set_opacity(key, value))

        form.addRow(button)
        form.addRow("文件", path_label)
        form.addRow(visible)
        form.addRow("点大小", point_size)
        form.addRow("透明度", opacity)
        return group

    def _selection_group(self) -> QGroupBox:
        group = QGroupBox("A / B 选中点")
        outer = QVBoxLayout(group)
        points_row = QHBoxLayout()
        self.point_detail_labels: dict[str, dict[str, QLabel]] = {}
        for slot in ("A", "B"):
            slot_group = QGroupBox(f"{slot} 点")
            form = QFormLayout(slot_group)
            form.setVerticalSpacing(2)
            labels = {
                "source": QLabel("未选择"),
                "type": QLabel("—"),
                "xyz": QLabel("—"),
                "label": QLabel("—"),
                "wheel": QLabel("—"),
                "index": QLabel("—"),
            }
            form.addRow("来源", labels["source"])
            form.addRow("类型", labels["type"])
            form.addRow("坐标", labels["xyz"])
            form.addRow("标签", labels["label"])
            form.addRow("车轮", labels["wheel"])
            form.addRow("索引", labels["index"])
            points_row.addWidget(slot_group)
            self.point_detail_labels[slot] = labels
        outer.addLayout(points_row)
        clear_button = QPushButton("清除 A/B")
        clear_button.clicked.connect(self._clear_selection)
        outer.addWidget(clear_button, 0, Qt.AlignmentFlag.AlignRight)
        return group

    def _filter_group(self) -> QGroupBox:
        group = QGroupBox("标签过滤与图例")
        group.setMinimumHeight(320)
        group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(7, 7, 7, 7)
        actions = QHBoxLayout()
        select_all = QPushButton("全选")
        select_all.clicked.connect(lambda: self._set_all_labels(True))
        clear_all = QPushButton("全不选")
        clear_all.clicked.connect(lambda: self._set_all_labels(False))
        diff_only = QPushButton("仅差异标签")
        diff_only.clicked.connect(self._select_count_differences)
        actions.addWidget(select_all)
        actions.addWidget(clear_all)
        actions.addWidget(diff_only)
        layout.addLayout(actions)

        self.show_invalid = QCheckBox("显示无效/空标签（0、26）")
        self.show_invalid.toggled.connect(self._refresh)
        layout.addWidget(self.show_invalid)

        self.point_limit = QSpinBox()
        self.point_limit.setRange(10_000, 3_000_000)
        self.point_limit.setSingleStep(50_000)
        self.point_limit.setValue(300_000)
        self.point_limit.valueChanged.connect(self._refresh)
        limit_layout = QHBoxLayout()
        limit_layout.addWidget(QLabel("每数据源最大绘制点数（分层采样）"))
        limit_layout.addWidget(self.point_limit)
        layout.addLayout(limit_layout)

        self.label_scroll = QScrollArea()
        self.label_scroll.setWidgetResizable(True)
        # Keep the legend compact: exactly four label rows remain visible; more rows scroll.
        self.label_scroll.setFixedHeight(142)
        self.label_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.label_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.label_scroll.setFrameShape(QFrame.Shape.StyledPanel)
        self.label_container = QWidget()
        self.label_grid = QGridLayout(self.label_container)
        self.label_grid.setContentsMargins(4, 4, 4, 4)
        self.label_grid.setHorizontalSpacing(6)
        self.label_grid.setVerticalSpacing(2)
        self.label_scroll.setWidget(self.label_container)
        layout.addWidget(self.label_scroll, 1)
        return group

    def _restore_last_scene_state(self) -> None:
        gt_scene, pred_scene, timestamp_ms = load_last_scene_state()
        self.gt_scene_dir = gt_scene
        self.pred_scene_dir = pred_scene
        self._restore_timestamp_ms = timestamp_ms
        if gt_scene is not None:
            self.gt_scene_label.setText(gt_scene.name)
            self.gt_scene_label.setToolTip(str(gt_scene))
        if pred_scene is not None:
            self.pred_scene_label.setText(pred_scene.name)
            self.pred_scene_label.setToolTip(str(pred_scene))
        if gt_scene is not None and pred_scene is not None:
            self.scene_summary_label.setText("已恢复上次场景，正在扫描共同帧…")
            QTimer.singleShot(0, self._scan_common_frames)
        elif gt_scene is not None or pred_scene is not None:
            self.scene_summary_label.setText("仅恢复了一个有效场景目录，请选择另一侧场景。")

    def _choose_scene_directory(self, source_id: str) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            f"选择{_SOURCE_NAMES[source_id]}场景目录",
            str(self.gt_scene_dir or self.pred_scene_dir or Path.home()),
        )
        if not path:
            return
        scene_dir = Path(path)
        if source_id == "ground_truth":
            self.gt_scene_dir = scene_dir
            self.gt_scene_label.setText(scene_dir.name)
            self.gt_scene_label.setToolTip(str(scene_dir))
        else:
            self.pred_scene_dir = scene_dir
            self.pred_scene_label.setText(scene_dir.name)
            self.pred_scene_label.setToolTip(str(scene_dir))
        self._clear_frame_pairs("场景目录已变更，请扫描共同帧")
        save_scene_state(self.gt_scene_dir, self.pred_scene_dir, None)

    def _clear_frame_pairs(self, message: str) -> None:
        self.scene_scan = None
        self.frame_pairs = ()
        self.matched_scene_pairs = ()
        self.current_matched_scene_index = -1
        if hasattr(self, "truth_image_overlay"):
            self.truth_image_overlay.set_image(None)
        if hasattr(self, "frame_list"):
            self.frame_list.blockSignals(True)
            self.frame_list.clear()
            self.frame_list.blockSignals(False)
        if hasattr(self, "scene_summary_label"):
            self.scene_summary_label.setText(message)
        if hasattr(self, "frame_position_label"):
            self._update_navigation_controls()

    def _scan_common_frames(self) -> None:
        if self.gt_scene_dir is None or self.pred_scene_dir is None:
            QMessageBox.warning(self, "无法扫描", "请先选择真值场景目录和预测场景目录。")
            return
        try:
            self._scene_scan_in_progress = True
            self.scene_summary_label.setText("正在扫描可用匹配场景，请稍候…")
            QApplication.processEvents()
            self.matched_scene_pairs = find_matched_scene_pairs(
                self.gt_scene_dir.parent, self.pred_scene_dir.parent
            )
        except SceneIndexError as exc:
            QMessageBox.critical(self, "扫描场景失败", str(exc))
            return
        finally:
            self._scene_scan_in_progress = False
        self.current_matched_scene_index = next(
            (index for index, pair in enumerate(self.matched_scene_pairs)
             if pair.gt_scene_dir == self.gt_scene_dir and pair.pred_scene_dir == self.pred_scene_dir),
            -1,
        )
        if self.current_matched_scene_index < 0:
            QMessageBox.warning(self, "未找到可用场景", "当前真值/预测场景没有可加载共同帧。")
            self.matched_scene_pairs = ()
            self._update_scene_navigation_controls()
            return
        self._activate_scene(
            self.current_matched_scene_index,
            self.matched_scene_pairs[self.current_matched_scene_index].scan_result,
            reset_view=True,
        )

    def _activate_scene(self, scene_index: int, result: SceneScanResult, reset_view: bool) -> None:
        if not (0 <= scene_index < len(self.matched_scene_pairs)):
            return
        scene_pair = self.matched_scene_pairs[scene_index]
        self._clear_selection()
        self.current_matched_scene_index = scene_index
        self.gt_scene_dir = scene_pair.gt_scene_dir
        self.pred_scene_dir = scene_pair.pred_scene_dir
        self.gt_scene_label.setText(scene_pair.scene_name)
        self.gt_scene_label.setToolTip(str(scene_pair.gt_scene_dir))
        self.pred_scene_label.setText(scene_pair.scene_name)
        self.pred_scene_label.setToolTip(str(scene_pair.pred_scene_dir))
        self.scene_scan = result
        self.frame_pairs = result.pairs
        self.frame_list.blockSignals(True)
        self.frame_list.clear()
        for pair in self.frame_pairs:
            item = QListWidgetItem(pair.display_name)
            item.setToolTip(
                f"时间戳: {pair.timestamp_ms} ms\n真值: {pair.gt_dense_path}\n"
                f"预测: {pair.pred_traj_path}\n真值图片: {pair.image_path if pair.image_path is not None else '未找到'}"
            )
            self.frame_list.addItem(item)
        self.frame_list.blockSignals(False)
        result = self.scene_scan
        self.scene_summary_label.setText(
            f"可加载共同帧 {len(result.pairs)} | GT {result.gt_frame_count} | Pred {result.pred_frame_count} | 同 timestamp {result.matching_timestamp_count}"
        )
        self._reset_view_on_next_frame_load = reset_view
        self._update_scene_navigation_controls()
        self._update_navigation_controls()
        restore_row = next(
            (index for index, pair in enumerate(self.frame_pairs) if pair.timestamp_ms == self._restore_timestamp_ms),
            0,
        )
        self._restore_timestamp_ms = None
        self.frame_list.setCurrentRow(restore_row)

    def _move_scene(self, step: int) -> None:
        target = self.current_matched_scene_index + step
        if 0 <= target < len(self.matched_scene_pairs):
            self._activate_scene(target, self.matched_scene_pairs[target].scan_result, reset_view=True)
        self._update_scene_navigation_controls()

    def _update_scene_navigation_controls(self) -> None:
        count = len(self.matched_scene_pairs)
        current = self.current_matched_scene_index
        if hasattr(self, "previous_scene_button"):
            self.previous_scene_button.setEnabled(not self._scene_scan_in_progress and current > 0)
            self.next_scene_button.setEnabled(not self._scene_scan_in_progress and 0 <= current < count - 1)
            if 0 <= current < count:
                self.scene_position_label.setText(f"场景 {current + 1} / {count}：{self.matched_scene_pairs[current].scene_name}")
            else:
                self.scene_position_label.setText(f"场景 0 / {count}")

    def _move_frame(self, step: int) -> None:
        current = self.frame_list.currentRow()
        target = current + step
        if 0 <= target < len(self.frame_pairs):
            self.frame_list.setCurrentRow(target)

    def _update_navigation_controls(self) -> None:
        count = len(self.frame_pairs)
        current = self.frame_list.currentRow() if hasattr(self, "frame_list") else -1
        if hasattr(self, "previous_frame_button"):
            self.previous_frame_button.setEnabled(count > 0 and current > 0)
            self.next_frame_button.setEnabled(count > 0 and 0 <= current < count - 1)
            if 0 <= current < count:
                self.frame_position_label.setText(f"第 {current + 1} / {count} 帧：{self.frame_pairs[current].display_name}")
            else:
                self.frame_position_label.setText(f"第 0 / {count} 帧")

    def _load_selected_frame(self, row: int) -> None:
        self._update_navigation_controls()
        if not (0 <= row < len(self.frame_pairs)):
            return
        pair = self.frame_pairs[row]
        self.setEnabled(False)
        self.statusBar().showMessage(f"正在加载共同帧 {row + 1}/{len(self.frame_pairs)}：{pair.display_name}")
        QApplication.processEvents()
        try:
            gt_cloud = load_point_cloud(pair.gt_dense_path, "ground_truth")
            pred_cloud = load_point_cloud(pair.pred_traj_path, "prediction")
        except PointCloudLoadError as exc:
            QMessageBox.critical(
                self,
                "帧加载失败",
                f"帧: {pair.display_name}\n真值: {pair.gt_dense_path}\n预测: {pair.pred_traj_path}\n\n{exc}",
            )
            self.setEnabled(True)
            return

        self._set_cloud("ground_truth", gt_cloud)
        self._set_cloud("prediction", pred_cloud)
        self.truth_image_overlay.set_image(pair.image_path)
        self._clear_selection()
        self._rebuild_label_filters()
        self._refresh()
        if self._reset_view_on_next_frame_load:
            self._reset_camera()
            self._reset_view_on_next_frame_load = False
        else:
            self._schedule_axis_update()
        save_scene_state(self.gt_scene_dir, self.pred_scene_dir, pair.timestamp_ms)
        self.setEnabled(True)
        self._update_navigation_controls()
        self.statusBar().showMessage(
            f"共同帧 {row + 1}/{len(self.frame_pairs)}：{pair.display_name} | timestamp={pair.timestamp_ms} ms"
        )

    def _set_cloud(self, source_id: str, cloud: PointCloudData) -> None:
        self.clouds[source_id] = cloud
        self.renderers[source_id].set_cloud(cloud)
        label = self.__dict__[f"{source_id}_path_label"]
        label.setText(f"{cloud.display_name} | {cloud.kind.value} | {cloud.point_count:,} 点")
        label.setToolTip(f"{cloud.source_path}\n{cloud.kind.coordinate_description}")

    def _choose_file(self, source_id: str) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"选择{_SOURCE_NAMES[source_id]}点云/预测文件",
            str(Path.home()),
            "Point cloud files (*.npy);;All files (*)",
        )
        if not path:
            return
        try:
            cloud = load_point_cloud(path, source_id)
        except PointCloudLoadError as exc:
            QMessageBox.critical(self, "文件加载失败", str(exc))
            return

        self._set_cloud(source_id, cloud)
        self._clear_selection()
        self._rebuild_label_filters()
        self._refresh()
        self._reset_camera()

    def _set_visibility(self, source_id: str, checked: bool) -> None:
        self.renderers[source_id].settings.enabled = checked
        self._refresh()

    def _set_point_size(self, source_id: str, value: int) -> None:
        self.renderers[source_id].settings.point_size = float(value)
        self._refresh()

    def _set_opacity(self, source_id: str, value: int) -> None:
        self.renderers[source_id].settings.opacity = value
        self._refresh()

    def _rebuild_label_filters(self) -> None:
        selected = self._selected_labels()
        while self.label_grid.count():
            item = self.label_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.label_checks.clear()

        extra: set[int] = set()
        for cloud in self.clouds.values():
            if cloud is not None:
                extra.update(cloud.label_counts)
        labels = all_known_labels(extra)
        columns = 3
        row_height = 34
        row_count = (len(labels) + columns - 1) // columns
        self.label_container.setMinimumHeight(row_count * row_height + 8)
        for index, label_id in enumerate(labels):
            style = label_style(label_id)
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setMinimumWidth(140)
            card.setFixedHeight(30)
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(3, 1, 3, 1)
            card_layout.setSpacing(2)
            color = f"rgb({style.rgb[0]}, {style.rgb[1]}, {style.rgb[2]})"
            swatch = QLabel("■")
            swatch.setStyleSheet(f"color: {color}; font-size: 16px;")
            checkbox = QCheckBox(f"{label_id} {style.name}")
            checkbox.setChecked(label_id in selected if selected else label_id not in {0, 26})
            checkbox.toggled.connect(self._refresh)
            checkbox.setToolTip(f"label={label_id}: {style.name}")
            counts = self._count_label(label_id)
            count_label = QLabel(f"G{counts[0]:,}/P{counts[1]:,}")
            count_label.setStyleSheet("color: #555; font-size: 10px;")
            card_layout.addWidget(swatch)
            card_layout.addWidget(checkbox, 1)
            card_layout.addWidget(count_label)
            row, column = divmod(index, columns)
            self.label_grid.addWidget(card, row, column)
            self.label_checks[label_id] = checkbox

    def _count_label(self, label_id: int) -> tuple[int, int]:
        counts: list[int] = []
        for source_id in ("ground_truth", "prediction"):
            cloud = self.clouds[source_id]
            counts.append(0 if cloud is None else cloud.label_counts.get(label_id, 0))
        return counts[0], counts[1]

    def _selected_labels(self) -> set[int]:
        return {label for label, checkbox in self.label_checks.items() if checkbox.isChecked()}

    def _set_all_labels(self, checked: bool) -> None:
        for checkbox in self.label_checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self._refresh()

    def _select_count_differences(self) -> None:
        for label, checkbox in self.label_checks.items():
            gt_count, pred_count = self._count_label(label)
            checkbox.blockSignals(True)
            checkbox.setChecked(gt_count != pred_count)
            checkbox.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        selected_labels = self._selected_labels()
        for renderer in self.renderers.values():
            renderer.refresh(selected_labels, self.point_limit.value(), self.show_invalid.isChecked())
        self._clear_hidden_selection()
        self._update_status()
        self._schedule_axis_update()

    def _visible_bbox(self) -> tuple[np.ndarray | None, np.ndarray | None, str]:
        point_sets: list[np.ndarray] = []
        kinds: set[SourceKind] = set()
        for source_id, renderer in self.renderers.items():
            if not renderer.settings.enabled or renderer.cloud is None:
                continue
            _, points = renderer.pickable_points()
            if points.size:
                point_sets.append(points)
                kinds.add(renderer.cloud.kind)
        if not point_sets:
            return None, None, "m"
        points = np.concatenate(point_sets, axis=0)
        unit = "idx" if kinds == {SourceKind.OCC} else "m"
        return points.min(axis=0), points.max(axis=0), unit

    def _schedule_axis_update(self) -> None:
        self._axis_timer.start()

    def _update_axes(self) -> None:
        lower, upper, unit = self._visible_bbox()
        self.axis_overlay.update(lower, upper, unit)

    def _update_status(self) -> None:
        summaries = []
        coordinate_kinds: set[SourceKind] = set()
        for source_id, renderer in self.renderers.items():
            cloud = self.clouds[source_id]
            if cloud is None:
                continue
            coordinate_kinds.add(cloud.kind)
            suffix = "（已采样）" if renderer.was_sampled else ""
            summaries.append(
                f"{_SOURCE_NAMES[source_id]}: 原始 {cloud.point_count:,} | 筛选 {renderer.last_filtered_count:,} | 绘制 {renderer.last_drawn_count:,}{suffix}"
            )
        if SourceKind.OCC in coordinate_kinds and len(coordinate_kinds) > 1:
            summaries.append("警告：OCC 为栅格索引，不能直接与米坐标数据空间对齐")
        if self._selected_points:
            summaries.append(" | ".join(
                f"{slot}: {_SOURCE_NAMES[source]} label={int(self.clouds[source].labels[index])}"
                for slot, (source, index) in zip(("A", "B"), self._selected_points)
                if self.clouds[source] is not None
            ))
        self.statusBar().showMessage("   ·   ".join(summaries) or "加载真值和预测文件后开始比较。")

    def _reset_camera(self) -> None:
        reset_camera(self.view, list(self.clouds.values()), self._selected_labels(), self.show_invalid.isChecked())
        apply_view_preset(self.view, VIEW_PRESETS[self.current_view_preset_key])
        self._schedule_axis_update()

    def _pick_point(self, click_position: QPointF) -> None:
        candidates: list[PickCandidate] = []
        for source_id, renderer in self.renderers.items():
            indices, points = renderer.pickable_points()
            if not indices.size:
                continue
            screen, visible, depth = project_points_to_screen(self.view, points)
            if not np.any(visible):
                continue
            distances = np.hypot(screen[:, 0] - click_position.x(), screen[:, 1] - click_position.y())
            distances[~visible] = np.inf
            local_index = int(np.argmin(distances))
            if np.isfinite(distances[local_index]):
                candidates.append(PickCandidate(source_id, int(indices[local_index]), float(distances[local_index]), float(depth[local_index])))
        selected = nearest_screen_candidate(candidates)
        if selected is None:
            self._update_status()
            return
        self._add_selected_point(selected.source_id, selected.original_index)

    def _add_selected_point(self, source_id: str, original_index: int) -> None:
        candidate = (source_id, original_index)
        if candidate in self._selected_points:
            return
        if len(self._selected_points) < 2:
            self._selected_points.append(candidate)
        else:
            self._selected_points = [self._selected_points[1], candidate]
        self._update_selected_point_details()
        self._update_status()

    def _update_selected_point_details(self) -> None:
        marker_colors = (
            np.asarray([[0.1, 1.0, 1.0, 1.0]], dtype=np.float32),
            np.asarray([[1.0, 0.55, 0.05, 1.0]], dtype=np.float32),
        )
        markers = (self.selection_marker_a, self.selection_marker_b)
        for slot_index, slot_name in enumerate(("A", "B")):
            labels = self.point_detail_labels[slot_name]
            if slot_index >= len(self._selected_points):
                labels["source"].setText("未选择")
                labels["type"].setText("—")
                labels["xyz"].setText("—")
                labels["label"].setText("—")
                labels["wheel"].setText("—")
                labels["index"].setText("—")
                markers[slot_index].setData(pos=np.empty((0, 3), dtype=np.float32))
                continue
            source_id, original_index = self._selected_points[slot_index]
            cloud = self.clouds[source_id]
            if cloud is None:
                continue
            xyz = cloud.xyz[original_index]
            label_id = int(cloud.labels[original_index])
            style = label_style(label_id)
            unit = "idx" if cloud.kind is SourceKind.OCC else "m"
            labels["source"].setText(_SOURCE_NAMES[source_id])
            labels["type"].setText(cloud.kind.value)
            labels["xyz"].setText(f"x={xyz[0]:.3f}{unit}, y={xyz[1]:.3f}{unit}, z={xyz[2]:.3f}{unit}")
            labels["label"].setText(f"{label_id} ({style.name})")
            if cloud.wheel_ids is not None:
                wheel_id = int(cloud.wheel_ids[original_index])
                labels["wheel"].setText(_WHEEL_NAMES[wheel_id] if 0 <= wheel_id < len(_WHEEL_NAMES) else str(wheel_id))
            else:
                labels["wheel"].setText("—")
            labels["index"].setText(str(original_index))
            markers[slot_index].setData(
                pos=np.asarray([xyz], dtype=np.float32),
                color=marker_colors[slot_index],
                size=18.0,
                pxMode=True,
            )

    def _clear_hidden_selection(self) -> None:
        visible_points: list[tuple[str, int]] = []
        for source_id, original_index in self._selected_points:
            if original_index in set(self.renderers[source_id].drawn_indices.tolist()):
                visible_points.append((source_id, original_index))
        if len(visible_points) != len(self._selected_points):
            self._selected_points = visible_points
            self._update_selected_point_details()

    def _clear_selection(self) -> None:
        self._selected_points = []
        if hasattr(self, "point_detail_labels"):
            self._update_selected_point_details()

    def _save_screenshot(self) -> None:
        default = Path.cwd() / "screenshots" / "pointcloud_view.png"
        default.parent.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "保存当前视图", str(default), "PNG image (*.png)")
        if path:
            self.view.grabFramebuffer().save(path)
            self.statusBar().showMessage(f"截图已保存：{path}")


def run() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()
