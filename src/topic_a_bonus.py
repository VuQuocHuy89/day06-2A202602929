"""Run reproducible Topic A stress, latency, and cross-dataset measurements."""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from starter.datasets import list_frames, load_frame
from starter.perturb import gaussian_noise, random_dropout
from src.topic_a import DEFAULT_FRAMES, frame_measurements, measure_points

YAW_LEVELS = [0.0, 0.5, 1.0, 2.0, 3.0]
NUSCENES_FRAMES = ["scene-1094_000", "scene-1094_007", "scene-1094_008"]


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError(f"No measurements to write to {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def all_metric(data_root: str, frame_id: str, frame: dict, points: np.ndarray) -> dict:
    rows = measure_points(data_root, frame_id, frame, points, [0.0])
    metric = next((row for row in rows if row["class"] == "All"), None)
    if metric is None:
        raise ValueError(f"Frame {frame_id} has no visible labeled object points")
    return metric


def run_stress(args, results: Path) -> None:
    rows = []
    keep_ratios = args.keep_ratios
    noise_sigmas = args.noise_sigmas
    for frame_index, frame_id in enumerate(args.stress_frames):
        frame = load_frame(args.kitti_root, frame_id)
        points = frame["points"]
        clean = all_metric(args.kitti_root, frame_id, frame, points)
        conditions = [("baseline", 0.0, points)]
        conditions.extend(
            ("random_dropout", keep, random_dropout(points, keep, seed=args.seed + frame_index * 100 + index))
            for index, keep in enumerate(keep_ratios)
        )
        conditions.extend(
            ("gaussian_noise", sigma, gaussian_noise(points, sigma_xyz_m=sigma,
                                                       seed=args.seed + frame_index * 100 + index))
            for index, sigma in enumerate(noise_sigmas)
        )
        for perturbation, level, changed_points in conditions:
            metric = all_metric(args.kitti_root, frame_id, frame, changed_points)
            rows.append({
                "dataset": Path(args.kitti_root).name,
                "frame": frame_id,
                "perturbation": perturbation,
                "level": level,
                "input_points": len(changed_points),
                "object_points": metric["object_points"],
                "box_hits": metric["hits_in_2d_box"],
                "box_hit_ratio_pct": 100 * metric["hit_ratio"],
                "object_support_vs_clean_pct": 100 * metric["object_points"] / clean["object_points"],
            })

    path = results / "topic_a_stress.csv"
    write_csv(path, rows)
    grouped = {}
    for perturbation, levels in (("random_dropout", keep_ratios), ("gaussian_noise", noise_sigmas)):
        for level in levels:
            selected = [row for row in rows if row["perturbation"] == perturbation and row["level"] == level]
            hits = sum(row["box_hits"] for row in selected)
            object_points = sum(row["object_points"] for row in selected)
            clean_points = sum(
                row["object_points"] for row in rows
                if row["perturbation"] == "baseline" and row["frame"] in {r["frame"] for r in selected}
            )
            grouped[(perturbation, level)] = {
                "hit_ratio": 100 * hits / object_points if object_points else 0.0,
                "support": 100 * object_points / clean_points if clean_points else 0.0,
            }

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    for ax, perturbation, levels, labels, title in (
        (axes[0], "random_dropout", keep_ratios, [f"keep {100*x:.0f}%" for x in keep_ratios], "Random point dropout"),
        (axes[1], "gaussian_noise", noise_sigmas, [f"σ={x:.2f} m" for x in noise_sigmas], "Gaussian XYZ noise"),
    ):
        x = np.arange(len(levels))
        ax.plot(x, [grouped[(perturbation, level)]["hit_ratio"] for level in levels], marker="o",
                label="2D box hit ratio")
        ax.plot(x, [grouped[(perturbation, level)]["support"] for level in levels], marker="s",
                label="Object point support vs clean")
        ax.axhline(100, color="gray", linestyle="--", linewidth=1, label="Clean support" if ax is axes[0] else None)
        ax.set_xticks(x, labels)
        ax.set_title(title)
        ax.set_ylim(0, 110)
        ax.set_ylabel("Percent (%)")
        ax.grid(alpha=0.3)
    axes[0].legend(fontsize=8)
    fig.suptitle(f"Topic A stress test ({len(args.stress_frames)} KITTI frames)")
    fig.tight_layout()
    figure = results / "figures" / "topic_a_stress_test.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure, dpi=150)
    plt.close(fig)
    print(f"B2 stress test: {path}, {figure}")
    for key, value in grouped.items():
        print(f"  {key[0]} level={key[1]}: hit={value['hit_ratio']:.1f}%, support={value['support']:.1f}%")


def hardware_info() -> tuple[str, float, str]:
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = os.uname().machine
    ram_gb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    gpu = "not detected"
    if shutil.which("nvidia-smi"):
        result = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                capture_output=True, text=True, check=False, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            gpu = result.stdout.strip().replace("\n", "; ")
    return cpu, ram_gb, gpu


def run_latency(args, results: Path) -> None:
    frame = load_frame(args.kitti_root, args.latency_frame)
    points = frame["points"]
    cpu, ram_gb, gpu = hardware_info()
    rows = []
    elapsed = []
    for run in range(args.latency_runs):
        start = time.perf_counter()
        measure_points(args.kitti_root, args.latency_frame, frame, points, YAW_LEVELS)
        milliseconds = (time.perf_counter() - start) * 1000
        elapsed.append(milliseconds)
        rows.append({
            "run": run,
            "warmup": run == 0,
            "latency_ms": milliseconds,
            "frame": args.latency_frame,
            "yaw_levels_deg": ";".join(map(str, YAW_LEVELS)),
            "cpu": cpu,
            "ram_gb": round(ram_gb, 1),
            "gpu_detected": gpu,
        })
    measured = np.asarray(elapsed[1:])
    path = results / "topic_a_latency.csv"
    write_csv(path, rows)
    print(f"B3 latency: {path}; p50={np.percentile(measured, 50):.2f} ms, "
          f"p95={np.percentile(measured, 95):.2f} ms; CPU={cpu}; RAM={ram_gb:.1f} GB; GPU={gpu}")


def run_dataset_compare(args, results: Path) -> None:
    rows = []
    datasets = (
        (args.kitti_root, args.kitti_frames),
        (args.nuscenes_root, args.nuscenes_frames),
    )
    for data_root, frame_ids in datasets:
        available = set(list_frames(data_root))
        missing = [frame_id for frame_id in frame_ids if frame_id not in available]
        if missing:
            raise ValueError(f"Frames not found in {data_root}: {missing}")
        for frame_id in frame_ids:
            rows.extend(frame_measurements(data_root, frame_id, YAW_LEVELS))

    path = results / "topic_a_dataset_compare.csv"
    write_csv(path, rows)
    fig, ax = plt.subplots(figsize=(7, 4.5))
    all_rows = [row for row in rows if row["class"] == "All"]
    for data_root, _ in datasets:
        dataset = Path(data_root).name
        grouped = []
        for yaw in YAW_LEVELS:
            sample = [row for row in all_rows if row["dataset"] == dataset and row["yaw_deg"] == yaw]
            denominator = sum(row["object_points"] for row in sample)
            hits = sum(row["hits_in_2d_box"] for row in sample)
            grouped.append(100 * hits / denominator if denominator else np.nan)
        ax.plot(YAW_LEVELS, grouped, marker="o", label=dataset)
    ax.set_xlabel("Yaw calibration drift (degrees)")
    ax.set_ylabel("Visible GT-object points inside labeled 2D box (%)")
    ax.set_ylim(0, 105)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    figure = results / "figures" / "topic_a_dataset_compare.png"
    figure.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(figure, dpi=150)
    plt.close(fig)
    print(f"B5 dataset comparison: {path}, {figure}")
    for dataset in (Path(args.kitti_root).name, Path(args.nuscenes_root).name):
        values = []
        for yaw in (0.0, 1.0, 2.0):
            sample = [row for row in all_rows if row["dataset"] == dataset and row["yaw_deg"] == yaw]
            denominator = sum(row["object_points"] for row in sample)
            hits = sum(row["hits_in_2d_box"] for row in sample)
            values.append(f"{yaw:g}°={100*hits/denominator:.1f}%" if denominator else f"{yaw:g}°=n/a")
        print(f"  {dataset}: " + ", ".join(values))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("all", "stress", "latency", "compare"), default="all",
                        help="which bonus experiment to run; defaults to all")
    parser.add_argument("--kitti-root", default="data/kitti_mini", help="KITTI dataset root")
    parser.add_argument("--nuscenes-root", default="data/nuscenes_mini_subset", help="nuScenes dataset root")
    parser.add_argument("--stress-frames", nargs="+", default=list(DEFAULT_FRAMES),
                        help="KITTI frames used by the degradation stress test")
    parser.add_argument("--keep-ratios", nargs="+", type=float, default=[0.9, 0.7, 0.5],
                        help="random-dropout point retention ratios")
    parser.add_argument("--noise-sigmas", nargs="+", type=float, default=[0.02, 0.05, 0.1],
                        help="Gaussian XYZ noise standard deviations in meters")
    parser.add_argument("--seed", type=int, default=42, help="random seed for repeatable stress tests")
    parser.add_argument("--latency-frame", default="000011", help="KITTI frame used for latency measurement")
    parser.add_argument("--latency-runs", type=int, default=21,
                        help="latency runs including one warm-up run; must be at least 2")
    parser.add_argument("--kitti-frames", nargs="+", default=list(DEFAULT_FRAMES),
                        help="KITTI frames used in the cross-dataset comparison")
    parser.add_argument("--nuscenes-frames", nargs="+", default=list(NUSCENES_FRAMES),
                        help="nuScenes frames used in the cross-dataset comparison")
    args = parser.parse_args()
    if args.latency_runs < 2:
        parser.error("--latency-runs must be at least 2 so one warm-up can be excluded")

    results = Path("results")
    if args.mode in ("all", "stress"):
        run_stress(args, results)
    if args.mode in ("all", "latency"):
        run_latency(args, results)
    if args.mode in ("all", "compare"):
        run_dataset_compare(args, results)


if __name__ == "__main__":
    main()
