#!/usr/bin/env python3
"""PRIM Dataset Management web
--------
- Starts a small HTTP server and opens a browser UI.
- Prompts for a dataset root (defaults to viewer/dataset or last-used path).
- Scans for mesh.obj files in the dataset structure and exposes them through a JSON API.
- Streams OBJ files to the frontend for interactive viewing with three.js.
- Persists the last used dataset path and the latest scan into a cache folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import numpy as np
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, request, send_file


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CACHE_DIR = BASE_DIR / "cache"
DEFAULT_DATASET_ROOT = Path(__file__).resolve().parent.parent / "hl2ss-lk" / "viewer" / "dataset"
LAST_DATASET_FILE = CACHE_DIR / "last_dataset.txt"
CACHE_INDEX_FILE = CACHE_DIR / "mesh_index.json"
MERGED_DIR_SUFFIX = "_merged_obj"
TSDF_DIR_SUFFIX = "_tsdf_obj"
LEGACY_MERGED_DIR_NAME = "__merged__"
MERGE_NODE_LABEL = "Merge session meshes"
TSDF_NODE_LABEL = "TSDF session mesh"
mimetypes.add_type("application/javascript", ".js")


@dataclass
class PreviewAsset:
    id: str
    name: str
    rel_path: str
    size: int
    mtime: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "rel_path": self.rel_path.replace("\\", "/"),
            "size": self.size,
            "mtime": self.mtime,
        }


@dataclass
class RIRAsset:
    id: str
    name: str
    rel_path: str
    size: int
    mtime: float
    channel: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "rel_path": self.rel_path.replace("\\", "/"),
            "size": self.size,
            "mtime": self.mtime,
            "channel": self.channel,
        }


@dataclass
class MeshEntry:
    id: str
    name: str
    rel_path: str
    size: int
    mtime: float
    previews: List[PreviewAsset]
    rirs: List["RIRAsset"]
    mic_position: Optional[List[float]]
    mic_positions: Optional[List[List[float]]]
    source_position: Optional[List[float]]

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "rel_path": self.rel_path.replace("\\", "/"),
            "size": self.size,
            "mtime": self.mtime,
            "previews": [preview.as_dict() for preview in self.previews],
            "rirs": [rir.as_dict() for rir in self.rirs],
            "markers": {
                "mic": self.mic_position,
                "mics": self.mic_positions,
                "source": self.source_position,
            },
        }


class AppState:
    def __init__(self, dataset_root: Path):
        self.dataset_root = dataset_root
        self.entries: List[MeshEntry] = []
        self.path_map: Dict[str, Path] = {}
        self.preview_map: Dict[str, Path] = {}
        self.rir_map: Dict[str, Path] = {}
        self.tree: List[Dict[str, object]] = []
        self.lock = threading.Lock()


STATE: Optional[AppState] = None

# Flask app, manual static
app = Flask(__name__)


def load_cached_dataset() -> Optional[Path]:
    """Return the last used dataset path if it exists."""
    if LAST_DATASET_FILE.exists():
        cached = LAST_DATASET_FILE.read_text(encoding="utf-8").strip()
        if cached:
            path = Path(cached)
            if path.exists():
                return path
    return None


def remember_dataset(path: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_DATASET_FILE.write_text(str(path), encoding="utf-8")


def choose_dataset_root(args_dataset: Optional[str], allow_dialog: bool = True) -> Path:
    """Pick dataset root from CLI arg, cache, or a folder dialog."""
    candidate = Path(args_dataset) if args_dataset else (load_cached_dataset() or DEFAULT_DATASET_ROOT)

    if not allow_dialog:
        return candidate

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        selected = filedialog.askdirectory(
            initialdir=str(candidate),
            title="Select mesh dataset root (contains room/session_xxx/.../mesh/mesh.obj)",
        )
        root.destroy()
        if selected:
            return Path(selected)
    except Exception as exc:  # no cover, tk maybe missing
        print(f"[mesh-viewer] Folder dialog unavailable ({exc}); using {candidate}")

    return candidate


def _collect_previews(mesh_path: Path, dataset_root: Path) -> Tuple[List[PreviewAsset], Dict[str, Path]]:
    """Return preview assets (png thumbnails) living next to the mesh."""
    preview_assets: List[PreviewAsset] = []
    preview_map: Dict[str, Path] = {}

    # layout: .../mesh/mesh.obj -> sibling image/
    candidate = mesh_path.parent.parent / "image"
    if not candidate.exists():
        candidate = mesh_path.parent / "image"

    if candidate.exists():
        for png in sorted(candidate.glob("*.png")):
            if not png.is_file():
                continue
            stat = png.stat()
            preview_id = hashlib.sha1(str(png).encode("utf-8")).hexdigest()[:12]
            asset = PreviewAsset(
                id=preview_id,
                name=png.name,
                rel_path=str(png.relative_to(dataset_root)),
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
            preview_assets.append(asset)
            preview_map[preview_id] = png

    return preview_assets, preview_map


def _collect_rirs(mesh_path: Path, dataset_root: Path) -> Tuple[List[RIRAsset], Dict[str, Path]]:
    """Return RIR audio files (wav) living next to the mesh."""
    rir_assets: List[RIRAsset] = []
    rir_map: Dict[str, Path] = {}

    candidate = mesh_path.parent.parent / "audio"
    if not candidate.exists():
        candidate = mesh_path.parent / "audio"

    if candidate.exists():
        for wav in sorted(candidate.glob("*.wav")):
            if not wav.is_file():
                continue
            stat = wav.stat()
            rir_id = hashlib.sha1(str(wav).encode("utf-8")).hexdigest()[:12]
            asset = RIRAsset(
                id=rir_id,
                name=wav.name,
                rel_path=str(wav.relative_to(dataset_root)),
                size=stat.st_size,
                mtime=stat.st_mtime,
                channel=wav.stem,
            )
            rir_assets.append(asset)
            rir_map[rir_id] = wav

    return rir_assets, rir_map


def _load_markers(mesh_path: Path) -> Tuple[Optional[List[float]], Optional[List[float]], Optional[List[List[float]]]]:
    """Load mic/source; source from session source_pov, receivers from time positions."""

    def _session_root(path: Path) -> Optional[Path]:
        for parent in path.parents:
            if parent.name.startswith("session_") and not parent.name.endswith(
                (MERGED_DIR_SUFFIX, TSDF_DIR_SUFFIX)
            ):
                return parent
        return None

    def _load_origin(file: Path) -> Optional[np.ndarray]:
        if not file.exists():
            return None
        try:
            arr = np.load(file)
        except Exception:
            return None
        if arr.ndim != 2 or arr.shape[1] < 3 or arr.shape[0] == 0:
            return None
        return arr

    session_dir = _session_root(mesh_path)

    # source: fixed per session from source_pov
    src = None
    if session_dir:
        src_arr = _load_origin(session_dir / "source_pov" / "position" / "origin.npy")
        if src_arr is not None:
            src = src_arr[0, :3].astype(float).tolist()

    mic = None
    mic_positions = None

    is_aggregate = False
    if session_dir and _is_aggregate_output_dir(session_dir, mesh_path.parent.name):
        is_aggregate = True

    if is_aggregate and session_dir:
        receiver_positions: List[List[float]] = []
        for time_dir in sorted(p for p in session_dir.iterdir() if p.is_dir()):
            if time_dir.name == "source_pov" or _is_aggregate_output_dir(session_dir, time_dir.name):
                continue
            mic_dir = time_dir / "position"
            mic_arr = _load_origin(mic_dir / "origin.npy") if mic_dir.exists() else None
            if mic_arr is None:
                continue
            receiver_positions.append(mic_arr[0, :3].astype(float).tolist())
        if receiver_positions:
            mic_positions = receiver_positions
            mic = receiver_positions[0]
    else:
        mic_dir = mesh_path.parent.parent / "position"
        if not mic_dir.exists():
            mic_dir = mesh_path.parent / "position"
        mic_arr = _load_origin(mic_dir / "origin.npy") if mic_dir.exists() else None
        if mic_arr is not None:
            mic = mic_arr[0, :3].astype(float).tolist()
            mic_positions = [mic]

    return mic, src, mic_positions


def _build_mesh_entry(mesh_path: Path, dataset_root: Path) -> Tuple[MeshEntry, Dict[str, Path], Dict[str, Path]]:
    """Create a MeshEntry from a mesh path plus its nearby assets."""
    rel_path = mesh_path.relative_to(dataset_root)
    parts = list(rel_path.parts[:-1])  # drop file
    if parts and parts[-1] == "mesh":
        parts = parts[:-1]
    display_name = " / ".join(parts) or mesh_path.name
    entry_id = hashlib.sha1(str(mesh_path).encode("utf-8")).hexdigest()[:12]
    stat = mesh_path.stat()
    previews, local_preview_map = _collect_previews(mesh_path, dataset_root)
    rirs, local_rir_map = _collect_rirs(mesh_path, dataset_root)
    mic_pos, src_pos, mic_positions = _load_markers(mesh_path)

    entry = MeshEntry(
        id=entry_id,
        name=display_name,
        rel_path=str(rel_path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        previews=previews,
        rirs=rirs,
        mic_position=mic_pos,
        mic_positions=mic_positions,
        source_position=src_pos,
    )
    return entry, local_preview_map, local_rir_map


def _merged_dir_name(session_dir: Path) -> str:
    return f"{session_dir.name}{MERGED_DIR_SUFFIX}"


def _tsdf_dir_name(session_dir: Path) -> str:
    return f"{session_dir.name}{TSDF_DIR_SUFFIX}"


def _is_aggregate_output_dir(session_dir: Path, dir_name: str) -> bool:
    return dir_name in (
        _merged_dir_name(session_dir),
        _tsdf_dir_name(session_dir),
        LEGACY_MERGED_DIR_NAME,
    )


def _resolve_dataset_path(rel_path: str) -> Path:
    """Resolve a dataset-relative path, guarding against traversal."""
    assert STATE is not None
    dataset_root = STATE.dataset_root.resolve()
    target = (dataset_root / rel_path).resolve()
    try:
        target.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError("Path escapes dataset root") from exc
    return target


def _collect_session_meshes(session_dir: Path) -> List[Path]:
    """Collect mesh.obj paths under a session (excluding aggregate outputs)."""
    if not session_dir.exists():
        return []

    def _is_under_aggregate_output(obj_path: Path) -> bool:
        for parent in obj_path.parents:
            if parent == session_dir:
                break
            if _is_aggregate_output_dir(session_dir, parent.name):
                return True
        return False

    def _time_dir_for_mesh(obj_path: Path) -> Path:
        if obj_path.parent.name == "mesh":
            return obj_path.parent.parent
        return obj_path.parent

    chosen: Dict[Path, Path] = {}
    for obj_path in session_dir.rglob("mesh.obj"):
        if not obj_path.is_file():
            continue
        if _is_under_aggregate_output(obj_path):
            continue
        time_dir = _time_dir_for_mesh(obj_path)
        if _is_aggregate_output_dir(session_dir, time_dir.name):
            continue
        existing = chosen.get(time_dir)
        if existing is None:
            chosen[time_dir] = obj_path
            continue
        if existing.parent.name != "mesh" and obj_path.parent.name == "mesh":
            chosen[time_dir] = obj_path

    return [chosen[time_dir] for time_dir in sorted(chosen.keys(), key=lambda p: p.name)]


def _count_obj_components(obj_path: Path) -> Tuple[int, int, int]:
    v_count = 0
    vt_count = 0
    vn_count = 0
    with obj_path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if line.startswith("v "):
                v_count += 1
            elif line.startswith("vt "):
                vt_count += 1
            elif line.startswith("vn "):
                vn_count += 1
    return v_count, vt_count, vn_count


def _shift_index(raw: str, offset: int, total: int) -> str:
    if not raw:
        return raw
    try:
        idx = int(raw)
    except ValueError:
        return raw
    if idx < 0:
        idx = total + idx + 1
    return str(idx + offset)


def _offset_face_token(
    token: str,
    v_offset: int,
    vt_offset: int,
    vn_offset: int,
    v_total: int,
    vt_total: int,
    vn_total: int,
) -> str:
    parts = token.split("/")
    if not parts:
        return token
    v = _shift_index(parts[0], v_offset, v_total) if parts[0] else parts[0]
    if len(parts) == 1:
        return v
    vt = _shift_index(parts[1], vt_offset, vt_total) if len(parts) > 1 and parts[1] else ""
    if len(parts) == 2:
        return f"{v}/{vt}"
    vn = _shift_index(parts[2], vn_offset, vn_total) if len(parts) > 2 and parts[2] else ""
    return f"{v}/{vt}/{vn}"


def _merge_obj_files(obj_paths: List[Path], output_path: Path) -> None:
    """Merge OBJ files by concatenating vertices and offsetting faces."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    v_offset = 0
    vt_offset = 0
    vn_offset = 0
    with output_path.open("w", encoding="utf-8") as out:
        out.write(f"# merged {len(obj_paths)} meshes\n")
        for obj_path in obj_paths:
            v_total, vt_total, vn_total = _count_obj_components(obj_path)
            out.write(f"\n# source {obj_path.name}\n")
            with obj_path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if line.startswith("v "):
                        out.write(line)
                    elif line.startswith("vt "):
                        out.write(line)
                    elif line.startswith("vn "):
                        out.write(line)
                    elif line.startswith("f "):
                        tokens = line.strip().split()[1:]
                        if not tokens:
                            continue
                        shifted = [
                            _offset_face_token(
                                token,
                                v_offset,
                                vt_offset,
                                vn_offset,
                                v_total,
                                vt_total,
                                vn_total,
                            )
                            for token in tokens
                        ]
                        out.write("f " + " ".join(shifted) + "\n")
            v_offset += v_total
            vt_offset += vt_total
            vn_offset += vn_total


