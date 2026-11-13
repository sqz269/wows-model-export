// Map overlays (`map_overlay/v1`, reference/schemas/MAP_OVERLAY.md): optional
// annotation layers drawn over a map on the Maps page. A producer writes them
// into `<map folder>/overlays/<name>.overlay.json`:
//
//   points     markers of a symbol and colour (filled or hollow), with an
//              optional heading arrow and label
//   polylines  open outlines
//   polygons   closed outlines
//
// Coordinates are the map's scene frame (the map GLB's, or the frame
// map_scene.json places its models in): right-handed, +Y up, +X east, -Z
// north, in the export's units. Positions are horizontal [x, z]; each is
// placed on the surface (the height field or the sea, whichever is higher).
// Headings are degrees from north (-Z) toward east (+X).
//
// Drawing. Everything is drawn after the map and without the depth test, so
// a marker hidden behind an island at an orbit angle still reads.
//   - Points: two THREE.Points per layer over one geometry, drawn in the
//     fragment shader at a constant screen size, each with a dark outline.
//     Every heading arrow is drawn first and every marker after it, so in a
//     tight formation one ship's arrow never covers the marker ahead. The
//     arrow points along the heading as projected on the screen, so it
//     reads from straight above and at any orbit angle.
//   - Lines: LineSegments2 (screen-space width), subdivided and laid on the
//     surface.
//   - Labels: HTML elements over the canvas (CSS2DRenderer).
//
// No Svelte imports: the Maps route owns the lifecycle.

import * as THREE from 'three';
import { CSS2DObject, CSS2DRenderer } from 'three/addons/renderers/CSS2DRenderer.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';

import type { HeightSampler } from './workspace_map';

export const MAP_OVERLAY_SCHEMA = 'map_overlay/v1';

/** Marker symbols, in shader index order. */
export const OVERLAY_SYMBOLS = [
  'circle',
  'diamond',
  'triangle',
  'square',
  'cross',
  'star',
  'dot',
] as const;
export type OverlaySymbol = (typeof OVERLAY_SYMBOLS)[number];

export interface MapOverlayPoint {
  /** [x, z] in the map's scene frame. */
  pos: [number, number];
  /** Degrees from north (-Z) toward east (+X). No arrow when absent. */
  heading_deg?: number;
  label?: string;
  /** Extra text shown on hover. */
  info?: string;
  color?: string;
  symbol?: OverlaySymbol;
  /** Marker size in CSS pixels. */
  size?: number;
  hollow?: boolean;
}

export interface MapOverlayShape {
  /** [x, z] vertices in the map's scene frame. */
  points: [number, number][];
  label?: string;
  info?: string;
  color?: string;
  /** Line width in CSS pixels. */
  width?: number;
  dashed?: boolean;
}

export interface MapOverlayLayer {
  id: string;
  title?: string;
  /** Default visibility (true when absent). */
  visible?: boolean;
  /** Layer defaults for its items. */
  color?: string;
  symbol?: OverlaySymbol;
  size?: number;
  hollow?: boolean;
  width?: number;
  dashed?: boolean;
  points?: MapOverlayPoint[];
  polylines?: MapOverlayShape[];
  polygons?: MapOverlayShape[];
}

export interface MapOverlayDoc {
  schema: string;
  title?: string;
  description?: string;
  layers: MapOverlayLayer[];
}

/** What the pointer is over. */
export interface MapOverlayHit {
  overlayTitle: string;
  layerTitle: string;
  label: string | null;
  info: string | null;
  /** Marker centre, CSS px relative to the canvas. */
  x: number;
  y: number;
}

export interface MapOverlayOptions {
  scene: THREE.Scene;
  /** Map scene frame -> world (the map root's world matrix). */
  frame: THREE.Matrix4;
  /** Element the label layer goes into; it must cover the canvas. */
  labelHost: HTMLElement;
  /** Ground height in the map frame, or null where there is none. */
  heightAt: HeightSampler | null;
  /** Sea level in the map frame. */
  seaLevel: number;
  /** Longest segment of a draped outline, map units. */
  drapeStep: number;
  /** Height above the surface, map units. */
  lift?: number;
}

