from PySide6.QtWidgets import (
    QMainWindow, QLabel, QComboBox,
    QPushButton, QTreeWidget, QPlainTextEdit, QTreeWidgetItem
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtCore import Signal, Slot, QDateTime, Qt
from PySide6.QtGui import QImage, QPixmap
from config.path import UI_PATH, OUTPUT_PATH
from enum import Enum, auto
from time import perf_counter

class State(Enum):
    INITIAL = auto()
    SEARCHED = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    STARTING = auto()
    ACQUIRING = auto()
    ERROR = auto()

class MainWindow(QMainWindow):
    search_requested = Signal(dict)
    connect_requested = Signal(dict)
    start_requested = Signal()
    stop_requested = Signal()
    disconnect_requested = Signal()
    open_settings_requested = Signal()

    def __init__(self):
        super().__init__()
        self.state = State.INITIAL
        self.framegrabbers = []
        self.fps_lists = []
        self._gui_frame_count = 0
        self._gui_fps_started = None
        self._gui_fps = 0.0
        loader = QUiLoader()
        self.main_widget = loader.load(str(UI_PATH / "mainwidget.ui"), self)
        if self.main_widget is None:
            raise RuntimeError(loader.errorString())
        self.setCentralWidget(self.main_widget)
        self.setup_ui()
        self.connect_signals()
        self.update_ui_state()

    def setup_ui(self):
        # Camera Labels
        self.camera_labels = [
            self.findChild(QLabel, f"labelCamera{i:02d}")
            for i in range(1, 13)
        ]
        self.mode_combo = self.findChild(QComboBox, "comboMode")
        self.search_device_btn = self.findChild(QPushButton, "buttonSearch")
        self.device_tree = self.findChild(QTreeWidget, "treeDevices")
        self.device_count_label = self.findChild(QLabel, "labelDeviceCount")
        self.device_state_label = self.findChild(QLabel, "labelState")
        self.selected_device_label = self.findChild(QLabel, "labelSelectedDevice")
        self.connect_device_btn = self.findChild(QPushButton, "buttonConnect")
        self.disconnect_device_btn = self.findChild(QPushButton, "buttonDisconnect")
        self.start_capture_btn = self.findChild(QPushButton, "buttonStart")
        self.stop_capture_btn = self.findChild(QPushButton, "buttonStop")
        self.log_text = self.findChild(QPlainTextEdit, "editLog")
        self.clear_log_btn = self.findChild(QPushButton, "buttonClearLog")
        self.fps_label = self.findChild(QLabel, "labelFps")
        self.setting_button = self.findChild(QPushButton, "buttonSettings")
    
    def connect_signals(self):
        self.search_device_btn.clicked.connect(self.search_device)
        self.connect_device_btn.clicked.connect(self.connect_device)
        self.disconnect_device_btn.clicked.connect(self.disconnect_device)
        self.clear_log_btn.clicked.connect(self.clear_log)
        self.start_capture_btn.clicked.connect(self.start_capture)
        self.stop_capture_btn.clicked.connect(self.stop_capture)
        self.setting_button.clicked.connect(self.open_settings)
        
    def update_ui_state(self):
        has_cameras = any(fg["cameras"] for fg in self.framegrabbers)
        self.mode_combo.setEnabled(self.state in (State.INITIAL, State.SEARCHED))
        self.search_device_btn.setEnabled(self.state in (State.INITIAL, State.SEARCHED))
        self.connect_device_btn.setEnabled(self.state == State.SEARCHED and has_cameras)
        self.disconnect_device_btn.setEnabled(self.state in (State.CONNECTED, State.ERROR))
        self.start_capture_btn.setEnabled(self.state == State.CONNECTED)
        self.stop_capture_btn.setEnabled(self.state == State.ACQUIRING)
        self.device_state_label.setText(self.state.name)
        self.setting_button.setEnabled(self.state == State.CONNECTED)
        if getattr(self, "_settings_applying", False):
            self.start_capture_btn.setEnabled(False)
            self.disconnect_device_btn.setEnabled(False)
            self.setting_button.setEnabled(False)
        if self.state != State.ACQUIRING:
            self.fps_lists = [0.0] * len(self.fps_lists)
            self._gui_frame_count = 0
            self._gui_fps_started = None
            self._gui_fps = 0.0
            self.fps_label.setText("Capture: 0.0 FPS\nGUI: 0.0 FPS")
    
    @Slot()
    def search_device(self):
        self.update_ui_state()
        self.fps_lists = []
        for label in self.camera_labels:
            label.clear()
            label.setText("No signal")
        self.search_requested.emit({"virtual": self.mode_combo.currentIndex() == 0})
    
    @Slot(dict)
    def on_search_finished(self, result: dict):
        self.device_tree.clear()
        self.framegrabbers = []
        self.selected_device_label.setText("—")
        if not result["success"]:
            # Harvester.update() invalidates previous discovery objects.
            self.state = State.INITIAL
            self.device_count_label.setText("0 frame grabbers · 0 cameras")
            self.append_log(result.get("error") or "Search failed.")
            self.update_ui_state()
            return
        self.state = State.SEARCHED
        self.framegrabbers = result["framegrabbers"]
        framegrabbers = self.framegrabbers
        camera_count = 0
        for fg in framegrabbers:
            fg_item = QTreeWidgetItem([
                fg.get("display_name", fg["id"]),
                fg["id"],
                ""
            ])
            self.device_tree.addTopLevelItem(fg_item)
            for camera in fg["cameras"]:
                camera_item = QTreeWidgetItem([
                    camera.get("user_defined_name", camera["id"]),
                    camera["id"],
                    camera["serial_number"]
                ])
                fg_item.addChild(camera_item)
                camera_count += 1
        self.fps_lists = [0.0] * camera_count
        self.device_tree.expandAll()
        self.device_count_label.setText(
            f"{len(framegrabbers)} frame grabbers · {camera_count} cameras"
        )
        self.selected_device_label.setText(f"All cameras ({camera_count})" if camera_count else "—")
        self.append_log(f"{len(framegrabbers)} Framegrabbers ({camera_count} Cameras) Searched.")
        self.update_ui_state()

    @Slot()
    def connect_device(self):
        if not any(fg["cameras"] for fg in self.framegrabbers):
            return
        self.state = State.CONNECTING
        self.update_ui_state()
        self.connect_requested.emit({
            "framegrabbers": self.framegrabbers,
            "virtual": self.mode_combo.currentIndex() == 0,
        })

    @Slot(dict)
    def on_connect_finished(self, result: dict):
        if not result["success"]:
            self.state = State.ERROR
            self.append_log(result.get("error") or "Connection failed.")
            self.update_ui_state()
            return
        self.state = State.CONNECTED
        self.append_log("All cameras connected.")
        self.update_ui_state()

    @Slot()
    def disconnect_device(self):
        self.update_ui_state()
        self.disconnect_requested.emit()

    @Slot(dict)
    def on_disconnect_finished(self, result: dict):
        if not result["success"]:
            self.state = State.ERROR
            self.append_log(result.get("error") or "Disconnection failed.")
            self.update_ui_state()
            return
        self.state = State.SEARCHED
        self.append_log("Device Disconnected")
        self.update_ui_state()

    @Slot()
    def start_capture(self):
        self.state = State.STARTING
        self.update_ui_state()
        self.start_requested.emit()

    @Slot(dict)
    def on_start_finished(self, result: dict):
        if not result["success"]:
            self.state = State.ERROR
            self.append_log(result.get("error") or "Start capture failed.")
            self.update_ui_state()
            return
        self.state = State.ACQUIRING
        self._gui_fps_started = perf_counter()
        self.append_log("Capture Started")
        self.update_ui_state()

    @Slot()
    def stop_capture(self):
        self.update_ui_state()
        self.stop_requested.emit()

    @Slot(dict)
    def on_stop_finished(self, result: dict):
        if not result["success"]:
            self.state = State.ERROR
            self.append_log(result.get("error") or "Stop capture failed.")
            self.update_ui_state()
            return
        self.state = State.CONNECTED
        self.append_log("Capture Stopped")
        self.update_ui_state()

    @Slot(dict)
    def on_error_occurred(self, result: dict):
        if result["success"]:
            return
        self.state = State.ERROR
        self.append_log(result.get("error") or "Acquisition failed")
        self.update_ui_state()

    @Slot(dict)
    def on_shutdown_finished(self, result: dict):
        if result["success"]:
            self.state = State.INITIAL
        else:
            self.state = State.ERROR
            self.append_log(result.get("error") or "Shutdown failed; close again to retry")
        self.update_ui_state()

    @Slot()
    def clear_log(self):
        self.log_text.clear()

    @Slot(int, QImage, float)
    def on_frame_received(self, camera_idx: int, frame: QImage, fps: float):
        """Display an owned QImage on camera 0..11 in the GUI thread."""
        if self.state != State.ACQUIRING:
            return
        if not 0 <= camera_idx < min(len(self.camera_labels), len(self.fps_lists)):
            return
        if not isinstance(frame, QImage):
            return

        label = self.camera_labels[camera_idx]
        if label is None:
            return
        now = perf_counter()
        if frame.isNull():
            label.clear()
            self.fps_lists[camera_idx] = 0.0
        else:
            self.fps_lists[camera_idx] = fps
            self._gui_frame_count += 1

        # Count valid GUI deliveries, averaged over all connected cameras.
        # Update here only; no separate FPS timer or callback is needed.
        if self._gui_fps_started is None:
            self._gui_fps_started = now
        elapsed = now - self._gui_fps_started
        if elapsed >= 1.0:
            self._gui_fps = self._gui_frame_count / elapsed / len(self.fps_lists)
            self._gui_frame_count = 0
            self._gui_fps_started = now

        average_fps = sum(self.fps_lists) / len(self.fps_lists)
        self.fps_label.setText(
            f"Capture: {average_fps:.1f} FPS\nGUI: {self._gui_fps:.1f} FPS"
        )
        if frame.isNull() or label.contentsRect().size().isEmpty():
            return
        pixmap = QPixmap.fromImage(frame).scaled(
            label.contentsRect().size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(pixmap)
    
    @Slot()
    def open_settings(self):
        self.open_settings_requested.emit()

    @Slot(str)
    def append_log(self, message: str):
        now = QDateTime.currentDateTime()
        timestamp = now.toString("yy-MM-dd hh:mm:ss")
        date = now.toString("yy-MM-dd")
        log_msg = f"[{timestamp}] {message}"
        self.log_text.appendPlainText(log_msg)
        try:
            log_dir = OUTPUT_PATH / date
            log_dir.mkdir(parents=True, exist_ok=True)
            with (log_dir / "UI_Logs.txt").open("a", encoding="utf-8") as f:
                f.write(log_msg + "\n")
        except OSError as error:
            self.log_text.appendPlainText(f"[{timestamp}] Log file write failed: {error}")


    
