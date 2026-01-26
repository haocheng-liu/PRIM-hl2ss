import * as THREE from './lib/three.module.js';
import { OrbitControls } from './lib/OrbitControls.js';
import { OBJLoader } from './lib/OBJLoader.js';

const els = {
  datasetPath: document.getElementById('dataset-path'),
  meshCount: document.getElementById('mesh-count'),
  tree: document.getElementById('dataset-tree'),
  rescanBtn: document.getElementById('rescan-btn'),
  selectedName: document.getElementById('selected-name'),
  meta: document.getElementById('meta'),
  viewer: document.getElementById('viewer'),
  status: document.getElementById('status'),
  previewGrid: document.getElementById('preview-grid'),
  rirGrid: document.getElementById('rir-grid'),
  rirModeToggle: document.getElementById('rir-mode-toggle'),
};

function openLightbox(src, alt) {
  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  const img = document.createElement('img');
  img.src = src;
  img.alt = alt || '';
  overlay.appendChild(img);
  overlay.addEventListener('click', () => overlay.remove());
  document.body.appendChild(overlay);
}

const loader = new OBJLoader();
const meshCache = new Map();
const rirDataCache = new Map();
const state = {
  entries: [],
  entryById: new Map(),
  tree: [],
  selected: null,
  activeTimeKey: null,
  rirMode: 'waveform',
};

const markerGroup = new THREE.Group();
let activeTreeNode = null;

// three.js setup
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.setPixelRatio(window.devicePixelRatio);

const viewerRect = () => els.viewer.getBoundingClientRect();
const initSize = viewerRect();
renderer.setSize(initSize.width, initSize.height);
els.viewer.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b101f);

const camera = new THREE.PerspectiveCamera(60, initSize.width / initSize.height, 0.01, 5000);
camera.position.set(2, 2, 2);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

const ambient = new THREE.AmbientLight(0xffffff, 0.35);
const key = new THREE.DirectionalLight(0xffffff, 0.8);
key.position.set(4, 4, 4);
const fill = new THREE.DirectionalLight(0x8fb5ff, 0.5);
fill.position.set(-3, 2, -2);
scene.add(ambient, key, fill);
scene.add(markerGroup);

let activeMesh = null;

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

function resize() {
  const rect = viewerRect();
  camera.aspect = rect.width / rect.height;
  camera.updateProjectionMatrix();
  renderer.setSize(rect.width, rect.height);
}
window.addEventListener('resize', resize);

// helpers
function setStatus(text) {
  // quick status hint
  if (!text) {
    els.status.classList.add('hidden');
    els.status.textContent = '';
    return;
  }
  els.status.textContent = text;
  els.status.classList.remove('hidden');
}