const DEFAULT_COLOR = '#ffffff';
const DEFAULT_SIZE = 12;
const DEFAULT_WIDTH = 2;
const LINE_RENDER_ORDER = 1000;
/** Heading arrows of every layer, under every marker. */
const ARROW_RENDER_ORDER = 1001;
const MARKER_RENDER_ORDER = 1002;
/** Marker quad side / marker size: room for the heading arrow. */
const QUAD_SCALE = 3.4;
/** Most vertices one draped segment is split into. */
const MAX_DRAPE_STEPS = 512;

const POINT_VERTEX = /* glsl */ `
uniform vec2 uViewport;
uniform float uPixelRatio;
attribute vec3 aColor;
attribute vec4 aStyle; // symbol index, size (CSS px), hollow, has heading
attribute vec3 aDir;   // heading direction, map frame
varying vec3 vColor;
varying float vSymbol;
varying float vHollow;
varying float vArrow;
varying float vAngle;
varying float vQuad;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  gl_Position = projectionMatrix * mv;
  vQuad = aStyle.y * ${QUAD_SCALE.toFixed(2)} * uPixelRatio;
  gl_PointSize = vQuad;
  vColor = aColor;
  vSymbol = aStyle.x;
  vHollow = aStyle.z;
  vArrow = 0.0;
  vAngle = 0.0;
#ifdef ARROW_PASS
  if (aStyle.w > 0.5 && gl_Position.w > 0.0) {
    vec3 d = (modelViewMatrix * vec4(aDir, 0.0)).xyz;
    float len = length(d);
    if (len > 0.0) {
      // The heading as it appears on screen: project a short step along it.
      vec4 tip = projectionMatrix * vec4(mv.xyz + d * (0.01 * max(-mv.z, 1e-3) / len), 1.0);
      if (tip.w > 0.0) {
        vec2 s = (tip.xy / tip.w - gl_Position.xy / gl_Position.w) * uViewport;
        if (dot(s, s) > 0.0025) {
          vAngle = atan(s.y, s.x);
          vArrow = 1.0;
        }
      }
    }
  }
  // No arrow: the fragment stage discards the point.
  if (vArrow < 0.5) gl_PointSize = 1.0;
#endif
}
`;