def _camera_to_world(eye: np.ndarray, center: np.ndarray, up: np.ndarray) -> np.ndarray:
    forward = center - eye
    norm = np.linalg.norm(forward)
    if norm == 0:
        return np.eye(4, dtype=np.float64)
    forward /= norm
    right = np.cross(up, forward)
    right_norm = np.linalg.norm(right)
    if right_norm == 0:
        return np.eye(4, dtype=np.float64)
    right /= right_norm
    up_vec = np.cross(forward, right)

    mat = np.eye(4, dtype=np.float64)
    mat[:3, 0] = right
    mat[:3, 1] = up_vec
    mat[:3, 2] = forward
    mat[:3, 3] = eye
    return mat


def _tsdf_mesh_from_paths(obj_paths: List[Path], output_path: Path) -> None:
    """Fuse meshes into a TSDF volume and extract a mesh."""
    try:
        import open3d as o3d
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("open3d is required for TSDF fusion") from exc

    combined = o3d.geometry.TriangleMesh()
    for obj_path in obj_paths:
        mesh = o3d.io.read_triangle_mesh(str(obj_path))
        if mesh.is_empty():
            continue
        combined += mesh

    if combined.is_empty():
        raise ValueError("All meshes are empty")

    bbox = combined.get_axis_aligned_bounding_box()
    center = np.asarray(bbox.get_center(), dtype=np.float64)
    extent = np.asarray(bbox.get_extent(), dtype=np.float64)
    max_dim = float(np.max(extent)) if np.all(np.isfinite(extent)) else 0.0
    if max_dim <= 0:
        max_dim = 1.0
    dist = max_dim * 1.6

    # TSDF quality knobs (tuned for ~5 min on typical sessions).
    width, height = 1280, 960
    fov = 60.0
    fx = fy = 0.5 * width / np.tan(np.deg2rad(fov) / 2.0)
    cx, cy = width / 2.0, height / 2.0
    intrinsic = o3d.camera.PinholeCameraIntrinsic(width, height, fx, fy, cx, cy)
    intrinsic_matrix = intrinsic.intrinsic_matrix
    intrinsic_t = o3d.core.Tensor(intrinsic_matrix, dtype=o3d.core.Dtype.Float64)

    voxel_length = max(0.004, min(0.02, max_dim / 384.0))
    sdf_trunc = voxel_length * 6.0
    volume = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel_length,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.NoColor,
    )

    tmesh = o3d.t.geometry.TriangleMesh.from_legacy(combined)
    scene = o3d.t.geometry.RaycastingScene()
    _ = scene.add_triangles(tmesh)

    view_count = 60
    golden_angle = np.pi * (3.0 - np.sqrt(5.0))
    directions = np.zeros((view_count, 3), dtype=np.float64)
    for i in range(view_count):
        y = 1.0 - (2.0 * i + 1.0) / view_count
        radius = np.sqrt(max(0.0, 1.0 - y * y))
        theta = golden_angle * i
        directions[i] = [np.cos(theta) * radius, y, np.sin(theta) * radius]

    depth_trunc = dist * 2.5

    for direction in directions:
        eye = center + direction * dist
        up = np.array([0.0, 0.0, 1.0], dtype=np.float64)
        if abs(np.dot(direction, up)) > 0.9:
            up = np.array([0.0, 1.0, 0.0], dtype=np.float64)
        cam_to_world = _camera_to_world(eye, center, up)
        world_to_cam = np.linalg.inv(cam_to_world)
        world_to_cam_t = o3d.core.Tensor(world_to_cam, dtype=o3d.core.Dtype.Float64)

        rays = o3d.t.geometry.RaycastingScene.create_rays_pinhole(
            intrinsic_t, world_to_cam_t, width, height
        )
        ray_data = rays.numpy().reshape(-1, 6)
        origins = ray_data[:, :3]
        ray_dirs = ray_data[:, 3:]

        ans = scene.cast_rays(rays)
        t_hit = ans["t_hit"].numpy().reshape(-1)
        hit_mask = np.isfinite(t_hit)
        depth = np.zeros_like(t_hit)
        if np.any(hit_mask):
            hit_points = origins[hit_mask] + ray_dirs[hit_mask] * t_hit[hit_mask][:, None]
            cam_points = (world_to_cam[:3, :3] @ hit_points.T + world_to_cam[:3, 3:4]).T
            z = cam_points[:, 2]
            z[z < 0] = 0
            depth[hit_mask] = z

        depth = depth.reshape((height, width))
        depth_image = o3d.geometry.Image(depth.astype(np.float32))
        color_image = o3d.geometry.Image(np.zeros((height, width, 3), dtype=np.uint8))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            color_image,
            depth_image,
            depth_scale=1.0,
            depth_trunc=depth_trunc,
            convert_rgb_to_intensity=False,
        )
        volume.integrate(rgbd, intrinsic, world_to_cam)

    mesh = volume.extract_triangle_mesh()
    mesh = mesh.filter_smooth_taubin(20, 0.5, -0.53)
    mesh.compute_vertex_normals()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    o3d.io.write_triangle_mesh(str(output_path), mesh)


