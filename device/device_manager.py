"""Harvester camera manager. Move this QObject to a QThread before using it.

Search: {"virtual": bool}
Connect: {"framegrabbers": search_result["framegrabbers"], "virtual": bool (optional)}
All finished signals carry {"success": bool, "error": str | None, "state": str}.
"""

from copy import deepcopy
import io
import logging
from pathlib import Path
from time import perf_counter

import numpy as np
from genicam.gentl import GenericException
from harvesters.core import Harvester
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QImage

from config.path import VCTI_PATH


class DeviceManager(QObject):
    search_finished = Signal(dict)
    connect_finished = Signal(dict)
    disconnect_finished = Signal(dict)
    start_finished = Signal(dict)
    stop_finished = Signal(dict)
    shutdown_finished = Signal(dict)
    frame_received = Signal(int, QImage, float)  # Camera index, image, received FPS.
    fps_updated = Signal(float)  # Mean received FPS per connected camera.
    error_occurred = Signal(dict)  # Unsolicited acquisition/cleanup errors.

    def __init__(self, *, virtual_cti_path=VCTI_PATH, real_cti_path=None,
                 preview_size=(640, 480), preview_fps=15.0, parent=None):
        super().__init__(parent)
        self._virtual_cti = Path(virtual_cti_path).resolve()
        self._real_cti = Path(real_cti_path).resolve() if real_cti_path else None
        if (len(preview_size) != 2 or any(type(n) is not int or n < 1 for n in preview_size)):
            raise ValueError("preview_size must contain two positive integers")
        if not np.isfinite(preview_fps) or not 0 < preview_fps <= 120:
            raise ValueError("preview_fps must be between 0 and 120")
        self._preview_size = tuple(preview_size)
        self._preview_period = 1.0 / preview_fps
        self._harvester = None
        self._virtual = None
        self._framegrabbers = []
        self._device_infos = {}
        self._cameras = []
        self._state = "INITIAL"
        self._closed = False
        self._next_camera = 0
        self._next_preview = []
        self._frame_counts = []
        self._camera_fps = []
        self._fps_start = perf_counter()
        self._capture_timer = QTimer(self)
        self._capture_timer.setInterval(5)
        self._capture_timer.timeout.connect(self._poll_frames)

    def _check_thread(self):
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Call DeviceManager through queued Qt signals in its own thread")
        if self._closed:
            raise RuntimeError("DeviceManager has been shut down")

    def _finish(self, signal, success, error=None, **fields):
        signal.emit({"success": success, "error": str(error) if error else None,
                     "state": self._state, **fields})

    @staticmethod
    def _mode(request):
        if not isinstance(request, dict) or type(request.get("virtual")) is not bool:
            raise ValueError('Request must contain "virtual": True or False (a bool)')
        return request["virtual"]

    @staticmethod
    def _optional_info(device, name, fallback):
        try:
            return str(getattr(device, name)) or fallback
        except (AttributeError, GenericException):
            return fallback

    @Slot(dict)
    def search_device(self, request: dict):
        """Refresh through Harvester; the virtual CTI reads JSON, not this class."""
        try:
            self._check_thread()
            if self._cameras:
                raise RuntimeError("Disconnect cameras before searching again")
            virtual = self._mode(request)
            path = self._virtual_cti if virtual else self._real_cti
            # A failed search invalidates the previous GUI discovery result.
            self._framegrabbers = []
            self._device_infos = {}
            self._state = "INITIAL"
            if self._harvester is not None:
                self._harvester.reset()
                self._harvester = None
            self._virtual = None
            if path is None:
                raise ValueError("Set real_cti_path to the manufacturer's CTI when creating DeviceManager")
            if not path.is_file():
                raise FileNotFoundError(f"CTI file does not exist: {path}")

            h = self._harvester = Harvester()
            h.add_file(str(path), check_existence=True, check_validity=True)
            # Harvester 1.4.2 logs some enumeration failures instead of raising.
            # Capture those failures without confusing them with zero cameras.
            messages = io.StringIO()
            handler = logging.StreamHandler(messages)
            handler.setLevel(logging.WARNING)
            owner_thread = QThread.currentThread()
            handler.addFilter(lambda record: QThread.currentThread() == owner_thread)
            logger = logging.getLogger("harvesters.core")
            logger.addHandler(handler)
            try:
                h.update()
            finally:
                logger.removeHandler(handler)
                handler.close()
            if messages.getvalue().strip():
                raise RuntimeError(messages.getvalue().strip())
            # Harvester has no public interface-list property. These internals
            # also preserve interfaces which currently contain zero cameras.
            if not h.has_revised_device_info_list or not h._systems:
                raise RuntimeError("CTI discovery failed; check the producer and its configuration")
            expected = sum(len(system.interface_info_list) for system in h._systems)
            if len(h._ifaces) != expected:
                raise RuntimeError("One or more interfaces could not be opened")
            boards = {}
            for interface in h._ifaces:
                board_id = str(interface.id_)
                if board_id in boards:
                    raise ValueError(f"Ambiguous interface ID: {board_id}")
                boards[board_id] = {
                    "id": board_id,
                    "display_name": self._optional_info(interface, "display_name", board_id),
                    "cameras": [],
                }
            for info in h.device_info_list:
                board_id, camera_id = str(info.parent.id_), str(info.id_)
                key = (board_id, camera_id)
                if key in self._device_infos:
                    raise ValueError(f"Ambiguous camera ID: {key}")
                camera = {
                    "id": camera_id,
                    "serial_number": self._optional_info(info, "serial_number", camera_id),
                    "user_defined_name": self._optional_info(info, "user_defined_name", camera_id),
                    "vendor": self._optional_info(info, "vendor", ""),
                    "model": self._optional_info(info, "model", ""),
                }
                boards[board_id]["cameras"].append(camera)
                self._device_infos[key] = info
            self._framegrabbers = list(boards.values())
            self._virtual = virtual
            self._state = "SEARCHED"
        except Exception as error:
            # Invalid requests while connected must not destroy live cameras.
            if not self._cameras:
                self._framegrabbers = []
                self._device_infos = {}
                self._state = "INITIAL"
            self._finish(self.search_finished, False, error, framegrabbers=[])
            return
        self._finish(self.search_finished, True, framegrabbers=deepcopy(self._framegrabbers),
                     virtual=self._virtual)

    @Slot(dict)
    def connect_device(self, request: dict):
        opened = []
        try:
            self._check_thread()
            if self._state != "SEARCHED" or self._cameras:
                raise RuntimeError("Search first and disconnect existing cameras before connecting")
            if not isinstance(request, dict) or not isinstance(request.get("framegrabbers"), list):
                raise ValueError('Connect requires {"framegrabbers": [...]}')
            if "virtual" in request and self._mode(request) != self._virtual:
                raise ValueError("Mode differs from the last search; search again in the desired mode")
            selection = []
            for board in request["framegrabbers"]:
                for camera in board["cameras"]:
                    key = (board["id"], camera["id"])
                    if key not in self._device_infos:
                        raise ValueError(f"Unknown/stale camera: {key}; search again")
                    info = self._device_infos[key]
                    serial = self._optional_info(info, "serial_number", str(info.id_))
                    if camera["serial_number"] != serial:
                        raise ValueError(f"Camera serial changed: {key}; search again")
                    selection.append(key)
            if not selection:
                raise ValueError("No cameras were found")
            if len(selection) != len(set(selection)) or set(selection) != set(self._device_infos):
                raise ValueError("Connect must contain all discovered cameras exactly once")
            if len(selection) > 12:
                raise ValueError("The current GUI supports at most 12 camera previews")
            settings = request.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError("settings must be a dict of GenICam feature names and values")
            self._state = "CONNECTING"
            for key in selection:
                ia = self._harvester.create(self._device_infos[key])
                opened.append(ia)
                if len(ia.data_streams) != 1:
                    raise ValueError(f"{key}: only one image stream per camera is supported")
                nm = ia.remote_device.node_map
                # This manager captures continuously, without software triggers.
                for name, value in (("AcquisitionMode", "Continuous"), ("TriggerMode", "Off")):
                    node = getattr(nm, name, None)
                    if node is not None and node.value != value:
                        node.value = value
                for name, value in settings.items():
                    if name in ("AcquisitionMode", "TriggerMode"):
                        raise ValueError("This manager requires Continuous acquisition and TriggerMode Off")
                    getattr(nm, name).value = value
                ia.num_buffers = 3
                ia.timeout_period_on_update_event_data_call = 1  # ms, not an infinite wait
            self._cameras = opened
            self._state = "CONNECTED"
        except Exception as error:
            cleanup_errors = []
            for ia in reversed(opened):
                try:
                    ia.destroy()
                except Exception as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))
                    self._cameras.append(ia)
            if self._state == "CONNECTING":
                self._state = "ERROR" if cleanup_errors else "SEARCHED"
            message = str(error)
            if cleanup_errors:
                message += "; rollback failed: " + "; ".join(cleanup_errors)
            self._finish(self.connect_finished, False, message)
            return
        self._finish(self.connect_finished, True, camera_count=len(self._cameras))

    @Slot()
    def start_capture(self):
        try:
            self._check_thread()
            if self._state != "CONNECTED" or not self._cameras:
                raise RuntimeError("Connect cameras before starting acquisition")
            self._state = "STARTING"
            for camera in self._cameras:
                camera.start()
            self._next_camera = 0
            self._next_preview = [0.0] * len(self._cameras)
            self._frame_counts = [0] * len(self._cameras)
            self._camera_fps = [0.0] * len(self._cameras)
            self._fps_start = perf_counter()
            self._state = "ACQUIRING"
            self._capture_timer.start()
        except Exception as error:
            if self._state == "STARTING":
                # start() can fail after buffers/streams were armed but before
                # is_acquiring() becomes true. Destroy the entire batch so that
                # no half-started native resources survive a retry.
                errors = self._destroy_cameras()
                self._state = "ERROR" if errors else "SEARCHED"
                if errors:
                    error = RuntimeError(f"{error}; rollback failed: {'; '.join(errors)}")
            self._finish(self.start_finished, False, error)
            return
        self._finish(self.start_finished, True)

    def _stop_cameras(self):
        self._capture_timer.stop()
        errors = []
        for camera in reversed(self._cameras):
            try:
                if camera.is_acquiring():
                    camera.stop()
            except Exception as error:
                errors.append(str(error))
        self.fps_updated.emit(0.0)
        return errors

    @Slot()
    def stop_capture(self):
        try:
            self._check_thread()
            if self._state != "ACQUIRING":
                raise RuntimeError("Acquisition is not running")
            errors = self._stop_cameras()
            self._state = "ERROR" if errors else "CONNECTED"
            if errors:
                raise RuntimeError("Could not stop all cameras: " + "; ".join(errors))
        except Exception as error:
            self._finish(self.stop_finished, False, error)
            return
        self._finish(self.stop_finished, True)

    def _destroy_cameras(self):
        errors = self._stop_cameras()
        remaining = []
        for camera in reversed(self._cameras):
            try:
                camera.destroy()
            except Exception as error:
                remaining.append(camera)
                errors.append(str(error))
        self._cameras = list(reversed(remaining))
        return errors

    @Slot()
    def disconnect_device(self):
        try:
            self._check_thread()
            if self._state not in ("CONNECTED", "ERROR"):
                raise RuntimeError("Stop acquisition before disconnecting cameras")
            errors = self._destroy_cameras()
            self._state = "ERROR" if errors else "SEARCHED"
            if errors:
                raise RuntimeError("Could not disconnect all cameras: " + "; ".join(errors))
        except Exception as error:
            self._finish(self.disconnect_finished, False, error)
            return
        self._finish(self.disconnect_finished, True)

    def _to_image(self, component):
        formats = {
            "Mono8": (QImage.Format.Format_Grayscale8, 1, np.dtype("uint8")),
            "Mono16": (QImage.Format.Format_Grayscale16, 2, np.dtype("uint16")),
            "RGB8": (QImage.Format.Format_RGB888, 3, np.dtype("uint8")),
            "BGR8": (QImage.Format.Format_BGR888, 3, np.dtype("uint8")),
            "RGBa8": (QImage.Format.Format_RGBA8888, 4, np.dtype("uint8")),
        }
        name = component.data_format
        if name not in formats:
            raise ValueError(f"Unsupported pixel format {name}; configure Mono8, Mono16, RGB8, BGR8 or RGBa8")
        image_format, bytes_per_pixel, dtype = formats[name]
        width, height = component.width, component.height
        data = np.ascontiguousarray(component.data)
        if data.dtype != dtype or width < 1 or height < 1:
            raise ValueError("Invalid image dimensions or pixel data type")
        row_bytes = width * bytes_per_pixel + component.x_padding
        raw = data.view(np.uint8).reshape(-1)
        if raw.size < row_bytes * height:
            raise ValueError("Incomplete image payload")
        image = QImage(raw.data, width, height, row_bytes, image_format)
        if image.isNull():
            raise ValueError("Cannot create QImage from image payload")
        if width > self._preview_size[0] or height > self._preview_size[1]:
            image = image.scaled(*self._preview_size, Qt.KeepAspectRatio, Qt.FastTransformation)
        # This method is called while the Harvester buffer is still borrowed.
        # Even if no scaling occurred, detach from the reusable transport memory.
        return image.copy()

    @Slot()
    def _poll_frames(self):
        if self._state != "ACQUIRING":
            return
        try:
            deadline = perf_counter() + 0.010
            visited = 0
            while visited < len(self._cameras) and perf_counter() < deadline:
                index = self._next_camera
                self._next_camera = (index + 1) % len(self._cameras)
                visited += 1
                # timeout=0 means an INFINITE wait in Harvester 1.4.2.
                buffer = self._cameras[index].try_fetch(timeout=0.001)
                if buffer is None:
                    continue
                with buffer:
                    self._frame_counts[index] += 1
                    now = perf_counter()
                    if now < self._next_preview[index]:
                        continue
                    if len(buffer.payload.components) != 1:
                        raise ValueError("Only single-component images are supported")
                    image = self._to_image(buffer.payload.components[0])
                    # Keep the preview cadence instead of restarting the wait
                    # after each delivery. Otherwise arrival jitter at the FPS
                    # limit discards alternating frames. Clamp after a stall
                    # so missed intervals never accumulate a catch-up burst.
                    self._next_preview[index] = max(
                        self._next_preview[index] + self._preview_period, now
                    )
                self.frame_received.emit(index, image, self._camera_fps[index])
            now = perf_counter()
            elapsed = now - self._fps_start
            if elapsed >= 1.0:
                self._camera_fps = [count / elapsed for count in self._frame_counts]
                self.fps_updated.emit(sum(self._camera_fps) / len(self._cameras))
                self._frame_counts = [0] * len(self._cameras)
                self._fps_start = now
        except Exception as error:
            errors = self._stop_cameras()
            self._state = "ERROR" if errors else "CONNECTED"
            message = str(error)
            if errors:
                message += "; stop failed: " + "; ".join(errors)
            self._finish(self.error_occurred, False, message)

    @Slot()
    def shutdown(self):
        """Run in the manager thread before quitting it; releases all native resources."""
        try:
            if QThread.currentThread() != self.thread():
                raise RuntimeError("Queue shutdown to the manager thread")
            errors = self._destroy_cameras()
            if self._harvester is not None:
                try:
                    self._harvester.reset()
                    self._harvester = None
                    self._cameras = []
                except Exception as error:
                    errors.append(str(error))
            self._device_infos = {}
            self._framegrabbers = []
            self._closed = not errors
            self._state = "ERROR" if errors else "INITIAL"
            if errors:
                raise RuntimeError("Shutdown failed: " + "; ".join(errors))
        except Exception as error:
            self._finish(self.shutdown_finished, False, error)
            return
        self._finish(self.shutdown_finished, True)
