"""Regression checks using the bundled CTI: python -m unittest discover -s tests."""
from pathlib import Path
from time import perf_counter
import unittest

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from device.device_manager import DeviceManager


class AcquisitionResponsivenessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        cti = Path(__file__).resolve().parents[1] / 'externals/virtualfg/VirtualFG.cti'
        self.manager = DeviceManager(cti)
        self.results = []
        for signal in (self.manager.search_finished, self.manager.connect_finished,
                       self.manager.start_finished, self.manager.stop_finished,
                       self.manager.shutdown_finished, self.manager.error_occurred):
            signal.connect(self.results.append)
        self.addCleanup(self.cleanup_manager)
        self.manager.search_device({'virtual': True})
        self.assertTrue(self.results[-1]['success'], self.results[-1])

    def cleanup_manager(self):
        self.manager.shutdown()
        self.assertTrue(self.results[-1]['success'], self.results[-1])
        self.manager.deleteLater()

    def check_responsiveness(self, trigger_mode):
        self.manager.connect_device({'settings': {
            'AcquisitionMode': 'Continuous', 'TriggerMode': trigger_mode,
            'TriggerSource': 'Software',
        }})
        self.assertTrue(self.results[-1]['success'], self.results[-1])
        self.assertEqual(len(self.manager._cameras), 12,
                         'This regression uses the bundled 12-camera configuration')
        previews = [0] * 12

        def receive(index, image, fps):
            if not image.isNull():
                previews[index] += 1

        self.manager.frame_received.connect(receive)
        loop = QEventLoop()
        gaps = []
        last = begin = perf_counter()

        def heartbeat():
            nonlocal last
            now = perf_counter()
            gaps.append(now - last)
            last = now
            if now - begin >= 1.5:
                loop.quit()

        timer = QTimer()
        timer.timeout.connect(heartbeat)
        timer.start(10)
        try:
            self.manager.start_capture()
            loop.exec()
        finally:
            timer.stop()
            self.manager.stop_capture()
        self.assertTrue(all(r['success'] for r in self.results), self.results)
        self.assertLess(max(gaps), 0.25, f'Qt event loop stalled: {max(gaps):.3f}s')
        if trigger_mode == 'Off':
            self.assertTrue(all(n >= 10 for n in previews), previews)
        else:
            self.assertEqual(previews, [0] * 12)

    def test_continuous_preview_does_not_starve_qt(self):
        self.check_responsiveness('Off')

    def test_waiting_for_trigger_does_not_starve_qt(self):
        self.check_responsiveness('On')


if __name__ == '__main__':
    unittest.main()
