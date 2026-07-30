import { useEffect, useRef, useCallback, useState, forwardRef, useImperativeHandle } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

interface ScenePoint {
  x: number;
  y: number;
  z: number;
  /** Per-point hex color e.g. "#ff6600". When present on ≥1 point, vertex
   *  colors are used so image-derived point clouds render with true pixel hues. */
  color?: string;
}

interface SceneData {
  type: "points" | "mesh";
  points?: ScenePoint[];
  color?: string;
  vertices?: number[];
  faces?: number[];
}

interface ViewportPanelProps {
  sceneData: string | null;
}

export interface ViewportPanelHandle {
  exportPLY: () => void;
}

const ViewportPanel = forwardRef<ViewportPanelHandle, ViewportPanelProps>(
  function ViewportPanel({ sceneData }: ViewportPanelProps, ref) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [webglError, setWebglError] = useState<string | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const animFrameRef = useRef<number>(0);
  const userGroupRef = useRef<THREE.Group | null>(null);
  const defaultMeshRef = useRef<THREE.Mesh | null>(null);
  const pointLightRef = useRef<THREE.PointLight | null>(null);
  const customObjectsRef = useRef<THREE.Object3D[]>([]);
  const clockRef = useRef(new THREE.Timer());

  // Expose exportPLY to parent via ref
  useImperativeHandle(ref, () => ({
    exportPLY() {
      const pointObjects = customObjectsRef.current.filter(
        (o) => o instanceof THREE.Points
      ) as THREE.Points[];

      if (pointObjects.length === 0) return;

      const allPositions: number[] = [];
      const allColors: number[] = [];
      let hasColors = false;

      for (const pts of pointObjects) {
        const geo = pts.geometry;
        const pos = geo.getAttribute("position") as THREE.BufferAttribute;
        const col = geo.getAttribute("color") as THREE.BufferAttribute | undefined;
        const matColor = (pts.material as THREE.PointsMaterial).color;

        for (let i = 0; i < pos.count; i++) {
          allPositions.push(pos.getX(i), pos.getY(i), pos.getZ(i));
          if (col) {
            hasColors = true;
            allColors.push(
              Math.round(col.getX(i) * 255),
              Math.round(col.getY(i) * 255),
              Math.round(col.getZ(i) * 255)
            );
          } else {
            allColors.push(
              Math.round(matColor.r * 255),
              Math.round(matColor.g * 255),
              Math.round(matColor.b * 255)
            );
          }
        }
      }

      const count = allPositions.length / 3;
      let ply =
        `ply\nformat ascii 1.0\nelement vertex ${count}\n` +
        `property float x\nproperty float y\nproperty float z\n` +
        `property uchar red\nproperty uchar green\nproperty uchar blue\n` +
        `end_header\n`;

      for (let i = 0; i < count; i++) {
        ply +=
          `${allPositions[i * 3]} ${allPositions[i * 3 + 1]} ${allPositions[i * 3 + 2]} ` +
          `${allColors[i * 3]} ${allColors[i * 3 + 1]} ${allColors[i * 3 + 2]}\n`;
      }

      const blob = new Blob([ply], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "scene.ply";
      a.click();
      URL.revokeObjectURL(url);
    },
  }), []);

  const initScene = useCallback(() => {
    if (!mountRef.current) return;

    const container = mountRef.current;
    const w = container.clientWidth;
    const h = container.clientHeight;

    // Scene
    const scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x0b1118, 0.08);
    sceneRef.current = scene;

    // Camera
    const camera = new THREE.PerspectiveCamera(60, w / h, 0.01, 200);
    // Slight off-axis angle so luminance-depth displacement is immediately
    // visible when a point cloud loads — pure (0,0,6) collapses z into 2-D.
    camera.position.set(2.5, 2.0, 7);
    cameraRef.current = camera;

    // Renderer
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: false,
        preserveDrawingBuffer: true,
      });
    } catch {
      setWebglError("WebGL is not available in this environment.");
      return;
    }
    if (!renderer.getContext()) {
      setWebglError("WebGL context could not be created.");
      renderer.dispose();
      return;
    }
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x0b1118, 1);
    renderer.shadowMap.enabled = false;
    rendererRef.current = renderer;
    container.appendChild(renderer.domElement);

    // ── OrbitControls — replaces manual drag; handles mouse + touch ───────────
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.autoRotate = false;          // no auto-spin; user drives 100%
    controls.enableDamping = true;        // smooth inertia on release
    controls.dampingFactor = 0.08;
    controls.enableZoom = true;
    controls.enablePan = true;
    controls.rotateSpeed = 0.8;
    controls.zoomSpeed = 1.0;
    controls.panSpeed = 0.6;
    // Touch: one finger = orbit, two fingers = dolly+pan
    controls.touches = {
      ONE: THREE.TOUCH.ROTATE,
      TWO: THREE.TOUCH.DOLLY_PAN,
    };
    controlsRef.current = controls;

    // Group for custom scene objects (centered in world space)
    const userGroup = new THREE.Group();
    scene.add(userGroup);
    userGroupRef.current = userGroup;

    // Lights
    const ambientLight = new THREE.AmbientLight(0x0d2033, 2.5);
    scene.add(ambientLight);

    const pointLight = new THREE.PointLight(0x00d4ff, 4, 20);
    pointLight.position.set(3, 3, 3);
    scene.add(pointLight);
    pointLightRef.current = pointLight;

    const pointLight2 = new THREE.PointLight(0x0066aa, 2, 15);
    pointLight2.position.set(-3, -2, 2);
    scene.add(pointLight2);

    // Default wireframe icosahedron
    const icoGeo = new THREE.IcosahedronGeometry(1.4, 1);
    const icoMat = new THREE.MeshPhongMaterial({
      color: 0x00d4ff,
      wireframe: true,
      opacity: 0.6,
      transparent: true,
    });
    const icoMesh = new THREE.Mesh(icoGeo, icoMat);
    userGroup.add(icoMesh);
    defaultMeshRef.current = icoMesh;

    // Inner solid icosahedron
    const icoGeoSolid = new THREE.IcosahedronGeometry(1.35, 1);
    const icoMatSolid = new THREE.MeshPhongMaterial({
      color: 0x001a2e,
      opacity: 0.85,
      transparent: true,
    });
    userGroup.add(new THREE.Mesh(icoGeoSolid, icoMatSolid));

    // Grid helper
    const gridHelper = new THREE.GridHelper(20, 30, 0x0d2033, 0x091420);
    gridHelper.position.y = -2.5;
    scene.add(gridHelper);

    // Subtle star field
    const starCount = 300;
    const starPositions = new Float32Array(starCount * 3);
    for (let i = 0; i < starCount; i++) {
      starPositions[i * 3]     = (Math.random() - 0.5) * 80;
      starPositions[i * 3 + 1] = (Math.random() - 0.5) * 80;
      starPositions[i * 3 + 2] = (Math.random() - 0.5) * 80;
    }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute("position", new THREE.BufferAttribute(starPositions, 3));
    const starMat = new THREE.PointsMaterial({
      color: 0x335566,
      size: 0.08,
      sizeAttenuation: true,
    });
    scene.add(new THREE.Points(starGeo, starMat));

    // Animation loop
    const animate = () => {
      try {
        animFrameRef.current = requestAnimationFrame(animate);
        clockRef.current.update();
        const elapsed = clockRef.current.getElapsed();

        // Animate point light color only — no object auto-rotation
        if (pointLightRef.current) {
          const g = (Math.sin(elapsed * 0.2 + 2) * 0.5 + 0.5) * 0.6 + 0.4;
          pointLightRef.current.color.setRGB(0, g * 0.5, 1.0);
          pointLightRef.current.intensity = 3 + Math.sin(elapsed * 0.8) * 1.5;
        }

        // Pulse default mesh scale when visible
        if (defaultMeshRef.current && defaultMeshRef.current.visible) {
          const s = 1 + Math.sin(elapsed * 1.2) * 0.06;
          defaultMeshRef.current.scale.setScalar(s);
        }

        // OrbitControls requires update() each frame when damping is enabled
        controls.update();

        renderer.render(scene, camera);
      } catch {
        cancelAnimationFrame(animFrameRef.current);
      }
    };
    animate();

    // ResizeObserver
    const ro = new ResizeObserver(() => {
      if (!mountRef.current || !rendererRef.current || !cameraRef.current) return;
      const nw = mountRef.current.clientWidth;
      const nh = mountRef.current.clientHeight;
      if (nw === 0 || nh === 0) return;
      cameraRef.current.aspect = nw / nh;
      cameraRef.current.updateProjectionMatrix();
      rendererRef.current.setSize(nw, nh);
    });
    ro.observe(container);

    return () => {
      ro.disconnect();
    };
  }, []);

  useEffect(() => {
    const cleanup = initScene();

    return () => {
      cleanup?.();
      cancelAnimationFrame(animFrameRef.current);
      if (controlsRef.current) {
        controlsRef.current.dispose();
        controlsRef.current = null;
      }
      if (rendererRef.current) {
        rendererRef.current.dispose();
        if (rendererRef.current.domElement.parentNode) {
          rendererRef.current.domElement.parentNode.removeChild(
            rendererRef.current.domElement
          );
        }
        rendererRef.current = null;
      }
    };
  }, [initScene]);

  // Apply scene data from code execution
  useEffect(() => {
    if (!sceneData || !sceneRef.current || !userGroupRef.current) return;

    let parsed: SceneData;
    try {
      parsed = JSON.parse(sceneData);
    } catch {
      console.warn("Invalid sceneData JSON");
      return;
    }

    // Remove previous custom objects
    customObjectsRef.current.forEach((obj) => {
      userGroupRef.current?.remove(obj);
    });
    customObjectsRef.current = [];

    // Hide default icosahedron when custom scene is loaded
    if (defaultMeshRef.current) {
      defaultMeshRef.current.visible = false;
    }

    if (parsed.type === "points" && parsed.points) {
      const count = parsed.points.length;
      const positions = new Float32Array(count * 3);
      const colorsBuf = new Float32Array(count * 3);
      let hasVertexColors = false;

      parsed.points.forEach((p, i) => {
        positions[i * 3]     = p.x;
        positions[i * 3 + 1] = p.y;
        positions[i * 3 + 2] = p.z;
        if (p.color) {
          const c = new THREE.Color(p.color);
          colorsBuf[i * 3]     = c.r;
          colorsBuf[i * 3 + 1] = c.g;
          colorsBuf[i * 3 + 2] = c.b;
          hasVertexColors = true;
        }
      });

      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      if (hasVertexColors) {
        geo.setAttribute("color", new THREE.BufferAttribute(colorsBuf, 3));
      }

      const mat = new THREE.PointsMaterial({
        // Per-point colors when available (image-derived clouds); fall back to
        // the top-level palette color for single-color Python-generated clouds.
        color: hasVertexColors ? 0xffffff : new THREE.Color(parsed.color ?? "#00ffcc"),
        vertexColors: hasVertexColors,
        size: hasVertexColors ? 0.08 : 0.08,
        sizeAttenuation: true,
      });

      const points = new THREE.Points(geo, mat);
      userGroupRef.current.add(points);
      customObjectsRef.current.push(points);
    } else if (parsed.type === "mesh" && parsed.vertices && parsed.faces) {
      const geo = new THREE.BufferGeometry();

      // Vertex positions
      geo.setAttribute(
        "position",
        new THREE.BufferAttribute(new Float32Array(parsed.vertices), 3)
      );

      // Per-vertex colors: backend sends packed (r<<16|g<<8|b) integers
      const hasColors = Array.isArray(parsed.colors) && parsed.colors.length > 0;
      if (hasColors) {
        const packed = parsed.colors!;
        const colorBuf = new Float32Array(packed.length * 3);
        for (let i = 0; i < packed.length; i++) {
          colorBuf[i * 3]     = ((packed[i] >> 16) & 0xff) / 255;
          colorBuf[i * 3 + 1] = ((packed[i] >> 8)  & 0xff) / 255;
          colorBuf[i * 3 + 2] = ( packed[i]        & 0xff) / 255;
        }
        geo.setAttribute("color", new THREE.BufferAttribute(colorBuf, 3));
      }

      // Face indices — Uint32Array handles vertex counts > 65 535
      geo.setIndex(new THREE.BufferAttribute(new Uint32Array(parsed.faces), 1));
      geo.computeVertexNormals();

      const mat = new THREE.MeshPhongMaterial({
        color: 0xffffff,
        vertexColors: hasColors,
        wireframe: false,
        side: THREE.DoubleSide,
        shininess: 45,
        specular: new THREE.Color(0x333333),
      });
      const mesh = new THREE.Mesh(geo, mat);
      userGroupRef.current.add(mesh);
      customObjectsRef.current.push(mesh);
    }

    // Force an immediate render so the new geometry appears on the very next paint
    if (rendererRef.current && sceneRef.current && cameraRef.current) {
      rendererRef.current.render(sceneRef.current, cameraRef.current);
    }
  }, [sceneData]);

  if (webglError) {
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          background: "#0b1118",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          color: "#00d4ff",
          fontFamily: "monospace",
          gap: "12px",
          padding: "24px",
          textAlign: "center",
        }}
      >
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/>
          <polyline points="3.27 6.96 12 12.01 20.73 6.96"/>
          <line x1="12" y1="22.08" x2="12" y2="12"/>
        </svg>
        <span style={{ fontSize: "13px", color: "#4a6b7a" }}>
          3D Viewport — WebGL unavailable
        </span>
        <span style={{ fontSize: "11px", color: "#2a4455", maxWidth: "240px", lineHeight: "1.5" }}>
          Run your Python code to generate scene data. The 3D scene will render when WebGL is supported in your browser.
        </span>
      </div>
    );
  }

  return (
    <div
      ref={mountRef}
      data-testid="viewport-3d"
      style={{
        width: "100%",
        height: "100%",
        cursor: "grab",
        background: "#0b1118",
        touchAction: "none",  // let OrbitControls handle all touch events
      }}
    />
  );
});

export default ViewportPanel;
