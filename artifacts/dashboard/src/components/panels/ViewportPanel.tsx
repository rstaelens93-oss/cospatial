import { useEffect, useRef, useCallback, useState } from "react";
import * as THREE from "three";

interface ScenePoint {
  x: number;
  y: number;
  z: number;
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

export default function ViewportPanel({ sceneData }: ViewportPanelProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [webglError, setWebglError] = useState<string | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const animFrameRef = useRef<number>(0);
  const rotationGroupRef = useRef<THREE.Group | null>(null);
  const userGroupRef = useRef<THREE.Group | null>(null);
  const defaultMeshRef = useRef<THREE.Mesh | null>(null);
  const pointLightRef = useRef<THREE.PointLight | null>(null);
  const customObjectsRef = useRef<THREE.Object3D[]>([]);
  const clockRef = useRef(new THREE.Timer());

  // Mouse drag state
  const isDraggingRef = useRef(false);
  const lastMouseRef = useRef({ x: 0, y: 0 });
  const dragRotationRef = useRef({ x: 0, y: 0 });

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
    camera.position.set(0, 0, 6);
    cameraRef.current = camera;

    // Renderer — guard against environments where WebGL is unavailable
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
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

    // Rotation groups
    const rotationGroup = new THREE.Group();
    scene.add(rotationGroup);
    rotationGroupRef.current = rotationGroup;

    const userGroup = new THREE.Group(); // for user drag rotation
    rotationGroup.add(userGroup);
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
    const icoMeshSolid = new THREE.Mesh(icoGeoSolid, icoMatSolid);
    userGroup.add(icoMeshSolid);

    // Grid helper
    const gridHelper = new THREE.GridHelper(20, 30, 0x0d2033, 0x091420);
    gridHelper.position.y = -2.5;
    scene.add(gridHelper);

    // Subtle star field
    const starCount = 300;
    const starPositions = new Float32Array(starCount * 3);
    for (let i = 0; i < starCount; i++) {
      starPositions[i * 3] = (Math.random() - 0.5) * 80;
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
    const stars = new THREE.Points(starGeo, starMat);
    scene.add(stars);

    // Animation loop — wrapped in try/catch so any THREE.js throw
    // does NOT escape to window.onerror (which triggers the Vite overlay).
    const animate = () => {
      try {
        animFrameRef.current = requestAnimationFrame(animate);
        clockRef.current.update();
        const elapsed = clockRef.current.getElapsed();

        if (rotationGroupRef.current) {
          rotationGroupRef.current.rotation.y = elapsed * 0.15;
        }

        if (pointLightRef.current) {
          const r = (Math.sin(elapsed * 0.3) * 0.5 + 0.5) * 0.2;
          const g = (Math.sin(elapsed * 0.2 + 2) * 0.5 + 0.5) * 0.6 + 0.4;
          const b = 1.0;
          pointLightRef.current.color.setRGB(r * 0.2, g * 0.5, b);
          pointLightRef.current.intensity = 3 + Math.sin(elapsed * 0.8) * 1.5;
        }

        if (defaultMeshRef.current && defaultMeshRef.current.visible) {
          const s = 1 + Math.sin(elapsed * 1.2) * 0.06;
          defaultMeshRef.current.scale.setScalar(s);
        }

        renderer.render(scene, camera);
      } catch {
        // Cancel the loop on error to prevent 60×/s window.onerror floods.
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

  // Mouse drag handlers
  const handleMouseDown = useCallback((e: MouseEvent) => {
    isDraggingRef.current = true;
    lastMouseRef.current = { x: e.clientX, y: e.clientY };
  }, []);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!isDraggingRef.current || !userGroupRef.current) return;
    const dx = e.clientX - lastMouseRef.current.x;
    const dy = e.clientY - lastMouseRef.current.y;
    lastMouseRef.current = { x: e.clientX, y: e.clientY };

    dragRotationRef.current.y += dx * 0.008;
    dragRotationRef.current.x += dy * 0.008;

    userGroupRef.current.rotation.y = dragRotationRef.current.y;
    userGroupRef.current.rotation.x = dragRotationRef.current.x;
  }, []);

  const handleMouseUp = useCallback(() => {
    isDraggingRef.current = false;
  }, []);

  useEffect(() => {
    const cleanup = initScene();

    const el = mountRef.current;
    if (el) {
      el.addEventListener("mousedown", handleMouseDown);
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
    }

    return () => {
      cleanup?.();
      cancelAnimationFrame(animFrameRef.current);
      if (el) {
        el.removeEventListener("mousedown", handleMouseDown);
        window.removeEventListener("mousemove", handleMouseMove);
        window.removeEventListener("mouseup", handleMouseUp);
      }
      if (rendererRef.current) {
        rendererRef.current.dispose();
        if (rendererRef.current.domElement.parentNode) {
          rendererRef.current.domElement.parentNode.removeChild(
            rendererRef.current.domElement
          );
        }
      }
    };
  }, [initScene, handleMouseDown, handleMouseMove, handleMouseUp]);

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
      const positions = new Float32Array(parsed.points.length * 3);
      parsed.points.forEach((p, i) => {
        positions[i * 3] = p.x;
        positions[i * 3 + 1] = p.y;
        positions[i * 3 + 2] = p.z;
      });

      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));

      const colorHex = parsed.color ?? "#00ffcc";
      const color = new THREE.Color(colorHex);
      const mat = new THREE.PointsMaterial({
        color: color,
        size: 0.08,
        sizeAttenuation: true,
      });

      const points = new THREE.Points(geo, mat);
      userGroupRef.current.add(points);
      customObjectsRef.current.push(points);
    } else if (parsed.type === "mesh" && parsed.vertices && parsed.faces) {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute(
        "position",
        new THREE.BufferAttribute(new Float32Array(parsed.vertices), 3)
      );
      geo.setIndex(parsed.faces);
      geo.computeVertexNormals();

      const mat = new THREE.MeshPhongMaterial({
        color: new THREE.Color(parsed.color ?? "#00d4ff"),
        wireframe: false,
        side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(geo, mat);
      userGroupRef.current.add(mesh);
      customObjectsRef.current.push(mesh);
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
      }}
      onMouseDown={(e) => e.preventDefault()}
    />
  );
}
