// In the running Vite browser:
// (await import('/scripts/test-water-depth.browser.mjs')).verifyWaterDepth(window.__wowsShipViewer__)
import * as THREE from 'three';

export function verifyWaterDepth(viewer) {
  const env = viewer.env;
  const water = env.scene.getObjectByName('FauxWaterSurface');
  const renderer = env.renderer;
  const previousTarget = renderer.getRenderTarget();
  const target = new THREE.WebGLRenderTarget(16, 16);
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 100);
  camera.position.set(0, 10, 0);
  camera.up.set(0, 0, -1);
  camera.lookAt(0, 0, 0);
  const waterMaterial = water.material.clone();
  // Constant colors isolate depth behavior from procedural lighting.
  waterMaterial.fragmentShader = 'void main() { gl_FragColor = vec4(0., 0., 1., 1.); }';
  const surface = new THREE.Mesh(water.geometry, waterMaterial);
  surface.rotation.copy(water.rotation);
  const hullMaterial = new THREE.MeshBasicMaterial({ color: 0xff0000, toneMapped: false });
  const hull = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), hullMaterial);
  hull.rotation.copy(water.rotation);
  scene.add(surface, hull);
  const results = [];
  try {
    for (const order of [-1, 1]) {
      surface.renderOrder = order;
      for (const height of [-0.2, 0.2, 0]) {
        hull.position.y = height;
        renderer.setRenderTarget(target);
        renderer.clear();
        renderer.render(scene, camera);
        const pixel = new Uint8Array(4);
        renderer.readRenderTargetPixels(target, 8, 8, 1, 1, pixel);
        const expected = height < 0 ? [0, 0, 255] : [255, 0, 0];
        if (expected.some((v, i) => Math.abs(pixel[i] - v) > 1)) {
          throw new Error(`Water depth mismatch at height=${height}, order=${order}: ${pixel}`);
        }
        results.push({ height, waterOrder: order, pixel: [...pixel] });
      }
    }
  } finally {
    renderer.setRenderTarget(previousTarget);
    target.dispose();
    waterMaterial.dispose();
    hull.geometry.dispose();
    hullMaterial.dispose();
  }
  // The actual multipass renderer must omit water only from soft depth,
  // while retaining it in the color snapshot used by wake distortion.
  const draws = [];
  const oldHook = water.onBeforeRender;
  const oldVisible = water.visible;
  try {
    water.visible = true;
    water.onBeforeRender = () => draws.push(renderer.getRenderTarget()?.texture.name ?? 'screen');
    env.render();
  } finally {
    water.onBeforeRender = oldHook;
    water.visible = oldVisible;
  }
  if (draws.includes('WowsOpaqueDepthColorDiscard')) throw new Error('Water entered soft-particle depth');
  if (!draws.includes('WowsDistortionSceneColor')) throw new Error('Enable ship particles before testing distortion capture');
  return { results, waterDraws: draws };
}
