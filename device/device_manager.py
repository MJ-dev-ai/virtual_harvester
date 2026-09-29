"""Per-camera acquisition threads and frame buffers shared within one process.

Workers write frames under frame_buffer.lock. The manager is intended to read
and resize frames under the same lock and emit independently owned QImages
for the GUI.
"""

from threading import Event, Lock, Thread
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QImage
import io
import logging
from copy import deepcopy
from math import isclose, isfinite
from pathlib import Path
import numpy as np
from time import perf_counter
from harvesters.core import Harvester
from genicam import genapi


SETTINGS_FEATURES = (
    "ExposureAuto", "ExposureTime", "GainAuto", "Gain",
    "Width", "Height", "OffsetX", "OffsetY", "PixelFormat", "BalanceWhiteAuto",
    "AcquisitionFrameRateEnable", "AcquisitionFrameRate",
    "TriggerMode", "TriggerSelector", "TriggerSource", "TriggerActivation",
)

class FrameBuffer:
    """Store one camera's frame and lock; frame_number is zero until the first frame."""

    def __init__(self, shape, dtype=np.uint8):
        self.lock = Lock()
        self.frame = np.empty(shape, dtype=dtype)
        self.frame_number = 0
        self.timestamp = None


class CameraWorker(Thread):
    """Own one camera; start()/stop() control acquisition and shutdown() ends the thread."""

    def __init__(self, ia, settings=None):
        super().__init__()
        self._ia = ia
        self.settings = settings if settings is not None else {}
        node_map = ia.remote_device.node_map
        for name, value in self.settings.items():
            getattr(node_map, name).value = value
        self.pixel_format = node_map.PixelFormat.value
        if self.pixel_format not in ("Mono8", "RGB8"):
            raise ValueError("Only Mono8 and RGB8 images are supported")
        shape = (int(node_map.Height.value), int(node_map.Width.value))
        if self.pixel_format == "RGB8":
            shape += (3,)
        self.frame_buffer = FrameBuffer(
            shape=shape,
            dtype=np.uint8
        )
        self._start_event = Event()
        self._stop_event = Event()
        self._shutdown_event = Event()
        self.error = None  # Exception from settings, acquisition, or cleanup; None if no error.

    def run(self):
        """Acquire Mono8 or interleaved RGB8 frames on request; the manager destroys the image acquirer."""
        try:
            while not self._shutdown_event.is_set():
                self._start_event.wait()
                if (
                    not self._shutdown_event.is_set()
                    and not self._stop_event.is_set()
                ):
                    with self.frame_buffer.lock:
                        self.frame_buffer.frame.fill(0)
                        self.frame_buffer.frame_number = 0
                        self.frame_buffer.timestamp = None
                    try:
                        self._ia.start()
                        while (
                            not self._stop_event.is_set()
                            and not self._shutdown_event.is_set()
                        ):
                            # GenTL waits can hold the GIL. Poll without a native
                            # wait and sleep in Python when no frame is ready.
                            # Harvester treats timeout=0 as an unlimited retry,
                            # so its outer retry budget must remain positive.
                            buffer = self._ia.try_fetch(timeout=0.0001)
                            if buffer is None:
                                self._stop_event.wait(0.005)
                                continue
                            with buffer:
                                if len(buffer.payload.components) != 1:
                                    raise ValueError("Expected exactly one payload component")
                                component = buffer.payload.components[0]
                                if component.data_format != self.pixel_format or component.x_padding != 0:
                                    raise ValueError("Unexpected pixel format or unsupported row padding")
                                shape = (component.height, component.width)
                                if self.pixel_format == "RGB8":
                                    shape += (3,)
                                frame = component.data.reshape(shape)

                                with self.frame_buffer.lock:
                                    if frame.shape != self.frame_buffer.frame.shape:
                                        raise ValueError("Frame shape does not match the allocated frame buffer")
                                    np.copyto(self.frame_buffer.frame, frame, casting="no")
                                    self.frame_buffer.frame_number += 1
                                    self.frame_buffer.timestamp = perf_counter()
                    finally:
                        self._ia.stop()
        except Exception as error:
            self.error = error
    
    def start(self):
        """Start the thread once and request acquisition on this or a later call."""
        if self._shutdown_event.is_set():
            raise RuntimeError("Cannot start a camera worker after shutdown has been requested")
        if self.ident is None:
            super().start()
        elif not self.is_alive():
            raise RuntimeError("Camera worker has exited; disconnect and reconnect the camera")
        self._stop_event.clear()
        self._start_event.set()

    def stop(self):
        """Request acquisition to stop without terminating the worker thread."""
        self._start_event.clear()
        self._stop_event.set()
    
    def shutdown(self):
        """Request thread shutdown and wake the worker if it is waiting to start."""
        self._shutdown_event.set()
        self._start_event.set()


