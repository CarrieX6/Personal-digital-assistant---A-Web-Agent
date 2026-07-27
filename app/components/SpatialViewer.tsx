"use client";

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

type SpatialViewerProps = {
  sourceUrl: string;
  depthUrl: string;
  backgroundUrl?: string;
  foregroundUrl?: string;
  title: string;
};

type LayerSet = {
  background: THREE.Mesh;
  foreground: THREE.Mesh | null;
  depth: THREE.Mesh;
  camera: THREE.PerspectiveCamera;
};

const DEFAULT_STRENGTH = 0.26;
const MAX_MOTION = 0.78;
const CAMERA_TRAVEL_X = 0.125;
const CAMERA_TRAVEL_Y = 0.08;
const VIEW_OVERSCAN = 1.075;

export function SpatialViewer({
  sourceUrl,
  depthUrl,
  backgroundUrl,
  foregroundUrl,
  title,
}: SpatialViewerProps) {
  const viewerRootRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const layersRef = useRef<LayerSet | null>(null);
  const renderRef = useRef<(() => void) | null>(null);
  const demoRef = useRef<(() => void) | null>(null);
  const syncStrengthRef = useRef<(value: number) => void>(() => undefined);
  const strengthRef = useRef(DEFAULT_STRENGTH);
  const showDepthRef = useRef(false);
  const [strength, setStrength] = useState(DEFAULT_STRENGTH);
  const [showDepth, setShowDepth] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [renderError, setRenderError] = useState("");
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    if (!isFullscreen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const exitPageFullscreen = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsFullscreen(false);
    };
    window.addEventListener("keydown", exitPageFullscreen);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", exitPageFullscreen);
    };
  }, [isFullscreen]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let disposed = false;
    let frame = 0;
    let resizeFrame = 0;
    let imageAspect = 1;
    const demoTimers: number[] = [];
    const reducedMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    const target = new THREE.Vector2();
    const current = new THREE.Vector2();

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: false,
        powerPreference: "low-power",
        stencil: true,
      });
    } catch {
      queueMicrotask(() =>
        setRenderError("当前浏览器无法启动 WebGL，已保留原图预览。"),
      );
      return;
    }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.setAttribute("aria-label", `${title} 的可交互空间视图`);
    renderer.domElement.setAttribute("role", "img");
    renderer.domElement.tabIndex = 0;
    container.replaceChildren(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color("#101512");
    const camera = new THREE.PerspectiveCamera(48, 1, 0.05, 10);
    let geometry: THREE.PlaneGeometry | null = null;
    let backgroundTexture: THREE.Texture | null = null;
    let foregroundTexture: THREE.Texture | null = null;
    let depthTexture: THREE.Texture | null = null;
    let backgroundMaterial: THREE.MeshBasicMaterial | null = null;
    let foregroundMaterial: THREE.MeshBasicMaterial | null = null;
    let depthMaterial: THREE.MeshBasicMaterial | null = null;

    const clearDemoTimers = () => {
      while (demoTimers.length) {
        const timer = demoTimers.pop();
        if (timer !== undefined) window.clearTimeout(timer);
      }
    };

    const alignLayerToSourceView = (mesh: THREE.Mesh) => {
      const scale = Math.max(
        (camera.position.z - mesh.position.z) / camera.position.z,
        0.8,
      );
      mesh.scale.setScalar(scale * VIEW_OVERSCAN);
    };

    const syncStrength = (value: number) => {
      const layers = layersRef.current;
      if (!layers) return;
      layers.background.position.z = -value * 0.22;
      layers.depth.position.z = 0;
      if (layers.foreground) {
        layers.foreground.position.z = value * 0.42;
      }
      alignLayerToSourceView(layers.background);
      alignLayerToSourceView(layers.depth);
      if (layers.foreground) alignLayerToSourceView(layers.foreground);
    };
    syncStrengthRef.current = syncStrength;

    const fitCamera = () => {
      const width = Math.max(container.clientWidth, 1);
      const height = Math.max(container.clientHeight, 1);
      camera.aspect = width / height;
      const halfFov = THREE.MathUtils.degToRad(camera.fov / 2);
      const verticalDistance = 0.58 / Math.tan(halfFov);
      const horizontalDistance =
        imageAspect / (2 * Math.tan(halfFov) * camera.aspect) + 0.12;
      camera.position.z = Math.max(verticalDistance, horizontalDistance, 1.25);
      camera.updateProjectionMatrix();
      renderer.setSize(width, height, false);
      syncStrength(strengthRef.current);
    };

    const draw = () => renderer.render(scene, camera);
    renderRef.current = draw;

    const tick = () => {
      frame = 0;
      if (reducedMotion) current.copy(target);
      else current.lerp(target, 0.12);
      camera.position.x = current.x * CAMERA_TRAVEL_X;
      camera.position.y = current.y * CAMERA_TRAVEL_Y;
      camera.lookAt(0, 0, 0);
      draw();
      if (!reducedMotion && current.distanceToSquared(target) > 0.00001) {
        frame = window.requestAnimationFrame(tick);
      }
    };

    const requestMotionFrame = () => {
      if (!frame) frame = window.requestAnimationFrame(tick);
    };

    const runDemo = () => {
      if (reducedMotion) return;
      clearDemoTimers();
      target.set(-0.64, 0.1);
      requestMotionFrame();
      demoTimers.push(
        window.setTimeout(() => {
          target.set(0.64, -0.08);
          requestMotionFrame();
        }, 900),
        window.setTimeout(() => {
          target.set(0, 0);
          requestMotionFrame();
        }, 2100),
      );
    };
    demoRef.current = runDemo;

    const updatePointer = (event: PointerEvent) => {
      clearDemoTimers();
      const rect = renderer.domElement.getBoundingClientRect();
      target.set(
        THREE.MathUtils.clamp(
          (((event.clientX - rect.left) / rect.width) * 2 - 1) * MAX_MOTION,
          -MAX_MOTION,
          MAX_MOTION,
        ),
        THREE.MathUtils.clamp(
          -(((event.clientY - rect.top) / rect.height) * 2 - 1) * MAX_MOTION,
          -MAX_MOTION,
          MAX_MOTION,
        ),
      );
      requestMotionFrame();
    };

    const resetPointer = () => {
      target.set(0, 0);
      requestMotionFrame();
    };

    const onKeyDown = (event: KeyboardEvent) => {
      const amount = 0.16;
      if (event.key === "ArrowLeft") {
        target.x = Math.max(-MAX_MOTION, target.x - amount);
      } else if (event.key === "ArrowRight") {
        target.x = Math.min(MAX_MOTION, target.x + amount);
      } else if (event.key === "ArrowUp") {
        target.y = Math.min(MAX_MOTION, target.y + amount);
      } else if (event.key === "ArrowDown") {
        target.y = Math.max(-MAX_MOTION, target.y - amount);
      } else if (event.key === "Escape") target.set(0, 0);
      else return;
      event.preventDefault();
      requestMotionFrame();
    };

    renderer.domElement.addEventListener("pointermove", updatePointer);
    renderer.domElement.addEventListener("pointerleave", resetPointer);
    renderer.domElement.addEventListener("keydown", onKeyDown);

    const resizeObserver = new ResizeObserver(() => {
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = 0;
        fitCamera();
        draw();
      });
    });
    resizeObserver.observe(container);

    const retrySuffix = `viewer_retry=${retryToken}`;
    const retryUrl = (url: string) =>
      `${url}${url.includes("?") ? "&" : "?"}${retrySuffix}`;
    const loader = new THREE.TextureLoader();
    loader.setCrossOrigin("anonymous");
    Promise.all([
      loader.loadAsync(retryUrl(backgroundUrl || sourceUrl)),
      loader.loadAsync(retryUrl(depthUrl)),
      foregroundUrl
        ? loader.loadAsync(retryUrl(foregroundUrl))
        : Promise.resolve(null),
    ])
      .then(([loadedBackground, loadedDepth, loadedForeground]) => {
        if (disposed) {
          loadedBackground.dispose();
          loadedDepth.dispose();
          loadedForeground?.dispose();
          return;
        }
        backgroundTexture = loadedBackground;
        depthTexture = loadedDepth;
        foregroundTexture = loadedForeground;
        backgroundTexture.colorSpace = THREE.SRGBColorSpace;
        depthTexture.colorSpace = THREE.NoColorSpace;
        if (foregroundTexture) {
          foregroundTexture.colorSpace = THREE.SRGBColorSpace;
        }
        for (const texture of [
          backgroundTexture,
          depthTexture,
          foregroundTexture,
        ]) {
          if (!texture) continue;
          texture.minFilter = THREE.LinearFilter;
          texture.magFilter = THREE.LinearFilter;
        }

        const image = backgroundTexture.image as {
          width?: number;
          height?: number;
        };
        imageAspect =
          image.width && image.height ? image.width / image.height : 1;
        geometry = new THREE.PlaneGeometry(imageAspect, 1);
        backgroundMaterial = new THREE.MeshBasicMaterial({
          map: backgroundTexture,
          stencilWrite: true,
          stencilRef: 1,
          stencilFunc: THREE.AlwaysStencilFunc,
          stencilZPass: THREE.ReplaceStencilOp,
        });
        depthMaterial = new THREE.MeshBasicMaterial({
          map: depthTexture,
        });
        const backgroundMesh = new THREE.Mesh(geometry, backgroundMaterial);
        const depthMesh = new THREE.Mesh(geometry, depthMaterial);
        let foregroundMesh: THREE.Mesh | null = null;
        scene.add(backgroundMesh);
        scene.add(depthMesh);
        if (foregroundTexture) {
          foregroundMaterial = new THREE.MeshBasicMaterial({
            map: foregroundTexture,
            transparent: true,
            alphaTest: 0.01,
            depthWrite: false,
            stencilWrite: true,
            stencilRef: 1,
            stencilFunc: THREE.EqualStencilFunc,
            stencilFail: THREE.KeepStencilOp,
            stencilZFail: THREE.KeepStencilOp,
            stencilZPass: THREE.KeepStencilOp,
          });
          foregroundMesh = new THREE.Mesh(geometry, foregroundMaterial);
          foregroundMesh.renderOrder = 2;
          scene.add(foregroundMesh);
        }

        layersRef.current = {
          background: backgroundMesh,
          foreground: foregroundMesh,
          depth: depthMesh,
          camera,
        };
        backgroundMesh.visible = !showDepthRef.current;
        if (foregroundMesh) foregroundMesh.visible = !showDepthRef.current;
        depthMesh.visible = showDepthRef.current;
        fitCamera();
        draw();
        setRenderError("");
        demoTimers.push(window.setTimeout(runDemo, 260));
      })
      .catch(() => {
        if (!disposed) {
          setRenderError(
            "原图或空间分层纹理加载失败，请确认本地服务在线后重试。",
          );
        }
      });

    return () => {
      disposed = true;
      if (frame) window.cancelAnimationFrame(frame);
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      clearDemoTimers();
      resizeObserver.disconnect();
      renderer.domElement.removeEventListener("pointermove", updatePointer);
      renderer.domElement.removeEventListener("pointerleave", resetPointer);
      renderer.domElement.removeEventListener("keydown", onKeyDown);
      layersRef.current = null;
      renderRef.current = null;
      demoRef.current = null;
      syncStrengthRef.current = () => undefined;
      geometry?.dispose();
      backgroundMaterial?.dispose();
      foregroundMaterial?.dispose();
      depthMaterial?.dispose();
      backgroundTexture?.dispose();
      foregroundTexture?.dispose();
      depthTexture?.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    };
  }, [
    backgroundUrl,
    depthUrl,
    foregroundUrl,
    retryToken,
    sourceUrl,
    title,
  ]);

  useEffect(() => {
    strengthRef.current = strength;
    syncStrengthRef.current(strength);
    renderRef.current?.();
  }, [strength]);

  useEffect(() => {
    showDepthRef.current = showDepth;
    const layers = layersRef.current;
    if (layers) {
      layers.background.visible = !showDepth;
      if (layers.foreground) layers.foreground.visible = !showDepth;
      layers.depth.visible = showDepth;
      renderRef.current?.();
    }
  }, [showDepth]);

  const toggleFullscreen = () => setIsFullscreen((currentValue) => !currentValue);

  return (
    <div
      ref={viewerRootRef}
      className={`spatial-viewer${isFullscreen ? " is-fullscreen" : ""}`}
    >
      <div className="viewer-stage">
        <div ref={containerRef} className="viewer-canvas" />
        {renderError ? (
          <div className="viewer-fallback">
            {/* The URL points to the user's local FastAPI service. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={sourceUrl} alt={title} />
            <div className="viewer-fallback-actions">
              <p>{renderError}</p>
              <button
                type="button"
                onClick={() => {
                  setRenderError("正在重新加载空间纹理…");
                  setRetryToken((currentValue) => currentValue + 1);
                }}
              >
                重试加载
              </button>
            </div>
          </div>
        ) : null}
        <span className="viewer-hint">轻移鼠标或使用方向键观察空间视差</span>
      </div>
      <div className="viewer-controls">
        <label>
          <span>立体强度</span>
          <input
            type="range"
            min="0.04"
            max="0.46"
            step="0.01"
            value={strength}
            onChange={(event) => setStrength(Number(event.target.value))}
          />
          <output>{Math.round(strength * 100)}</output>
        </label>
        <button
          type="button"
          className={showDepth ? "is-active" : ""}
          aria-pressed={showDepth}
          onClick={() => setShowDepth((currentValue) => !currentValue)}
        >
          {showDepth ? "返回彩色" : "查看深度"}
        </button>
        <button type="button" onClick={() => demoRef.current?.()}>
          演示视角
        </button>
        <button type="button" onClick={toggleFullscreen}>
          {isFullscreen ? "退出全屏" : "全屏观看"}
        </button>
        <button type="button" onClick={() => setStrength(DEFAULT_STRENGTH)}>
          重置
        </button>
      </div>
    </div>
  );
}