def _find_mesh_id(mesh_path: Path) -> Optional[str]:
    assert STATE is not None
    with STATE.lock:
        for mesh_id, path in STATE.path_map.items():
            if path == mesh_path:
                return mesh_id
    return None


def build_mesh_index(
    dataset_root: Path,
) -> Tuple[List[MeshEntry], Dict[str, Path], Dict[str, Path], Dict[str, Path], List[Dict[str, object]]]:
    """Scan dataset_root for mesh.obj files and build index + id->path map + folder tree."""
    entries: List[MeshEntry] = []
    path_map: Dict[str, Path] = {}
    preview_map: Dict[str, Path] = {}
    rir_map: Dict[str, Path] = {}
    tree: List[Dict[str, object]] = []
    time_to_mesh: Dict[str, str] = {}

    if not dataset_root.exists():
        return entries, path_map, preview_map, rir_map, tree

    # pass1: collect meshes/assets
    for obj_path in sorted(dataset_root.rglob("mesh.obj")):
        if not obj_path.is_file():
            continue
        entry, local_preview_map, local_rir_map = _build_mesh_entry(obj_path, dataset_root)
        entries.append(entry)
        path_map[entry.id] = obj_path
        preview_map.update(local_preview_map)
        rir_map.update(local_rir_map)

        time_dir = obj_path.parent if obj_path.parent.name != "mesh" else obj_path.parent.parent
        rel_time = str(time_dir.relative_to(dataset_root)).replace("\\", "/")
        time_to_mesh[rel_time] = entry.id

    # pass2: build tree incl missing mesh
    for room_dir in sorted(dataset_root.iterdir()):
        if not room_dir.is_dir():
            continue
        room_node = {
            "name": room_dir.name,
            "rel_path": str(room_dir.relative_to(dataset_root)).replace("\\", "/"),
            "sessions": [],
        }

        for session_dir in sorted(p for p in room_dir.iterdir() if p.is_dir()):
            session_node = {
                "name": session_dir.name,
                "rel_path": str(session_dir.relative_to(dataset_root)).replace("\\", "/"),
                "times": [],
            }
            time_dirs = [
                p
                for p in session_dir.iterdir()
                if p.is_dir()
                and not _is_aggregate_output_dir(session_dir, p.name)
                and (
                    p.name == "source_pov"
                    or p.name.startswith("time_")
                    or (p / "mesh" / "mesh.obj").exists()
                    or (p / "mesh.obj").exists()
                )
            ]
            time_dirs = sorted(time_dirs, key=lambda p: (0 if p.name == "source_pov" else 1, p.name))
            for time_dir in time_dirs:
                rel_time = str(time_dir.relative_to(dataset_root)).replace("\\", "/")
                mesh_id = time_to_mesh.get(rel_time)
                session_node["times"].append(
                    {
                        "name": time_dir.name,
                        "rel_path": rel_time,
                        "mesh_id": mesh_id,
                        "has_mesh": mesh_id is not None,
                    }
                )

            merge_rel = f"{session_node['rel_path']}/{_merged_dir_name(session_dir)}"
            tsdf_rel = f"{session_node['rel_path']}/{_tsdf_dir_name(session_dir)}"
            merge_id = time_to_mesh.get(merge_rel)
            tsdf_id = time_to_mesh.get(tsdf_rel)
            if session_node["times"] or merge_id or tsdf_id:
                session_node["times"].append(
                    {
                        "name": MERGE_NODE_LABEL,
                        "rel_path": merge_rel,
                        "mesh_id": merge_id,
                        "has_mesh": merge_id is not None,
                        "kind": "merge",
                        "session_rel_path": session_node["rel_path"],
                    }
                )
                session_node["times"].append(
                    {
                        "name": TSDF_NODE_LABEL,
                        "rel_path": tsdf_rel,
                        "mesh_id": tsdf_id,
                        "has_mesh": tsdf_id is not None,
                        "kind": "tsdf",
                        "session_rel_path": session_node["rel_path"],
                    }
                )

            if session_node["times"]:
                room_node["sessions"].append(session_node)

        if room_node["sessions"]:
            tree.append(room_node)

    return entries, path_map, preview_map, rir_map, tree


