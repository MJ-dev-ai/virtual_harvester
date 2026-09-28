"""Run the camera GUI and its Harvester worker."""

import argparse
from pathlib import Path
import signal
import sys

from PySide6.QtCore import QObject, QEvent, QMetaObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from config.path import VCTI_PATH
from device.device_manager import DeviceManager
from gui.mainwidget import MainWindow, State


class ApplicationController(QObject):
    """Wire the GUI to a worker without running camera operations on the GUI thread."""

    search_requested = Signal(dict)
    connect_requested = Signal(dict)
    start_requested = Signal()
    stop_requested = Signal()
    disconnect_requested = Signal()
    shutdown_requested = Signal()

    def __init__(self, window, *, virtual_cti_path=VCTI_PATH,
                 real_cti_path=None, preview_fps=15.0):
        super().__init__(window)
        self.window = window
        self._pending = None
        self._closing = False
        self._shutdown_complete = False
        self._stopped = False
        self.thread = QThread(self)
        self.thread.setObjectName("CameraWorker")
        self.manager = DeviceManager(
            virtual_cti_path=virtual_cti_path,
            real_cti_path=real_cti_path,
            preview_fps=preview_fps,
        )
        self.manager.moveToThread(self.thread)
        self.thread.finished.connect(self.manager.deleteLater)
        self.thread.finished.connect(self._on_thread_finished)

        # MainWindow currently emits a parameterless search request. Read its
        # mode here, on the GUI thread, and send a dict to the worker.
        window.search_requested.connect(self._search)
        window.connect_requested.connect(self._connect)
        window.start_requested.connect(self._start)
        window.stop_requested.connect(self._stop)
        window.disconnect_requested.connect(self._disconnect)
        self.search_requested.connect(self.manager.search_device, Qt.QueuedConnection)
        self.connect_requested.connect(self.manager.connect_device, Qt.QueuedConnection)
        self.start_requested.connect(self.manager.start_capture, Qt.QueuedConnection)
        self.stop_requested.connect(self.manager.stop_capture, Qt.QueuedConnection)
        self.disconnect_requested.connect(self.manager.disconnect_device, Qt.QueuedConnection)
        self.shutdown_requested.connect(self.manager.shutdown, Qt.QueuedConnection)

        self.manager.search_finished.connect(self._search_finished)
        self.manager.connect_finished.connect(self._connect_finished)
        self.manager.start_finished.connect(self._start_finished)
        self.manager.stop_finished.connect(self._stop_finished)
        self.manager.disconnect_finished.connect(self._disconnect_finished)
        self.manager.frame_received.connect(self._frame_received)
        self.manager.fps_updated.connect(self._fps_updated)
        self.manager.error_occurred.connect(self._manager_error)
        self.manager.shutdown_finished.connect(self._shutdown_finished)
        # quit() is thread-safe. This also permits cleanup if the GUI event
        # loop has already exited (there is no dependency on a GUI callback).
        self.manager.shutdown_finished.connect(self._quit_on_success, Qt.DirectConnection)

        window.installEventFilter(self)
        QApplication.instance().installEventFilter(self)
        self.thread.start()

    def _refresh(self):
        self.window.update_ui_state()
        # Recovery from a partial stop/disconnect is supported by the manager.
        if self.window.state == State.ERROR and self._pending is None and not self._closing:
            self.window.disconnect_device_btn.setEnabled(True)

    def _begin(self, operation):
        if self._closing or self._pending is not None:
            return False
        self._pending = operation
        self._refresh()
        return True

    @Slot()
    def _search(self):
        if not self._begin("search"):
            return
        self.window.fps_lists = []
        self._fps_updated(0.0)
        for label in self.window.camera_labels:
            label.clear()
            label.setText("No signal")
        self.search_requested.emit({"virtual": self.window.mode_combo.currentIndex() == 0})

    @Slot(dict)
    def _connect(self, request):
        if not self._begin("connect"):
            return
        payload = dict(request)
        payload.setdefault("virtual", self.window.mode_combo.currentIndex() == 0)
        self.connect_requested.emit(payload)

    @Slot()
    def _start(self):
        if self._begin("start"):
            self.window.fps_lists = [0.0] * len(self.window.fps_lists)
            self._fps_updated(0.0)
            self.start_requested.emit()

    @Slot()
    def _stop(self):
        if self._begin("stop"):
            self.stop_requested.emit()

    @Slot()
    def _disconnect(self):
        if self._begin("disconnect"):
            self.disconnect_requested.emit()

    def _complete(self, callback, result):
        self._pending = None
        callback(result)
        # The worker's state is authoritative, especially for partial failure.
        self.window.state = State[result["state"]]
        self._refresh()
        if self.window.state != State.ACQUIRING:
            self.window.fps_lists = [0.0] * len(self.window.fps_lists)
            self._fps_updated(0.0)

    @Slot(dict)
    def _search_finished(self, result):
        self._complete(self.window.on_search_finished, result)

    @Slot(dict)
    def _connect_finished(self, result):
        self._complete(self.window.on_connect_finished, result)

    @Slot(dict)
    def _start_finished(self, result):
        self._complete(self.window.on_start_finished, result)

    @Slot(dict)
    def _stop_finished(self, result):
        self._complete(self.window.on_stop_finished, result)

    @Slot(dict)
    def _disconnect_finished(self, result):
        self._complete(self.window.on_disconnect_finished, result)

    @Slot(int, QImage, float)
    def _frame_received(self, index, image, fps):
        if self._closing or self.window.state != State.ACQUIRING:
            return
        if not 0 <= index < len(self.window.fps_lists):
            return
        # Manager: (index, image, fps); current GUI: (index, fps, image).
        self.window.on_frame_received(index, fps, image)

    @Slot(float)
    def _fps_updated(self, fps):
        self.window.fps_label.setText(f"{fps:.1f} FPS")

    @Slot(dict)
    def _manager_error(self, result):
        self.window.append_log(result.get("error") or "Acquisition failed")
        self.window.state = State[result["state"]]
        self._refresh()
        self._fps_updated(0.0)

    def eventFilter(self, watched, event):
        closing_window = watched is self.window and event.type() == QEvent.Close
        quitting_app = watched is QApplication.instance() and event.type() == QEvent.Quit
        if (closing_window or quitting_app) and not self._stopped:
            event.ignore()
            self.request_shutdown()
            return True
        return super().eventFilter(watched, event)

    @Slot()
    def request_shutdown(self):
        if self._closing or self._stopped:
            return
        self._closing = True
        self._refresh()
        self.window.device_state_label.setText("Shutting down…")
        self.shutdown_requested.emit()

    @Slot(dict)
    def _quit_on_success(self, result):
        # This slot may run on the worker thread: do not touch GUI objects.
        if result["success"]:
            self.thread.quit()

    @Slot(dict)
    def _shutdown_finished(self, result):
        self._pending = None
        self._shutdown_complete = result["success"]
        if not result["success"]:
            self._closing = False
            self.window.state = State[result["state"]]
            self.window.append_log(result.get("error") or "Shutdown failed; close again to retry")
            self._refresh()

    @Slot()
    def _on_thread_finished(self):
        self._stopped = True
        if self._shutdown_complete:
            self.window.close()
            QApplication.instance().quit()
        else:
            self.window.append_log("Camera worker exited unexpectedly")
            self.window.state = State.ERROR
            self._closing = True
            self._refresh()

    def cleanup(self):
        """Fallback for application.exit()/exceptions bypassing normal window close."""
        if self.thread.isRunning():
            QMetaObject.invokeMethod(self.manager, "shutdown", Qt.BlockingQueuedConnection)
            self.thread.quit()
            self.thread.wait()


def parse_bool(value):
    if value.lower() not in ("true", "false"):
        raise argparse.ArgumentTypeError("Use True or False")
    return value.lower() == "true"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--virtual", type=parse_bool, default=True)
    parser.add_argument("--virtual-cti", type=Path, default=VCTI_PATH)
    parser.add_argument("--real-cti", "--cti", dest="real_cti", type=Path)
    parser.add_argument("--preview-fps", type=float, default=15.0)
    args = parser.parse_args(argv)
    if not args.virtual and args.real_cti is None:
        parser.error("Hardware mode requires --real-cti /path/to/vendor.cti")

    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow()
    window.mode_combo.setCurrentIndex(0 if args.virtual else 1)
    window.setWindowTitle("Camera Manager")
    window.resize(window.main_widget.size())
    controller = ApplicationController(
        window, virtual_cti_path=args.virtual_cti,
        real_cti_path=args.real_cti, preview_fps=args.preview_fps,
    )
    # Let Python's signal handlers run even while Qt is otherwise idle.
    heartbeat = QTimer(controller)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start(100)
    previous_handler = signal.signal(signal.SIGINT, lambda *_: controller.request_shutdown())
    window.show()
    try:
        return app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        controller.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