const POINT_FRAGMENT = /* glsl */ `
uniform float uPixelRatio;
varying vec3 vColor;
varying float vSymbol;
varying float vHollow;
varying float vArrow;
varying float vAngle;
varying float vQuad;

float sdBox(vec2 p, vec2 b) {
  vec2 d = abs(p) - b;
  return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0);
}
float sdSegment(vec2 p, vec2 a, vec2 b) {
  vec2 pa = p - a;
  vec2 ba = b - a;
  float h = clamp(dot(pa, ba) / dot(ba, ba), 0.0, 1.0);
  return length(pa - ba * h);
}
float sdTriangle(vec2 p, vec2 p0, vec2 p1, vec2 p2) {
  vec2 e0 = p1 - p0;
  vec2 e1 = p2 - p1;
  vec2 e2 = p0 - p2;
  vec2 v0 = p - p0;
  vec2 v1 = p - p1;
  vec2 v2 = p - p2;
  vec2 pq0 = v0 - e0 * clamp(dot(v0, e0) / dot(e0, e0), 0.0, 1.0);
  vec2 pq1 = v1 - e1 * clamp(dot(v1, e1) / dot(e1, e1), 0.0, 1.0);
  vec2 pq2 = v2 - e2 * clamp(dot(v2, e2) / dot(e2, e2), 0.0, 1.0);
  float s = sign(e0.x * e2.y - e0.y * e2.x);
  vec2 d = min(min(vec2(dot(pq0, pq0), s * (v0.x * e0.y - v0.y * e0.x)),
                   vec2(dot(pq1, pq1), s * (v1.x * e1.y - v1.y * e1.x))),
                   vec2(dot(pq2, pq2), s * (v2.x * e2.y - v2.y * e2.x)));
  return -sqrt(d.x) * sign(d.y);
}
float sdEquilateral(vec2 p, float r) {
  const float k = 1.7320508;
  p.x = abs(p.x) - r;
  p.y = p.y + r / k;
  if (p.x + k * p.y > 0.0) p = vec2(p.x - k * p.y, -k * p.x - p.y) / 2.0;
  p.x -= clamp(p.x, -2.0 * r, 0.0);
  return -length(p) * sign(p.y);
}
float sdStar5(vec2 p, float r, float rf) {
  const vec2 k1 = vec2(0.809016994375, -0.587785252292);
  const vec2 k2 = vec2(-0.809016994375, -0.587785252292);
  p.x = abs(p.x);
  p -= 2.0 * max(dot(k1, p), 0.0) * k1;
  p -= 2.0 * max(dot(k2, p), 0.0) * k2;
  p.x = abs(p.x);
  p.y -= r;
  vec2 ba = rf * vec2(-k1.y, k1.x) - vec2(0.0, 1.0);
  float h = clamp(dot(p, ba) / dot(ba, ba), 0.0, r);
  return length(p - ba * h) * sign(p.y * ba.x - p.x * ba.y);
}
float symbolSdf(float s, vec2 p, float r) {
  if (s < 0.5) return length(p) - r;                                      // circle
  if (s < 1.5) return (abs(p.x) + abs(p.y) - r * 1.25) * 0.70710678;      // diamond
  if (s < 2.5) return sdEquilateral(p, r * 1.05);                         // triangle
  if (s < 3.5) return sdBox(p, vec2(r * 0.85));                           // square
  if (s < 4.5) return min(sdBox(p, vec2(r, r * 0.3)), sdBox(p, vec2(r * 0.3, r))); // cross
  if (s < 5.5) return sdStar5(p, r * 1.3, 0.45);                          // star
  return length(p) - r * 0.6;                                             // dot
}

void main() {
  // Marker frame: unit half-quad, y up on screen.
  vec2 p = vec2(gl_PointCoord.x * 2.0 - 1.0, 1.0 - gl_PointCoord.y * 2.0);
  float px = 2.0 / vQuad;        // one drawing-buffer pixel
  float cpx = px * uPixelRatio;  // one CSS pixel
  float r = 1.0 / ${QUAD_SCALE.toFixed(2)};
#ifdef ARROW_PASS
  if (vArrow < 0.5) discard;
  float c = cos(vAngle);
  float s = sin(vAngle);
  vec2 q = vec2(c * p.x + s * p.y, -s * p.x + c * p.y);
  float tip = 1.0 - 2.0 * cpx;
  float head = 6.5 * cpx;
  float shaft = sdSegment(q, vec2(r * 1.1, 0.0), vec2(tip - head * 0.7, 0.0)) - 1.0 * cpx;
  float arrow = sdTriangle(q, vec2(tip, 0.0), vec2(tip - head, 4.0 * cpx), vec2(tip - head, -4.0 * cpx));
  float d = min(shaft, arrow);
#else
  float d = symbolSdf(vSymbol, p, r);
  if (vHollow > 0.5) d = max(d, -(d + 2.4 * cpx));
#endif
  float fill = 1.0 - smoothstep(-0.5 * px, 0.5 * px, d);
  float edge = 1.0 - smoothstep(1.3 * cpx - 0.5 * px, 1.3 * cpx + 0.5 * px, d);
  float outline = 0.85 * edge * (1.0 - fill);
  float alpha = fill + outline;
  if (alpha < 0.01) discard;
  gl_FragColor = vec4(vColor * (fill / alpha), alpha);
  #include <colorspace_fragment>
}
`;

interface PointRecord {
  world: THREE.Vector3;
  size: number;
  label: string | null;
  info: string | null;
}

interface LayerEntry {
  id: string;
  title: string;
  group: THREE.Group;
  labels: THREE.Group;
  points: PointRecord[];
}