class DeviceManager(QObject):
    """Manage device discovery, camera workers, and frame buffer lifetimes."""
    search_finished = Signal(dict)
    connect_finished = Signal(dict)
    disconnect_finished = Signal(dict)
    start_finished = Signal(dict)
    stop_finished = Signal(dict)
    shutdown_finished = Signal(dict)
    frame_received = Signal(int, QImage, float)
    error_occurred = Signal(dict)
    camera_settings_received = Signal(dict)
    settings_apply_finished = Signal(dict)

    def __init__(self, virtual_cti_path, real_cti_path=None, preview_fps=30.0):
        super().__init__()
        self._applying_settings = False
        self._harvester = None
        self._vcti_path = virtual_cti_path
        self._rcti_path = real_cti_path
        self._device_infos = []
        self._cameras = {}
        self._frame_buffers = {}
        self._previous_frame_info = {}
        if not isfinite(preview_fps) or preview_fps <= 0:
            raise ValueError("Preview FPS must be a finite positive number")
        self._process_timer = QTimer(self)
        self._process_timer.setInterval(max(1, round(1000 / preview_fps)))
        self._process_timer.timeout.connect(self._process_frames)

    @Slot(dict)
    def search_device(self, request):
        """Discover interfaces and cameras using the selected CTI producer."""
        try:
            if QThread.currentThread() != self.thread():
                raise RuntimeError("This method must run in the DeviceManager Qt thread; use a queued signal when calling from another thread")
            if self._cameras:
                raise RuntimeError("Disconnect cameras before searching again")
            cti_path = self._vcti_path if request["virtual"] else self._rcti_path
            if cti_path is None or not cti_path.is_file():
                raise FileNotFoundError(f"CTI path is unset or does not point to a file: {cti_path}")
            if self._harvester is not None:
                self._harvester.reset()
            self._harvester = Harvester()
            self._harvester.add_file(str(cti_path), check_existence=True, check_validity=True)

            messages = io.StringIO()
            handler = logging.StreamHandler(messages)
            handler.setLevel(logging.WARNING)
            logger = logging.getLogger("harvesters.core")
            logger.addHandler(handler)
            try:
                self._harvester.update()
            finally:
                logger.removeHandler(handler)
                handler.close()
            if messages.getvalue().strip():
                raise RuntimeError(messages.getvalue().strip())

            if not self._harvester.has_revised_device_info_list or not self._harvester._systems:
                raise RuntimeError("Device discovery failed: no GenTL system was opened or the device list was not refreshed")
            
            self._device_infos = []
            for interface in self._harvester._ifaces:
                framegrabber = {
                    "id": str(interface.id_),
                    "display_name": getattr(interface, "display_name", None) or str(interface.id_),
                    "cameras": []
                }
                self._device_infos.append(framegrabber)

            for info in self._harvester.device_info_list:
                camera = {
                    "id": str(info.id_),
                    "serial_number": getattr(info, "serial_number", None) or str(info.id_),
                    "user_defined_name": getattr(info, "user_defined_name", None) or str(info.id_),
                    "vendor": getattr(info, "vendor", None) or "",
                    "model": getattr(info, "model", None) or ""
                }
                for fg in self._device_infos:
                    if fg["id"] == str(info.parent.id_):
                        fg["cameras"].append(camera)
                        break
            self.search_finished.emit({
                "success": True,
                "error": None,
                "framegrabbers": self._device_infos
            })
        except Exception as e:
            self.search_finished.emit({
                "success": False,
                "error": str(e),
                "framegrabbers": []
            })

    @Slot(dict)
    def connect_device(self, request: dict):
        """Open all discovered cameras and prepare their buffers and workers."""
        opened = []
        cameras = {}
        frame_buffers = {}
        try:
            if QThread.currentThread() != self.thread():
                raise RuntimeError("This method must run in the DeviceManager Qt thread; use a queued signal when calling from another thread")
            settings = request.get("settings", {"AcquisitionMode": "Continuous", "TriggerMode": "Off"})
            if not isinstance(settings, dict):
                raise ValueError("Camera settings must be a dictionary")
            if self._cameras:
                raise RuntimeError("Cameras are already connected; disconnect them before connecting again")
            if self._harvester is None or not self._harvester.device_info_list:
                raise RuntimeError("No discovered cameras are available to connect; search for cameras first")
            if len(self._harvester.device_info_list) > 12:
                raise ValueError(f"Cannot connect more than 12 cameras (discovered: {len(self._harvester.device_info_list)})")
            for info in self._harvester.device_info_list:
                key = (str(info.parent.id_), str(info.id_))
                ia = self._harvester.create(info)
                opened.append(ia)

                if len(ia.data_streams) != 1:
                    raise ValueError(f"{key}: Expected exactly one data stream per camera")
                ia.num_buffers = 3
                # CameraWorker supplies the interruptible wait between polls.
                ia.timeout_period_on_update_event_data_call = 0
                camera = CameraWorker(ia, settings=settings)
                cameras[key] = camera
                frame_buffers[key] = camera.frame_buffer

            self._cameras = cameras
            self._frame_buffers = frame_buffers
            self.connect_finished.emit({"success": True, "error": None})
            self.emit_camera_settings()
        except Exception as e:
            cleanup_errors = []
            for ia in reversed(opened):
                try:
                    ia.destroy()
                except Exception as cleanup_error:
                    cleanup_errors.append(str(cleanup_error))

            message = str(e)
            if cleanup_errors:
                message += "; cleanup failed: " + "; ".join(cleanup_errors)
            self.connect_finished.emit({"success": False, "error": message})

    @staticmethod
    def _read_setting(node_map, name):
        """Return plain data only; GenApi objects stay on the worker side."""
        result = {
            "supported": False, "available": False, "readable": False,
            "writable": False, "type": None, "value": None,
            "min": None, "max": None, "inc": None, "unit": "",
            "choices": [], "error": None,
        }
        try:
            node = node_map.get_node(name)
        except genapi.GenericException:
            return result
        if node is None:
            return result
        try:
            result["supported"] = genapi.is_implemented(node)
            result["available"] = genapi.is_available(node)
            result["readable"] = genapi.is_readable(node)
            result["writable"] = genapi.is_writable(node)
            for node_type, kind in ((genapi.IEnumeration, "enum"),
                                    (genapi.IBoolean, "bool"),
                                    (genapi.IInteger, "int"),
                                    (genapi.IFloat, "float")):
                if isinstance(node, node_type):
                    result["type"] = kind
                    break
            if not result["available"]:
                return result
            if result["type"] == "enum":
                result["choices"] = [entry.symbolic for entry in node.entries
                                     if genapi.is_available(entry)]
            if result["readable"]:
                result["value"] = node.value
                if result["type"] in ("int", "float"):
                    result.update(min=node.min, max=node.max, unit=node.unit)
                    if result["type"] == "int" or node.has_inc():
                        result["inc"] = node.inc
        except Exception as error:
            result["error"] = str(error)
            result["writable"] = False
        return result

    @Slot()
    def emit_camera_settings(self):
        """Publish settings grouped by vendor/model, keeping each camera's values."""
        try:
            if QThread.currentThread() != self.thread():
                raise RuntimeError("Request settings through a queued signal")
            groups = {}
            for (board_id, camera_id), camera in self._cameras.items():
                node_map = camera._ia.remote_device.node_map
                identity = []
                for name in ("DeviceVendorName", "DeviceModelName"):
                    try:
                        identity.append(str(getattr(node_map, name).value))
                    except genapi.GenericException:
                        identity.append("")
                vendor, model = identity
                # Do not combine unidentified cameras into an invented model.
                key = (vendor, model) if vendor and model else (board_id, camera_id)
                group = groups.setdefault(key, {
                    "vendor": vendor, "model": model, "cameras": [],
                })
                group["cameras"].append({
                    "framegrabber_id": board_id,
                    "camera_id": camera_id,
                    "settings": {name: self._read_setting(node_map, name)
                                 for name in SETTINGS_FEATURES},
                })
            for group in groups.values():
                group["settings"] = {
                    name: self._common_setting([
                        camera["settings"][name] for camera in group["cameras"]
                    ]) for name in SETTINGS_FEATURES
                }
            self.camera_settings_received.emit({
                "success": True, "error": None, "groups": list(groups.values()),
            })
        except Exception as error:
            self.camera_settings_received.emit({
                "success": False, "error": str(error), "groups": [],
            })

    @staticmethod
    def _common_setting(features):
        """Describe the values that can safely be offered to the whole model group."""
        result = deepcopy(features[0])
        for flag in ("supported", "available", "readable", "writable"):
            result[flag] = all(feature[flag] for feature in features)
        result["mixed"] = any(f["value"] != result["value"] for f in features)
        if result["mixed"] or not result["readable"]:
            result["value"] = None
        errors = list(dict.fromkeys(f["error"] for f in features if f["error"]))
        result["error"] = "; ".join(errors) or None
        if errors:
            result["writable"] = False
            return result
        if not result["supported"] or not result["available"]:
            result["writable"] = False
            return result
        if any(f["type"] != result["type"] or f["unit"] != result["unit"] for f in features):
            result.update(writable=False, error="Camera feature types or units differ")
        elif result["type"] == "enum":
            result["choices"] = [choice for choice in result["choices"]
                                 if all(choice in f["choices"] for f in features)]
            result["writable"] &= bool(result["choices"])
        elif result["type"] in ("int", "float") and result["readable"]:
            result["min"] = max(f["min"] for f in features)
            result["max"] = min(f["max"] for f in features)
            if result["min"] > result["max"]:
                result.update(writable=False, error="No common numeric range")
                return result
            # Equal bounds alone do not guarantee compatible stepped values.
            # Keep incompatible grids read-only instead of offering invalid edits.
            step = result["inc"]
            if any(f["inc"] != step for f in features):
                result.update(writable=False, error="Camera increments differ")
            elif step:
                if result["type"] == "int":
                    if any((result["min"] - f["min"]) % step for f in features):
                        result.update(writable=False, error="Camera increment origins differ")
                    else:
                        result["max"] -= (result["max"] - result["min"]) % step
                elif any(not isclose((result["min"] - f["min"]) / step,
                                   round((result["min"] - f["min"]) / step),
                                   rel_tol=0, abs_tol=1e-7) for f in features):
                    result.update(writable=False, error="Camera increment origins differ")
                else:
                    result["max"] = result["min"] + int(
                        (result["max"] - result["min"]) / step + 1e-9
                    ) * step
        return result

    @Slot(dict)
    def apply_settings(self, pending_settings):
        """Apply GenApi values with acquisition stopped; always publish actual values.

        Writes are not transactional: if a camera rejects a value, preceding
        writes remain applied and the refreshed snapshot reflects that state.
        Queued manager operations run only after this slot returns.
        """
        if QThread.currentThread() != self.thread():
            raise RuntimeError("Apply settings through a queued signal")
        if self._applying_settings:
            return
        self._applying_settings = True
        resume = self._process_timer.isActive()
        stopped = False
        pause_requested = False
        errors = []
        try:
            if not self._cameras:
                raise ValueError("Connect cameras before applying settings")
            targets = {}
            for group in pending_settings["groups"]:
                values = group["settings"]
                if set(values) - set(SETTINGS_FEATURES):
                    raise ValueError("Unknown camera setting name")
                if "PixelFormat" in values and values["PixelFormat"] not in ("Mono8", "RGB8"):
                    raise ValueError("The current image pipeline supports only Mono8 and RGB8")
                for target in group["cameras"]:
                    key = (target["framegrabber_id"], target["camera_id"])
                    if key not in self._cameras or key in targets:
                        raise ValueError(f"Invalid or duplicate camera target: {key}")
                    targets[key] = values
            if not targets:
                return
            pause_requested = True
            self._process_timer.stop()
            # Joining removes the stop/start race and ensures TLParamsLocked
            # has been released before any GenApi writes or buffer changes.
            for camera in self._cameras.values():
                camera.shutdown()
            deadline = perf_counter() + 2.0
            for camera in self._cameras.values():
                if camera.ident is not None:
                    camera.join(max(0.0, deadline - perf_counter()))
                if camera.is_alive():
                    raise RuntimeError("Camera did not stop before settings timeout; disconnect and reconnect")
            stopped = True
            for camera in self._cameras.values():
                if camera.error is not None:
                    raise RuntimeError(str(camera.error))
            # Selectors and automatic-mode controls precede dependent values.
            order = ("ExposureAuto", "GainAuto", "BalanceWhiteAuto",
                     "AcquisitionFrameRateEnable", "TriggerSelector", "TriggerMode",
                     "TriggerSource", "TriggerActivation", "PixelFormat",
                     "OffsetX", "OffsetY", "Width", "Height", "ExposureTime",
                     "Gain", "AcquisitionFrameRate")
            for key, values in targets.items():
                nm = self._cameras[key]._ia.remote_device.node_map
                for name in order:
                    if name not in values:
                        continue
                    meta = self._read_setting(nm, name)
                    value = values[name]
                    if not meta["writable"] or meta["error"]:
                        raise ValueError(f"{key}: {name} is not writable")
                    if meta["type"] == "enum" and value not in meta["choices"]:
                        raise ValueError(f"{key}: invalid {name} symbol: {value}")
                    if meta["type"] == "bool" and type(value) is not bool:
                        raise ValueError(f"{key}: {name} requires a boolean")
                    if meta["type"] in ("int", "float"):
                        if (type(value) not in (int, float) or not isfinite(value)
                                or not meta["min"] <= value <= meta["max"]):
                            raise ValueError(f"{key}: {name} is outside its current range")
                        if meta["type"] == "int" and int(value) != value:
                            raise ValueError(f"{key}: {name} requires an integer")
                    try:
                        nm.get_node(name).value = value
                    except Exception as error:
                        raise RuntimeError(f"{key}: {name}: {error}") from error
        except Exception as error:
            errors.append(str(error))
        finally:
            if stopped:
                # Rebuild even after partial failure, using actual camera sizes.
                for key, camera in list(self._cameras.items()):
                    try:
                        replacement = CameraWorker(camera._ia)
                        self._cameras[key] = replacement
                        self._frame_buffers[key] = replacement.frame_buffer
                        self._previous_frame_info[key] = (0, perf_counter())
                    except Exception as error:
                        errors.append(f"{key}: buffer update failed: {error}")
            self._applying_settings = False
            self.emit_camera_settings()
            if resume and stopped and not errors:
                self.start_capture()
            elif resume and pause_requested and errors:
                self.error_occurred.emit({"success": False, "error": "; ".join(errors)})
            self.settings_apply_finished.emit({
                "success": not errors, "error": "; ".join(errors) or None,
            })

    @Slot()
    def start_capture(self):
        """Request acquisition on each camera worker."""
        if self._applying_settings:
            return
        started = []
        try:
            if QThread.currentThread() != self.thread():
                raise RuntimeError("This method must run in the DeviceManager Qt thread; use a queued signal when calling from another thread")
            if self._cameras is None or len(self._cameras) == 0:
                raise ValueError("Connect cameras before starting acquisition")

            for key, camera in self._cameras.items():
                self._previous_frame_info[key] = (0, perf_counter())
                started.append((key, camera))
                camera.start()
            self._process_timer.start()
            self.start_finished.emit({"success": True, "error": None})
        except Exception as e:
            self._process_timer.stop()
            errors = [str(e)]
            for key, camera in reversed(started):
                try:
                    camera.stop()
                except Exception as cleanup_error:
                    errors.append(f"{key}: stop request failed: {cleanup_error}")

            self.start_finished.emit({"success": False, "error": "; ".join(errors)})

    @Slot()
    def stop_capture(self):
        """Request acquisition to stop on all workers."""
        try:
            
            if QThread.currentThread() != self.thread():
                raise RuntimeError("This method must run in the DeviceManager Qt thread")
            self._process_timer.stop()
            if self._cameras is None:
                raise ValueError("Connect cameras before stopping acquisition")
            errors = []
            for key, camera in self._cameras.items():
                try:
                    camera.stop()
                except Exception as e:
                    errors.append(f"{key}: {str(e)}")
            if errors:
                raise RuntimeError("; ".join(errors))
            self.stop_finished.emit({"success": True, "error": None})
        except Exception as error:
            self.stop_finished.emit({"success": False, "error": str(error)})

    @Slot()
    def _process_frames(self):
        errors = []
        for index, (key, camera) in enumerate(self._cameras.items()):
            try:
                if camera.error is not None:
                    errors.append(f"{key}: {camera.error}")
                    continue
                frame_buffer = camera.frame_buffer

                with frame_buffer.lock:
                    frame_number = frame_buffer.frame_number
                    timestamp = frame_buffer.timestamp
                    prev_frame_number, prev_timestamp = self._previous_frame_info.get(key)
                    if frame_number == 0 or timestamp is None:
                        image = QImage()
                        fps = 0.0
                    else:
                        if prev_timestamp is not None and frame_number == prev_frame_number:
                            continue
                
                        elapsed = timestamp - prev_timestamp
                        if elapsed <= 0:
                            continue
                        fps = (frame_number - prev_frame_number) / elapsed
                

                        frame = frame_buffer.frame
                        height, width = frame.shape[:2]

                        image = QImage(
                            frame.data,
                            width, height,
                            frame.strides[0],
                            (QImage.Format.Format_RGB888 if camera.pixel_format == "RGB8"
                             else QImage.Format.Format_Grayscale8),
                        )
                        if width > 640 or height > 480:
                            image = image.scaled(
                                640, 480,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.FastTransformation,
                            )
                        image = image.copy()
                        self._previous_frame_info[key] = (frame_number, timestamp)
                self.frame_received.emit(index, image, fps)
            except Exception as error:
                errors.append(f"{key}: {error}")
        if errors:
            self._process_timer.stop()
            for key, camera in self._cameras.items():
                try:
                    camera.stop()
                except Exception as cleanup_error:
                    errors.append(f"{key}: stop request failed: {cleanup_error}")
            self.error_occurred.emit({"success": False, "error": "; ".join(errors)})

    def _release_cameras(self):
        """Stop workers and release cameras; preserve failed resources for retry."""
        if QThread.currentThread() != self.thread():
            raise RuntimeError("This method must run in the DeviceManager Qt thread; use a queued signal when calling from another thread")
        self._process_timer.stop()
        cameras = list(self._cameras.items())
        errors = []

        # Request shutdown on all workers before waiting so they can exit concurrently.
        for key, camera in cameras:
            try:
                camera.shutdown()
            except Exception as error:
                errors.append(f"{key}: shutdown request failed: {error}")

        deadline = perf_counter() + 2.0
        for key, camera in reversed(cameras):
            try:
                # A thread that has never been started cannot be joined.
                if camera.ident is not None:
                    camera.join(timeout=max(0.0, deadline - perf_counter()))
                if camera.is_alive():
                    raise RuntimeError("Capture thread did not exit before the shared shutdown deadline")

                # Destroy the image acquirer only after its worker has exited.
                camera._ia.destroy()
                del self._cameras[key]
                self._frame_buffers.pop(key, None)
                self._previous_frame_info.pop(key, None)
            except Exception as error:
                # Keep cameras and buffers whose cleanup failed so disconnect can be retried.
                errors.append(f"{key}: {error}")

        if errors:
            raise RuntimeError("; ".join(errors))

    @Slot()
    def disconnect_device(self):
        """Release cameras and report the result."""
        if self._applying_settings:
            return
        try:
            self._release_cameras()
        except Exception as error:
            self.disconnect_finished.emit({"success": False, "error": str(error)})
        else:
            self.disconnect_finished.emit({"success": True, "error": None})

    @Slot()
    def shutdown(self):
        """Release all cameras and the GenTL producer, then report the result."""
        try:
            self._release_cameras()
            # Before discovery, there is no Harvester instance to reset.
            getattr(self._harvester, "reset", lambda: None)()
            self._harvester = None
            self._device_infos.clear()
            self._frame_buffers.clear()
            self._previous_frame_info.clear()
        except Exception as error:
            self.shutdown_finished.emit({"success": False, "error": str(error)})
        else:
            self.shutdown_finished.emit({"success": True, "error": None})
