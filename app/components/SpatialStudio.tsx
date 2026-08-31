"use client";

import {
  ChangeEvent,
  DragEvent,
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import dynamic from "next/dynamic";

const SpatialViewer = dynamic(
  () =>
    import("./SpatialViewer").then((module) => module.SpatialViewer),
  {
    ssr: false,
    loading: () => (
      <div className="viewer-empty">
        <strong>正在启动 3D Viewer…</strong>
      </div>
    ),
  },
);

type Asset = {
  id: string;
  kind: "spatial_scene" | "photo_style_transfer";
  name: string;
  status: "processing" | "ready" | "failed";
  width: number | null;
  height: number | null;
  source_url: string;
  preview_url: string | null;
  depth_url: string | null;
  background_url: string | null;
  foreground_url: string | null;
  foreground_mask_url: string | null;
  manifest_url: string | null;
  model_name: string | null;
  segmentation_model: string | null;
  segmentation_quality: number | null;
  segmentation_warnings: string[];
  recommended_strength: number | null;
  created_at: string;
  updated_at: string;
};

type Job = {
  id: string;
  kind: "spatial_scene";
  status: "queued" | "running" | "completed" | "failed";
  progress: number;
  stage: string;
  message: string;
  asset_id: string;
  error: string | null;
};

type CreateResponse = {
  asset: Asset;
  job: Job;
};

type SpatialStudioProps = {
  apiBase: string;
  onConnectionChange: (ready: boolean) => void;
  requestedAssetId?: string | null;
};

const supportedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);

function resolveUrl(apiBase: string, path: string | null) {
  return path ? `${apiBase}${path}` : "";
}

