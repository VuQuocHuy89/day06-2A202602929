"""Run the Topic A calibration sensitivity experiment on KITTI frames."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import matplotlib.pyplot as plt
import numpy as np

from starter.datasets import load_frame
from starter.projection import (
    cam_to_image,
    draw_box2d,
    overlay_points,
    perturb_extrinsic,
    project_velo_to_image,
    velo_to_cam,
)

CLASSES = ("Car", "Van", "Pedestrian", "Cyclist")
DEFAULT_FRAMES = ("000008", "000011", "000049")
DEMO_FRAMES = ("000019", "000011", "000004")


def points_in_box(points_cam: np.ndarray, obj) -> np.ndarray:
    h, w, length = obj.dimensions
    c, s = np.cos(obj.rotation_y), np.sin(obj.rotation_y)
    rotation = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    local = (points_cam - obj.location) @ rotation
    return (
        (np.abs(local[:, 0]) <= length / 2)
        & (local[:, 1] <= 0)
        & (local[:, 1] >= -h)
        & (np.abs(local[:, 2]) <= w / 2)
    )


def measure_points(
    data_root: str, frame_id: str, frame: dict, points: np.ndarray, yaw_levels: list[float]
) -> list[dict]:
    calib = frame["calib"]
    cam_true = velo_to_cam(points[:, :3], calib)
    object_masks: dict[str, list[tuple[object, np.ndarray]]] = {name: [] for name in CLASSES}
    for obj in frame["labels"]:
        if obj.type in object_masks:
            object_masks[obj.type].append((obj, points_in_box(cam_true, obj)))

    rows = []
    for yaw in yaw_levels:
        changed = perturb_extrinsic(calib, yaw_deg=yaw)
        uv, _, visible = project_velo_to_image(points, changed, frame["image"].shape)
        uv_by_point = np.full((len(points), 2), np.nan)
        uv_by_point[visible] = uv
        totals: dict[str, list[int]] = {name: [0, 0] for name in (*CLASSES, "All")}

        for class_name, objects in object_masks.items():
            for obj, object_mask in objects:
                selected = object_mask & visible
                count = int(selected.sum())
                if not count:
                    continue
                u, v = uv_by_point[selected].T
                x1, y1, x2, y2 = obj.bbox
                hits = int(((u >= x1) & (u <= x2) & (v >= y1) & (v <= y2)).sum())
                totals[class_name][0] += count
                totals[class_name][1] += hits

        for class_name in CLASSES:
            totals["All"][0] += totals[class_name][0]
            totals["All"][1] += totals[class_name][1]

        for class_name, (object_points, hits) in totals.items():
            if object_points:
                rows.append({
                    "dataset": Path(data_root).name,
                    "frame": frame_id,
                    "yaw_deg": yaw,
                    "class": class_name,
                    "object_points": object_points,
                    "hits_in_2d_box": hits,
                    "hit_ratio": hits / object_points,
                    "inside_image": int(visible.sum()),
                })
    return rows


def frame_measurements(data_root: str, frame_id: str, yaw_levels: list[float]) -> list[dict]:
    frame = load_frame(data_root, frame_id)
    return measure_points(data_root, frame_id, frame, frame["points"], yaw_levels)


def render_overlay(frame: dict, yaw_deg: float = 0.0) -> np.ndarray:
    calib = perturb_extrinsic(frame["calib"], yaw_deg=yaw_deg)
    uv, depth, _ = project_velo_to_image(frame["points"], calib, frame["image"].shape)
    image = overlay_points(frame["image"], uv, depth)
    for obj in frame["labels"]:
        image = draw_box2d(image, obj.bbox, label=obj.type)

    return image


def save_overlay(data_root: str, frame_id: str, out: Path, yaw_deg: float = 0.0) -> np.ndarray:
    frame = load_frame(data_root, frame_id)
    image = render_overlay(frame, yaw_deg)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), image)
    return image


def save_failure(data_root: str, frame_id: str, out: Path) -> None:
    frame = load_frame(data_root, frame_id)
    clean = render_overlay(frame, yaw_deg=0)
    drifted = render_overlay(frame, yaw_deg=2)
    panels = []
    for title, image in (("Calibration goc (0 deg)", clean), ("Calibration lech yaw 2 deg", drifted)):
        header = np.full((36, image.shape[1], 3), 255, dtype=np.uint8)
        cv2.putText(header, title, (12, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (20, 20, 20), 2)
        panels.append(np.vstack((header, image)))
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), np.hstack(panels))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/kitti_mini", help="KITTI or nuScenes dataset root")
    parser.add_argument("--frames", nargs="+", default=list(DEFAULT_FRAMES), help="frame IDs to benchmark")
    parser.add_argument("--yaw-levels", nargs="+", type=float, default=[0, 0.5, 1, 2, 3],
                        help="yaw perturbations in degrees")
    args = parser.parse_args()

    rows = [
        row
        for frame_id in args.frames
        for row in frame_measurements(args.data_root, frame_id, args.yaw_levels)
    ]
    results = Path("results")
    figures = results / "figures"
    results.mkdir(exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    csv_path = results / "topic_a_yaw_sweep.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    all_rows = [row for row in rows if row["class"] == "All"]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for frame_id in args.frames:
        group = [row for row in all_rows if row["frame"] == frame_id]
        if group:
            ax.plot(
                [row["yaw_deg"] for row in group],
                [100 * row["hit_ratio"] for row in group],
                marker="o",
                label=f"frame {frame_id}",
            )
    ax.set_xlabel("Yaw calibration drift (degrees)")
    ax.set_ylabel("Visible GT-object points inside labeled 2D box (%)")
    ax.set_ylim(0, 105)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(figures / "topic_a_yaw_sweep.png", dpi=150)
    plt.close(fig)

    for frame_id in DEMO_FRAMES:
        save_overlay(args.data_root, frame_id, figures / f"overlay_kitti_{frame_id}.png")
    save_failure(args.data_root, "000011", figures / "fail_01_yaw_2deg_000011.png")
    print(f"Saved {len(rows)} measurements to {csv_path}")
    print(f"Saved overlays, plot, and failure image to {figures}")
    for row in all_rows:
        print(
            f"frame={row['frame']} yaw={row['yaw_deg']} deg "
            f"hit_ratio={row['hit_ratio']:.3f} "
            f"object_points={row['object_points']}"
        )


if __name__ == "__main__":
    main()
