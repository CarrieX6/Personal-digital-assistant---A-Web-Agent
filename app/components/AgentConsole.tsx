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
};

const examples = [
  "把这张图片生成可拖动视角的空间照片",
  "分析这段文字：医学人工智能正在改变影像诊断流程，模型评估与数据质量同样重要。",
  "查看我的个人资产",
  "现在几点？",
];

type SourceImage = {
  id: string;
  original_name: string;
  width: number;
  height: number;
  size_bytes: number;
};

type AgentJob = {
  id: string;
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
}: AgentConsoleProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const previewUrlRef = useRef("");
  const [message, setMessage] = useState(examples[1]);
  const [attachment, setAttachment] = useState<File | null>(null);
  const [attachmentPreview, setAttachmentPreview] = useState("");
  const [result, setResult] = useState<AgentRun | null>(null);
  const [job, setJob] = useState<AgentJob | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    return () => {
      if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
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
          onSpatialSceneReady(nextJob.asset_id);
        } else if (nextJob.status === "failed") {
          setError(nextJob.error || nextJob.message);
        }
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "无法读取空间照片任务状态。",
        );
      }
    }, 800);
    return () => window.clearTimeout(timer);
  }, [apiBase, job, onConnectionChange, onSpatialSceneReady]);

  function chooseAttachment(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    setError("");
    if (!file) return;
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      setError("附件仅支持 JPG、PNG 或 WebP 图片。");
      event.target.value = "";
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      setError("图片附件不能超过 20MB。");
      event.target.value = "";
      return;
    }
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = URL.createObjectURL(file);
    setAttachmentPreview(previewUrlRef.current);
    setAttachment(file);
    setMessage(examples[0]);
  }

  function clearAttachment() {
    if (previewUrlRef.current) URL.revokeObjectURL(previewUrlRef.current);
    previewUrlRef.current = "";
    setAttachmentPreview("");
    setAttachment(null);
    if (inputRef.current) inputRef.current.value = "";
  }

  async function runAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!message.trim() || loading) return;

    setLoading(true);
    setError("");
    setResult(null);
    setJob(null);
    let sourceImage: SourceImage | null = null;

    try {
      if (attachment) {
        const uploadPayload = new FormData();
        uploadPayload.append("file", attachment);
        const uploadResponse = await fetch(`${apiBase}/api/source-images`, {
          method: "POST",
          body: uploadPayload,
        });
        if (!uploadResponse.ok) {
          throw new Error(await responseError(uploadResponse));
        }
        sourceImage = (await uploadResponse.json()) as SourceImage;
      }
      const response = await fetch(`${apiBase}/api/agent/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: message.trim(),
          source_image_id: sourceImage?.id,
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
        setJob({
          id: sceneOutput.job_id as string,
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
              : "空间照片任务已创建。",
          error: null,
        });
        clearAttachment();
      } else if (sourceImage) {
        await fetch(`${apiBase}/api/source-images/${sourceImage.id}`, {
          method: "DELETE",
        });
      }
      onConnectionChange(true);
    } catch (requestError) {
      onConnectionChange(false);
      if (sourceImage) {
        void fetch(`${apiBase}/api/source-images/${sourceImage.id}`, {
          method: "DELETE",
        });
      }
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
              ref={inputRef}
              className="sr-only"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={chooseAttachment}
            />
            {attachment ? (
              <div className="attachment-chip">
                {/* Local object URL; the image has not left this device. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={attachmentPreview} alt="待处理附件预览" />
                <div>
                  <strong>{attachment.name}</strong>
                  <span>
                    {(attachment.size / 1024 / 1024).toFixed(1)} MB · 仅本机
                  </span>
                </div>
                <button
                  type="button"
                  onClick={clearAttachment}
                  aria-label="移除图片附件"
                >
                  移除
                </button>
              </div>
            ) : (
              <button
                className="attach-button"
                type="button"
                onClick={() => inputRef.current?.click()}
              >
                <svg
                  viewBox="0 0 24 24"
                  width="16"
                  height="16"
                  aria-hidden="true"
                >
                  <path
                    d="M8.5 12.5 14.8 6.2a3 3 0 0 1 4.2 4.2l-8.1 8.1a5 5 0 0 1-7.1-7.1l8.1-8.1"
                    fill="none"
                    stroke="currentColor"
                    strokeLinecap="round"
                    strokeWidth="1.8"
                  />
                </svg>
                添加本地图片
              </button>
            )}
            <span>图片不会发送给大模型，Agent 只获得临时资产 ID</span>
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
          {examples.map((example, index) => (
            <button key={example} type="button" onClick={() => setMessage(example)}>
              {index === 0
                ? "生成空间照片"
                : index === 1
                  ? "组合分析"
                  : index === 2
                    ? "个人资产"
                    : "时间"}
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
          aria-label="Agent 空间照片任务进度"
        >
          <div>
            <p className="eyebrow">Agent → Spatial photo</p>
            <strong>{job.message}</strong>
            <span>
              {job.status === "completed"
                ? "生成完成，正在打开空间照片"
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