def write_index_cache(dataset_root: Path, entries: List[MeshEntry], tree: List[Dict[str, object]]) -> None:
    """Persist the latest scan for quick inspection or reuse."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_root": str(dataset_root),
        "generated_at": time.time(),
        "mesh_count": len(entries),
        "entries": [entry.as_dict() for entry in entries],
        "tree": tree,
    }
    CACHE_INDEX_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def refresh_index() -> List[MeshEntry]:
    """Rebuild the mesh index and update global state + cache."""
    global STATE
    assert STATE is not None

    entries, path_map, preview_map, rir_map, tree = build_mesh_index(STATE.dataset_root)

    with STATE.lock:
        STATE.entries = entries
        STATE.path_map = path_map
        STATE.preview_map = preview_map
        STATE.rir_map = rir_map
        STATE.tree = tree

    write_index_cache(STATE.dataset_root, entries, tree)
    return entries


def serialize_entries() -> List[Dict[str, object]]:
    assert STATE is not None
    with STATE.lock:
        return [entry.as_dict() for entry in STATE.entries]


def snapshot_tree() -> List[Dict[str, object]]:
    """Return a JSON-safe copy of the folder tree."""
    assert STATE is not None
    with STATE.lock:
        return json.loads(json.dumps(STATE.tree))


def _serve_path(target: Optional[Path], not_found_msg: str) -> Response:
    if not target or not target.exists():
        abort(404, description=not_found_msg)
    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return send_file(target, mimetype=mime, conditional=True)


@app.route("/api/list", methods=["GET"])
def api_list() -> Response:
    payload = {
        "dataset_root": str(STATE.dataset_root if STATE else ""),
        "mesh_count": len(STATE.entries) if STATE else 0,
        "entries": serialize_entries(),
        "tree": snapshot_tree(),
        "cache_dir": str(CACHE_DIR),
    }
    return jsonify(payload)


@app.route("/api/rescan", methods=["POST"])
def api_rescan() -> Response:
    entries = [entry.as_dict() for entry in refresh_index()]
    payload = {
        "dataset_root": str(STATE.dataset_root if STATE else ""),
        "mesh_count": len(entries),
        "entries": entries,
        "tree": snapshot_tree(),
        "cache_dir": str(CACHE_DIR),
    }
    return jsonify(payload)


@app.route("/api/merge-session", methods=["POST"])
def api_merge_session() -> Response:
    assert STATE is not None
    data = request.get_json(silent=True) or {}
    session_rel_path = data.get("session_rel_path")
    if not session_rel_path or not isinstance(session_rel_path, str):
        return jsonify({"error": "session_rel_path is required"}), 400
    try:
        session_dir = _resolve_dataset_path(session_rel_path)
    except ValueError:
        return jsonify({"error": "Invalid session path"}), 400
    if not session_dir.exists() or not session_dir.is_dir():
        return jsonify({"error": "Session not found"}), 404

    mesh_paths = _collect_session_meshes(session_dir)
    if not mesh_paths:
        return jsonify({"error": "No mesh.obj found in this session"}), 400

    merge_dir_name = _merged_dir_name(session_dir)
    merged_path = session_dir / merge_dir_name / "mesh.obj"
    try:
        _merge_obj_files(mesh_paths, merged_path)
    except Exception as exc:
        return jsonify({"error": f"Merge failed: {exc}"}), 500

    entries = [entry.as_dict() for entry in refresh_index()]
    merged_rel_path = str(merged_path.relative_to(STATE.dataset_root)).replace("\\", "/")
    output_time_rel_path = str(merged_path.parent.relative_to(STATE.dataset_root)).replace("\\", "/")
    payload = {
        "dataset_root": str(STATE.dataset_root),
        "mesh_count": len(entries),
        "entries": entries,
        "tree": snapshot_tree(),
        "cache_dir": str(CACHE_DIR),
        "merged_mesh_id": _find_mesh_id(merged_path),
        "merged_rel_path": merged_rel_path,
        "output_time_rel_path": output_time_rel_path,
    }
    return jsonify(payload)


@app.route("/api/tsdf-session", methods=["POST"])
def api_tsdf_session() -> Response:
    assert STATE is not None
    data = request.get_json(silent=True) or {}
    session_rel_path = data.get("session_rel_path")
    if not session_rel_path or not isinstance(session_rel_path, str):
        return jsonify({"error": "session_rel_path is required"}), 400
    try:
        session_dir = _resolve_dataset_path(session_rel_path)
    except ValueError:
        return jsonify({"error": "Invalid session path"}), 400
    if not session_dir.exists() or not session_dir.is_dir():
        return jsonify({"error": "Session not found"}), 404

    mesh_paths = _collect_session_meshes(session_dir)
    if not mesh_paths:
        return jsonify({"error": "No mesh.obj found in this session"}), 400

    tsdf_dir_name = _tsdf_dir_name(session_dir)
    tsdf_path = session_dir / tsdf_dir_name / "mesh.obj"
    try:
        _tsdf_mesh_from_paths(mesh_paths, tsdf_path)
    except Exception as exc:
        return jsonify({"error": f"TSDF failed: {exc}"}), 500

    entries = [entry.as_dict() for entry in refresh_index()]
    tsdf_rel_path = str(tsdf_path.relative_to(STATE.dataset_root)).replace("\\", "/")
    output_time_rel_path = str(tsdf_path.parent.relative_to(STATE.dataset_root)).replace("\\", "/")
    payload = {
        "dataset_root": str(STATE.dataset_root),
        "mesh_count": len(entries),
        "entries": entries,
        "tree": snapshot_tree(),
        "cache_dir": str(CACHE_DIR),
        "merged_mesh_id": _find_mesh_id(tsdf_path),
        "merged_rel_path": tsdf_rel_path,
        "output_time_rel_path": output_time_rel_path,
    }
    return jsonify(payload)


@app.route("/mesh/<mesh_id>", methods=["GET"])
def mesh(mesh_id: str) -> Response:
    assert STATE is not None
    with STATE.lock:
        target = STATE.path_map.get(mesh_id)
    return _serve_path(target, "Mesh not found")


@app.route("/preview/<preview_id>", methods=["GET"])
def preview(preview_id: str) -> Response:
    assert STATE is not None
    with STATE.lock:
        target = STATE.preview_map.get(preview_id)
    return _serve_path(target, "Preview not found")


@app.route("/rir/<rir_id>", methods=["GET"])
def rir(rir_id: str) -> Response:
    assert STATE is not None
    with STATE.lock:
        target = STATE.rir_map.get(rir_id)
    return _serve_path(target, "RIR not found")


@app.route("/", defaults={"asset_path": "index.html"})
@app.route("/<path:asset_path>")
def frontend(asset_path: str) -> Response:
    # serve web dir, block traversal
    target = (WEB_DIR / asset_path).resolve()
    try:
        target.relative_to(WEB_DIR)
    except ValueError:
        abort(404)
    if not target.exists() or not target.is_file():
        abort(404)
    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return send_file(target, mimetype=mime, conditional=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the mesh viewer web UI")
    parser.add_argument("--dataset", type=str, help="Path to dataset root (defaults to last used or viewer/dataset)")
    parser.add_argument("--port", type=int, default=8800, help="Port for the local web server")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    parser.add_argument("--no-dialog", action="store_true", help="Skip folder selection dialog")
    return parser.parse_args()


def main() -> None:
    global STATE
    args = parse_args()

    dataset_root = choose_dataset_root(args.dataset, allow_dialog=not args.no_dialog).resolve()
    remember_dataset(dataset_root)

    STATE = AppState(dataset_root=dataset_root)
    entries = refresh_index()

    print("==============================================")
    print(" Mesh Viewer")
    print("----------------------------------------------")
    print(f" Dataset: {dataset_root}")
    print(f" Meshes : {len(entries)} found (mesh.obj files)")
    print(f" Cache  : {CACHE_DIR}")
    print(f" Web UI : http://localhost:{args.port}")
    print("==============================================")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    try:
        app.run(host="0.0.0.0", port=args.port, threaded=True)
    except KeyboardInterrupt:
        print("\n[mesh-viewer] Shutting down...")


if __name__ == "__main__":
    main()
