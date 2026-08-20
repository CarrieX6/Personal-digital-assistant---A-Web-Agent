"use client";

import {
  ChangeEvent,
  FormEvent,
  useEffect,
  useRef,
  useState,
} from "react";

export type HealthInfo = {
  status: "ok";
  agent_mode: string;
  llm_configured: boolean;
  model?: string | null;
  tool_count: number;
};

type TraceStep = {
  index: number;
  stage: "planning" | "tool" | "final";
  label: string;
  detail: string;
  duration_ms: number;
  output?: Record<string, unknown> | null;
};

type AgentRun = {
  run_id: string;
  status: "completed" | "failed";
  mode: string;
  answer: string;
  steps: TraceStep[];
  total_duration_ms: number;
};

type AgentConsoleProps = {
  apiBase: string;
  health: HealthInfo | null;
  onConnectionChange: (ready: boolean) => void;
  onSpatialSceneReady: (assetId: string) => void;
  onPhotoStyleReady: (assetId: string) => void;
};

const examples = [
  {
    label: "生成空间照片",
    message: "把这张图片生成可拖动视角的空间照片",
  },
  {
    label: "图片风格化",
    message: "使用第 1 张图片作为内容图，其余图片作为风格参考，进行图片风格化",
  },
  {
    label: "组合分析",
    message:
      "分析这段文字：医学人工智能正在改变影像诊断流程，模型评估与数据质量同样重要。",
  },
  { label: "个人资产", message: "查看我的个人资产" },
  { label: "时间", message: "现在几点？" },
];

type LocalAttachment = {
  file: File;
  previewUrl: string;
};

function AttachmentIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path
        d="M8.5 12.5 14.8 6.2a3 3 0 0 1 4.2 4.2l-8.1 8.1a5 5 0 0 1-7.1-7.1l8.1-8.1"
        fill="none"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

type SourceImage = {
  id: string;
  original_name: string;
  width: number;
  height: number;
  size_bytes: number;
};

type AgentJob = {
  id: string;
  kind?: "spatial_scene" | "photo_style_transfer";
  status: "queued" | "running" | "completed" | "failed";
  progress: number;
  message: string;
  asset_id: string;
  error: string | null;
};

const stageName: Record<TraceStep["stage"], string> = {
  planning: "规划",
  tool: "工具",
  final: "回答",
};

