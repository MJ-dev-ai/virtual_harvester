"""Exercise VirtualFG settings through Harvester/GenApi, without DeviceManager.

Run with a Python environment containing harvesters, genicam and numpy:
    python native/virtualfg/test_settings.py
"""

from pathlib import Path
import unittest

import numpy as np

from genicam import genapi
from harvesters.core import Harvester


CTI = Path(__file__).resolve().parents[2] / "externals/virtualfg/VirtualFG.cti"


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.h = Harvester()
        self.addCleanup(self.h.reset)
        self.h.add_file(str(CTI), check_existence=True, check_validity=True)
        self.h.update()
        self.assertTrue(self.h.device_info_list, "Configure at least one virtual camera")
        self.ia = self.h.create(0)
        self.addCleanup(self.ia.destroy)
        self.nm = self.ia.remote_device.node_map

    def test_default_continuous(self):
        self.assertEqual(self.nm.AcquisitionMode.value, "Continuous")
        self.assertEqual(self.nm.TriggerMode.value, "Off")
        self.ia.start()
        try:
            for _ in range(3):
                with self.ia.fetch(timeout=2) as buffer:
                    component = buffer.payload.components[0]
                    self.assertEqual(component.data_format, "Mono8")
                    self.assertEqual(component.data.size, component.width * component.height)
        finally:
            self.ia.stop()

    def test_settings_and_payload(self):
        settings = {"AcquisitionMode": "Continuous", "TriggerMode": "Off",
                    "Width": 320, "Height": 240, "ExposureTime": 2000.0,
                    "AcquisitionFrameRate": 30.0, "PixelFormat": "Mono8"}
        for name, value in settings.items():
            node = getattr(self.nm, name)
            self.assertTrue(genapi.is_writable(node), name)
            node.value = value
            self.assertEqual(node.value, value)
            self.assertEqual(node.node.name_space, genapi.ENameSpace.Standard)
        self.assertEqual(self.nm.PayloadSize.value, 320 * 240)
        self.ia.start()
        try:
            with self.ia.fetch(timeout=2) as buffer:
                component = buffer.payload.components[0]
                self.assertEqual((component.width, component.height), (320, 240))
        finally:
            self.ia.stop()

    def test_rgb24_payload_channels_and_format_switching(self):
        self.assertEqual(set(self.nm.PixelFormat.symbolics), {'Mono8', 'RGB8'})
        # Odd width exercises packed RGB rows without implicit four-byte padding.
        self.nm.Width.value = 321
        self.nm.Height.value = 241
        for pixel_format, channels in [('RGB8', 3), ('Mono8', 1), ('RGB8', 3)]:
            self.nm.PixelFormat.value = pixel_format
            self.assertEqual(self.nm.PayloadSize.value, 321 * 241 * channels)
            self.ia.start()
            try:
                with self.ia.fetch(timeout=2) as buffer:
                    component = buffer.payload.components[0]
                    self.assertEqual(component.data_format, pixel_format)
                    self.assertEqual(component.x_padding, 0)
                    self.assertEqual(component.data.size, 321 * 241 * channels)
                    self.assertEqual(component.data.dtype, np.uint8)
                    if channels == 3:
                        pixels = component.data.reshape(241, 321, 3)
                        # First camera sees the first (red) circle on a black background.
                        self.assertTrue(np.any(np.all(pixels == (220, 60, 30), axis=2)))
                        self.assertTrue(np.any(np.all(pixels == (0, 0, 0), axis=2)))
            finally:
                self.ia.stop()

    def test_configuration_access_tracks_acquisition(self):
        names = ("Width", "Height", "PixelFormat", "AcquisitionMode",
                 "TriggerMode", "TriggerSelector", "TriggerSource",
                 "ExposureTime", "AcquisitionFrameRate")
        self.nm.TLParamsLocked.value = 1
        self.assertFalse(genapi.is_writable(self.nm.Width))
        self.nm.TLParamsLocked.value = 0
        self.assertTrue(genapi.is_writable(self.nm.Width))
        self.ia.start()
        try:
            for name in names:
                self.assertFalse(genapi.is_writable(getattr(self.nm, name)), name)
            with self.assertRaises(genapi.GenericException):
                self.nm.Width.value = 123
        finally:
            self.ia.stop()
        for name in names:
            self.assertTrue(genapi.is_writable(getattr(self.nm, name)), name)

    def test_software_trigger(self):
        self.assertFalse(genapi.is_available(self.nm.TriggerSoftware))
        self.assertTrue(self.nm.TriggerSelector.node.is_selector())
        selected = {node.node.name for node in self.nm.TriggerSelector.node.selected_features}
        self.assertTrue({"TriggerMode", "TriggerSource", "TriggerSoftware"} <= selected)
        self.nm.TriggerSelector.value = "FrameStart"
        self.nm.TriggerSource.value = "Software"
        self.nm.TriggerMode.value = "On"
        self.assertFalse(genapi.is_available(self.nm.TriggerSoftware))
        self.ia.start()
        try:
            self.assertTrue(genapi.is_available(self.nm.TriggerSoftware))
            self.assertIsNone(self.ia.try_fetch(timeout=0.1))
            for _ in range(2):
                self.nm.TriggerSoftware.execute()
                with self.ia.fetch(timeout=2) as buffer:
                    self.assertEqual(len(buffer.payload.components), 1)
                self.assertIsNone(self.ia.try_fetch(timeout=0.1))
        finally:
            self.ia.stop()
        self.assertFalse(genapi.is_available(self.nm.TriggerSoftware))

    def test_invalid_and_fixed_settings(self):
        for name, value in (("Width", 0), ("Height", 8193),
                            ("ExposureTime", 0.0), ("AcquisitionFrameRate", 1001.0),
                            ("PixelFormat", "BGR8"), ("TriggerSource", "Line1")):
            node = getattr(self.nm, name)
            original = node.value
            with self.assertRaises(genapi.GenericException, msg=name):
                node.value = value
            self.assertEqual(node.value, original)
        for name in ("WidthMax", "HeightMax", "OffsetX", "OffsetY",
                     "DeviceVendorName", "DeviceModelName", "DeviceSerialNumber", "DeviceUserID"):
            self.assertFalse(genapi.is_writable(getattr(self.nm, name)), name)


if __name__ == "__main__":
    unittest.main()
