import * as THREE from './lib/three.module.js';
import { OrbitControls } from './lib/OrbitControls.js';
import { OBJLoader } from './lib/OBJLoader.js';

const els = {
  datasetPath: document.getElementById('dataset-path'),
  meshCount: document.getElementById('mesh-count'),
  grid: document.getElementById('mesh-grid'),
  rescanBtn: document.getElementById('rescan-btn'),
  selectedName: document.getElementById('selected-name'),
  meta: document.getElementById('meta'),
  viewer: document.getElementById('viewer'),
  status: document.getElementById('status'),
  previewGrid: document.getElementById('preview-grid'),
  rirGrid: document.getElementById('rir-grid'),
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
  selected: null,
};

const markerGroup = new THREE.Group();

// --- Three.js viewer setup --------------------------------------------------
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

// --- Helpers ----------------------------------------------------------------
function setStatus(text) {
  // Keep a quick visual indicator of what failed/succeeded.
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
    const dir = new THREE.Vector3(0.85, 0.45, 0.7).normalize(); // biased toward looking slightly downward/sideways
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
  const src = markers.source;
  if (!mic && !src) return;

  const box = new THREE.Box3().setFromObject(mesh);
  const diag = box.getSize(new THREE.Vector3()).length();
  const base = Math.max(0.02, Math.min(0.05, diag * 0.02 || 0.05));

  if (mic) {
    const geom = new THREE.SphereGeometry(base * 0.5, 16, 12);
    const mat = new THREE.MeshStandardMaterial({ color: 0xff3b30, emissive: 0x3b0a0a, emissiveIntensity: 0.6 });
    const sphere = new THREE.Mesh(geom, mat);
    sphere.position.set(mic[0], mic[1], mic[2]);
    markerGroup.add(sphere);
  }

  if (src) {
    const spriteMap = new THREE.CanvasTexture(createSpeakerSprite());
    const material = new THREE.SpriteMaterial({ map: spriteMap, transparent: true, depthWrite: false, sizeAttenuation: true });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(base * 3, base * 3, 1);
    sprite.position.set(src[0], src[1], src[2]);
    markerGroup.add(sprite);
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

async function renderThumbnail(entry, canvas) {
  try {
    const obj = (await getMesh(entry)).clone(true);
    applyMaterial(obj);
    const width = canvas.clientWidth || 120;
    const height = canvas.clientHeight || 90;
    const thumbRenderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      canvas,
    });
    thumbRenderer.setSize(width, height, false);
    thumbRenderer.outputColorSpace = THREE.SRGBColorSpace;
    const thumbScene = new THREE.Scene();
    const thumbCamera = new THREE.PerspectiveCamera(55, width / height, 0.01, 5000);
    thumbScene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const light = new THREE.DirectionalLight(0xffffff, 0.6);
    light.position.set(2, 3, 2);
    thumbScene.add(light);
    thumbScene.add(obj);
    fitToView(obj, thumbCamera, { target: new THREE.Vector3(), update() {} });
    thumbRenderer.render(thumbScene, thumbCamera);
    thumbRenderer.dispose();
  } catch (err) {
    console.warn('Failed to render thumbnail', err);
    canvas.classList.add('muted');
  }
}

function createCard(entry) {
  const card = document.createElement('div');
  card.className = 'card';

  const preferred = (entry.previews || []).find((p) => p.name.toLowerCase().startsWith('personal_video'));
  const cover = preferred || (entry.previews && entry.previews[0]);

  let canvas = null;
  let thumbNode = null;
  if (cover) {
    const img = document.createElement('img');
    img.className = 'thumb-img';
    img.src = `/preview/${cover.id}`;
    img.alt = cover.name;
    thumbNode = img;
  } else {
    canvas = document.createElement('canvas');
    canvas.className = 'thumb';
    canvas.width = 240;
    canvas.height = 180;
    thumbNode = canvas;
  }

  const body = document.createElement('div');
  const title = document.createElement('h3');
  title.textContent = entry.name;
  const meta = document.createElement('div');
  meta.className = 'meta';
  const previewCount = (entry.previews && entry.previews.length) || 0;
  const previewLabel = previewCount ? ` · ${previewCount} previews` : '';
  meta.textContent = `${formatBytes(entry.size)} | ${entry.rel_path}${previewLabel}`;

  body.appendChild(title);
  body.appendChild(meta);

  card.appendChild(thumbNode);
  card.appendChild(body);

  card.addEventListener('click', () => loadMesh(entry));

  if (canvas) {
    renderThumbnail(entry, canvas);
  }
  return card;
}

function renderGrid(entries) {
  els.grid.innerHTML = '';
  if (entries.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No mesh.obj found. Check the dataset folder.';
    els.grid.appendChild(empty);
    return;
  }
  entries.forEach((entry) => els.grid.appendChild(createCard(entry)));
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

    // Try WebAudio first
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

    // Manual PCM16 little-endian parse
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
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = '#38bdf8';
  ctx.lineWidth = 1;
  try {
    const rir = await loadRIRData(url);
    const data = rir.samples;
    const amp = height / 2;

    // Full-scan peak to normalize reliably.
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

function renderRirs(entry) {
  els.rirGrid.innerHTML = '';
  const rirs = entry.rirs || [];
  if (!rirs.length) {
    const empty = document.createElement('p');
    empty.className = 'muted tiny';
    empty.textContent = 'No RIR files found.';
    els.rirGrid.appendChild(empty);
    return;
  }

  rirs.forEach((rir) => {
    const card = document.createElement('div');
    card.className = 'rir-card';

    const canvas = document.createElement('canvas');
    canvas.width = 320;
  canvas.height = 70;
    drawWaveform(`/rir/${rir.id}`, canvas);

    const label = document.createElement('div');
    label.className = 'label';
    label.textContent = `${rir.channel} (${Math.round(rir.size / 1024)} KB)`;

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

async function init() {
  setStatus('Loading index...');
  try {
    const data = await fetchList();
    state.entries = data.entries || [];
    els.datasetPath.textContent = data.dataset_root || 'Dataset not set';
    els.meshCount.textContent = data.mesh_count ?? state.entries.length;
    renderGrid(state.entries);
    setStatus('');
    if (state.entries.length) {
      loadMesh(state.entries[0]);
    }
  } catch (err) {
    console.error(err);
    setStatus('Cannot fetch list. Check server logs.');
  }
}

els.rescanBtn.addEventListener('click', async () => {
  setStatus('Rescanning...');
  try {
    const data = await fetchList('/api/rescan');
    state.entries = data.entries || [];
    els.meshCount.textContent = data.mesh_count ?? state.entries.length;
    renderGrid(state.entries);
    setStatus(state.entries.length ? '' : 'No mesh.obj detected.');
  } catch (err) {
    console.error(err);
    setStatus('Rescan failed.');
  }
});

// Surface unexpected errors directly in the UI to help debugging without devtools.
window.addEventListener('error', (e) => {
  setStatus(`JS error: ${e.message}`);
});
window.addEventListener('unhandledrejection', (e) => {
  setStatus(`Promise error: ${e.reason}`);
});

init();
