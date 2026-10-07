"""Small numeric self-check for the CP2 LiDAR-to-camera projection."""
import numpy as np

from starter.datasets import load_frame
from starter.projection import cam_to_image, velo_to_cam


def main() -> None:
    frame = load_frame("data/synthetic", "000000")
    points = np.array([
        [10.0, 0.0, 0.0],
        [np.nan, 0.0, 0.0],
        [-10.0, 0.0, 0.0],
        [10.0, 50.0, 0.0],
    ])
    cam = velo_to_cam(points, frame["calib"])
    uv, depth, mask = cam_to_image(cam, frame["calib"].P2, frame["image"].shape)

    assert cam.shape == (4, 3)
    assert abs(cam[0, 2] - 9.73) < 0.01
    assert mask.tolist() == [True, False, False, False]
    assert uv.shape == (1, 2) and depth.shape == (1,)
    assert np.allclose(uv[0], [614, 175], atol=1)
    print("CP2 self-check passed")


if __name__ == "__main__":
    main()
