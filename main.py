"""Run the camera GUI and its Harvester worker."""

import argparse
from pathlib import Path
import signal
import sys

from PySide6.QtCore import QObject, QEvent, QMetaObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication

from config.path import VCTI_PATH
from device.device_manager import DeviceManager
from gui.mainwidget import MainWindow
from gui.settings import SettingsDialog


class ApplicationController(QObject):
    """Wire the GUI to a worker without running camera operations on the GUI thread."""

    shutdown_requested = Signal()

    def __init__(self, window, *, virtual_cti_path=VCTI_PATH,
                 real_cti_path=None, preview_fps=30.0):
        super().__init__(window)
        self.window = window
        self.settings_dialog = SettingsDialog(window)
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

        window.search_requested.connect(self.manager.search_device, Qt.QueuedConnection)
        window.connect_requested.connect(self.manager.connect_device, Qt.QueuedConnection)
        window.start_requested.connect(self.manager.start_capture, Qt.QueuedConnection)
        window.stop_requested.connect(self.manager.stop_capture, Qt.QueuedConnection)
        window.disconnect_requested.connect(self.manager.disconnect_device, Qt.QueuedConnection)
        window.open_settings_requested.connect(self.settings_dialog.open)
        self.settings_dialog.apply_requested.connect(self._settings_apply_started)
        self.settings_dialog.apply_requested.connect(
            self.manager.apply_settings, Qt.QueuedConnection,
        )
        self.manager.settings_apply_finished.connect(
            self._settings_apply_finished, Qt.QueuedConnection,
        )
        self.shutdown_requested.connect(self.manager.shutdown, Qt.QueuedConnection)

        self.manager.search_finished.connect(window.on_search_finished)
        self.manager.connect_finished.connect(window.on_connect_finished)
        self.manager.camera_settings_received.connect(
            self.settings_dialog.on_camera_settings_received, Qt.QueuedConnection,
        )
        self.manager.start_finished.connect(window.on_start_finished)
        self.manager.stop_finished.connect(window.on_stop_finished)
        self.manager.disconnect_finished.connect(window.on_disconnect_finished)
        self.manager.disconnect_finished.connect(self.settings_dialog.on_disconnected)
        self.manager.frame_received.connect(window.on_frame_received)
        self.manager.error_occurred.connect(window.on_error_occurred)
        self.manager.shutdown_finished.connect(window.on_shutdown_finished)
        self.manager.shutdown_finished.connect(self.settings_dialog.on_disconnected)
        self.manager.shutdown_finished.connect(self._shutdown_finished)
        # quit() is thread-safe. This also permits cleanup if the GUI event
        # loop has already exited (there is no dependency on a GUI callback).
        self.manager.shutdown_finished.connect(self._quit_on_success, Qt.DirectConnection)

        window.installEventFilter(self)
        QApplication.instance().installEventFilter(self)
        self.thread.start()

    @Slot(dict)
    def _settings_apply_started(self, pending):
        self.window._settings_applying = True
        self.window.update_ui_state()

    @Slot(dict)
    def _settings_apply_finished(self, result):
        self.window._settings_applying = False
        self.window.update_ui_state()
        self.window.append_log("Settings applied." if result["success"] else
                               f"Settings apply failed: {result['error']}")

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
        self.window.device_state_label.setText("Shutting down…")
        self.shutdown_requested.emit()

    @Slot(dict)
    def _quit_on_success(self, result):
        # This slot may run on the worker thread: do not touch GUI objects.
        if result["success"]:
            self.thread.quit()

    @Slot(dict)
    def _shutdown_finished(self, result):
        self._shutdown_complete = result["success"]
        if not result["success"]:
            self._closing = False

    @Slot()
    def _on_thread_finished(self):
        self._stopped = True
        if self._shutdown_complete:
            self.window.close()
            QApplication.instance().quit()
        else:
            self.window.on_error_occurred({
                "success": False, "error": "Camera worker exited unexpectedly",
            })
            self._closing = True

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
    parser.add_argument("--preview-fps", type=float, default=30.0)
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
