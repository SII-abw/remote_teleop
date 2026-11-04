"""Read-only dataset inspection confined to configured roots, with bounded scans."""

from __future__ import annotations

import json
import math
from pathlib import Path


def hil_features(names, shapes):
    features = {
        key: {"dtype": "float32", "shape": (14,), "names": names}
        for key in ("observation.state", "action", "complementary_info.policy_action")
    }
    for key, name in (
        ("complementary_info.is_intervention", "is_intervention"),
        ("complementary_info.state", "state"),
    ):
        features[key] = {"dtype": "float32", "shape": (1,), "names": [name]}
    features["complementary_info.collector_policy_id"] = {
        "dtype": "string",
        "shape": (1,),
        "names": ["collector_policy_id"],
    }
    for name, shape in shapes.items():
        features[f"observation.images.{name}"] = {
            "dtype": "video",
            "shape": shape,
            "names": ["height", "width", "channels"],
        }
    return features


class DatasetInspector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.roots = [
            Path(p).expanduser().resolve() for p in [cfg.recording.root, *cfg.recording.inspection_roots]
        ]

    def _path(self, identifier):
        root_index, name = identifier.split(":", 1)
        index = int(root_index)
        if (
            index < 0
            or index >= len(self.roots)
            or not name
            or Path(name).name != name
            or name in (".", "..")
        ):
            raise ValueError("INVALID_DATASET")
        root = self.roots[index]
        path = (root / name).resolve()
        if path.parent != root:
            raise ValueError("DATASET_PATH_ESCAPE")
        return path

    @staticmethod
    def _json(path):
        if path.stat().st_size > 2_000_000:
            raise ValueError("METADATA_TOO_LARGE")
        return json.loads(path.read_text())

    def list(self):
        result = []
        for i, root in enumerate(self.roots):
            if not root.is_dir():
                continue
            for path in sorted(root.iterdir())[:1000]:
                if path.is_symlink() or not path.is_dir():
                    continue
                info_path = path / "meta/info.json"
                if info_path.is_file() and info_path.resolve().is_relative_to(path):
                    try:
                        info = self._json(info_path)
                        result.append(
                            {
                                "id": f"{i}:{path.name}",
                                "name": path.name,
                                "fps": info.get("fps"),
                                "episodes": info.get("total_episodes"),
                                "frames": info.get("total_frames"),
                                "robot_type": info.get("robot_type"),
                                "root": str(root),
                            }
                        )
                    except (OSError, ValueError):
                        result.append(
                            {"id": f"{i}:{path.name}", "name": path.name, "error": "INVALID_METADATA"}
                        )
        return result

    def inspect(self, identifier, deep=False):
        path = self._path(identifier)
        info_path = (path / "meta/info.json").resolve()
        if not info_path.is_relative_to(path):
            raise ValueError("DATASET_PATH_ESCAPE")
        info = self._json(info_path)
        issues = []
        expected = hil_features(self.cfg.joint_names, {c.name: c.output_shape for c in self.cfg.cameras})
        actual = info.get("features", {})
        for key, feature in expected.items():
            if key not in actual:
                issues.append({"code": "MISSING_FEATURE", "field": key})
                continue
            for prop in ("dtype", "shape", "names"):
                got, want = actual[key].get(prop), feature[prop]
                if prop in ("shape", "names"):
                    got, want = list(got or []), list(want)
                if got != want:
                    issues.append(
                        {
                            "code": "FEATURE_MISMATCH",
                            "field": key,
                            "property": prop,
                            "actual": got,
                            "expected": want,
                        }
                    )
        if info.get("fps") != self.cfg.recording.fps:
            issues.append(
                {"code": "FPS_MISMATCH", "actual": info.get("fps"), "expected": self.cfg.recording.fps}
            )
        if info.get("robot_type") != "ros_follower":
            issues.append({"code": "ROBOT_TYPE_MISMATCH"})
        if (path / "audit/incomplete.json").exists():
            issues.append({"code": "INCOMPLETE_EPISODE"})
        files = sorted((path / "data").glob("chunk-*/*.parquet"))
        videos = sorted((path / "videos").glob("*/chunk-*/*.mp4"))
        samples, episodes, rows = [], [], 0
        truncated = False
        if deep:
            import pyarrow.parquet as pq

            for file in files[:200]:
                if not file.resolve().is_relative_to(path):
                    raise ValueError("DATASET_PATH_ESCAPE")
                parquet = pq.ParquetFile(file)
                rows += parquet.metadata.num_rows
                if len(samples) < 120:
                    columns = [
                        k
                        for k in (
                            "frame_index",
                            "episode_index",
                            "timestamp",
                            "action",
                            "observation.state",
                            "complementary_info.state",
                            "complementary_info.is_intervention",
                            "complementary_info.collector_policy_id",
                        )
                        if k in parquet.schema_arrow.names
                    ]
                    batch = next(
                        parquet.iter_batches(batch_size=min(120 - len(samples), 120), columns=columns), None
                    )
                    if batch:
                        samples.extend(batch.to_pylist())
            truncated = len(files) > 200
            if not truncated and rows != info.get("total_frames"):
                issues.append(
                    {"code": "FRAME_COUNT_MISMATCH", "actual": rows, "expected": info.get("total_frames")}
                )
            for file in sorted((path / "meta/episodes").glob("chunk-*/*.parquet"))[:20]:
                if not file.resolve().is_relative_to(path):
                    raise ValueError("DATASET_PATH_ESCAPE")
                batch = next(pq.ParquetFile(file).iter_batches(batch_size=100), None)
                if batch:
                    episodes.extend(
                        [
                            {k: row.get(k) for k in ("episode_index", "length", "episode_success", "tasks")}
                            for row in batch.to_pylist()
                        ]
                    )
        for row in samples:
            for key in ("action", "observation.state"):
                values = row.get(key, [])
                if len(values) != 14 or not all(math.isfinite(v) for v in values):
                    issues.append({"code": "INVALID_FRAME", "field": key, "frame": row.get("frame_index")})
        return {
            "id": identifier,
            "info": info,
            "issues": issues,
            "compatible": not issues,
            "parquet_files": len(files),
            "video_files": len(videos),
            "samples": samples,
            "episodes": episodes,
            "scan_truncated": truncated,
            "scan_scope": "metadata_and_sampled_rows" if deep else "metadata_only",
        }

    def preview(self, identifier, camera, frame_index=0):
        if camera not in ("main", "left_hand", "right_hand", "cam0", "cam1", "cam2"):
            raise ValueError("INVALID_CAMERA")
        if type(frame_index) is not int or not 0 <= frame_index <= 300:
            raise ValueError("INVALID_FRAME_INDEX")
        path = self._path(identifier)
        files = sorted((path / f"videos/observation.images.{camera}").glob("chunk-*/*.mp4"))
        if not files or not files[0].resolve().is_relative_to(path):
            raise ValueError("VIDEO_UNAVAILABLE")
        import cv2

        cap = cv2.VideoCapture(str(files[0]))
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = cap.read()
            if not ok:
                raise ValueError("VIDEO_DECODE_FAILED")
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                raise ValueError("VIDEO_DECODE_FAILED")
            return encoded.tobytes()
        finally:
            cap.release()
