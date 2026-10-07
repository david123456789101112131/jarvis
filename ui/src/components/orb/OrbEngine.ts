import {
  AdditiveBlending,
  BufferAttribute,
  BufferGeometry,
  Group,
  IcosahedronGeometry,
  LineLoop,
  LineSegments,
  Mesh,
  PerspectiveCamera,
  PlaneGeometry,
  Points,
  Scene,
  ShaderMaterial,
  SphereGeometry,
  TorusGeometry,
  Vector2,
  Vector3,
  WebGLRenderer,
  WireframeGeometry,
  type IUniform,
  type Material,
  type Object3D,
} from 'three';
import type { Theme } from '../../api/types';
import type { OrbVisualState } from '../../state/selectors';
import { ERROR_PALETTE, ORB_PALETTES, THINK_PALETTE, desaturate, hexToRgb, mixRgb, type RGB } from './palette';
import {
  coreFragment,
  glassFragment,
  glassVertex,
  haloFragment,
  linesFragment,
  linesVertex,
  nebulaFragment,
  planeVertex,
  pointsFragment,
  pointsVertex,
  ringFragment,
  ringVertex,
  scanFragment,
  scanVertex,
} from './shaders';

interface Params {
  spin: number;
  scale: number;
  pulseAmp: number;
  pulseHz: number;
  ringSpeed: number;
  intensity: number;
  noise: number;
  levelGain: number;
  think: number;
  red: number;
  dim: number;
  scan: number;
  boost: number;
  flow: number;
}

const PRESETS: Record<OrbVisualState, Params> = {
  idle: { spin: 0.1, scale: 0.9, pulseAmp: 0.016, pulseHz: 0.2, ringSpeed: 0.55, intensity: 0.9, noise: 0.01, levelGain: 0, think: 0, red: 0, dim: 0, scan: 0, boost: 0, flow: 0.5 },
  busy: { spin: 0.2, scale: 0.93, pulseAmp: 0.018, pulseHz: 0.45, ringSpeed: 1.15, intensity: 0.98, noise: 0.016, levelGain: 0, think: 0, red: 0, dim: 0, scan: 0.35, boost: 0.1, flow: 1 },
  wake: { spin: 0.3, scale: 1.04, pulseAmp: 0.03, pulseHz: 1.1, ringSpeed: 1.7, intensity: 1.2, noise: 0.025, levelGain: 0.04, think: 0, red: 0, dim: 0, scan: 0, boost: 0.45, flow: 1.2 },
  listening: { spin: 0.18, scale: 1.0, pulseAmp: 0.008, pulseHz: 0.6, ringSpeed: 1.1, intensity: 1.05, noise: 0.018, levelGain: 0.11, think: 0, red: 0, dim: 0, scan: 0, boost: 0.25, flow: 1 },
  processing: { spin: 0.7, scale: 0.97, pulseAmp: 0.02, pulseHz: 1.6, ringSpeed: 3.2, intensity: 1.12, noise: 0.03, levelGain: 0, think: 0.5, red: 0, dim: 0, scan: 1, boost: 0.3, flow: 2.2 },
  speaking: { spin: 0.24, scale: 1.0, pulseAmp: 0, pulseHz: 0.8, ringSpeed: 1.3, intensity: 1.08, noise: 0.014, levelGain: 0.09, think: 0, red: 0, dim: 0, scan: 0, boost: 0.35, flow: 1.2 },
  error: { spin: 0.12, scale: 0.95, pulseAmp: 0.03, pulseHz: 1.3, ringSpeed: 0.7, intensity: 1.08, noise: 0.035, levelGain: 0, think: 0, red: 1, dim: 0, scan: 0, boost: 0.2, flow: 0.4 },
  disabled: { spin: 0.04, scale: 0.86, pulseAmp: 0.008, pulseHz: 0.12, ringSpeed: 0.2, intensity: 0.45, noise: 0, levelGain: 0, think: 0, red: 0, dim: 0.7, scan: 0, boost: 0, flow: 0.1 },
};

export interface OrbEngineOptions {
  theme: Theme;
  quality?: 'high' | 'low';
  reducedMotion?: boolean;
  onContextLost?: () => void;
}

type Uniforms = Record<string, IUniform>;

