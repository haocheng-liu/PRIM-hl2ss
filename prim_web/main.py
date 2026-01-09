#!/usr/bin/env python3
"""Lightweight mesh dataset viewer with a local HTML frontend.

Features
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

from flask import Flask, Response, abort, jsonify, send_file


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CACHE_DIR = BASE_DIR / "cache"
DEFAULT_DATASET_ROOT = Path(__file__).resolve().parent.parent / "hl2ss-lk" / "viewer" / "dataset"
LAST_DATASET_FILE = CACHE_DIR / "last_dataset.txt"
CACHE_INDEX_FILE = CACHE_DIR / "mesh_index.json"
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


def _load_markers(mesh_path: Path) -> Tuple[Optional[List[float]], Optional[List[float]]]:
    """Load mic/source; source from session source_pov, mic from local time."""

    def _session_root(path: Path) -> Optional[Path]:
        for parent in path.parents:
            if parent.name.startswith("session_"):
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

    # mic: prefer local time position
    mic = None
    mic_dir = mesh_path.parent.parent / "position"
    if not mic_dir.exists():
        mic_dir = mesh_path.parent / "position"
    mic_arr = _load_origin(mic_dir / "origin.npy") if mic_dir.exists() else None
    if mic_arr is not None:
        mic = mic_arr[0, :3].astype(float).tolist()

    return mic, src


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
    mic_pos, src_pos = _load_markers(mesh_path)

    entry = MeshEntry(
        id=entry_id,
        name=display_name,
        rel_path=str(rel_path),
        size=stat.st_size,
        mtime=stat.st_mtime,
        previews=previews,
        rirs=rirs,
        mic_position=mic_pos,
        source_position=src_pos,
    )
    return entry, local_preview_map, local_rir_map


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