export function AgentConsole({
  apiBase,
  health,
  onConnectionChange,
  onSpatialSceneReady,
  onPhotoStyleReady,
}: AgentConsoleProps) {
  const contentInputRef = useRef<HTMLInputElement>(null);
  const styleInputRef = useRef<HTMLInputElement>(null);
  const previewUrlRef = useRef<string[]>([]);
  const [message, setMessage] = useState(examples[2].message);
  const [contentAttachment, setContentAttachment] =
    useState<LocalAttachment | null>(null);
  const [styleAttachments, setStyleAttachments] = useState<LocalAttachment[]>([]);
  const [result, setResult] = useState<AgentRun | null>(null);
  const [job, setJob] = useState<AgentJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    return () => {
      previewUrlRef.current.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/jobs/${job.id}`);
        if (!response.ok) throw new Error(await responseError(response));
        const nextJob = (await response.json()) as AgentJob;
        setJob(nextJob);
        onConnectionChange(true);
        if (nextJob.status === "completed") {
          if (nextJob.kind === "photo_style_transfer") {
            onPhotoStyleReady(nextJob.asset_id);
          } else {
            onSpatialSceneReady(nextJob.asset_id);
          }
        } else if (nextJob.status === "failed") {
          setError(nextJob.error || nextJob.message);
        }
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "无法读取 Agent 图片任务状态。",
        );
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [
    apiBase,
    job,
    onConnectionChange,
    onPhotoStyleReady,
    onSpatialSceneReady,
  ]);

  function validateAttachments(files: File[], maxCount: number) {
    if (files.length > maxCount) {
      return maxCount === 1
        ? "内容图只能选择一张。"
        : "风格参考图最多选择三张。";
    }
    if (
      files.some(
        (file) => !["image/jpeg", "image/png", "image/webp"].includes(file.type),
      )
    ) {
      return "附件仅支持 JPG、PNG 或 WebP 图片。";
    }
    if (files.some((file) => file.size > 20 * 1024 * 1024)) {
      return "每张图片附件不能超过 20MB。";
    }
    return "";
  }

  function releasePreview(attachment: LocalAttachment) {
    URL.revokeObjectURL(attachment.previewUrl);
    previewUrlRef.current = previewUrlRef.current.filter(
      (url) => url !== attachment.previewUrl,
    );
  }

  function chooseContentAttachment(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    setError("");
    if (!files.length) return;
    const validationError = validateAttachments(files, 1);
    if (validationError) {
      setError(validationError);
      event.target.value = "";
      return;
    }
    if (contentAttachment) releasePreview(contentAttachment);
    const nextAttachment = {
      file: files[0],
      previewUrl: URL.createObjectURL(files[0]),
    };
    previewUrlRef.current.push(nextAttachment.previewUrl);
    setContentAttachment(nextAttachment);
    setMessage(
      styleAttachments.length ? examples[1].message : examples[0].message,
    );
    event.target.value = "";
  }

  function chooseStyleAttachments(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    setError("");
    if (!files.length) return;
    const validationError = validateAttachments(files, 3);
    if (validationError) {
      setError(validationError);
      event.target.value = "";
      return;
    }
    styleAttachments.forEach(releasePreview);
    const nextAttachments = files.map((file) => ({
      file,
      previewUrl: URL.createObjectURL(file),
    }));
    previewUrlRef.current.push(
      ...nextAttachments.map((attachment) => attachment.previewUrl),
    );
    setStyleAttachments(nextAttachments);
    setMessage(examples[1].message);
    event.target.value = "";
  }

  function clearAttachments() {
    previewUrlRef.current.forEach((url) => URL.revokeObjectURL(url));
    previewUrlRef.current = [];
    setContentAttachment(null);
    setStyleAttachments([]);
    if (contentInputRef.current) contentInputRef.current.value = "";
    if (styleInputRef.current) styleInputRef.current.value = "";
  }

  function removeContentAttachment() {
    if (!contentAttachment) return;
    releasePreview(contentAttachment);
    setContentAttachment(null);
  }

  function removeStyleAttachment(index: number) {
    const selected = styleAttachments[index];
    if (selected) releasePreview(selected);
    const nextAttachments = styleAttachments.filter(
      (_, itemIndex) => itemIndex !== index,
    );
    setStyleAttachments(nextAttachments);
    if (
      nextAttachments.length === 0 &&
      contentAttachment &&
      message === examples[1].message
    ) {
      setMessage(examples[0].message);
    }
  }

  async function runAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!message.trim() || loading) return;
    if (styleAttachments.length && !contentAttachment) {
      setError("请先选择内容图，再运行图片风格化任务。");
      return;
    }

    setLoading(true);
    setError("");
    setResult(null);
    setJob(null);
    const stagedImages: SourceImage[] = [];
    const orderedAttachments = [
      ...(contentAttachment ? [contentAttachment] : []),
      ...styleAttachments,
    ];

    try {
      for (const attachment of orderedAttachments) {
        const uploadPayload = new FormData();
        uploadPayload.append("file", attachment.file);
        const uploadResponse = await fetch(`${apiBase}/api/source-images`, {
          method: "POST",
          body: uploadPayload,
        });
        if (!uploadResponse.ok) {
          throw new Error(await responseError(uploadResponse));
        }
        stagedImages.push((await uploadResponse.json()) as SourceImage);
      }
      const response = await fetch(`${apiBase}/api/agent/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: message.trim(),
          source_image_id: stagedImages[0]?.id,
          style_image_ids: stagedImages.slice(1).map((image) => image.id),
        }),
      });
      if (!response.ok) {
        throw new Error(await responseError(response));
      }
      const nextResult = (await response.json()) as AgentRun;
      setResult(nextResult);
      const sceneOutput = nextResult.steps
        .map((step) => step.output)
        .find(
          (output) =>
            typeof output?.job_id === "string" &&
            typeof output?.asset_id === "string",
        );
      if (sceneOutput) {
        const jobKind =
          sceneOutput.kind === "photo_style_transfer"
            ? "photo_style_transfer"
            : "spatial_scene";
        setJob({
          id: sceneOutput.job_id as string,
          kind: jobKind,
          asset_id: sceneOutput.asset_id as string,
          status:
            sceneOutput.status === "running" ? "running" : "queued",
          progress:
            typeof sceneOutput.progress === "number"
              ? sceneOutput.progress
              : 0,
          message:
            typeof sceneOutput.message === "string"
              ? sceneOutput.message
              : jobKind === "photo_style_transfer"
                ? "图片风格化任务已创建。"
                : "空间照片任务已创建。",
          error: null,
        });
        if (jobKind === "spatial_scene") {
          await deleteSourceImages(apiBase, stagedImages.slice(1));
        }
        clearAttachments();
      } else {
        await deleteSourceImages(apiBase, stagedImages);
      }
      onConnectionChange(true);
    } catch (requestError) {
      onConnectionChange(false);
      await deleteSourceImages(apiBase, stagedImages);
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法连接本地后端。",
      );
    } finally {
      setLoading(false);
    }
  }

  const modeLabel = health?.llm_configured
    ? `LLM · ${health.model}`
    : "Demo planner";

  return (
    <div className="agent-console">
      <section className="composer-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Agent console</p>
            <h2>运行个人助手任务</h2>
          </div>
          <span className={`mode-pill ${health?.llm_configured ? "llm" : ""}`}>
            {modeLabel}
          </span>
        </div>

        <form onSubmit={runAgent}>
          <label className="sr-only" htmlFor="agent-task">
            输入 Agent 任务
          </label>
          <textarea
            id="agent-task"
            value={message}
            maxLength={4000}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="例如：查看我的个人资产"
            rows={5}
          />
          <div className="agent-attachment-row">
            <input
              ref={contentInputRef}
              className="sr-only"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              aria-label="内容图文件输入"
              onChange={chooseContentAttachment}
            />
            <input
              ref={styleInputRef}
              className="sr-only"
              type="file"
              multiple
              accept="image/jpeg,image/png,image/webp"
              aria-label="风格参考图文件输入"
              onChange={chooseStyleAttachments}
            />
            {contentAttachment || styleAttachments.length ? (
              <div className="attachment-groups">
                {contentAttachment ? (
                  <div className="attachment-group content-group">
                    <span className="attachment-group-label">内容图</span>
                    <div
                      className="attachment-chip content"
                      key={contentAttachment.previewUrl}
                    >
                      {/* Local object URL; the image has not left this device. */}
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img
                        src={contentAttachment.previewUrl}
                        alt="内容图片预览"
                      />
                      <div>
                        <span className="attachment-role">内容图</span>
                        <strong>{contentAttachment.file.name}</strong>
                        <span>
                          {(contentAttachment.file.size / 1024 / 1024).toFixed(1)} MB · 仅本机
                        </span>
                      </div>
                      <button
                        type="button"
                        onClick={removeContentAttachment}
                        aria-label={`移除内容图 ${contentAttachment.file.name}`}
                      >
                        移除
                      </button>
                    </div>
                  </div>
                ) : null}
                {styleAttachments.length ? (
                  <div className="attachment-group style-group">
                    <span className="attachment-group-label">
                      风格参考图 · {styleAttachments.length}/3
                    </span>
                    <div
                      className="attachment-style-list"
                      data-count={styleAttachments.length}
                    >
                      {styleAttachments.map((attachment, index) => (
                        <div
                          className="attachment-chip"
                          key={attachment.previewUrl}
                        >
                          {/* Local object URL; the image has not left this device. */}
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={attachment.previewUrl}
                            alt="风格参考图片预览"
                          />
                          <div>
                            <span className="attachment-role">
                              风格参考 {index + 1}
                            </span>
                            <strong>{attachment.file.name}</strong>
                            <span>
                              {(attachment.file.size / 1024 / 1024).toFixed(1)} MB · 仅本机
                            </span>
                          </div>
                          <button
                            type="button"
                            onClick={() => removeStyleAttachment(index)}
                            aria-label={`移除风格参考图 ${attachment.file.name}`}
                          >
                            移除
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            ) : null}
            <div className="attachment-actions">
              <button
                className="attach-button"
                type="button"
                onClick={() => contentInputRef.current?.click()}
              >
                <AttachmentIcon />
                {contentAttachment ? "更换内容图" : "选择内容图"}
              </button>
              <button
                className="attach-button secondary"
                type="button"
                onClick={() => styleInputRef.current?.click()}
              >
                <AttachmentIcon />
                {styleAttachments.length
                  ? `更换风格参考（${styleAttachments.length}/3）`
                  : "选择风格参考（可多选）"}
              </button>
            </div>
            <span className="attachment-help">
              内容图与风格参考分开选择，角色不会受文件排序影响；参考图最多 3 张。图片不会发送给大模型。
            </span>
          </div>
          <div className="composer-footer">
            <span>{message.length} / 4000</span>
            <button type="submit" disabled={loading || !message.trim()}>
              {loading ? "执行中…" : "运行任务"}
            </button>
          </div>
        </form>

        <div className="examples">
          <span>示例</span>
          {examples.map((example) => (
            <button
              key={example.label}
              type="button"
              onClick={() => setMessage(example.message)}
            >
              {example.label}
            </button>
          ))}
        </div>
      </section>

      {error ? (
        <div className="error-banner" role="alert">
          {error}
        </div>
      ) : null}

      {job ? (
        <section
          className={`agent-job-card ${job.status}`}
          aria-live="polite"
          aria-label={`Agent ${job.kind === "photo_style_transfer" ? "图片风格化" : "空间照片"}任务进度`}
        >
          <div>
            <p className="eyebrow">
              Agent → {job.kind === "photo_style_transfer" ? "Photo style" : "Spatial photo"}
            </p>
            <strong>{job.message}</strong>
            <span>
              {job.status === "completed"
                ? job.kind === "photo_style_transfer"
                  ? "生成完成，正在打开风格化结果"
                  : "生成完成，正在打开空间照片"
                : job.kind === "photo_style_transfer"
                  ? "风格编码与 SDXL 生成均在本机执行"
                  : "深度估计与分层均在本机执行"}
            </span>
          </div>
          <div className="job-progress">
            <progress max="100" value={job.progress} />
            <span>{job.progress}%</span>
          </div>
        </section>
      ) : null}

      <section className="result-grid" aria-live="polite">
        <div className="trace-card">
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">Execution trace</p>
              <h2>执行轨迹</h2>
            </div>
            {result ? (
              <span className="duration">{result.total_duration_ms} ms</span>
            ) : null}
          </div>

          {result ? (
            <ol className="trace-list">
              {result.steps.map((step) => (
                <li key={`${step.index}-${step.label}`}>
                  <span className={`trace-index ${step.stage}`}>{step.index}</span>
                  <div>
                    <div className="trace-title">
                      <strong>{step.label}</strong>
                      <span>
                        {stageName[step.stage]} · {step.duration_ms} ms
                      </span>
                    </div>
                    <p>{step.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <div className="empty-state">
              <span>1 → 2 → 3</span>
              <p>运行任务后，这里会展示 Agent 如何规划并调用工具。</p>
            </div>
          )}
        </div>

        <div className="answer-card">
          <div className="section-heading compact">
            <div>
              <p className="eyebrow">Final answer</p>
              <h2>最终回答</h2>
            </div>
            {result ? <span className="success-pill">完成</span> : null}
          </div>
          {result ? (
            <p className="answer-text">{result.answer}</p>
          ) : (
            <div className="empty-state small">
              <p>Agent 整合工具结果后，会在这里生成回答。</p>
            </div>
          )}
          {result ? <code className="run-id">run_id: {result.run_id}</code> : null}
        </div>
      </section>
    </div>
  );
}

async function responseError(response: Response) {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

async function deleteSourceImages(apiBase: string, images: SourceImage[]) {
  await Promise.allSettled(
    images.map((image) =>
      fetch(`${apiBase}/api/source-images/${image.id}`, { method: "DELETE" }),
    ),
  );
}