interface Ring {
  mesh: Mesh;
  uniforms: Uniforms;
  dots: Points;
  dotGeom: BufferGeometry;
  radius: number;
  speed: number;
  head: number;
  dotPhase: number[];
}

function fibonacciSphere(count: number, radius: number, jitter = 0): Float32Array {
  const out = new Float32Array(count * 3);
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i += 1) {
    const y = 1 - (i / (count - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i;
    const v = new Vector3(Math.cos(theta) * r, y, Math.sin(theta) * r);
    if (jitter) v.add(new Vector3((Math.random() - 0.5) * jitter, (Math.random() - 0.5) * jitter, (Math.random() - 0.5) * jitter)).normalize();
    v.multiplyScalar(radius);
    out.set([v.x, v.y, v.z], i * 3);
  }
  return out;
}

function randomAttr(count: number, min: number, max: number): Float32Array {
  const out = new Float32Array(count);
  for (let i = 0; i < count; i += 1) out[i] = min + Math.random() * (max - min);
  return out;
}

/**
 * Three.js renderer for the JARVIS core: particle shell, plexus network, geodesic wireframe,
 * glass rim, nebula interior, core glow, halo bloom, orbit rings and a processing scanner.
 */
export class OrbEngine {
  private renderer: WebGLRenderer;
  private scene = new Scene();
  private camera = new PerspectiveCamera(30, 1, 0.1, 50);
  private root = new Group();
  private spinner = new Group();
  private inner: Points;
  private geodesic: LineSegments;
  private core: Mesh;
  private rings: Ring[] = [];
  private scan: LineLoop;
  private shared: Uniforms;
  private materials: ShaderMaterial[] = [];
  private scanUniforms: Uniforms;
  private canvas: HTMLCanvasElement;

  private state: OrbVisualState = 'idle';
  private target: Params = { ...PRESETS.idle };
  private cur: Params = { ...PRESETS.idle };
  private theme: Theme;
  private allowThink = true;
  private levelSource: (() => number) | null = null;
  private level = 0;
  private burst = 0;
  private stateTime = 0;
  private pointer = new Vector2();
  private pointerTarget = new Vector2();
  private followMouse = false;
  private reducedMotion: boolean;
  private time = Math.random() * 100;
  private last = 0;
  private raf = 0;
  private running = false;
  private disposed = false;
  private cssHeight = 300;
  private onContextLost?: () => void;

  constructor(canvas: HTMLCanvasElement, options: OrbEngineOptions) {
    this.canvas = canvas;
    this.theme = options.theme;
    this.reducedMotion = !!options.reducedMotion;
    this.onContextLost = options.onContextLost;
    this.allowThink = options.theme === 'blue';
    this.renderer = new WebGLRenderer({ canvas, alpha: true, antialias: true, premultipliedAlpha: true, powerPreference: 'high-performance' });
    this.renderer.setClearColor(0x000000, 0);
    this.camera.position.set(0, 0, 5.6);
    this.camera.lookAt(0, 0, 0);

    const high = options.quality !== 'low';
    this.shared = {
      uTime: { value: 0 },
      uColor: { value: new Vector3() },
      uHot: { value: new Vector3() },
      uDeep: { value: new Vector3() },
      uIntensity: { value: 1 },
      uOpacity: { value: 1 },
      uPixelRatio: { value: 1 },
      uNoise: { value: 0.01 },
      uLevel: { value: 0 },
      uLight: { value: new Vector2(-0.45, 0.55) },
    };

    this.scene.add(this.root);
    this.root.add(this.spinner);

    // Halo bloom behind everything.
    const halo = new Mesh(new PlaneGeometry(3.0, 3.0), this.material(planeVertex, haloFragment, { uRim: { value: 0.7 } }));
    halo.position.z = -0.2;
    halo.renderOrder = 0;
    this.root.add(halo);

    // Nebula interior and glass rim.
    const sphereGeom = new SphereGeometry(1, high ? 64 : 40, high ? 48 : 28);
    const nebula = new Mesh(sphereGeom, this.material(glassVertex, nebulaFragment, {}));
    nebula.scale.setScalar(0.97);
    nebula.renderOrder = 1;
    this.spinner.add(nebula);
    const glass = new Mesh(sphereGeom, this.material(glassVertex, glassFragment, {}));
    glass.renderOrder = 2;
    this.root.add(glass);

    // Geodesic wireframe.
    const ico = new IcosahedronGeometry(1.012, 2);
    const wire = new WireframeGeometry(ico);
    ico.dispose();
    const wireCount = wire.getAttribute('position').count;
    wire.setAttribute('aT', new BufferAttribute(new Float32Array(wireCount).map((_, i) => i % 2), 1));
    wire.setAttribute('aSeed', new BufferAttribute(randomAttr(wireCount, 0, 1), 1));
    this.geodesic = new LineSegments(wire, this.material(linesVertex, linesFragment, { uAlpha: { value: 0.085 }, uFlow: { value: 0 } }));
    this.geodesic.renderOrder = 3;
    this.root.add(this.geodesic);

    // Plexus: nodes on the sphere connected to their nearest neighbours.
    const nodeCount = high ? 96 : 64;
    const nodes = fibonacciSphere(nodeCount, 1.0, 0.22);
    const segs: number[] = [];
    const ts: number[] = [];
    const seeds: number[] = [];
    const tmpA = new Vector3();
    const tmpB = new Vector3();
    for (let i = 0; i < nodeCount; i += 1) {
      tmpA.fromArray(nodes, i * 3);
      const near: [number, number][] = [];
      for (let j = 0; j < nodeCount; j += 1) {
        if (j === i) continue;
        const d = tmpA.distanceTo(tmpB.fromArray(nodes, j * 3));
        if (d < 0.56) near.push([d, j]);
      }
      near.sort((a, b) => a[0] - b[0]);
      for (const [, j] of near.slice(0, 3)) {
        if (j < i) continue;
        const seed = Math.random();
        segs.push(nodes[i * 3], nodes[i * 3 + 1], nodes[i * 3 + 2], nodes[j * 3], nodes[j * 3 + 1], nodes[j * 3 + 2]);
        ts.push(0, 1);
        seeds.push(seed, seed);
      }
    }
    const plexusGeom = new BufferGeometry();
    plexusGeom.setAttribute('position', new BufferAttribute(new Float32Array(segs), 3));
    plexusGeom.setAttribute('aT', new BufferAttribute(new Float32Array(ts), 1));
    plexusGeom.setAttribute('aSeed', new BufferAttribute(new Float32Array(seeds), 1));
    const plexus = new LineSegments(plexusGeom, this.material(linesVertex, linesFragment, { uAlpha: { value: 0.3 }, uFlow: { value: 1 } }));
    plexus.renderOrder = 4;
    this.spinner.add(plexus);

    const nodeGeom = new BufferGeometry();
    nodeGeom.setAttribute('position', new BufferAttribute(nodes, 3));
    nodeGeom.setAttribute('aSeed', new BufferAttribute(randomAttr(nodeCount, 0, 1), 1));
    nodeGeom.setAttribute('aScale', new BufferAttribute(randomAttr(nodeCount, 1.6, 2.6), 1));
    const nodePoints = new Points(nodeGeom, this.material(pointsVertex, pointsFragment, { uSize: { value: 4 }, uGain: { value: 1 } }));
    nodePoints.renderOrder = 7;
    this.spinner.add(nodePoints);

    // Particle shell.
    const shellCount = high ? 1500 : 800;
    const shellGeom = new BufferGeometry();
    shellGeom.setAttribute('position', new BufferAttribute(fibonacciSphere(shellCount, 1.0, 0.02), 3));
    shellGeom.setAttribute('aSeed', new BufferAttribute(randomAttr(shellCount, 0, 1), 1));
    shellGeom.setAttribute('aScale', new BufferAttribute(randomAttr(shellCount, 0.55, 1.3), 1));
    const shell = new Points(shellGeom, this.material(pointsVertex, pointsFragment, { uSize: { value: 3 }, uGain: { value: 1 } }));
    shell.renderOrder = 5;
    this.spinner.add(shell);

    // Inner counter-rotating particles.
    const innerCount = high ? 420 : 220;
    const innerGeom = new BufferGeometry();
    innerGeom.setAttribute('position', new BufferAttribute(fibonacciSphere(innerCount, 0.58, 0.3), 3));
    innerGeom.setAttribute('aSeed', new BufferAttribute(randomAttr(innerCount, 0, 1), 1));
    innerGeom.setAttribute('aScale', new BufferAttribute(randomAttr(innerCount, 0.5, 1.1), 1));
    this.inner = new Points(innerGeom, this.material(pointsVertex, pointsFragment, { uSize: { value: 2.4 }, uGain: { value: 0.6 } }));
    this.inner.renderOrder = 6;
    this.root.add(this.inner);

    // Core glow ("eye").
    this.core = new Mesh(new PlaneGeometry(1.5, 1.5), this.material(planeVertex, coreFragment, { uBoost: { value: 0 } }));
    this.core.position.z = 0.25;
    this.core.renderOrder = 8;
    this.root.add(this.core);

    // Orbit rings with travelling light dots.
    const ringDefs = [
      { radius: 1.3, tilt: [1.2, 0.0, 0.32], speed: 0.16, dots: 2 },
      { radius: 1.42, tilt: [1.38, 0.55, -0.2], speed: -0.11, dots: 2 },
      { radius: 1.2, tilt: [0.5, -0.65, 0.1], speed: 0.21, dots: 1 },
    ];
    for (const def of ringDefs) {
      const uniforms: Uniforms = { uHead: { value: Math.random() }, uAlpha: { value: 0.75 } };
      const mesh = new Mesh(new TorusGeometry(def.radius, 0.0042, 6, 240), this.material(ringVertex, ringFragment, uniforms));
      mesh.rotation.set(def.tilt[0], def.tilt[1], def.tilt[2]);
      mesh.renderOrder = 9;
      const dotGeom = new BufferGeometry();
      dotGeom.setAttribute('position', new BufferAttribute(new Float32Array(def.dots * 3), 3));
      dotGeom.setAttribute('aSeed', new BufferAttribute(new Float32Array(def.dots).fill(0.99), 1));
      dotGeom.setAttribute('aScale', new BufferAttribute(new Float32Array(def.dots).fill(2.6), 1));
      const dots = new Points(dotGeom, this.material(pointsVertex, pointsFragment, { uSize: { value: 4.5 }, uGain: { value: 1.2 }, uNoise: { value: 0 }, uLevel: { value: 0 } }));
      dots.renderOrder = 10;
      mesh.add(dots);
      this.root.add(mesh);
      this.rings.push({ mesh, uniforms, dots, dotGeom, radius: def.radius, speed: def.speed, head: Math.random(), dotPhase: Array.from({ length: def.dots }, () => Math.random()) });
    }

    // Processing scanner: a horizontal circle sweeping up and down the sphere.
    const scanPts = new Float32Array(160 * 3);
    for (let i = 0; i < 160; i += 1) {
      const a = (i / 160) * Math.PI * 2;
      scanPts.set([Math.cos(a), 0, Math.sin(a)], i * 3);
    }
    const scanGeom = new BufferGeometry();
    scanGeom.setAttribute('position', new BufferAttribute(scanPts, 3));
    this.scanUniforms = { uAlpha: { value: 0 } };
    this.scan = new LineLoop(scanGeom, this.material(scanVertex, scanFragment, this.scanUniforms));
    this.scan.renderOrder = 11;
    this.root.add(this.scan);

    this.applyColors();
    canvas.addEventListener('webglcontextlost', this.handleContextLost, false);
    document.addEventListener('visibilitychange', this.handleVisibility);
  }

  private material(vertexShader: string, fragmentShader: string, own: Uniforms): ShaderMaterial {
    // Shared uniform objects are referenced (not copied), so one update reaches every material.
    const material = new ShaderMaterial({
      uniforms: { ...this.shared, ...own },
      vertexShader,
      fragmentShader,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: AdditiveBlending,
      premultipliedAlpha: true,
    });
    this.materials.push(material);
    return material;
  }

  // ------------------------------------------------------------------ public API

  setState(state: OrbVisualState) {
    if (state === this.state) return;
    const prev = this.state;
    this.state = state;
    this.target = { ...PRESETS[state] };
    this.stateTime = this.time;
    if (state === 'wake' || (state === 'listening' && prev !== 'wake') || state === 'error') this.burst = 1;
  }

  setTheme(theme: Theme) {
    this.theme = theme;
    this.allowThink = theme === 'blue';
    this.applyColors();
  }

  setOpacity(opacity: number) {
    this.shared.uOpacity.value = Math.max(0.1, Math.min(1, opacity));
  }

  setLevelSource(source: (() => number) | null) {
    this.levelSource = source;
  }

  setFollowMouse(follow: boolean) {
    this.followMouse = follow;
    if (!follow) this.pointerTarget.set(0, 0);
  }

  /** Pointer position relative to the orb centre, -1..1 on both axes (y up). */
  setPointer(x: number, y: number) {
    if (!this.followMouse) return;
    this.pointerTarget.set(Math.max(-1, Math.min(1, x)), Math.max(-1, Math.min(1, y)));
  }

  resize(width: number, height: number, dpr: number) {
    if (this.disposed || width < 2 || height < 2) return;
    const ratio = Math.min(2, Math.max(1, dpr || 1));
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(width, height, false);
    const aspect = width / height;
    this.camera.aspect = aspect;
    const base = 30;
    this.camera.fov = aspect < 1 ? (2 * Math.atan(Math.tan((base * Math.PI) / 360) / aspect) * 180) / Math.PI : base;
    this.camera.updateProjectionMatrix();
    this.cssHeight = Math.min(width, height);
    this.shared.uPixelRatio.value = ratio;
    if (!this.running) this.renderOnce();
  }

  start() {
    if (this.running || this.disposed) return;
    this.running = true;
    this.last = performance.now();
    this.raf = requestAnimationFrame(this.frame);
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  dispose() {
    if (this.disposed) return;
    this.stop();
    this.disposed = true;
    this.canvas.removeEventListener('webglcontextlost', this.handleContextLost, false);
    document.removeEventListener('visibilitychange', this.handleVisibility);
    this.scene.traverse((obj: Object3D) => {
      const geometry = (obj as Mesh).geometry as BufferGeometry | undefined;
      geometry?.dispose();
    });
    for (const material of this.materials) (material as Material).dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }

  // ------------------------------------------------------------------ internals

  private handleContextLost = (event: Event) => {
    event.preventDefault();
    this.stop();
    this.onContextLost?.();
  };

  private handleVisibility = () => {
    if (document.hidden) this.stop();
    else this.start();
  };

  private applyColors() {
    const palette = ORB_PALETTES[this.theme];
    const cur = this.cur;
    const think = this.allowThink ? cur.think : 0;
    const pick = (key: 'base' | 'hot' | 'deep'): RGB => {
      let c = hexToRgb(palette[key]);
      if (think > 0.001) c = mixRgb(c, hexToRgb(THINK_PALETTE[key]), think);
      if (cur.red > 0.001) c = mixRgb(c, hexToRgb(ERROR_PALETTE[key]), cur.red);
      if (cur.dim > 0.001) c = desaturate(c, cur.dim * 0.8, cur.dim * 0.35);
      return c;
    };
    (this.shared.uColor.value as Vector3).fromArray(pick('base'));
    (this.shared.uHot.value as Vector3).fromArray(pick('hot'));
    (this.shared.uDeep.value as Vector3).fromArray(pick('deep'));
  }

  private renderOnce() {
    if (this.disposed) return;
    this.step(0);
    this.renderer.render(this.scene, this.camera);
  }

  private frame = (now: number) => {
    if (!this.running) return;
    this.raf = requestAnimationFrame(this.frame);
    const dt = Math.min(0.05, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    this.step(dt);
    this.renderer.render(this.scene, this.camera);
  };

  private step(dt: number) {
    const motion = this.reducedMotion ? 0.35 : 1;
    this.time += dt * motion;
    const k = 1 - Math.exp(-dt * 3);
    const cur = this.cur;
    const target = this.target;
    for (const key of Object.keys(cur) as (keyof Params)[]) cur[key] += (target[key] - cur[key]) * k;

    const raw = this.levelSource ? Math.max(0, Math.min(1, this.levelSource())) : 0;
    const lk = raw > this.level ? 1 - Math.exp(-dt * 22) : 1 - Math.exp(-dt * 7);
    this.level += (raw - this.level) * lk;
    this.burst *= Math.exp(-dt * 2.4);

    const t = this.time;
    const age = t - this.stateTime;
    const errorFresh = this.state === 'error' && age < 1.1;

    this.spinner.rotation.y += cur.spin * dt * motion;
    this.spinner.rotation.x = Math.sin(t * 0.13) * 0.16;
    this.inner.rotation.y -= cur.spin * 1.5 * dt * motion;
    this.inner.rotation.z += cur.spin * 0.4 * dt * motion;
    this.geodesic.rotation.y -= cur.spin * 0.45 * dt * motion;
    this.geodesic.rotation.x += cur.spin * 0.12 * dt * motion;

    const pulse = Math.sin(t * Math.PI * 2 * cur.pulseHz) * cur.pulseAmp;
    const scale = cur.scale * (1 + pulse + this.level * cur.levelGain * 1.4 + this.burst * 0.07);
    this.root.scale.setScalar(scale);

    const shake = errorFresh && !this.reducedMotion ? (1 - age / 1.1) * 0.045 : 0;
    this.root.position.x = shake ? Math.sin(t * 70) * shake : 0;
    this.root.position.y = shake ? Math.cos(t * 53) * shake * 0.5 : 0;
    const flicker = errorFresh ? 0.7 + Math.random() * 0.55 : 1;

    // Follow the pointer (or drift gently).
    const pk = 1 - Math.exp(-dt * 4);
    this.pointer.x += (this.pointerTarget.x - this.pointer.x) * pk;
    this.pointer.y += (this.pointerTarget.y - this.pointer.y) * pk;
    const driftX = this.followMouse ? 0 : Math.sin(t * 0.21) * 0.08;
    const driftY = this.followMouse ? 0 : Math.cos(t * 0.17) * 0.06;
    const px = this.pointer.x + driftX;
    const py = this.pointer.y + driftY;
    this.root.rotation.y = px * 0.32;
    this.root.rotation.x = -py * 0.26;
    this.core.position.x = px * 0.16;
    this.core.position.y = py * 0.16;
    (this.shared.uLight.value as Vector2).set(-0.45 + px * 0.9, 0.55 + py * 0.7);

    this.shared.uTime.value = t;
    this.shared.uIntensity.value = cur.intensity * flicker * (1 + this.level * 0.55 + this.burst * 0.35);
    this.shared.uNoise.value = cur.noise;
    this.shared.uLevel.value = this.level * (cur.levelGain > 0 ? 1 : 0.2);

    const sizeScale = Math.max(0.45, Math.min(1.6, this.cssHeight / 380));
    for (const material of this.materials) {
      const size = material.uniforms.uSize as IUniform | undefined;
      if (size && material.userData.baseSize === undefined) material.userData.baseSize = size.value;
      if (size) size.value = (material.userData.baseSize as number) * sizeScale;
      const flow = material.uniforms.uFlow as IUniform | undefined;
      if (flow && material.userData.baseFlow === undefined) material.userData.baseFlow = flow.value;
      if (flow && (material.userData.baseFlow as number) > 0) flow.value = cur.flow;
    }
    (this.core.material as ShaderMaterial).uniforms.uBoost.value = cur.boost + this.burst * 0.6 + this.level * 0.5;

    for (const ring of this.rings) {
      ring.head = (ring.head + ring.speed * cur.ringSpeed * dt * motion + 1) % 1;
      ring.uniforms.uHead.value = ring.head;
      const pos = ring.dotGeom.getAttribute('position') as BufferAttribute;
      ring.dotPhase.forEach((phase, i) => {
        const a = (ring.head + phase) * Math.PI * 2 * (i % 2 ? -1 : 1) + t * 0.05;
        pos.setXYZ(i, Math.cos(a) * ring.radius, Math.sin(a) * ring.radius, 0);
      });
      pos.needsUpdate = true;
    }

    const y = Math.sin(t * 1.5) * 0.86;
    const r = Math.sqrt(Math.max(0, 1 - y * y)) * 1.01;
    this.scan.position.y = y;
    this.scan.scale.set(r, 1, r);
    this.scanUniforms.uAlpha.value = cur.scan * 0.55;

    this.applyColors();
  }
}

/** True when this browser can create a WebGL context. */
export function webglAvailable(): boolean {
  try {
    const canvas = document.createElement('canvas');
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch {
    return false;
  }
}
