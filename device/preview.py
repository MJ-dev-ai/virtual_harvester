"""Calculate non-overlapping preview tiles from an axis-aligned FOV grid."""
from math import floor, isfinite, isclose


def preview_regions(sizes, positions, fov):
    """Return per-camera pixel ROIs and common layout rectangles.

    All cameras must use the same resolution/FOV. Boundaries are computed in
    one pixel coordinate system. Every camera uses the same crop, including
    the outer cameras; the outer overlap margins are intentionally excluded.
    """
    if not sizes:
        return {}, []
    if len(set(sizes.values())) != 1:
        raise ValueError("Overlap preview requires the same frame size for all cameras")
    width, height = next(iter(sizes.values()))
    fw, fh = fov
    if not all(isfinite(v) and v > 0 for v in (fw, fh)):
        raise ValueError("Camera FOV must be positive and finite")
    points = {}
    for key in sizes:
        if key not in positions:
            raise ValueError(f"Missing preview position for {key} in config/settings.py")
        x, y = positions[key]
        if not all(isfinite(v) for v in (x, y)):
            raise ValueError(f"Invalid preview position for {key}")
        points[key] = (x, y)
    xs = sorted({p[0] for p in points.values()})
    ys = sorted({p[1] for p in points.values()})
    if len(set(points.values())) != len(points) or len(xs) * len(ys) != len(points):
        raise ValueError("Preview positions must form a complete rectangular grid without duplicates")

    def axis(coords, pixels, field):
        if len(coords) == 1:
            return [0], [0, pixels]
        pitch = coords[1] - coords[0]
        if any(not isclose(b - a, pitch, rel_tol=0, abs_tol=1e-6)
               for a, b in zip(coords, coords[1:])):
            raise ValueError("Camera spacing must be uniform within each axis")
        step = floor(pitch * pixels / field + 0.5)
        if pitch > field or not 1 <= step <= pixels:
            raise ValueError("Neighboring FOVs must overlap or touch, with distinct pixel origins")
        # Round the common pitch once, not each absolute origin. This keeps
        # overlap and output sizes identical even at fractional-pixel pitches.
        origins = [i * step for i in range(len(coords))]
        crop = (pixels - step) // 2
        edges = [crop + i * step for i in range(len(coords) + 1)]
        return origins, edges

    ox, ex = axis(xs, width, fw)
    oy, ey = axis(ys, height, fh)
    rois, tiles = {}, []
    for index, (key, (x, y)) in enumerate(points.items()):
        col, row = xs.index(x), ys.index(y)
        left, top = ex[col] - ox[col], ey[row] - oy[row]
        w, h = ex[col + 1] - ex[col], ey[row + 1] - ey[row]
        rois[key] = (left, top, w, h)
        tiles.append({"index": index, "rect": (ex[col], ey[row], w, h)})
    return rois, tiles
