// GLSL for the JARVIS core. Every material renders additively with premultiplied output
// (rgb already multiplied by alpha), so the orb glows correctly on a transparent canvas.

const DEPTH = /* glsl */ `
  // 0 at the front of the sphere, 1 at the back (camera at z = 5.6).
  float depthFade(vec4 mv) { return clamp((-mv.z - 4.6) / 2.0, 0.0, 1.0); }
`;

export const pointsVertex = /* glsl */ `
  uniform float uTime;
  uniform float uSize;
  uniform float uPixelRatio;
  uniform float uNoise;
  uniform float uLevel;
  attribute float aSeed;
  attribute float aScale;
  varying float vAlpha;
  varying float vHot;
  ${DEPTH}
  void main() {
    vec3 p = position;
    float w = sin(p.y * 6.0 + uTime * 2.1 + aSeed * 6.2831) * sin(p.x * 5.0 - uTime * 1.6)
            + 0.5 * sin(p.z * 7.0 + uTime * 1.3 + aSeed * 3.0);
    p *= 1.0 + w * (uNoise + uLevel * 0.06);
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    float tw = 0.62 + 0.38 * sin(uTime * (0.7 + aSeed * 2.3) + aSeed * 40.0);
    float d = depthFade(mv);
    vAlpha = tw * mix(1.0, 0.16, d);
    vHot = step(0.965, fract(aSeed * 13.7));
    gl_PointSize = uSize * aScale * uPixelRatio * (5.6 / -mv.z) * (0.78 + 0.44 * tw);
  }
`;

export const pointsFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uGain;
  varying float vAlpha;
  varying float vHot;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c) * 2.0;
    float a = smoothstep(1.0, 0.0, d);
    a *= a;
    float alpha = a * vAlpha * uIntensity * uOpacity * uGain;
    vec3 col = mix(uColor, uHot, clamp(vHot * 0.85 + pow(a, 5.0) * 0.55, 0.0, 1.0));
    gl_FragColor = vec4(col * alpha, alpha);
  }
`;

export const linesVertex = /* glsl */ `
  uniform float uTime;
  attribute float aT;
  attribute float aSeed;
  varying float vAlpha;
  varying float vT;
  varying float vSeed;
  ${DEPTH}
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    vAlpha = mix(1.0, 0.1, depthFade(mv));
    vT = aT;
    vSeed = aSeed;
  }
`;

export const linesFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform float uTime;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uAlpha;
  uniform float uFlow;
  varying float vAlpha;
  varying float vT;
  varying float vSeed;
  void main() {
    float f = fract(vT - uTime * (0.35 + vSeed * 0.4) + vSeed * 7.0);
    float pulse = pow(max(0.0, 1.0 - abs(f - 0.5) * 5.0), 3.0) * uFlow;
    float a = vAlpha * uAlpha * uIntensity * uOpacity * (1.0 + pulse * 2.5);
    vec3 col = mix(uColor, uHot, pulse);
    gl_FragColor = vec4(col * a, a);
  }
`;

export const glassVertex = /* glsl */ `
  varying vec3 vN;
  varying vec3 vV;
  varying vec3 vPos;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vN = normalize(normalMatrix * normal);
    vV = normalize(-mv.xyz);
    vPos = position;
    gl_Position = projectionMatrix * mv;
  }
`;

export const glassFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform vec3 uDeep;
  uniform vec2 uLight;
  uniform float uIntensity;
  uniform float uOpacity;
  varying vec3 vN;
  varying vec3 vV;
  void main() {
    float facing = max(dot(vN, vV), 0.0);
    float f = 1.0 - facing;
    float rim = pow(f, 2.4);
    float inner = pow(facing, 2.0) * 0.07;
    vec3 L = normalize(vec3(uLight, 1.1));
    float spec = pow(max(dot(reflect(-L, vN), vV), 0.0), 28.0) * 0.55;
    float k = uIntensity * uOpacity;
    vec3 rgb = (uColor * rim * 1.15 + uDeep * inner + uHot * spec) * k;
    float a = clamp(rim * 0.8 + inner * 0.6 + spec, 0.0, 1.0) * k;
    gl_FragColor = vec4(rgb, a);
  }