async function errorMessage(response: Response) {
  try {
    const data = (await response.json()) as { detail?: string };
    return data.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

export function SpatialStudio({
  apiBase,
  onConnectionChange,
  requestedAssetId,
}: SpatialStudioProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const previewUrlRef = useRef("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [title, setTitle] = useState("");
  const [currentJob, setCurrentJob] = useState<Job | null>(null);
  const [uploading, setUploading] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [loadingAssets, setLoadingAssets] = useState(true);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [error, setError] = useState("");

  const loadAssets = useCallback(async () => {
    try {
      const [assetsResponse, jobsResponse] = await Promise.all([
        fetch(`${apiBase}/api/assets`),
        fetch(`${apiBase}/api/jobs?limit=50`, { cache: "no-store" }),
      ]);
      if (!assetsResponse.ok) {
        throw new Error(await errorMessage(assetsResponse));
      }
      if (!jobsResponse.ok) {
        throw new Error(await errorMessage(jobsResponse));
      }
      const data = (await assetsResponse.json()) as { assets: Asset[] };
      const jobsData = (await jobsResponse.json()) as { jobs: Job[] };
      const spatialAssets = data.assets.filter(
        (asset) => asset.kind === "spatial_scene",
      );
      setAssets(spatialAssets);
      setSelectedAssetId((current) => {
        if (current && spatialAssets.some((asset) => asset.id === current)) {
          return current;
        }
        return spatialAssets.find((asset) => asset.status === "ready")?.id ?? null;
      });
      const recoverableJob = jobsData.jobs.find(
        (job) =>
          job.kind === "spatial_scene" &&
          ["queued", "running", "failed"].includes(job.status),
      );
      setCurrentJob((current) => current ?? recoverableJob ?? null);
      onConnectionChange(true);
    } catch (requestError) {
      onConnectionChange(false);
      setError(
        requestError instanceof Error
          ? `${requestError.message}。请确认本地后端已启动。`
          : "无法读取本地资产库。",
      );
    } finally {
      setLoadingAssets(false);
    }
  }, [apiBase, onConnectionChange]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadAssets(), 0);
    return () => window.clearTimeout(timer);
  }, [loadAssets]);

  useEffect(() => {
    if (!requestedAssetId) return;
    const timer = window.setTimeout(async () => {
      await loadAssets();
      setSelectedAssetId(requestedAssetId);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadAssets, requestedAssetId]);

  useEffect(() => {
    return () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    };
  }, []);

  useEffect(() => {
    if (!currentJob || !["queued", "running"].includes(currentJob.status)) return;

    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/jobs/${currentJob.id}`);
        if (!response.ok) throw new Error(await errorMessage(response));
        const job = (await response.json()) as Job;
        setCurrentJob(job);
        onConnectionChange(true);
        if (job.status === "completed") {
          await loadAssets();
          setSelectedAssetId(job.asset_id);
        } else if (job.status === "failed") {
          setError(job.error || job.message);
          await loadAssets();
        }
      } catch (requestError) {
        onConnectionChange(false);
        setError(
          requestError instanceof Error
            ? requestError.message
            : "任务状态读取失败。",
        );
        setCurrentJob((current) => (current ? { ...current } : null));
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [apiBase, currentJob, loadAssets, onConnectionChange]);

  function chooseFile(file: File | undefined) {
    setError("");
    if (!file) return;
    if (!supportedTypes.has(file.type)) {
      setError("请选择 JPG、PNG 或 WebP 图片。");
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      setError("图片不能超过 20MB。");
      return;
    }
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = URL.createObjectURL(file);
    setPreviewUrl(previewUrlRef.current);
    setSelectedFile(file);
    setTitle(file.name.replace(/\.[^.]+$/, "").slice(0, 80));
  }

  function handleInput(event: ChangeEvent<HTMLInputElement>) {
    chooseFile(event.target.files?.[0]);
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    chooseFile(event.dataTransfer.files?.[0]);
  }

  async function createScene(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || uploading) return;
    setUploading(true);
    setError("");
    const payload = new FormData();
    payload.append("file", selectedFile);
    if (title.trim()) payload.append("title", title.trim());

    try {
      const response = await fetch(`${apiBase}/api/spatial-scenes`, {
        method: "POST",
        body: payload,
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const created = (await response.json()) as CreateResponse;
      setCurrentJob(created.job);
      setAssets((current) => [
        created.asset,
        ...current.filter((asset) => asset.id !== created.asset.id),
      ]);
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
      previewUrlRef.current = "";
      setPreviewUrl("");
      setSelectedFile(null);
      setTitle("");
      onConnectionChange(true);
      if (created.job.status === "completed") {
        await loadAssets();
        setSelectedAssetId(created.asset.id);
      } else if (created.job.status === "failed") {
        setError(created.job.error || created.job.message);
      }
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "图片上传失败。",
      );
    } finally {
      setUploading(false);
    }
  }

  async function retryJob() {
    if (!currentJob || currentJob.status !== "failed" || retrying) return;
    setRetrying(true);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/api/jobs/${encodeURIComponent(currentJob.id)}/retry`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await errorMessage(response));
      const nextJob = (await response.json()) as Job;
      setCurrentJob(nextJob);
      setAssets((current) =>
        current.map((asset) =>
          asset.id === nextJob.asset_id
            ? { ...asset, status: "processing" }
            : asset,
        ),
      );
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "重新生成失败，请稍后再试。",
      );
    } finally {
      setRetrying(false);
    }
  }

  async function deleteAsset(assetId: string) {
    if (deleteConfirmId !== assetId) {
      setDeleteConfirmId(assetId);
      return;
    }
    try {
      const response = await fetch(`${apiBase}/api/assets/${assetId}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      setDeleteConfirmId(null);
      setAssets((current) => current.filter((asset) => asset.id !== assetId));
      setSelectedAssetId((current) => (current === assetId ? null : current));
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "删除失败。",
      );
    }
  }

  const selectedAsset = assets.find((asset) => asset.id === selectedAssetId);

  return (
    <div className="spatial-studio">
      <section className="studio-hero">
        <div>
          <p className="eyebrow">Spatial photo · local AI</p>
          <h1>让一张平面照片拥有空间视角</h1>
          <p>
            本地深度模型解析远近，语义主体蒙版保留完整前景并补全遮挡背景，再用小范围相机平移呈现自然运动视差。
            图片、空间分层和生成记录只保存在这台电脑。
          </p>
        </div>
        <div className="hero-metric" aria-label="当前实现特点">
          <strong>2.5D</strong>
          <span>分层深度</span>
          <small>适合人物、宠物和有明确前景的照片</small>
        </div>
      </section>

      <div className="spatial-workspace">
        <form className="upload-card" onSubmit={createScene}>
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">01 · Create</p>
              <h2>生成空间照片</h2>
            </div>
            <span className="local-pill">仅本机</span>
          </div>

          <div
            className={`drop-zone ${dragging ? "is-dragging" : ""} ${
              previewUrl ? "has-preview" : ""
            }`}
            onDragEnter={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            {previewUrl ? (
              <>
                {/* This is a local object URL created from the user's selection. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={previewUrl} alt="待生成图片预览" />
                <div className="preview-overlay">
                  <strong>{selectedFile?.name}</strong>
                  <span>{((selectedFile?.size ?? 0) / 1024 / 1024).toFixed(1)} MB</span>
                </div>
              </>
            ) : (
              <div className="drop-copy">
                <span className="upload-symbol" aria-hidden="true">
                  +
                </span>
                <strong>拖入一张图片</strong>
                <p>JPG、PNG、WebP · 最大 20MB</p>
              </div>
            )}
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={handleInput}
            />
            <button
              type="button"
              className="drop-action"
              onClick={() => inputRef.current?.click()}
            >
              {previewUrl ? "更换图片" : "选择图片"}
            </button>
          </div>

          <label className="title-field">
            <span>资产名称</span>
            <input
              type="text"
              value={title}
              maxLength={80}
              placeholder="例如：窗边的猫"
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>

          <div className="upload-guidance">
            <span>效果建议</span>
            <p>主体轮廓清晰、景深层次明显、避免大面积透明或镜面区域。</p>
          </div>

          <button
            className="create-button"
            type="submit"
            disabled={!selectedFile || uploading}
          >
            {uploading ? "正在提交…" : "生成空间照片"}
          </button>

          <p className="first-run-note">
            首次生成会下载约 100MB 的深度模型，之后可离线使用。
          </p>
        </form>

        <section className="viewer-card">
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">02 · Explore</p>
              <h2>{selectedAsset?.name ?? "空间预览"}</h2>
            </div>
            <div className="model-chip-row">
              {selectedAsset?.model_name ? (
                <span className="model-chip">{selectedAsset.model_name}</span>
              ) : null}
              {selectedAsset?.segmentation_model ? (
                <span
                  className="model-chip"
                  title={`主体蒙版质量 ${Math.round((selectedAsset.segmentation_quality ?? 0) * 100)}%`}
                >
                  {selectedAsset.segmentation_model}
                </span>
              ) : null}
            </div>
          </div>

          {selectedAsset?.status === "ready" && selectedAsset.depth_url ? (
            <SpatialViewer
              key={selectedAsset.id}
              sourceUrl={resolveUrl(apiBase, selectedAsset.source_url)}
              depthUrl={resolveUrl(apiBase, selectedAsset.depth_url)}
              backgroundUrl={resolveUrl(
                apiBase,
                selectedAsset.background_url,
              )}
              foregroundUrl={resolveUrl(
                apiBase,
                selectedAsset.foreground_url,
              )}
              title={selectedAsset.name}
              recommendedStrength={selectedAsset.recommended_strength}
            />
          ) : (
            <div className="viewer-empty">
              <div className="depth-layers" aria-hidden="true">
                <span />
                <span />
                <span />
              </div>
              <strong>等待第一张空间照片</strong>
              <p>上传后，生成结果会自动出现在这里。</p>
            </div>
          )}
        </section>
      </div>

      {currentJob ? (
        <section
          className={`job-card ${currentJob.status}`}
          aria-live="polite"
          aria-label="生成任务进度"
        >
          <div>
            <p className="eyebrow">Local task</p>
            <strong>{currentJob.message}</strong>
            {currentJob.status === "failed" ? (
              <small>原始图片仍保存在本机，可以直接重新生成。</small>
            ) : null}
          </div>
          <div className="job-progress-actions">
            <div className="job-progress">
              <progress max="100" value={currentJob.progress} />
              <span>{currentJob.progress}%</span>
            </div>
            {currentJob.status === "failed" ? (
              <button
                type="button"
                className="job-retry-button"
                onClick={() => void retryJob()}
                disabled={retrying}
              >
                {retrying ? "重新排队中…" : "重新生成"}
              </button>
            ) : null}
          </div>
        </section>
      ) : null}

      {error ? (
        <div className="error-banner spatial-error" role="alert">
          {error}
        </div>
      ) : null}

      <section className="asset-section">
        <div className="asset-heading">
          <div>
            <p className="eyebrow">Personal library</p>
            <h2>个人资产库</h2>
          </div>
          <span>{assets.length} 个资产</span>
        </div>

        {loadingAssets ? (
          <div className="asset-loading">正在读取本地资产…</div>
        ) : assets.length ? (
          <div className="asset-grid">
            {assets.map((asset) => (
              <article
                className={`asset-card ${
                  selectedAssetId === asset.id ? "is-selected" : ""
                }`}
                key={asset.id}
              >
                <button
                  type="button"
                  className="asset-select"
                  onClick={() => {
                    if (asset.status === "ready") setSelectedAssetId(asset.id);
                  }}
                  disabled={asset.status !== "ready"}
                >
                  <span className="asset-thumb">
                    {/* This URL is served by the user's local FastAPI service. */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={resolveUrl(apiBase, asset.preview_url)}
                      alt=""
                      loading="lazy"
                    />
                    <span className={`asset-status ${asset.status}`}>
                      {asset.status === "ready"
                        ? "可查看"
                        : asset.status === "processing"
                          ? "生成中"
                          : "失败"}
                    </span>
                  </span>
                  <span className="asset-meta">
                    <strong>{asset.name}</strong>
                    <small>
                      {asset.width && asset.height
                        ? `${asset.width} × ${asset.height}`
                        : "空间照片"}
                    </small>
                  </span>
                </button>
                <button
                  type="button"
                  className={`asset-delete ${
                    deleteConfirmId === asset.id ? "confirm" : ""
                  }`}
                  aria-label={
                    deleteConfirmId === asset.id
                      ? `确认删除${asset.name}`
                      : `删除${asset.name}`
                  }
                  onClick={() => void deleteAsset(asset.id)}
                  onBlur={() => setDeleteConfirmId(null)}
                >
                  {deleteConfirmId === asset.id ? "确认删除" : "删除"}
                </button>
              </article>
            ))}
          </div>
        ) : (
          <div className="library-empty">
            <strong>这里会成为你的个人生成资产库</strong>
            <p>先从一张照片开始，之后虚拟试衣和桌面宠物也会复用同一套资产底座。</p>
          </div>
        )}
      </section>
    </div>
  );
}
