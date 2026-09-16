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

type CreateResponse = { asset: StyleAsset; job: StyleJob; reused?: boolean };

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

type PendingSubmission = {
  fingerprint: string;
  idempotencyKey: string;
};

const supportedTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const stylePresetHints: Record<string, string> = {
  auto: "自动读取参考图的色彩、材质与笔触。",
  ink_wash: "强化宣纸、墨色层次、飞白与留白，抑制写实和油画质感。",
  cyberpunk: "强化青紫霓虹、湿地反射、暗部层次与未来材质。",
  oil_painting: "强化画布纹理、分层颜料和自然厚涂笔触。",
  post_impressionist: "强化顺应结构的旋转笔触、钴蓝与金黄厚涂。",
  watercolor: "强化透明叠染、纸张颗粒和自然水痕。",
  anime: "强化清晰轮廓、可控赛璐璐明暗与动画配色。",
  cinematic: "强化动机光、电影反差、胶片颗粒与统一调色。",
};

function inferStylePreset(files: File[]) {
  const names = files.map((file) => file.name.toLowerCase()).join(" ");
  if (/(水墨|国画|ink.?wash|sumi)/i.test(names)) return "ink_wash";
  if (/(赛博|cyber|neon|霓虹)/i.test(names)) return "cyberpunk";
  if (/(梵高|van.?gogh|后印象|post.?impression)/i.test(names)) {
    return "post_impressionist";
  }
  if (/(水彩|watercolor|watercolour)/i.test(names)) return "watercolor";
  if (/(油画|oil.?paint)/i.test(names)) return "oil_painting";
  if (/(动漫|动画|anime|manga)/i.test(names)) return "anime";
  if (/(电影|cinematic|film.?still)/i.test(names)) return "cinematic";
  return "auto";
}

function resolveUrl(apiBase: string, path: string | null) {
  if (!path) return "";
  if (/^(?:https?:|blob:|data:)/i.test(path)) return path;
  return `${apiBase.replace(/\/+$/, "")}/${path.replace(/^\/+/, "")}`;
}

function newIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `web-${Date.now()}-${Math.random().toString(36).slice(2)}`;
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
  const jobCardRef = useRef<HTMLElement>(null);
  const objectUrlsRef = useRef<string[]>([]);
  const submissionLockRef = useRef(false);
  const pendingSubmissionRef = useRef<PendingSubmission | null>(null);
  const [contentFile, setContentFile] = useState<File | null>(null);
  const [contentPreview, setContentPreview] = useState("");
  const [styleFiles, setStyleFiles] = useState<File[]>([]);
  const [stylePreviews, setStylePreviews] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [mode, setMode] = useState("preserve_layout");
  const [quality, setQuality] = useState("standard");
  const [stylePreset, setStylePreset] = useState("auto");
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
  const [submissionNotice, setSubmissionNotice] = useState("");
  const currentJobActive = Boolean(
    currentJob && ["queued", "running"].includes(currentJob.status),
  );

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
          setSubmissionNotice("图片风格化已完成，可以在下方资产库中预览结果。");
          await loadAssets();
          setSelectedAssetId(nextJob.asset_id);
        } else if (nextJob.status === "failed") {
          setSubmissionNotice("任务执行失败，没有创建新的重复任务。");
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
    setStylePreset(inferStylePreset(selected));
  }

  async function createTransfer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      !contentFile ||
      !styleFiles.length ||
      uploading ||
      currentJobActive ||
      submissionLockRef.current
    ) {
      return;
    }
    submissionLockRef.current = true;
    setUploading(true);
    setError("");
    setSubmissionNotice("正在上传图片并创建任务，请勿重复点击。");
    const payload = new FormData();
    payload.append("content_file", contentFile);
    styleFiles.forEach((file) => payload.append("style_files", file));
    payload.append("title", title.trim());
    payload.append("prompt", prompt.trim());
    payload.append("mode", mode);
    payload.append("quality", quality);
    payload.append("style_preset", stylePreset);
    payload.append("style_strength", String(styleStrength));
    payload.append("content_strength", String(contentStrength));
    payload.append("detail_strength", String(detailStrength));
    const fingerprint = JSON.stringify({
      content: [
        contentFile.name,
        contentFile.size,
        contentFile.lastModified,
        contentFile.type,
      ],
      styles: styleFiles.map((file) => [
        file.name,
        file.size,
        file.lastModified,
        file.type,
      ]),
      title: title.trim(),
      prompt: prompt.trim(),
      mode,
      quality,
      stylePreset,
      styleStrength,
      contentStrength,
      detailStrength,
    });
    const previousSubmission = pendingSubmissionRef.current;
    const idempotencyKey =
      previousSubmission?.fingerprint === fingerprint
        ? previousSubmission.idempotencyKey
        : newIdempotencyKey();
    pendingSubmissionRef.current = { fingerprint, idempotencyKey };
    try {
      const response = await fetch(`${apiBase}/api/photo-style-transfers`, {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: payload,
      });
      if (!response.ok) {
        if (
          response.status < 500 &&
          ![408, 425, 429].includes(response.status)
        ) {
          pendingSubmissionRef.current = null;
        }
        throw new Error(await errorMessage(response));
      }
      const created = (await response.json()) as CreateResponse;
      pendingSubmissionRef.current = null;
      setCurrentJob(created.job);
      setAssets((current) => [
        created.asset,
        ...current.filter((asset) => asset.id !== created.asset.id),
      ]);
      setSubmissionNotice(
        created.reused
          ? "检测到重复提交，已继续显示原任务，没有重复创建。"
          : "任务已创建，正在本机队列中生成。完成前不能重复提交。",
      );
      onConnectionChange(true);
      window.requestAnimationFrame(() => {
        jobCardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "图片上传失败。",
      );
      setSubmissionNotice(
        pendingSubmissionRef.current
          ? "提交结果暂未确认。再次点击会安全续接同一请求，不会重复创建任务。"
          : "任务未创建，请检查配置后重试。",
      );
    } finally {
      submissionLockRef.current = false;
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
    setStylePreset("auto");
    pendingSubmissionRef.current = null;
    if (!currentJobActive) setSubmissionNotice("");
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
  const upstreamProvider =
    typeof providerStatus?.details.upstream_provider === "string"
      ? providerStatus.details.upstream_provider
      : null;
  const productionQuality =
    providerStatus?.details.production_quality === true;
  const providerLabel = providerStatus === null
    ? "正在检测 SDXL 服务"
    : providerStatus.name === "sdxl_ip_adapter_8gb_v1"
      ? providerStatus.ready
        ? productionQuality
          ? "本机 SDXL 生产就绪"
          : "本机 SDXL 工程就绪 · 待验收"
        : "本机 SDXL 待准备"
      : providerStatus.name === "pic-style-http"
        ? providerStatus.ready
          ? productionQuality
            ? "SDXL 服务已连接"
            : "测试服务已连接"
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
        <form
          className="style-form-card"
          onSubmit={createTransfer}
          aria-busy={uploading || currentJobActive}
        >
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

          <div className="style-select-row style-select-row-three">
            <label>
              <span>风格预设</span>
              <select
                value={stylePreset}
                onChange={(event) => setStylePreset(event.target.value)}
              >
                <option value="auto">自动识别</option>
                <option value="ink_wash">中国水墨</option>
                <option value="cyberpunk">赛博朋克</option>
                <option value="oil_painting">经典油画</option>
                <option value="post_impressionist">后印象派笔触</option>
                <option value="watercolor">透明水彩</option>
                <option value="anime">动漫插画</option>
                <option value="cinematic">电影质感</option>
              </select>
            </label>
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
          <p className="style-preset-note">{stylePresetHints[stylePreset]}</p>

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
                currentJobActive ||
                providerStatus?.ready !== true
              }
            >
              {uploading
                ? "正在上传并创建任务…"
                : currentJobActive
                  ? "当前任务生成中…"
                  : "开始图片风格化"}
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
          {submissionNotice ? (
            <p className="style-submit-status" role="status" aria-live="polite">
              {uploading || currentJobActive ? (
                <span className="style-submit-spinner" aria-hidden="true" />
              ) : null}
              <span>{submissionNotice}</span>
            </p>
          ) : null}
          <p className="style-retain-note">
            提交后会保留当前图片、名称和描述，便于调整参数后继续生成。
          </p>
          <p className="first-run-note">
            {providerStatus === null
              ? "正在检查独立 SDXL + IP-Adapter 服务。"
              : providerStatus.name === "sdxl_ip_adapter_8gb_v1"
              ? providerStatus.ready
                ? productionQuality
                  ? "真实模型只读取本机固定版本权重，且已通过当前设备质量门禁。"
                  : "固定权重与加速器预检已通过，但当前设备质量门禁仍未完成，不代表生产效果。"
                : "已选择真实模型；请先安装 GPU 依赖并完成模型许可与完整性准备。"
              : providerStatus.name === "pic-style-http"
                ? providerStatus.ready
                  ? productionQuality
                    ? `独立 ${upstreamProvider ?? "SDXL + IP-Adapter"} 服务已通过健康检查。`
                    : `当前连接 ${upstreamProvider ?? "Fake Provider"}，只验证上传、任务、回传和重试链路，不代表真实风格化效果。`
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
        <section
          ref={jobCardRef}
          className={`job-card ${currentJob.status}`}
          aria-live="polite"
        >
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