`;

export const nebulaFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform vec3 uDeep;
  uniform float uTime;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uLevel;
  varying vec3 vN;
  varying vec3 vV;
  varying vec3 vPos;
  float hash(vec3 p) {
    p = fract(p * 0.3183099 + 0.1);
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float noise(vec3 x) {
    vec3 i = floor(x);
    vec3 f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i), hash(i + vec3(1.0, 0.0, 0.0)), f.x), mix(hash(i + vec3(0.0, 1.0, 0.0)), hash(i + vec3(1.0, 1.0, 0.0)), f.x), f.y),
      mix(mix(hash(i + vec3(0.0, 0.0, 1.0)), hash(i + vec3(1.0, 0.0, 1.0)), f.x), mix(hash(i + vec3(0.0, 1.0, 1.0)), hash(i + vec3(1.0, 1.0, 1.0)), f.x), f.y),
      f.z);
  }
  float fbm(vec3 p) {
    float v = 0.0;
    float a = 0.5;
    for (int i = 0; i < 4; i++) {
      v += a * noise(p);
      p *= 2.03;
      a *= 0.5;
    }
    return v;
  }
  void main() {
    float facing = max(dot(vN, vV), 0.0);
    vec3 p = vPos * 2.1 + vec3(0.0, uTime * 0.11, uTime * 0.06);
    float n = fbm(p + fbm(p * 0.7 + uTime * 0.05) * (1.2 + uLevel));
    float cloud = smoothstep(0.38, 0.92, n);
    float glow = pow(facing, 1.4);
    vec3 col = mix(uDeep, uColor, cloud) * (0.3 + 0.7 * glow) + uHot * pow(cloud, 3.0) * glow * 0.45;
    float a = (0.1 + 0.32 * cloud) * glow * uIntensity * uOpacity;
    gl_FragColor = vec4(col * a * 1.5, a);
  }
`;

export const planeVertex = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

export const coreFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uBoost;
  varying vec2 vUv;
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    float core = exp(-d * d * 22.0);
    float glow = exp(-d * d * 4.5) * 0.5;
    float a = (core * (1.0 + uBoost) + glow * (0.7 + uBoost)) * smoothstep(1.0, 0.75, d) * uIntensity * uOpacity;
    vec3 col = mix(uColor, uHot, clamp(core * 1.4, 0.0, 1.0));
    gl_FragColor = vec4(col * a, a);
  }
`;

export const haloFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uDeep;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uRim;
  varying vec2 vUv;
  void main() {
    float d = length(vUv - 0.5) * 2.0;
    float bloom = pow(1.0 - smoothstep(0.25, 1.0, d), 2.2) * 0.2;
    float rim = exp(-pow((d - uRim) / 0.07, 2.0)) * 0.22;
    float a = (bloom + rim) * smoothstep(1.0, 0.9, d) * uIntensity * uOpacity;
    vec3 col = mix(uDeep, uColor, 0.35 + rim * 2.0);
    gl_FragColor = vec4(col * a, a);
  }
`;

export const ringVertex = /* glsl */ `
  varying float vAng;
  varying float vDepth;
  ${DEPTH}
  void main() {
    vAng = atan(position.y, position.x);
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vDepth = depthFade(mv);
    gl_Position = projectionMatrix * mv;
  }
`;

export const ringFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform float uHead;
  uniform float uIntensity;
  uniform float uOpacity;
  uniform float uAlpha;
  varying float vAng;
  varying float vDepth;
  void main() {
    float t = fract(vAng / 6.2831853 + 0.5 - uHead);
    float trail = pow(t, 7.0);
    float a = (0.2 + trail * 1.5) * mix(1.0, 0.3, vDepth) * uAlpha * uIntensity * uOpacity;
    vec3 col = mix(uColor, uHot, trail * 0.75);
    gl_FragColor = vec4(col * a, a);
  }
`;

export const scanFragment = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uHot;
  uniform float uAlpha;
  uniform float uIntensity;
  uniform float uOpacity;
  void main() {
    float a = uAlpha * uIntensity * uOpacity;
    gl_FragColor = vec4(mix(uColor, uHot, 0.4) * a, a);
  }
`;

export const scanVertex = /* glsl */ `
  void main() {
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;
