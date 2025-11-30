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
import functools
import hashlib
import http.server
import json
import mimetypes
import numpy as np
import os
import shutil
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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
        self.lock = threading.Lock()


STATE: Optional[AppState] = None


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
    except Exception as exc:  # pragma: no cover - Tk not always available
        print(f"[mesh-viewer] Folder dialog unavailable ({exc}); using {candidate}")

    return candidate


def _collect_previews(mesh_path: Path, dataset_root: Path) -> Tuple[List[PreviewAsset], Dict[str, Path]]:
    """Return preview assets (png thumbnails) living next to the mesh."""
    preview_assets: List[PreviewAsset] = []
    preview_map: Dict[str, Path] = {}

    # Typical layout: .../<session>/<source>/mesh/mesh.obj -> sibling image/ contains PNGs.
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
    """Load mic/source positions from session-level source_pov/position/origin.npy.

    - mic: first row
    - source: last row
    """

    def _session_root(path: Path) -> Optional[Path]:
        for parent in path.parents:
            if parent.name.startswith("session_"):
                return parent
        return None

    session_dir = _session_root(mesh_path)
    candidate = None
    if session_dir:
        candidate = session_dir / "source_pov" / "position"
    if not candidate or not candidate.exists():
        # Fallback to local position (old behavior)
        candidate = mesh_path.parent.parent / "position"
        if not candidate.exists():
            candidate = mesh_path.parent / "position"

    origin_file = candidate / "origin.npy" if candidate else None
    if not origin_file or not origin_file.exists():
        return None, None

    try:
        arr = np.load(origin_file)
    except Exception:
        return None, None

    if arr.ndim != 2 or arr.shape[1] < 3 or arr.shape[0] == 0:
        return None, None

    mic = arr[0, :3].astype(float).tolist()
    src = arr[-1, :3].astype(float).tolist() if arr.shape[0] > 1 else None
    return mic, src


def build_mesh_index(dataset_root: Path) -> Tuple[List[MeshEntry], Dict[str, Path], Dict[str, Path], Dict[str, Path]]:
    """Scan dataset_root for mesh.obj files and build index + id->path map."""
    entries: List[MeshEntry] = []
    path_map: Dict[str, Path] = {}
    preview_map: Dict[str, Path] = {}
    rir_map: Dict[str, Path] = {}

    if not dataset_root.exists():
        return entries, path_map

    for obj_path in sorted(dataset_root.rglob("mesh.obj")):
        if not obj_path.is_file():
            continue
        rel_path = obj_path.relative_to(dataset_root)
        display_name = " / ".join(rel_path.parts[:-1]) or obj_path.name
        entry_id = hashlib.sha1(str(obj_path).encode("utf-8")).hexdigest()[:12]
        stat = obj_path.stat()
        previews, local_preview_map = _collect_previews(obj_path, dataset_root)
        rirs, local_rir_map = _collect_rirs(obj_path, dataset_root)
        mic_pos, src_pos = _load_markers(obj_path)

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
        entries.append(entry)
        path_map[entry_id] = obj_path
        preview_map.update(local_preview_map)
        rir_map.update(local_rir_map)

    return entries, path_map, preview_map, rir_map


def write_index_cache(dataset_root: Path, entries: List[MeshEntry]) -> None:
    """Persist the latest scan for quick inspection or reuse."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_root": str(dataset_root),
        "generated_at": time.time(),
        "mesh_count": len(entries),
        "entries": [entry.as_dict() for entry in entries],
    }
    CACHE_INDEX_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def refresh_index() -> List[MeshEntry]:
    """Rebuild the mesh index and update global state + cache."""
    global STATE
    assert STATE is not None

    entries, path_map, preview_map, rir_map = build_mesh_index(STATE.dataset_root)

    with STATE.lock:
        STATE.entries = entries
        STATE.path_map = path_map
        STATE.preview_map = preview_map
        STATE.rir_map = rir_map

    write_index_cache(STATE.dataset_root, entries)
    return entries


def serialize_entries() -> List[Dict[str, object]]:
    assert STATE is not None
    with STATE.lock:
        return [entry.as_dict() for entry in STATE.entries]


class MeshViewerHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler that serves the web UI plus mesh data APIs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Quieter logging; uncomment for debugging.
        # print("[mesh-viewer]", format % args)
        return

    def _send_json(self, payload: Dict[str, object], status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_mesh_file(self, mesh_id: str) -> None:
        assert STATE is not None
        with STATE.lock:
            target = STATE.path_map.get(mesh_id)

        if not target or not target.exists():
            self.send_error(404, "Mesh not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()

        try:
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)
        except (ConnectionResetError, BrokenPipeError):
            return

    def _send_preview_file(self, preview_id: str) -> None:
        assert STATE is not None
        with STATE.lock:
            target = STATE.preview_map.get(preview_id)

        if not target or not target.exists():
            self.send_error(404, "Preview not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()

        try:
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)
        except (ConnectionResetError, BrokenPipeError):
            return

    def _send_rir_file(self, rir_id: str) -> None:
        assert STATE is not None
        with STATE.lock:
            target = STATE.rir_map.get(rir_id)

        if not target or not target.exists():
            self.send_error(404, "RIR not found")
            return

        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()

        try:
            with target.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile)
        except (ConnectionResetError, BrokenPipeError):
            # Client disconnected mid-transfer; safe to ignore.
            return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/list":
            payload = {
                "dataset_root": str(STATE.dataset_root if STATE else ""),
                "mesh_count": len(STATE.entries) if STATE else 0,
                "entries": serialize_entries(),
                "cache_dir": str(CACHE_DIR),
            }
            self._send_json(payload)
            return

        if parsed.path.startswith("/mesh/"):
            mesh_id = parsed.path.split("/mesh/", 1)[1]
            self._send_mesh_file(mesh_id)
            return

        if parsed.path.startswith("/preview/"):
            preview_id = parsed.path.split("/preview/", 1)[1]
            self._send_preview_file(preview_id)
            return

        if parsed.path.startswith("/rir/"):
            rir_id = parsed.path.split("/rir/", 1)[1]
            self._send_rir_file(rir_id)
            return

        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/rescan":
            entries = [entry.as_dict() for entry in refresh_index()]
            payload = {
                "dataset_root": str(STATE.dataset_root if STATE else ""),
                "mesh_count": len(entries),
                "entries": entries,
                "cache_dir": str(CACHE_DIR),
            }
            self._send_json(payload)
            return

        self.send_error(404, "Not found")

    def guess_type(self, path: str) -> str:
        # Force JS to use an ES-module friendly MIME type regardless of OS defaults.
        ctype, _ = mimetypes.guess_type(path)
        if ctype == "text/plain" and path.endswith(".js"):
            return "application/javascript"
        return ctype or "application/octet-stream"


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

    handler = functools.partial(MeshViewerHandler)
    with socketserver.ThreadingTCPServer(("", args.port), handler) as httpd:
        httpd.daemon_threads = True
        if not args.no_browser:
            threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[mesh-viewer] Shutting down...")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    main()
