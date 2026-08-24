"use client";

import {
  ChangeEvent,
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

type StyleAsset = {
  id: string;
  kind: "photo_style_transfer";
  name: string;
  status: "processing" | "ready" | "failed";
  width: number | null;
  height: number | null;
  source_url: string;
  preview_url: string | null;
  result_url: string | null;
  style_reference_urls: string[];
  manifest_url: string | null;
  model_name: string | null;
  provider_name: string | null;
  parameters: Record<string, unknown>;
};

type StyleJob = {
  id: string;
  kind: "photo_style_transfer";
  status: "queued" | "running" | "completed" | "failed";
  progress: number;
  stage: string;
  message: string;
  asset_id: string;
  error: string | null;
};

type CreateResponse = { asset: StyleAsset; job: StyleJob };

type ProviderStatus = {
  name: string;
  model_name: string;
  ready: boolean | null;
  details: Record<string, unknown>;
};

type PhotoStyleStudioProps = {
  apiBase: string;
  onConnectionChange: (ready: boolean) => void;
  requestedAssetId?: string | null;
};

type ImagePreview = {
  url: string;
  label: string;
};

const supportedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);

function resolveUrl(apiBase: string, path: string | null) {
  if (!path) return "";
  if (/^(?:https?:|blob:|data:)/i.test(path)) return path;
  return `${apiBase.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}

async function errorMessage(response: Response) {
  try {
    const data = (await response.json()) as { detail?: string };
    return data.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function validateImage(file: File) {
  if (!supportedTypes.has(file.type)) return "请选择 JPG、PNG 或 WebP 图片。";
  if (file.size > 20 * 1024 * 1024) return "每张图片不能超过 20MB。";
  return "";
}

export function PhotoStyleStudio({
  apiBase,
  onConnectionChange,
  requestedAssetId,
}: PhotoStyleStudioProps) {
  const contentInputRef = useRef<HTMLInputElement>(null);
  const styleInputRef = useRef<HTMLInputElement>(null);
  const objectUrlsRef = useRef<string[]>([]);
  const [contentFile, setContentFile] = useState<File | null>(null);
  const [contentPreview, setContentPreview] = useState("");
  const [styleFiles, setStyleFiles] = useState<File[]>([]);
  const [stylePreviews, setStylePreviews] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("preserve_layout");
  const [quality, setQuality] = useState("standard");
  const [styleStrength, setStyleStrength] = useState(0.7);
  const [contentStrength, setContentStrength] = useState(0.8);
  const [detailStrength, setDetailStrength] = useState(0.7);
  const [assets, setAssets] = useState<StyleAsset[]>([]);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [currentJob, setCurrentJob] = useState<StyleJob | null>(null);
  const [loadingAssets, setLoadingAssets] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [providerStatus, setProviderStatus] = useState<ProviderStatus | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [imagePreview, setImagePreview] = useState<ImagePreview | null>(null);
  const [error, setError] = useState("");

  const rememberUrl = useCallback((file: File) => {
    const url = URL.createObjectURL(file);
    objectUrlsRef.current.push(url);
    return url;
  }, []);

  const loadAssets = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/api/assets`);
      if (!response.ok) throw new Error(await errorMessage(response));
      const payload = (await response.json()) as { assets: StyleAsset[] };
      const styleAssets = payload.assets.filter(
        (asset) => asset.kind === "photo_style_transfer",
      );
      setAssets(styleAssets);
      setSelectedAssetId((current) => {
        if (current && styleAssets.some((asset) => asset.id === current)) {
          return current;
        }
        return styleAssets.find((asset) => asset.status === "ready")?.id ?? null;
      });
      onConnectionChange(true);
    } catch (requestError) {
      onConnectionChange(false);
      setError(
        requestError instanceof Error
          ? `${requestError.message}。请确认本地后端已启动。`
          : "无法读取本地风格化资产。",
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
    let active = true;
    async function loadProviderStatus() {
      try {
        const response = await fetch(
          `${apiBase}/api/photo-style-transfers/provider`,
        );
        if (response.ok && active) {
          setProviderStatus((await response.json()) as ProviderStatus);
        }
      } catch {
        if (active) setProviderStatus(null);
      }
    }
    void loadProviderStatus();
    const timer = window.setInterval(() => void loadProviderStatus(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [apiBase]);

  useEffect(() => {
    if (!requestedAssetId) return;
    const timer = window.setTimeout(async () => {
      await loadAssets();
      setSelectedAssetId(requestedAssetId);
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadAssets, requestedAssetId]);

  useEffect(() => {
    const objectUrls = objectUrlsRef.current;
    return () => {
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!imagePreview) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setImagePreview(null);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [imagePreview]);

  useEffect(() => {
    if (!currentJob || !["queued", "running"].includes(currentJob.status)) return;
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/jobs/${currentJob.id}`);
        if (!response.ok) throw new Error(await errorMessage(response));
        const nextJob = (await response.json()) as StyleJob;
        setCurrentJob(nextJob);
        onConnectionChange(true);
        if (nextJob.status === "completed") {
          await loadAssets();
          setSelectedAssetId(nextJob.asset_id);
        } else if (nextJob.status === "failed") {
          setError(nextJob.error || nextJob.message);
          await loadAssets();
        }
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "任务状态读取失败。",
        );
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [apiBase, currentJob, loadAssets, onConnectionChange]);

  function chooseContent(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const validationError = validateImage(file);
    if (validationError) {
      setError(validationError);
      event.target.value = "";
      return;
    }
    setError("");
    setContentFile(file);
    setContentPreview(rememberUrl(file));
    setTitle(file.name.replace(/\.[^.]+$/, "").slice(0, 80));
  }

  function chooseStyles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []).slice(0, 3);
    if (!selected.length) return;
    const validationError = selected.map(validateImage).find(Boolean);
    if (validationError) {
      setError(validationError);
      event.target.value = "";
      return;
    }
    setError("");
    setStyleFiles(selected);
    setStylePreviews(selected.map(rememberUrl));
  }

  async function createTransfer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!contentFile || !styleFiles.length || uploading) return;
    setUploading(true);
    setError("");
    const payload = new FormData();
    payload.append("content_file", contentFile);
    styleFiles.forEach((file) => payload.append("style_files", file));
    payload.append("title", title.trim());
    payload.append("prompt", prompt.trim());
    payload.append("mode", mode);
    payload.append("quality", quality);
    payload.append("style_strength", String(styleStrength));
    payload.append("content_strength", String(contentStrength));
    payload.append("detail_strength", String(detailStrength));
    try {
      const response = await fetch(`${apiBase}/api/photo-style-transfers`, {
        method: "POST",
        body: payload,
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      const created = (await response.json()) as CreateResponse;
      setCurrentJob(created.job);
      setAssets((current) => [created.asset, ...current]);
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "图片上传失败。",
      );
    } finally {
      setUploading(false);
    }
  }

  function clearComposer() {
    setContentFile(null);
    setContentPreview("");
    setStyleFiles([]);
    setStylePreviews([]);
    setTitle("");
    setPrompt("");
    if (contentInputRef.current) contentInputRef.current.value = "";
    if (styleInputRef.current) styleInputRef.current.value = "";
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
      setAssets((current) => current.filter((asset) => asset.id !== assetId));
      setSelectedAssetId((current) => (current === assetId ? null : current));
      setDeleteConfirmId(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "删除失败。");
    }
  }

  const selectedAsset = assets.find((asset) => asset.id === selectedAssetId);
  const providerLabel = providerStatus === null
    ? "正在检测 SDXL 服务"
    : providerStatus.name === "sdxl_ip_adapter_8gb_v1"
      ? providerStatus.ready
        ? "本机 SDXL 就绪"
        : "本机 SDXL 待准备"
      : providerStatus.name === "pic-style-http"
        ? providerStatus.ready
          ? "SDXL 服务已连接"
          : "SDXL 服务未连接"
        : "开发预览 · 非生成模型";

  return (
    <div className="style-studio">
      <section className="studio-hero style-hero">
        <div>
          <p className="eyebrow">Photo style · provider neutral</p>
          <h1>把参考图的气质迁移到你的照片</h1>
          <p>
            选择一张内容图和一至三张参考图，控制风格、结构与细节保持程度。
            图片任务由独立的 SDXL + IP-Adapter GPU 服务生成；Agent 只负责安全上传、
            异步调度和结果回收。
          </p>
        </div>
        <div className="hero-metric style-metric" aria-label="图片风格化能力">
          <strong>1–3</strong>
          <span>风格参考图</span>
          <small>异步任务 · 私有资产 · 可复现参数</small>
        </div>
      </section>

      <div className="style-workspace">
        <form className="style-form-card" onSubmit={createTransfer}>
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">01 · Compose</p>
              <h2>配置风格迁移</h2>
            </div>
            <span className="local-pill" title={providerStatus?.model_name}>
              {providerLabel}
            </span>
          </div>

          <div className="style-input-grid">
            <div className="style-input-block">
              <span>内容图</span>
              <button type="button" onClick={() => contentInputRef.current?.click()}>
                {contentPreview ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={contentPreview} alt="内容图预览" />
                ) : (
                  <span className="style-input-empty">+ 选择主体照片</span>
                )}
              </button>
              <input
                ref={contentInputRef}
                className="sr-only"
                type="file"
                accept="image/jpeg,image/png,image/webp"
                onChange={chooseContent}
              />
            </div>
            <div className="style-input-block">
              <span>风格参考 · 最多 3 张</span>
              <button type="button" onClick={() => styleInputRef.current?.click()}>
                {stylePreviews.length ? (
                  <span
                    className="style-preview-stack"
                    data-count={stylePreviews.length}
                  >
                    {stylePreviews.map((preview, index) => (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={preview} alt={`风格参考 ${index + 1}`} key={preview} />
                    ))}
                  </span>
                ) : (
                  <span className="style-input-empty">+ 选择风格参考</span>
                )}
              </button>
              <input
                ref={styleInputRef}
                className="sr-only"
                type="file"
                multiple
                accept="image/jpeg,image/png,image/webp"
                onChange={chooseStyles}
              />
            </div>
          </div>

          <label className="title-field">
            <span>资产名称</span>
            <input
              value={title}
              maxLength={80}
              placeholder="例如：雨夜油画"
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>
          <label className="title-field">
            <span>补充描述（可选）</span>
            <input
              value={prompt}
              maxLength={1000}
              placeholder="例如：保留人物五官，强调厚涂笔触"
              onChange={(event) => setPrompt(event.target.value)}
            />
          </label>

          <div className="style-select-row">
            <label>
              <span>构图模式</span>
              <select value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="preserve_layout">保留布局</option>
                <option value="recompose">重新构图</option>
              </select>
            </label>
            <label>
              <span>质量</span>
              <select
                value={quality}
                onChange={(event) => setQuality(event.target.value)}
              >
                <option value="preview">预览</option>
                <option value="standard">标准</option>
                <option value="high">高质量</option>
              </select>
            </label>
          </div>

          <div className="style-sliders">
            {[
              ["风格强度", styleStrength, setStyleStrength],
              ["内容保持", contentStrength, setContentStrength],
              ["细节保持", detailStrength, setDetailStrength],
            ].map(([label, value, setter]) => (
              <label key={label as string}>
                <span>
                  {label as string} <strong>{Number(value).toFixed(2)}</strong>
                </span>
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={value as number}
                  onChange={(event) =>
                    (setter as (next: number) => void)(Number(event.target.value))
                  }
                />
              </label>
            ))}
          </div>

          <div className="style-form-actions">
            <button
              className="create-button"
              type="submit"
              disabled={
                !contentFile ||
                !styleFiles.length ||
                uploading ||
                providerStatus?.ready !== true
              }
            >
              {uploading ? "正在提交…" : "开始图片风格化"}
            </button>
            <button
              className="style-clear-button"
              type="button"
              disabled={
                uploading || (!contentFile && !styleFiles.length && !title && !prompt)
              }
              onClick={clearComposer}
            >
              清空本次配置
            </button>
          </div>
          <p className="style-retain-note">
            提交后会保留当前图片、名称和描述，便于调整参数后继续生成。
          </p>
          <p className="first-run-note">
            {providerStatus === null
              ? "正在检查独立 SDXL + IP-Adapter 服务。"
              : providerStatus.name === "sdxl_ip_adapter_8gb_v1"
              ? providerStatus.ready
                ? "真实模型只读取本机固定版本权重，运行时不会联网下载。"
                : "已选择真实模型；请先安装 GPU 依赖并完成模型许可与完整性准备。"
              : providerStatus.name === "pic-style-http"
                ? providerStatus.ready
                  ? "独立 SDXL + IP-Adapter 服务健康检查已通过。"
                  : "独立 GPU 服务未就绪；请启动 frogi-m/pic-style API 与单并发 Worker。"
                : "当前是显式开发预览模式，不代表真实生成式风格迁移效果。"}
          </p>
        </form>

        <section className="style-result-card">
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">02 · Compare</p>
              <h2>{selectedAsset?.name ?? "风格化结果"}</h2>
            </div>
            {selectedAsset?.provider_name ? (
              <span className="model-chip">{selectedAsset.provider_name}</span>
            ) : null}
          </div>
          {selectedAsset?.status === "ready" && selectedAsset.result_url ? (
            <div className="style-comparison">
              <figure>
                <button
                  className="style-image-preview"
                  type="button"
                  aria-label="完整预览原图"
                  onClick={() =>
                    setImagePreview({
                      url: resolveUrl(apiBase, selectedAsset.source_url),
                      label: "原图",
                    })
                  }
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={resolveUrl(apiBase, selectedAsset.source_url)} alt="原内容图" />
                  <span>点击完整预览</span>
                </button>
                <figcaption>原图</figcaption>
              </figure>
              <figure>
                <button
                  className="style-image-preview"
                  type="button"
                  aria-label="完整预览风格化结果"
                  onClick={() =>
                    setImagePreview({
                      url: resolveUrl(apiBase, selectedAsset.result_url),
                      label: "风格化结果",
                    })
                  }
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={resolveUrl(apiBase, selectedAsset.result_url)} alt="风格化结果" />
                  <span>点击完整预览</span>
                </button>
                <figcaption>风格化</figcaption>
              </figure>
              <a
                className="style-download"
                href={resolveUrl(apiBase, selectedAsset.result_url)}
                download={`${selectedAsset.name}.webp`}
              >
                下载结果
              </a>
            </div>
          ) : (
            <div className="viewer-empty style-empty">
              <span className="style-orbit" aria-hidden="true">✦</span>
              <strong>等待第一张风格化图片</strong>
              <p>结果会和原图并排显示，方便检查内容保持程度。</p>
            </div>
          )}
        </section>
      </div>

      {currentJob ? (
        <section className={`job-card ${currentJob.status}`} aria-live="polite">
          <div>
            <p className="eyebrow">Style task</p>
            <strong>{currentJob.message}</strong>
          </div>
          <div className="job-progress">
            <progress max="100" value={currentJob.progress} />
            <span>{currentJob.progress}%</span>
          </div>
        </section>
      ) : null}

      {error ? <div className="error-banner spatial-error" role="alert">{error}</div> : null}

      <section className="asset-section">
        <div className="asset-heading">
          <div>
            <p className="eyebrow">Styled library</p>
            <h2>风格化资产</h2>
          </div>
          <span>{assets.length} 个结果</span>
        </div>
        {loadingAssets ? (
          <div className="asset-loading">正在读取本地资产…</div>
        ) : assets.length ? (
          <div className="asset-grid">
            {assets.map((asset) => (
              <article
                className={`asset-card ${selectedAssetId === asset.id ? "is-selected" : ""}`}
                key={asset.id}
              >
                <button
                  type="button"
                  className="asset-select"
                  disabled={asset.status !== "ready"}
                  aria-label={`预览风格化资产：${asset.name}`}
                  onClick={() => {
                    setSelectedAssetId(asset.id);
                    const previewUrl = asset.result_url ?? asset.preview_url;
                    if (previewUrl) {
                      setImagePreview({
                        url: resolveUrl(apiBase, previewUrl),
                        label: `${asset.name} · 风格化结果`,
                      });
                    }
                  }}
                >
                  <span className="asset-thumb">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={resolveUrl(
                        apiBase,
                        asset.preview_url ?? asset.result_url,
                      )}
                      alt=""
                      loading="lazy"
                    />
                    {asset.status === "ready" ? (
                      <span className="asset-preview-hint">点击预览</span>
                    ) : null}
                    <span className={`asset-status ${asset.status}`}>
                      {asset.status === "ready"
                        ? "已完成"
                        : asset.status === "processing"
                          ? "生成中"
                          : "失败"}
                    </span>
                  </span>
                  <span className="asset-meta">
                    <strong>{asset.name}</strong>
                    <small>{asset.provider_name ?? "图片风格化"}</small>
                  </span>
                </button>
                <button
                  type="button"
                  className={`asset-delete ${deleteConfirmId === asset.id ? "confirm" : ""}`}
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
            <strong>还没有风格化结果</strong>
            <p>选择内容图与参考图，生成结果会保存在本机个人资产库。</p>
          </div>
        )}
      </section>

      {imagePreview ? (
        <div
          className="style-lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={`${imagePreview.label}完整预览`}
          onClick={() => setImagePreview(null)}
        >
          <button
            className="style-lightbox-close"
            type="button"
            aria-label="关闭图片预览"
            onClick={() => setImagePreview(null)}
          >
            ×
          </button>
          <figure onClick={(event) => event.stopPropagation()}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={imagePreview.url} alt={imagePreview.label} />
            <figcaption>{imagePreview.label} · 按 Esc 关闭</figcaption>
          </figure>
        </div>
      ) : null}
    </div>
  );
}