function formatBytes(bytes) {
  if (!bytes && bytes !== 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  const e = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** e).toFixed(e === 0 ? 0 : 1)} ${units[e]}`;
}

function formatTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function applyMaterial(object) {
  object.traverse((child) => {
    if (child.isMesh) {
      child.material = new THREE.MeshStandardMaterial({
        color: 0x67e8f9,
        metalness: 0.05,
        roughness: 0.8,
        flatShading: false,
        side: THREE.DoubleSide,
      });
      child.castShadow = false;
      child.receiveShadow = false;
    }
  });
}

function fitToView(object, targetCamera, targetControls) {
  const box = new THREE.Box3().setFromObject(object);
  if (!box.isEmpty()) {
    const size = new THREE.Vector3();
    box.getSize(size);
    const center = new THREE.Vector3();
    box.getCenter(center);
    const maxDim = Math.max(size.x, size.y, size.z, 0.01);
    const dir = new THREE.Vector3(0.85, 0.45, 0.7).normalize(); // slight down/side
    const dist = Math.max(maxDim * 0.4, maxDim * 0.15);
    const pos = center.clone().add(dir.multiplyScalar(dist));
    targetCamera.position.copy(pos);
    targetCamera.near = Math.max(maxDim / 2000, 0.001);
    targetCamera.far = Math.max(maxDim * 8, 10);
    targetCamera.updateProjectionMatrix();
    targetControls.target.copy(center);
    targetControls.update();
  }
}

function addMarkers(entry, mesh) {
  markerGroup.clear();
  const markers = entry.markers || {};
  const mic = markers.mic;
  const micList = Array.isArray(markers.mics) ? markers.mics : (mic ? [mic] : []);
  const src = markers.source;
  const isSourcePov = typeof entry.rel_path === 'string' && entry.rel_path.includes('source_pov');
  if (!micList.length && !src) return;

  const box = new THREE.Box3().setFromObject(mesh);
  const diag = box.getSize(new THREE.Vector3()).length();
  const base = Math.max(0.02, Math.min(0.05, diag * 0.02 || 0.05));

  if (src) {
    const spriteMap = new THREE.CanvasTexture(createSpeakerSprite());
    const material = new THREE.SpriteMaterial({ map: spriteMap, transparent: true, depthWrite: false, sizeAttenuation: true });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(base * 3, base * 3, 1);
    sprite.position.set(src[0], src[1], src[2]);
    markerGroup.add(sprite);
  }

  if (micList.length && !isSourcePov) {
    const spriteMap = new THREE.CanvasTexture(createEmojiSprite());
    const material = new THREE.SpriteMaterial({ map: spriteMap, transparent: true, depthWrite: false, sizeAttenuation: true });
    micList.forEach((pos) => {
      const sprite = new THREE.Sprite(material);
      sprite.scale.set(base * 2.5, base * 2.5, 1);
      sprite.position.set(pos[0], pos[1], pos[2]);
      markerGroup.add(sprite);
    });
  }
}

function createSpeakerSprite() {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, size, size);
  ctx.fillStyle = '#fbbf24';
  ctx.font = '72px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('🔈', size / 2, size / 2);
  return canvas;
}

function createEmojiSprite() {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, size, size);
  ctx.font = '96px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText('😎', size / 2, size / 2);
  return canvas;
}

function getMesh(entry) {
  if (!meshCache.has(entry.id)) {
    meshCache.set(
      entry.id,
      new Promise((resolve, reject) => {
        loader.load(
          `/mesh/${entry.id}`,
          (obj) => resolve(obj),
          undefined,
          (err) => reject(err),
        );
      }),
    );
  }
  return meshCache.get(entry.id);
}

function clearActiveMesh() {
  if (activeMesh) {
    scene.remove(activeMesh);
    activeMesh = null;
  }
  markerGroup.clear();
}

async function loadMesh(entry) {
  setStatus('Loading...');
  try {
    const original = await getMesh(entry);
    const mesh = original.clone(true);
    applyMaterial(mesh);
    clearActiveMesh();
    scene.add(mesh);
    activeMesh = mesh;
    fitToView(mesh, camera, controls);
    els.selectedName.textContent = entry.name;
    els.meta.textContent = `${formatBytes(entry.size)} | ${formatTime(entry.mtime)}`;
    state.selected = entry;
    renderPreviews(entry);
    renderRirs(entry);
    addMarkers(entry, mesh);
    setStatus('');
  } catch (err) {
    console.error(err);
    setStatus('Failed to load. Please check dataset files.');
  }
}



function setActiveTreeNode(node) {
  if (activeTreeNode) {
    activeTreeNode.classList.remove('active');
  }
  activeTreeNode = node;
  if (activeTreeNode) {
    activeTreeNode.classList.add('active');
  }
}

function showMissingSelection(time) {
  clearActiveMesh();
  els.selectedName.textContent = `${time.name} (no mesh)`;
  els.meta.textContent = '';
  els.previewGrid.innerHTML = '';
  els.rirGrid.innerHTML = '';

  const previewMsg = document.createElement('p');
  previewMsg.className = 'muted tiny';
  previewMsg.textContent = 'Mesh missing for this time folder.';
  els.previewGrid.appendChild(previewMsg);

  const rirMsg = document.createElement('p');
  rirMsg.className = 'muted tiny';
  rirMsg.textContent = 'Mesh missing for this time folder.';
  els.rirGrid.appendChild(rirMsg);
  setStatus('Mesh missing for this time folder.');
  state.selected = null;
}

function isMergeNode(time) {
  return time && time.kind === 'merge';
}

function isTsdfNode(time) {
  return time && time.kind === 'tsdf';
}

async function generateMergedSession(time, node) {
  if (!time?.session_rel_path) {
    setStatus('Missing session path for merge.');
    return;
  }
  if (node.classList.contains('loading')) return;

  const badge = node.querySelector('.badge');
  if (badge) badge.textContent = 'Generating...';
  node.classList.add('loading');
  setStatus('Generating merged mesh...');
  try {
    const res = await fetch('/api/merge-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_rel_path: time.session_rel_path }),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (err) {
      data = null;
    }
    if (!res.ok) {
      const msg = data?.error || `Merge failed (${res.status})`;
      throw new Error(msg);
    }
    if (!data) throw new Error('Merge failed (empty response).');
    if (data.merged_mesh_id) meshCache.delete(data.merged_mesh_id);
    syncStateFromResponse(data);
    const outputKey = data.output_time_rel_path
      || (data.merged_rel_path ? data.merged_rel_path.replace(/\/mesh\.obj$/, '') : null)
      || time.rel_path;
    state.activeTimeKey = outputKey;
    renderTree(state.tree);
  } catch (err) {
    console.error(err);
    setStatus(err.message || 'Merge failed.');
    node.classList.remove('loading');
    if (badge) badge.textContent = 'Generate';
  }
}

async function generateTsdfSession(time, node) {
  if (!time?.session_rel_path) {
    setStatus('Missing session path for TSDF.');
    return;
  }
  if (node.classList.contains('loading')) return;

  const badge = node.querySelector('.badge');
  if (badge) badge.textContent = 'Generating...';
  node.classList.add('loading');
  setStatus('Generating TSDF mesh...');
  try {
    const res = await fetch('/api/tsdf-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_rel_path: time.session_rel_path }),
    });
    let data = null;
    try {
      data = await res.json();
    } catch (err) {
      data = null;
    }
    if (!res.ok) {
      const msg = data?.error || `TSDF failed (${res.status})`;
      throw new Error(msg);
    }
    if (!data) throw new Error('TSDF failed (empty response).');
    if (data.merged_mesh_id) meshCache.delete(data.merged_mesh_id);
    syncStateFromResponse(data);
    const outputKey = data.output_time_rel_path
      || (data.merged_rel_path ? data.merged_rel_path.replace(/\/mesh\.obj$/, '') : null)
      || time.rel_path;
    state.activeTimeKey = outputKey;
    renderTree(state.tree);
  } catch (err) {
    console.error(err);
    setStatus(err.message || 'TSDF failed.');
    node.classList.remove('loading');
    if (badge) badge.textContent = 'Generate';
  }
}

function selectTime(time, node) {
  setActiveTreeNode(node);
  state.activeTimeKey = time.rel_path;
  if (isMergeNode(time) && !time.has_mesh) {
    generateMergedSession(time, node);
    return;
  }
  if (isTsdfNode(time) && !time.has_mesh) {
    generateTsdfSession(time, node);
    return;
  }
  if (!time.has_mesh || !time.mesh_id) {
    showMissingSelection(time);
    return;
  }
  const entry = state.entryById.get(time.mesh_id);
  if (!entry) {
    setStatus('Mesh metadata unavailable. Try rescanning.');
    return;
  }
  loadMesh(entry);
}

function createTimeRow(time) {
  const row = document.createElement('div');
  const mergeNode = isMergeNode(time);
  const tsdfNode = isTsdfNode(time);
  const missing = !time.has_mesh && !mergeNode && !tsdfNode;
  row.className = `tree-row time${missing ? ' missing' : ''}${mergeNode ? ' merge' : ''}${tsdfNode ? ' tsdf' : ''}`;
  row.dataset.timeKey = time.rel_path;

  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = time.name;

  const badge = document.createElement('span');
  if (mergeNode) {
    badge.className = `badge ${time.has_mesh ? 'badge-merged' : 'badge-merge'}`;
    badge.textContent = time.has_mesh ? 'Merged mesh' : 'Generate';
  } else if (tsdfNode) {
    badge.className = `badge ${time.has_mesh ? 'badge-tsdf-ready' : 'badge-tsdf'}`;
    badge.textContent = time.has_mesh ? 'TSDF mesh' : 'Generate';
  } else {
    badge.className = `badge ${time.has_mesh ? '' : 'badge-missing'}`;
    badge.textContent = time.has_mesh ? 'mesh.obj' : 'Mesh missing';
  }

  row.appendChild(label);
  row.appendChild(badge);

  row.addEventListener('click', (evt) => {
    evt.stopPropagation();
    selectTime(time, row);
  });

  return row;
}

function renderTree(tree) {
  els.tree.innerHTML = '';
  setActiveTreeNode(null);
  if (!tree.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No rooms found under dataset.';
    els.tree.appendChild(empty);
    return;
  }

  let selection = null;

  tree.forEach((room) => {
    const roomNode = document.createElement('details');
    roomNode.className = 'tree-node room';
    roomNode.open = true;

    const roomHeader = document.createElement('summary');
    roomHeader.textContent = room.name;
    roomNode.appendChild(roomHeader);

    const sessionWrap = document.createElement('div');
    sessionWrap.className = 'tree-children';

    (room.sessions || []).forEach((session) => {
      const sessionNode = document.createElement('details');
      sessionNode.className = 'tree-node session';
      sessionNode.open = false;
      const sessionHeader = document.createElement('summary');
      sessionHeader.textContent = session.name;
      sessionNode.appendChild(sessionHeader);

      const timeWrap = document.createElement('div');
      timeWrap.className = 'tree-children';
      (session.times || []).forEach((time) => {
        const row = createTimeRow(time);
        timeWrap.appendChild(row);
        if (time.rel_path === state.activeTimeKey) {
          selection = { time, row };
          sessionNode.open = true;
          roomNode.open = true;
        } else if (!selection && time.has_mesh) {
          selection = { time, row };
        }
      });

      if (timeWrap.children.length) {
        sessionNode.appendChild(timeWrap);
        sessionWrap.appendChild(sessionNode);
      }
    });

    if (sessionWrap.children.length) {
      roomNode.appendChild(sessionWrap);
      els.tree.appendChild(roomNode);
    }
  });

  if (selection) {
    selectTime(selection.time, selection.row);
  } else {
    setStatus(tree.length ? 'No mesh.obj detected under times.' : 'Dataset is empty.');
    clearActiveMesh();
    setActiveTreeNode(null);
    state.activeTimeKey = null;
    state.selected = null;
    els.selectedName.textContent = 'Select a mesh to inspect';
    els.meta.textContent = '';
    els.previewGrid.innerHTML = '';
    els.rirGrid.innerHTML = '';
  }
}

function renderPreviews(entry) {
  els.previewGrid.innerHTML = '';
  const previews = entry.previews || [];
  if (!previews.length) {
    const empty = document.createElement('p');
    empty.className = 'muted tiny';
    empty.textContent = 'No related captures found.';
    els.previewGrid.appendChild(empty);
    return;
  }

  previews.forEach((p) => {
    const card = document.createElement('div');
    card.className = 'preview-card';
    const img = document.createElement('img');
    img.alt = p.name;
    img.loading = 'lazy';
    img.src = `/preview/${p.id}`;
    img.addEventListener('click', () => openLightbox(`/preview/${p.id}`, p.name));
    const label = document.createElement('div');
    label.className = 'label';
    label.textContent = p.name;
    card.appendChild(img);
    card.appendChild(label);
    els.previewGrid.appendChild(card);
  });
}

async function loadRIRData(url) {
  if (rirDataCache.has(url)) return rirDataCache.get(url);

  const task = (async () => {
    const buf = await fetch(url).then((res) => {
      if (!res.ok) throw new Error(`Fetch failed ${res.status}`);
      return res.arrayBuffer();
    });

    // try WebAudio first
    if (window.AudioContext) {
      try {
        const audioBuffer = await new AudioContext().decodeAudioData(buf.slice(0));
        return {
          samples: audioBuffer.getChannelData(0),
          sampleRate: audioBuffer.sampleRate,
        };
      } catch (err) {
        console.warn('WebAudio decode failed, falling back to manual parse', err);
      }
    }

    // manual PCM16 parse
    const view = new DataView(buf);
    if (view.getUint32(0, false) !== 0x52494646) {
      throw new Error('Not RIFF');
    }
    let offset = 12;
    let fmt = null;
    let dataOffset = null;
    let dataSize = null;
    while (offset + 8 <= view.byteLength) {
      const chunkId = view.getUint32(offset, false);
      const chunkSize = view.getUint32(offset + 4, true);
      if (chunkId === 0x666d7420) fmt = { offset: offset + 8, size: chunkSize };
      if (chunkId === 0x64617461) {
        dataOffset = offset + 8;
        dataSize = chunkSize;
      }
      offset += 8 + chunkSize;
    }
    if (!fmt || dataOffset === null || dataSize === null) throw new Error('Missing fmt/data');
    const audioFormat = view.getUint16(fmt.offset + 0, true);
    const numChannels = view.getUint16(fmt.offset + 2, true);
    const sampleRate = view.getUint32(fmt.offset + 4, true);
    const bitsPerSample = view.getUint16(fmt.offset + 14, true);
    if (audioFormat !== 1 || bitsPerSample !== 16) throw new Error('Only PCM16 supported in fallback');
    const frameCount = Math.floor((dataSize / (bitsPerSample / 8)) / numChannels);
    const samples = new Float32Array(frameCount);
    for (let i = 0; i < frameCount; i += 1) {
      const sample = view.getInt16(dataOffset + i * numChannels * 2, true);
      samples[i] = sample / 32768;
    }
    return { samples, sampleRate };
  })();

  rirDataCache.set(url, task);
  return task;
}

async function drawWaveform(url, canvas) {
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;
  ctx.fillStyle = '#0a0f1f';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 1;
  try {
    const rir = await loadRIRData(url);
    const data = rir.samples;
    const amp = height / 2;

    // full scan peak for norm
    let maxAbs = 0;
    for (let i = 0; i < data.length; i += 1) {
      const a = Math.abs(data[i]);
      if (a > maxAbs) maxAbs = a;
    }
    if (maxAbs < 1e-9) maxAbs = 1;

    const step = Math.max(1, Math.floor(data.length / width));
    ctx.beginPath();
    for (let x = 0; x < width; x += 1) {
      const start = x * step;
      const end = Math.min(start + step, data.length);
      let min = 1.0;
      let max = -1.0;
      for (let i = start; i < end; i += 1) {
        const v = data[i] / maxAbs;
        if (v < min) min = v;
        if (v > max) max = v;
      }
      const yMin = amp - max * amp;
      const yMax = amp - min * amp;
      ctx.moveTo(x, yMin);
      ctx.lineTo(x, yMax);
    }
    ctx.stroke();
  } catch (err) {
    ctx.fillStyle = '#ef4444';
    ctx.fillText('Waveform unavailable', 8, height / 2);
    console.warn('Waveform render failed', err);
  }
}

function fftRadix2(re, im) {
  const n = re.length;
  if (n <= 1 || (n & (n - 1)) !== 0) return;

  for (let i = 1, j = 0; i < n; i += 1) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j &= ~bit;
    j |= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }

  for (let len = 2; len <= n; len <<= 1) {
    const ang = -2 * Math.PI / len;
    const wLenCos = Math.cos(ang);
    const wLenSin = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let wCos = 1;
      let wSin = 0;
      for (let j = 0; j < len / 2; j += 1) {
        const uRe = re[i + j];
        const uIm = im[i + j];
        const vRe = re[i + j + len / 2] * wCos - im[i + j + len / 2] * wSin;
        const vIm = re[i + j + len / 2] * wSin + im[i + j + len / 2] * wCos;
        re[i + j] = uRe + vRe;
        im[i + j] = uIm + vIm;
        re[i + j + len / 2] = uRe - vRe;
        im[i + j + len / 2] = uIm - vIm;
        const nextCos = wCos * wLenCos - wSin * wLenSin;
        wSin = wCos * wLenSin + wSin * wLenCos;
        wCos = nextCos;
      }
    }
  }
}

function spectrogramColor(t) {
  // crude viridis-like ramp
  const x = Math.min(Math.max(t, 0), 1);
  const r = Math.floor(255 * (0.267 + x * (0.718 - 0.267)));
  const g = Math.floor(255 * (0.004 + x * (0.828 - 0.004)));
  const b = Math.floor(255 * (0.329 + x * (0.299 - 0.329)));
  return [r, g, b];
}

async function drawSpectrogram(url, canvas) {
  const ctx = canvas.getContext('2d');
  const { width, height } = canvas;
  ctx.fillStyle = '#0a0f1f';
  ctx.fillRect(0, 0, width, height);
  try {
    const rir = await loadRIRData(url);
    const data = rir.samples;
    const fftSize = 512;
    const hop = 256;
    const window = new Float32Array(fftSize);
    for (let i = 0; i < fftSize; i += 1) {
      window[i] = 0.5 * (1 - Math.cos((2 * Math.PI * i) / (fftSize - 1))); // Hann
    }

    const frameCount = Math.max(1, Math.floor((data.length - fftSize) / hop) + 1);
    const spectra = new Array(frameCount);
    let maxMag = 1e-8;

    for (let frame = 0; frame < frameCount; frame += 1) {
      const re = new Float32Array(fftSize);
      const im = new Float32Array(fftSize);
      const start = frame * hop;
      for (let i = 0; i < fftSize; i += 1) {
        const idx = start + i;
        const sample = idx < data.length ? data[idx] : 0;
        re[i] = sample * window[i];
      }
      fftRadix2(re, im);
      const mags = new Float32Array(fftSize / 2);
      for (let k = 0; k < fftSize / 2; k += 1) {
        const mag = Math.hypot(re[k], im[k]);
        mags[k] = mag;
        if (mag > maxMag) maxMag = mag;
      }
      spectra[frame] = mags;
    }

    const dynRange = 60; // dB range
    const image = ctx.createImageData(width, height);
    for (let x = 0; x < width; x += 1) {
      const frameIdx = spectra.length === 1 ? 0 : Math.floor((x / (width - 1)) * (spectra.length - 1));
      const spectrum = spectra[frameIdx] || spectra[spectra.length - 1];
      for (let y = 0; y < height; y += 1) {
        const bin = spectrum.length === 1 ? 0 : Math.floor(((height - 1 - y) / (height - 1)) * (spectrum.length - 1));
        const mag = spectrum[bin];
        const db = 20 * Math.log10(mag / maxMag + 1e-8);
        const norm = Math.min(Math.max(1 + db / dynRange, 0), 1); // 0 at -dynRange
        const [r, g, b] = spectrogramColor(norm);
        const idx = (y * width + x) * 4;
        image.data[idx] = r;
        image.data[idx + 1] = g;
        image.data[idx + 2] = b;
        image.data[idx + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
  } catch (err) {
    ctx.fillStyle = '#ef4444';
    ctx.fillText('Spectrogram unavailable', 8, height / 2);
    console.warn('Spectrogram render failed', err);
  }
}

async function openRIRLightbox(rir, mode) {
  const overlay = document.createElement('div');
  overlay.className = 'lightbox';
  const canvas = document.createElement('canvas');
  canvas.width = mode === 'spectrogram' ? 1200 : 1200;
  canvas.height = mode === 'spectrogram' ? 600 : 400;
  overlay.appendChild(canvas);
  overlay.addEventListener('click', () => overlay.remove());
  document.body.appendChild(overlay);
  try {
    if (mode === 'spectrogram') {
      await drawSpectrogram(`/rir/${rir.id}`, canvas);
    } else {
      await drawWaveform(`/rir/${rir.id}`, canvas);
    }
  } catch (err) {
    console.warn('Lightbox render failed', err);
  }
}

function updateRirModeToggle() {
  if (!els.rirModeToggle) return;
  const mode = state.rirMode === 'spectrogram' ? 'spectrogram' : 'waveform';
  els.rirModeToggle.textContent = mode === 'waveform' ? 'Show STFT' : 'Show waveform';
}

function renderRirs(entry) {
  els.rirGrid.innerHTML = '';
  const rirs = entry?.rirs || [];
  if (!rirs.length) {
    const empty = document.createElement('p');
    empty.className = 'muted tiny';
    empty.textContent = 'No RIR files found.';
    els.rirGrid.appendChild(empty);
    return;
  }

  const mode = state.rirMode === 'spectrogram' ? 'spectrogram' : 'waveform';
  const renderFn = mode === 'spectrogram' ? drawSpectrogram : drawWaveform;
  const canvasHeight = mode === 'spectrogram' ? 140 : 70;

  rirs.forEach((rir) => {
    const card = document.createElement('div');
    card.className = 'rir-card';

    const canvas = document.createElement('canvas');
    canvas.width = 320;
    canvas.height = canvasHeight;
    canvas.style.height = `${canvasHeight}px`;
    canvas.title = `Click to view ${mode === 'spectrogram' ? 'STFT' : 'waveform'} in full size`;
    renderFn(`/rir/${rir.id}`, canvas);
    canvas.addEventListener('click', () => openRIRLightbox(rir, mode));

    const label = document.createElement('div');
    label.className = 'label';
    const kb = Math.round(rir.size / 1024);
    label.textContent = `${rir.channel} (${kb} KB) · ${mode === 'spectrogram' ? 'STFT' : 'Waveform'}`;

    const audio = document.createElement('audio');
    audio.controls = true;
    audio.src = `/rir/${rir.id}`;

    card.appendChild(canvas);
    card.appendChild(label);
    card.appendChild(audio);
    els.rirGrid.appendChild(card);
  });
}

async function fetchList(endpoint = '/api/list') {
  const res = await fetch(endpoint, { method: endpoint === '/api/rescan' ? 'POST' : 'GET' });
  if (!res.ok) throw new Error('Failed to fetch list');
  return res.json();
}

function syncStateFromResponse(data) {
  state.entries = data.entries || [];
  state.entryById = new Map(state.entries.map((entry) => [entry.id, entry]));
  state.tree = data.tree || [];
  els.datasetPath.textContent = data.dataset_root || 'Dataset not set';
  els.meshCount.textContent = data.mesh_count ?? state.entries.length;
}

async function init() {
  setStatus('Loading index...');
  try {
    const data = await fetchList();
    syncStateFromResponse(data);
    renderTree(state.tree);
  } catch (err) {
    console.error(err);
    setStatus('Cannot fetch list. Check server logs.');
  }
}

els.rescanBtn.addEventListener('click', async () => {
  setStatus('Rescanning...');
  try {
    const data = await fetchList('/api/rescan');
    meshCache.clear();
    rirDataCache.clear();
    syncStateFromResponse(data);
    renderTree(state.tree);
    setStatus(state.entries.length ? '' : 'No mesh.obj detected.');
  } catch (err) {
    console.error(err);
    setStatus('Rescan failed.');
  }
});

els.rirModeToggle.addEventListener('click', () => {
  state.rirMode = state.rirMode === 'spectrogram' ? 'waveform' : 'spectrogram';
  updateRirModeToggle();
  if (state.selected) {
    renderRirs(state.selected);
  }
});

// bubble unexpected errors
window.addEventListener('error', (e) => {
  setStatus(`JS error: ${e.message}`);
});
window.addEventListener('unhandledrejection', (e) => {
  setStatus(`Promise error: ${e.reason}`);
});

updateRirModeToggle();
init();