interface OverlayEntry {
  title: string;
  group: THREE.Group;
  layers: Map<string, LayerEntry>;
}

interface Segments {
  positions: number[];
  colors: number[];
}

function finite(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function xz(v: unknown): [number, number] | null {
  return Array.isArray(v) && v.length >= 2 && finite(v[0]) && finite(v[1]) ? [v[0], v[1]] : null;
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v.length > 0 ? v : null;
}

function symbolIndex(v: unknown): number {
  const i = OVERLAY_SYMBOLS.indexOf(v as OverlaySymbol);
  return i < 0 ? 0 : i;
}

const HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

/** A colour string as CSS text (for labels) and as a working-space colour. */
function colour(v: unknown, fallback: string): { css: string; linear: THREE.Color } {
  const css = typeof v === 'string' && HEX_COLOR.test(v) ? v : fallback;
  return { css, linear: new THREE.Color(css) };
}

/** Parse a fetched document; throws on anything that is not map_overlay/v1. */
export function parseMapOverlay(doc: unknown): MapOverlayDoc {
  if (!doc || typeof doc !== 'object') throw new Error('overlay is not a JSON object');
  const d = doc as Partial<MapOverlayDoc>;
  if (d.schema !== MAP_OVERLAY_SCHEMA) {
    throw new Error(`overlay schema ${JSON.stringify(d.schema)}, expected ${MAP_OVERLAY_SCHEMA}`);
  }
  if (!Array.isArray(d.layers)) throw new Error('overlay has no layers[]');
  return d as MapOverlayDoc;
}

export class MapOverlayRenderer {
  readonly root = new THREE.Group();
  private readonly opts: MapOverlayOptions;
  private readonly labelRenderer = new CSS2DRenderer();
  private readonly overlays = new Map<string, OverlayEntry>();
  /** Marker pass and heading-arrow pass; they share one uniforms object. */
  private readonly markerMaterial: THREE.ShaderMaterial;
  private readonly arrowMaterial: THREE.ShaderMaterial;
  private readonly lineMaterials = new Map<string, LineMaterial>();
  private readonly geometries: THREE.BufferGeometry[] = [];
  private labelsVisible = true;
  private readonly viewport = new THREE.Vector2(1, 1);
  private readonly ndc = new THREE.Vector3();

  constructor(opts: MapOverlayOptions) {
    this.opts = opts;
    this.root.name = 'MapOverlays';
    this.root.matrixAutoUpdate = false;
    this.root.matrix.copy(opts.frame);
    this.root.matrixWorldNeedsUpdate = true;
    opts.scene.add(this.root);
    this.root.updateMatrixWorld(true);

    const el = this.labelRenderer.domElement;
    el.style.position = 'absolute';
    el.style.left = '0';
    el.style.top = '0';
    el.style.pointerEvents = 'none';
    opts.labelHost.appendChild(el);

    const uniforms = {
      uViewport: { value: new THREE.Vector2(1, 1) },
      uPixelRatio: { value: 1 },
    };
    const pointMaterial = (name: string, defines: Record<string, number>) =>
      new THREE.ShaderMaterial({
        name,
        uniforms,
        defines,
        vertexShader: POINT_VERTEX,
        fragmentShader: POINT_FRAGMENT,
        transparent: true,
        depthTest: false,
        depthWrite: false,
        toneMapped: false,
      });
    this.markerMaterial = pointMaterial('MapOverlayMarkers', {});
    this.arrowMaterial = pointMaterial('MapOverlayArrows', { ARROW_PASS: 1 });
  }

  has(key: string): boolean {
    return this.overlays.has(key);
  }

  /** Build one overlay document; it starts with each layer's own visibility. */
  add(key: string, doc: MapOverlayDoc): void {
    this.remove(key);
    const group = new THREE.Group();
    group.name = `overlay:${key}`;
    const entry: OverlayEntry = {
      title: str(doc.title) ?? key,
      group,
      layers: new Map(),
    };
    for (const layer of doc.layers) {
      if (!layer || typeof layer !== 'object' || !str(layer.id) || entry.layers.has(layer.id)) {
        continue;
      }
      const built = this.buildLayer(layer);
      built.group.visible = layer.visible !== false;
      group.add(built.group);
      entry.layers.set(layer.id, built);
    }
    this.root.add(group);
    this.root.updateMatrixWorld(true);
    this.overlays.set(key, entry);
  }

  remove(key: string): void {
    const entry = this.overlays.get(key);
    if (!entry) return;
    this.root.remove(entry.group);
    // The marker and arrow passes share a geometry: dispose each once.
    const geoms = new Set<THREE.BufferGeometry>();
    entry.group.traverse((o) => {
      if (o instanceof CSS2DObject) o.element.remove();
      if (o instanceof THREE.Points || o instanceof LineSegments2) {
        geoms.add(o.geometry as THREE.BufferGeometry);
      }
    });
    for (const g of geoms) {
      g.dispose();
      const i = this.geometries.indexOf(g);
      if (i >= 0) this.geometries.splice(i, 1);
    }
    this.overlays.delete(key);
  }

  setOverlayVisible(key: string, on: boolean): void {
    const entry = this.overlays.get(key);
    if (entry) entry.group.visible = on;
  }

  setLayerVisible(key: string, layerId: string, on: boolean): void {
    const layer = this.overlays.get(key)?.layers.get(layerId);
    if (layer) layer.group.visible = on;
  }

  setLabelsVisible(on: boolean): void {
    this.labelsVisible = on;
    for (const entry of this.overlays.values()) {
      for (const layer of entry.layers.values()) layer.labels.visible = on;
    }
  }

  /** Canvas size in CSS pixels. */
  setSize(width: number, height: number): void {
    this.labelRenderer.setSize(width, height);
  }

  /** Per frame, after the map is drawn: screen-space sizes and the labels. */
  update(renderer: THREE.WebGLRenderer, camera: THREE.Camera): void {
    renderer.getSize(this.viewport);
    const u = this.markerMaterial.uniforms;
    (u.uViewport.value as THREE.Vector2).copy(this.viewport);
    u.uPixelRatio.value = renderer.getPixelRatio();
    for (const m of this.lineMaterials.values()) m.resolution.copy(this.viewport);
    this.labelRenderer.render(this.root as unknown as THREE.Scene, camera);
  }

  /** The visible marker nearest to a canvas position (CSS px), if close enough. */
  pick(
    x: number,
    y: number,
    camera: THREE.Camera,
    width: number,
    height: number,
  ): MapOverlayHit | null {
    let best: MapOverlayHit | null = null;
    let bestD2 = Infinity;
    for (const entry of this.overlays.values()) {
      if (!entry.group.visible) continue;
      for (const layer of entry.layers.values()) {
        if (!layer.group.visible) continue;
        for (const p of layer.points) {
          this.ndc.copy(p.world).project(camera);
          if (this.ndc.z < -1 || this.ndc.z > 1) continue;
          const sx = ((this.ndc.x + 1) / 2) * width;
          const sy = ((1 - this.ndc.y) / 2) * height;
          const d2 = (sx - x) ** 2 + (sy - y) ** 2;
          const reach = Math.max(6, p.size * 0.75);
          if (d2 > reach * reach || d2 >= bestD2) continue;
          bestD2 = d2;
          best = {
            overlayTitle: entry.title,
            layerTitle: layer.title,
            label: p.label,
            info: p.info,
            x: sx,
            y: sy,
          };
        }
      }
    }
    return best;
  }

  dispose(): void {
    for (const key of [...this.overlays.keys()]) this.remove(key);
    for (const g of this.geometries) g.dispose();
    this.geometries.length = 0;
    this.markerMaterial.dispose();
    this.arrowMaterial.dispose();
    for (const m of this.lineMaterials.values()) m.dispose();
    this.lineMaterials.clear();
    this.opts.scene.remove(this.root);
    this.labelRenderer.domElement.remove();
  }

  // ── building ────────────────────────────────────────────────────────

  private surfaceY(x: number, z: number): number {
    const sea = this.opts.seaLevel;
    const h = this.opts.heightAt?.(x, z) ?? null;
    return (h != null && h > sea ? h : sea) + (this.opts.lift ?? 0.2);
  }

  private buildLayer(layer: MapOverlayLayer): LayerEntry {
    const group = new THREE.Group();
    group.name = `layer:${layer.id}`;
    const labels = new THREE.Group();
    labels.name = 'labels';
    labels.visible = this.labelsVisible;
    group.add(labels);
    const entry: LayerEntry = {
      id: layer.id,
      title: str(layer.title) ?? layer.id,
      group,
      labels,
      points: [],
    };
    const layerColor = typeof layer.color === 'string' ? layer.color : DEFAULT_COLOR;

    // Outlines, grouped by width and dash (one material each).
    const buckets = new Map<string, Segments>();
    const shapes: Array<[MapOverlayShape, boolean]> = [
      ...(Array.isArray(layer.polylines) ? layer.polylines : []).map(
        (s) => [s, false] as [MapOverlayShape, boolean],
      ),
      ...(Array.isArray(layer.polygons) ? layer.polygons : []).map(
        (s) => [s, true] as [MapOverlayShape, boolean],
      ),
    ];
    for (const [shape, closed] of shapes) {
      if (!shape || !Array.isArray(shape.points)) continue;
      const verts = shape.points.map(xz).filter((v): v is [number, number] => v !== null);
      if (verts.length < 2) continue;
      if (closed && verts.length > 2) verts.push(verts[0]);
      const width = finite(shape.width)
        ? shape.width
        : finite(layer.width)
          ? layer.width
          : DEFAULT_WIDTH;
      const dashed = (shape.dashed ?? layer.dashed) === true;
      const key = `${width}|${dashed ? 1 : 0}`;
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = { positions: [], colors: [] };
        buckets.set(key, bucket);
      }
      const c = colour(shape.color, layerColor);
      this.drape(verts, c.linear, bucket);
      const label = str(shape.label);
      if (label) {
        const anchor = closed ? centroid(verts.slice(0, -1)) : verts[0];
        this.addLabel(labels, anchor, label, c.css, 0);
      }
    }
    for (const [key, seg] of buckets) {
      if (seg.positions.length === 0) continue;
      const [w, dash] = key.split('|');
      const geom = new LineSegmentsGeometry();
      geom.setPositions(seg.positions);
      geom.setColors(seg.colors);
      this.geometries.push(geom);
      const line = new LineSegments2(geom, this.lineMaterial(Number(w), dash === '1'));
      if (dash === '1') line.computeLineDistances();
      line.renderOrder = LINE_RENDER_ORDER;
      line.frustumCulled = false;
      line.name = `lines:${key}`;
      group.add(line);
    }

    // Markers.
    const pts = (Array.isArray(layer.points) ? layer.points : []).filter(
      (p) => p && xz(p.pos) !== null,
    );
    if (pts.length > 0) {
      const n = pts.length;
      const pos = new Float32Array(n * 3);
      const col = new Float32Array(n * 3);
      const style = new Float32Array(n * 4);
      const dir = new Float32Array(n * 3);
      const local = new THREE.Vector3();
      let headings = 0;
      for (let i = 0; i < n; i++) {
        const p = pts[i];
        const [x, z] = xz(p.pos)!;
        const y = this.surfaceY(x, z);
        pos.set([x, y, z], i * 3);
        const c = colour(p.color, layerColor);
        col.set([c.linear.r, c.linear.g, c.linear.b], i * 3);
        const size = finite(p.size) ? p.size : finite(layer.size) ? layer.size : DEFAULT_SIZE;
        const hollow = (p.hollow ?? layer.hollow) === true;
        const heading = finite(p.heading_deg) ? (p.heading_deg * Math.PI) / 180 : null;
        style.set(
          [symbolIndex(p.symbol ?? layer.symbol), size, hollow ? 1 : 0, heading == null ? 0 : 1],
          i * 4,
        );
        if (heading != null) {
          dir.set([Math.sin(heading), 0, -Math.cos(heading)], i * 3);
          headings++;
        }
        const label = str(p.label);
        entry.points.push({
          world: local.set(x, y, z).clone().applyMatrix4(this.root.matrixWorld),
          size,
          label,
          info: str(p.info),
        });
        if (label) this.addLabel(labels, [x, z], label, c.css, size, y);
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      geom.setAttribute('aColor', new THREE.BufferAttribute(col, 3));
      geom.setAttribute('aStyle', new THREE.BufferAttribute(style, 4));
      geom.setAttribute('aDir', new THREE.BufferAttribute(dir, 3));
      this.geometries.push(geom);
      const markers = new THREE.Points(geom, this.markerMaterial);
      markers.name = 'markers';
      markers.renderOrder = MARKER_RENDER_ORDER;
      markers.frustumCulled = false;
      group.add(markers);
      if (headings > 0) {
        const arrows = new THREE.Points(geom, this.arrowMaterial);
        arrows.name = 'arrows';
        arrows.renderOrder = ARROW_RENDER_ORDER;
        arrows.frustumCulled = false;
        group.add(arrows);
      }
    }
    return entry;
  }

  private lineMaterial(width: number, dashed: boolean): LineMaterial {
    const key = `${width}|${dashed ? 1 : 0}`;
    let m = this.lineMaterials.get(key);
    if (!m) {
      const dash = Math.max(this.opts.drapeStep * 2, 1e-3);
      m = new LineMaterial({
        linewidth: width,
        vertexColors: true,
        dashed,
        dashSize: dash,
        gapSize: dash * 0.6,
        transparent: true,
        depthTest: false,
        depthWrite: false,
        toneMapped: false,
      });
      m.resolution.copy(this.viewport);
      this.lineMaterials.set(key, m);
    }
    return m;
  }

  /** Lay a polyline on the surface: split each edge to the drape step. */
  private drape(verts: [number, number][], c: THREE.Color, out: Segments): void {
    const step = Math.max(this.opts.drapeStep, 1e-6);
    let prev: [number, number, number] | null = null;
    for (let i = 0; i < verts.length - 1; i++) {
      const [x0, z0] = verts[i];
      const [x1, z1] = verts[i + 1];
      const n = Math.min(
        MAX_DRAPE_STEPS,
        Math.max(1, Math.ceil(Math.hypot(x1 - x0, z1 - z0) / step)),
      );
      for (let k = i === 0 ? 0 : 1; k <= n; k++) {
        const t = k / n;
        const x = x0 + (x1 - x0) * t;
        const z = z0 + (z1 - z0) * t;
        const cur: [number, number, number] = [x, this.surfaceY(x, z), z];
        if (prev) {
          out.positions.push(...prev, ...cur);
          out.colors.push(c.r, c.g, c.b, c.r, c.g, c.b);
        }
        prev = cur;
      }
    }
  }

  private addLabel(
    labels: THREE.Group,
    at: [number, number],
    text: string,
    css: string,
    markerSize: number,
    y?: number,
  ): void {
    const el = document.createElement('div');
    el.textContent = text;
    el.style.color = css;
    el.style.font = '600 11px/1.15 ui-sans-serif, system-ui, sans-serif';
    el.style.whiteSpace = 'nowrap';
    el.style.textShadow = '0 0 2px #000, 0 0 3px #000, 1px 1px 1px #000';
    el.style.paddingLeft = `${Math.round(markerSize * 0.7) + 4}px`;
    el.style.pointerEvents = 'none';
    el.style.userSelect = 'none';
    const obj = new CSS2DObject(el);
    obj.center.set(0, 0.5);
    obj.position.set(at[0], y ?? this.surfaceY(at[0], at[1]), at[1]);
    labels.add(obj);
  }
}

function centroid(verts: [number, number][]): [number, number] {
  let x = 0;
  let z = 0;
  for (const v of verts) {
    x += v[0];
    z += v[1];
  }
  return [x / verts.length, z / verts.length];
}
