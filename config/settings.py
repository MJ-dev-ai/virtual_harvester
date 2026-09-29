"""Preview calibration. Coordinates are FOV top-left positions in millimetres.

Keys are (framegrabber_id, camera_id). Edit these positions for the real rig.
Rows/columns must form an axis-aligned rectangular grid, without rotation.
X and Y spacing may differ, but spacing within each axis must be uniform.
Neighboring FOVs must overlap or touch. This config affects preview only, not camera acquisition or GenApi.
Default: a 4x3 grid covers 1116x844 mm around a centered 900x675 mm object.
"""

OBJECT_SIZE_MM = (900.0, 675.0)
CAMERA_GRID = (4, 3)  # columns, rows
CAMERA_FOV_MM = (300.0, 300.0)
PREVIEW_OVERLAP_ENABLED = False
# Equal horizontal and vertical camera pitch; overlap is 300 - 272 = 28 mm.
CAMERA_SPACING_MM = (272.0, 272.0)
CAPTURE_AREA_MM = tuple(
    fov + (count - 1) * pitch
    for fov, count, pitch in zip(CAMERA_FOV_MM, CAMERA_GRID, CAMERA_SPACING_MM)
)
CAMERA_POSITIONS_MM = {
    (f"VFG-{row + 1:04d}", f"VCAM-{row * CAMERA_GRID[0] + col + 1:04d}"):
        (col * CAMERA_SPACING_MM[0], row * CAMERA_SPACING_MM[1])
    for row in range(CAMERA_GRID[1])
    for col in range(CAMERA_GRID[0])
}
