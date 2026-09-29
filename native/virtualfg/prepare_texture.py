"""Rectify the supplied plate photograph. Requires OpenCV and NumPy."""
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent


def main():
    source = cv2.imread(str(ROOT / "assets/steel_plate_source.png"))
    if source is None:
        raise FileNotFoundError("steel_plate_source.png")
    # Slightly inset top-face corners, clockwise; exclude background and side faces.
    corners = np.float32([(79, 26), (224, 17), (439, 221), (187, 347)])
    width, height = 900, 675
    target = np.float32([(0, 0), (width - 1, 0),
                         (width - 1, height - 1), (0, height - 1)])
    transform = cv2.getPerspectiveTransform(corners, target)
    texture = cv2.warpPerspective(source, transform, (width, height),
                                  flags=cv2.INTER_LINEAR)
    for name in ("steel_plate.png", "steel_plate.ppm"):
        if not cv2.imwrite(str(ROOT / "assets" / name), texture):
            raise OSError(f"Cannot write {name}")


if __name__ == "__main__":
    main()
