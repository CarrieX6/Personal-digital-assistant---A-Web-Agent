"use client";

import {
  ChangeEvent,
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AppIcon } from "./AppIcon";

export type HealthInfo = {
  status: "ok";
  agent_mode: string;
  llm_configured: boolean;
  llm_status?:
    | "disabled"
    | "unconfigured"
    | "configured_not_enabled"
    | "testing"
    | "ready"
    | "degraded"
    | "error";
  llm_provider?: string | null;
  model?: string | null;
  tool_count: number;
  feishu_status?:
    | "disabled"
    | "starting"
    | "connected"
    | "reconnecting"
    | "error";
};

type TraceStep = {
  index: number;
  stage:
    | "planning"
    | "policy"
    | "approval"
    | "tool"
    | "decision"
    | "final";
  label: string;
  detail: string;
  duration_ms: number;
  output?: Record<string, unknown> | null;
};

type AgentRun = {
  run_id: string;
  status: "waiting_approval" | "completed" | "failed";
  mode: string;
  answer: string;
  steps: TraceStep[];
  total_duration_ms: number;
  approval?: {
    tool?: string;
    description?: string;
    risk_level?: string;
    arguments?: Record<string, unknown>;
  } | null;
};

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
  kind?: "spatial_scene" | "photo_style_transfer";
};

type MessageAttachment = {
  name: string;
  preview?: string;
};

type PendingAttachment = MessageAttachment & {
  id: string;
  file: File;
  preview: string;
};

type LocalMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  attachments?: MessageAttachment[];
  run?: AgentRun;
  assetId?: string;
  assetKind?: "spatial_scene" | "photo_style_transfer";
};

type LocalThread = {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messageCount: number;
  messages: LocalMessage[];
};

type ConversationResponse = {
  id: string;
  channel: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
};

type ConversationMessageResponse = {
  id: number;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  created_at: string;
  run_id?: string | null;
  metadata?: {
    run?: AgentRun;
    asset_id?: string;
    asset_kind?: "spatial_scene" | "photo_style_transfer";
    attachment?: {
      name?: string;
      preview_url?: string;
    };
    style_attachments?: Array<{
      name?: string;
      preview_url?: string;
    }>;
    attachments?: Array<{
      name?: string;
      preview_url?: string;
    }>;
  };
};

type ChannelMessage = {
  id: number;
  platform: "feishu";
  chat_id: string;
  sender_id?: string | null;
  direction: "inbound" | "outbound" | "system";
  kind: "text" | "markdown" | "image" | "file" | "card" | "status";
  content: string;
  media_url?: string | null;
  created_at: string;
};

type DeleteTarget = {
  kind: "local" | "feishu";
  id: string;
  title: string;
};

type AgentConsoleProps = {
  apiBase: string;
  health: HealthInfo | null;
  onConnectionChange: (ready: boolean) => void;
  onSpatialSceneReady: (assetId: string) => void;
  onPhotoStyleReady: (assetId: string) => void;
};

const examples = [
  "分析这段文字：医学人工智能需要可靠评测与隐私保护。",
  "记住：我偏好所有私人图片在本机处理",
  "查看我的个人资产",
  "现在几点？",
];

function makeId() {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

export function AgentConsole({
  apiBase,
  health,
  onConnectionChange,
  onSpatialSceneReady,
  onPhotoStyleReady,
}: AgentConsoleProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const messageListRef = useRef<HTMLDivElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const previewUrlsRef = useRef(new Set<string>());
  const [localThreads, setLocalThreads] = useState<LocalThread[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [localLoading, setLocalLoading] = useState(true);
  const [channelMessages, setChannelMessages] = useState<ChannelMessage[]>([]);
  const [channelLoading, setChannelLoading] = useState(true);
  const [pendingApprovals, setPendingApprovals] = useState<AgentRun[]>([]);
  const [message, setMessage] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [imagePreview, setImagePreview] = useState<MessageAttachment | null>(null);
  const [loading, setLoading] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState("");
  const [job, setJob] = useState<AgentJob | null>(null);
  const [retryingJob, setRetryingJob] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleting, setDeleting] = useState(false);

  const loadLocalConversations = useCallback(async () => {
    try {
      let response = await fetch(
        `${apiBase}/api/conversations?channel=web&limit=50`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(await responseError(response));
      let body = (await response.json()) as {
        conversations: ConversationResponse[];
      };
      if (body.conversations.length === 0) {
        const createResponse = await fetch(`${apiBase}/api/conversations`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: "default", title: "新对话" }),
        });
        if (!createResponse.ok && createResponse.status !== 409) {
          throw new Error(await responseError(createResponse));
        }
        response = await fetch(
          `${apiBase}/api/conversations?channel=web&limit=50`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error(await responseError(response));
        body = (await response.json()) as {
          conversations: ConversationResponse[];
        };
      }

      const threads = await Promise.all(
        body.conversations.map(async (conversation): Promise<LocalThread> => {
          const messagesResponse = await fetch(
            `${apiBase}/api/conversations/${encodeURIComponent(conversation.id)}/messages?limit=200`,
            { cache: "no-store" },
          );
          if (!messagesResponse.ok) {
            throw new Error(await responseError(messagesResponse));
          }
          const messagesBody = (await messagesResponse.json()) as {
            messages: ConversationMessageResponse[];
          };
          return {
            id: conversation.id,
            title: conversation.title,
            createdAt: conversation.created_at,
            updatedAt: conversation.updated_at,
            messageCount: conversation.message_count,
            messages: messagesBody.messages
              .filter(
                (item) => item.role === "user" || item.role === "assistant",
              )
              .map((item) => ({
                id: String(item.id),
                role: item.role as "user" | "assistant",
                content: item.content,
                createdAt: item.created_at,
                attachments: (
                  item.metadata?.attachments?.length
                    ? item.metadata.attachments
                    : [
                        ...(item.metadata?.attachment
                          ? [item.metadata.attachment]
                          : []),
                        ...(item.metadata?.style_attachments ?? []),
                      ]
                )
                  .filter((attachment) => Boolean(attachment.name))
                  .map((attachment) => ({
                    name: attachment.name!,
                    preview: resolveApiMediaUrl(
                      apiBase,
                      attachment.preview_url,
                    ),
                  })),
                run: item.metadata?.run,
                assetId: item.metadata?.asset_id,
                assetKind: item.metadata?.asset_kind,
              })),
          };
        }),
      );
      setLocalThreads(threads);
      setSelectedId((current) => {
        if (
          current.startsWith("feishu:") ||
          threads.some((thread) => current === `local:${thread.id}`)
        ) {
          return current;
        }
        return threads[0] ? `local:${threads[0].id}` : "";
      });
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法读取本机会话。",
      );
      onConnectionChange(false);
    } finally {
      setLocalLoading(false);
    }
  }, [apiBase, onConnectionChange]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadLocalConversations(), 0);
    return () => window.clearTimeout(timer);
  }, [loadLocalConversations]);

  useEffect(() => {
    const urls = previewUrlsRef.current;
    return () => {
      urls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!imagePreview) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setImagePreview(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [imagePreview]);

  const loadChannelMessages = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/api/channels/messages?limit=200`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const body = (await response.json()) as { messages: ChannelMessage[] };
      setChannelMessages(body.messages);
      onConnectionChange(true);
    } catch {
      // Keep local chat usable if the external-channel log is unavailable.
    } finally {
      setChannelLoading(false);
    }
  }, [apiBase, onConnectionChange]);

  const loadPendingApprovals = useCallback(async () => {
    try {
      const response = await fetch(
        `${apiBase}/api/agent/runs?status=waiting_approval&limit=20`,
        { cache: "no-store" },
      );
      if (!response.ok) throw new Error(await responseError(response));
      const body = (await response.json()) as { runs: AgentRun[] };
      setPendingApprovals(body.runs);
    } catch {
      // Approval polling must not make the main chat unavailable.
    }
  }, [apiBase]);

  useEffect(() => {
    const refresh = () => {
      void loadChannelMessages();
      void loadPendingApprovals();
    };
    const initial = window.setTimeout(refresh, 0);
    const interval = window.setInterval(refresh, 3000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
    };
  }, [loadChannelMessages, loadPendingApprovals]);

  useEffect(() => {
    messageListRef.current?.scrollTo({
      top: messageListRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [localThreads, selectedId, channelMessages, loading, job]);

  useEffect(() => {
    if (!deleteTarget) return;
    deleteCancelRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleting) setDeleteTarget(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget, deleting]);

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/jobs/${job.id}`);
        if (!response.ok) throw new Error(await responseError(response));
        const nextJob = (await response.json()) as AgentJob;
        setJob(nextJob);
        onConnectionChange(true);
        if (nextJob.status === "failed") {
          setError(nextJob.error || nextJob.message);
        }
      } catch (requestError) {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "无法读取空间照片任务状态。",
        );
      }
    }, 900);
    return () => window.clearTimeout(timer);
  }, [apiBase, job, onConnectionChange]);

  useEffect(() => {
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(`${apiBase}/api/jobs?limit=50`, {
          cache: "no-store",
        });
        if (!response.ok) return;
        const body = (await response.json()) as { jobs: AgentJob[] };
        const recoverableJob = body.jobs.find(
          (item) =>
            item.kind === "spatial_scene" &&
            ["queued", "running", "failed"].includes(item.status),
        );
        if (recoverableJob) {
          setJob((current) => current ?? recoverableJob);
        }
      } catch {
        // The regular health checks own backend connectivity feedback.
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [apiBase]);

  async function retryCurrentJob() {
    if (!job || job.status !== "failed" || retryingJob) return;
    setRetryingJob(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `${apiBase}/api/jobs/${encodeURIComponent(job.id)}/retry`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await responseError(response));
      const nextJob = (await response.json()) as AgentJob;
      setJob({ ...nextJob, kind: job.kind });
      setNotice("已复用原始图片，任务重新进入本地处理队列。");
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "重新生成失败，请稍后再试。",
      );
    } finally {
      setRetryingJob(false);
    }
  }

  const feishuThreads = useMemo(() => {
    const grouped = new Map<string, ChannelMessage[]>();
    channelMessages.forEach((item) => {
      const current = grouped.get(item.chat_id) ?? [];
      current.push(item);
      grouped.set(item.chat_id, current);
    });
    return [...grouped.entries()]
      .map(([chatId, messages]) => ({
        id: chatId,
        messages,
        updatedAt: messages.at(-1)?.created_at ?? "",
        title: `飞书对话 · ${shortId(chatId)}`,
      }))
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }, [channelMessages]);

  const selectedLocal = selectedId.startsWith("local:")
    ? localThreads.find((item) => `local:${item.id}` === selectedId)
    : undefined;
  const selectedFeishu = selectedId.startsWith("feishu:")
    ? feishuThreads.find((item) => `feishu:${item.id}` === selectedId)
    : undefined;

  async function addThread() {
    setError("");
    setNotice("");
    try {
      const response = await fetch(`${apiBase}/api/conversations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "新对话" }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const created = (await response.json()) as ConversationResponse;
      setLocalThreads((current) => [
        {
          id: created.id,
          title: created.title,
          createdAt: created.created_at,
          updatedAt: created.updated_at,
          messageCount: 0,
          messages: [],
        },
        ...current,
      ]);
      setSelectedId(`local:${created.id}`);
      setMessage("");
      setJob(null);
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法创建新会话。",
      );
      onConnectionChange(false);
    }
  }

  function requestConversationDelete() {
    if (selectedLocal) {
      setDeleteTarget({
        kind: "local",
        id: selectedLocal.id,
        title: selectedLocal.title,
      });
    } else if (selectedFeishu) {
      setDeleteTarget({
        kind: "feishu",
        id: selectedFeishu.id,
        title: selectedFeishu.title,
      });
    }
  }

  async function confirmConversationDelete() {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    setError("");
    setNotice("");
    try {
      const endpoint =
        deleteTarget.kind === "local"
          ? `${apiBase}/api/conversations/${encodeURIComponent(deleteTarget.id)}`
          : `${apiBase}/api/channels/conversations/${encodeURIComponent(deleteTarget.id)}`;
      const response = await fetch(endpoint, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));

      if (deleteTarget.kind === "local") {
        setSelectedId("");
        await loadLocalConversations();
        setNotice("本机会话已删除；个人资产和长期记忆未受影响。");
      } else {
        setChannelMessages((current) =>
          current.filter((item) => item.chat_id !== deleteTarget.id),
        );
        setSelectedId(
          localThreads[0] ? `local:${localThreads[0].id}` : "",
        );
        setNotice("飞书会话的本机镜像已删除；飞书原消息未被修改。");
      }
      setDeleteTarget(null);
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法删除这个会话。",
      );
    } finally {
      setDeleting(false);
    }
  }

  function updateThread(
    threadId: string,
    updater: (thread: LocalThread) => LocalThread,
  ) {
    setLocalThreads((current) =>
      current.map((thread) => (thread.id === threadId ? updater(thread) : thread)),
    );
  }

  function chooseAttachment(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    setError("");
    if (!selected.length) return;
    if (
      selected.some(
        (file) =>
          !["image/jpeg", "image/png", "image/webp"].includes(file.type),
      )
    ) {
      setError("附件仅支持 JPG、PNG 或 WebP 图片。");
      event.target.value = "";
      return;
    }
    if (selected.some((file) => file.size > 20 * 1024 * 1024)) {
      setError("每张图片附件不能超过 20MB。");
      event.target.value = "";
      return;
    }
    setAttachments((current) => {
      const known = new Set(
        current.map(
          (item) =>
            `${item.file.name}:${item.file.size}:${item.file.lastModified}`,
        ),
      );
      const additions = selected
        .filter(
          (file) =>
            !known.has(`${file.name}:${file.size}:${file.lastModified}`),
        )
        .slice(0, Math.max(0, 4 - current.length))
        .map((file) => {
          const preview = URL.createObjectURL(file);
          previewUrlsRef.current.add(preview);
          return {
            id: makeId(),
            file,
            name: file.name,
            preview,
          };
        });
      if (current.length + additions.length < current.length + selected.length) {
        window.setTimeout(
          () => setError("一次消息最多添加 4 张图片。"),
          0,
        );
      }
      return [...current, ...additions];
    });
    event.target.value = "";
  }

  function removeAttachment(id: string) {
    setImagePreview(null);
    setAttachments((current) => {
      const removed = current.find((item) => item.id === id);
      if (removed) {
        URL.revokeObjectURL(removed.preview);
        previewUrlsRef.current.delete(removed.preview);
      }
      return current.filter((item) => item.id !== id);
    });
  }

  function clearAttachments() {
    setAttachments((current) => {
      current.forEach((item) => {
        URL.revokeObjectURL(item.preview);
        previewUrlsRef.current.delete(item.preview);
      });
      return [];
    });
    if (inputRef.current) inputRef.current.value = "";
  }

  async function runAgent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedLocal || (!message.trim() && !attachments.length) || loading) {
      return;
    }
    const threadId = selectedLocal.id;
    const userText = message.trim() || "请分析这些图片";
    const pendingAttachments = [...attachments];
    const userMessage: LocalMessage = {
      id: makeId(),
      role: "user",
      content: userText,
      createdAt: new Date().toISOString(),
      attachments: pendingAttachments.map(({ name, preview }) => ({
        name,
        preview,
      })),
    };
    const nextTitle =
      selectedLocal.messages.length === 0
        ? userText.replace(/\s+/g, " ").slice(0, 24)
        : selectedLocal.title;
    updateThread(threadId, (thread) => ({
      ...thread,
      title: nextTitle,
      updatedAt: userMessage.createdAt,
      messageCount: thread.messageCount + 1,
      messages: [...thread.messages, userMessage],
    }));
    setMessage("");
    setLoading(true);
    setError("");
    setNotice("");
    setJob(null);

    const stagedImages: SourceImage[] = [];
    try {
      for (const pending of pendingAttachments) {
        const uploadPayload = new FormData();
        uploadPayload.append("file", pending.file);
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
          message: userText,
          attachment_image_ids: stagedImages.map((image) => image.id),
          session_id: threadId,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = (await response.json()) as AgentRun;
      const sceneOutput = result.steps
        .map((step) => step.output)
        .find(
          (output) =>
            typeof output?.job_id === "string" &&
            typeof output?.asset_id === "string",
        );
      const attachmentAction = result.steps
        .map((step) => step.output?.attachment_action)
        .find((action) => typeof action === "string");
      const assistantMessage: LocalMessage = {
        id: makeId(),
        role: "assistant",
        content: result.answer,
        createdAt: new Date().toISOString(),
        run: result,
        assetId:
          typeof sceneOutput?.asset_id === "string"
            ? sceneOutput.asset_id
            : undefined,
        assetKind:
          sceneOutput?.kind === "photo_style_transfer"
            ? "photo_style_transfer"
            : "spatial_scene",
      };
      updateThread(threadId, (thread) => ({
        ...thread,
        updatedAt: assistantMessage.createdAt,
        messageCount: thread.messageCount + 1,
        messages: [...thread.messages, assistantMessage],
      }));
      if (sceneOutput) {
        setJob({
          id: sceneOutput.job_id as string,
          asset_id: sceneOutput.asset_id as string,
          status: sceneOutput.status === "running" ? "running" : "queued",
          progress:
            typeof sceneOutput.progress === "number" ? sceneOutput.progress : 0,
          message:
            typeof sceneOutput.message === "string"
              ? sceneOutput.message
              : "空间照片任务已创建。",
          error: null,
          kind:
            sceneOutput.kind === "photo_style_transfer"
              ? "photo_style_transfer"
              : "spatial_scene",
        });
      } else if (attachmentAction !== "vision") {
        await Promise.all(
          stagedImages.map((image) =>
            fetch(`${apiBase}/api/source-images/${image.id}`, {
              method: "DELETE",
            }),
          ),
        );
      }
      if (attachmentAction !== "clarification_required") {
        clearAttachments();
        await loadLocalConversations();
      } else {
        setNotice("附件已保留在输入框中，补充用途后可以直接继续发送。");
      }
      onConnectionChange(true);
    } catch (requestError) {
      onConnectionChange(false);
      stagedImages.forEach((image) => {
        void fetch(`${apiBase}/api/source-images/${image.id}`, {
          method: "DELETE",
        });
      });
      const detail =
        requestError instanceof Error
          ? requestError.message
          : "无法连接本地后端。";
      setError(detail);
      updateThread(threadId, (thread) => ({
        ...thread,
        updatedAt: new Date().toISOString(),
        messages: [
          ...thread.messages,
          {
            id: makeId(),
            role: "assistant",
            content: `任务执行失败：${detail}`,
            createdAt: new Date().toISOString(),
          },
        ],
      }));
    } finally {
      setLoading(false);
    }
  }

  async function decideApproval(runId: string, approved: boolean) {
    if (approvalBusy) return;
    setApprovalBusy(runId);
    setError("");
    try {
      const response = await fetch(
        `${apiBase}/api/agent/runs/${encodeURIComponent(runId)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approved }),
        },
      );
      if (!response.ok) throw new Error(await responseError(response));
      setPendingApprovals((current) =>
        current.filter((run) => run.run_id !== runId),
      );
      await loadLocalConversations();
      await loadChannelMessages();
      await loadPendingApprovals();
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "无法提交审批决定。",
      );
    } finally {
      setApprovalBusy("");
    }
  }

  const modeLabel = health?.llm_configured
    ? health.model ?? "真实模型"
    : health?.llm_status === "configured_not_enabled"
      ? "模型已配置，尚未启用"
      : health?.llm_status === "error"
        ? "模型配置异常"
        : "演示规划器";

  return (
    <>
      <div className="conversation-workspace">
      <aside className="conversation-sidebar" aria-label="对话列表">
        <div className="conversation-sidebar-header">
          <div>
            <p className="eyebrow">Conversations</p>
            <h1>对话</h1>
          </div>
          <button type="button" onClick={addThread} aria-label="新建本地对话">
            <AppIcon name="plus" width="18" height="18" />
          </button>
        </div>

        <div className="conversation-group">
          <span className="conversation-group-label">本机</span>
          {localLoading ? (
            <div className="conversation-list-note">正在读取本机会话…</div>
          ) : (
            localThreads.map((thread) => (
              <button
                type="button"
                key={thread.id}
                className={selectedId === `local:${thread.id}` ? "active" : ""}
                onClick={() => setSelectedId(`local:${thread.id}`)}
              >
                <span className="conversation-source local">
                  <AppIcon name="chat" width="16" height="16" />
                </span>
                <span>
                  <strong>{thread.title}</strong>
                  <small>
                    {thread.messageCount
                      ? `${thread.messageCount} 条消息 · ${formatRelative(thread.updatedAt)}`
                      : "尚无消息"}
                  </small>
                </span>
              </button>
            ))
          )}
        </div>

        <div className="conversation-group">
          <span className="conversation-group-label">
            飞书
            <i className={health?.feishu_status === "connected" ? "online" : ""}>
              {health?.feishu_status === "connected" ? "已连接" : "未连接"}
            </i>
          </span>
          {channelLoading ? (
            <div className="conversation-list-note">正在同步消息…</div>
          ) : feishuThreads.length ? (
            feishuThreads.map((thread) => (
              <button
                type="button"
                key={thread.id}
                className={selectedId === `feishu:${thread.id}` ? "active" : ""}
                onClick={() => setSelectedId(`feishu:${thread.id}`)}
              >
                <span className="conversation-source feishu">
                  <AppIcon name="link" width="16" height="16" />
                </span>
                <span>
                  <strong>{thread.title}</strong>
                  <small>
                    {thread.messages.length} 条消息 ·{" "}
                    {formatRelative(thread.updatedAt)}
                  </small>
                </span>
              </button>
            ))
          ) : (
            <div className="conversation-list-note">
              手机向机器人发消息后，会按会话单独显示。
            </div>
          )}
        </div>
      </aside>

      <section className="chat-surface" aria-label="当前对话">
        <header className="chat-header">
          <div>
            <div className="chat-title-row">
              <h2>{selectedLocal?.title ?? selectedFeishu?.title ?? "对话"}</h2>
              <span className={`channel-badge ${selectedFeishu ? "feishu" : ""}`}>
                {selectedFeishu ? "飞书 · 只读镜像" : "本机私有会话"}
              </span>
            </div>
            <p>
              {selectedFeishu
                ? `chat_id ${shortId(selectedFeishu.id)} · 与其他会话完全分开`
                : `${modeLabel} · 当前上下文只属于这个 session_id`}
            </p>
          </div>
          <div className="chat-header-actions">
            <span
              className="chat-security"
              title="本机 Root 管理员可以查看所有已记录的 Web 与飞书会话"
            >
              <span aria-hidden="true" />
              Root 管理员视图
            </span>
            {selectedLocal || selectedFeishu ? (
              <button
                className="conversation-delete-button"
                type="button"
                onClick={requestConversationDelete}
                aria-label={`删除${selectedLocal ? "本机" : "飞书镜像"}会话`}
              >
                <AppIcon name="trash" width="17" height="17" />
                <span>删除会话</span>
              </button>
            ) : null}
          </div>
        </header>

        {pendingApprovals.length ? (
          <section className="root-approval-queue" aria-label="待审批 Agent Run">
            <div>
              <strong>待审批任务</strong>
              <span>
                {pendingApprovals.length} 个高风险工具正在等待 Root 决定
              </span>
            </div>
            {pendingApprovals.slice(0, 3).map((run) => (
              <div className="root-approval-item" key={run.run_id}>
                <span>
                  <strong>{run.approval?.tool ?? "未知工具"}</strong>
                  <small>
                    {run.approval?.risk_level ?? "未标注"} ·{" "}
                    {shortId(run.run_id)}
                  </small>
                </span>
                <div>
                  <button
                    type="button"
                    disabled={Boolean(approvalBusy)}
                    onClick={() => decideApproval(run.run_id, false)}
                  >
                    拒绝
                  </button>
                  <button
                    className="approve"
                    type="button"
                    disabled={Boolean(approvalBusy)}
                    onClick={() => decideApproval(run.run_id, true)}
                  >
                    {approvalBusy === run.run_id ? "处理中…" : "批准"}
                  </button>
                </div>
              </div>
            ))}
          </section>
        ) : null}

        <div className="chat-message-scroll" ref={messageListRef} aria-live="polite">
          {selectedLocal ? (
            selectedLocal.messages.length ? (
              selectedLocal.messages.map((item) => (
                <LocalMessageBubble
                  key={item.id}
                  item={item}
                  onOpenAsset={onSpatialSceneReady}
                  onOpenStyle={onPhotoStyleReady}
                  onPreviewImage={setImagePreview}
                  onApprovalDecision={decideApproval}
                  approvalBusy={approvalBusy === item.run?.run_id}
                />
              ))
            ) : (
              <ChatWelcome onExample={setMessage} />
            )
          ) : selectedFeishu ? (
            selectedFeishu.messages.map((item) => (
              <ChannelMessageBubble key={item.id} item={item} apiBase={apiBase} />
            ))
          ) : (
            <ChatWelcome onExample={setMessage} />
          )}

          {loading ? (
            <div className="message-row assistant">
              <div className="assistant-avatar">A</div>
              <div className="message-bubble assistant typing-bubble">
                <span />
                <span />
                <span />
                <strong>正在规划和调用工具</strong>
              </div>
            </div>
          ) : null}

          {job ? (
            <div className="message-row assistant">
              <div className="assistant-avatar">A</div>
              <div className="message-bubble assistant job-message">
                <div>
                  <strong>{job.message}</strong>
                  <span>
                    {job.status === "completed"
                      ? job.kind === "photo_style_transfer"
                        ? "风格化结果已经可以查看"
                        : "空间照片已经可以查看"
                      : job.status === "failed"
                        ? "原始图片仍保存在本机，可以直接重试"
                      : job.kind === "photo_style_transfer"
                        ? "图片风格化正在本机执行"
                        : "深度估计与分层正在本机执行"}
                  </span>
                </div>
                <progress max="100" value={job.progress} />
                <div className="job-message-footer">
                  <span>{job.progress}%</span>
                  {job.status === "completed" ? (
                    <button
                      type="button"
                      onClick={() =>
                        job.kind === "photo_style_transfer"
                          ? onPhotoStyleReady(job.asset_id)
                          : onSpatialSceneReady(job.asset_id)
                      }
                    >
                      {job.kind === "photo_style_transfer"
                        ? "打开风格化结果"
                        : "打开空间照片"}
                    </button>
                  ) : job.status === "failed" &&
                    job.kind !== "photo_style_transfer" ? (
                    <button
                      type="button"
                      onClick={() => void retryCurrentJob()}
                      disabled={retryingJob}
                    >
                      {retryingJob ? "重新排队中…" : "重新生成"}
                    </button>
                  ) : null}
                </div>
              </div>
            </div>
          ) : null}
        </div>

        {error ? (
          <div className="chat-error" role="alert">
            {error}
          </div>
        ) : null}

        {notice ? (
          <div className="chat-notice" aria-live="polite">
            {notice}
          </div>
        ) : null}

        {selectedLocal ? (
          <form className="chat-composer" onSubmit={runAgent}>
            {attachments.length ? (
              <div className="composer-attachments" aria-label="待发送附件">
                {attachments.map((attachment, index) => (
                  <article className="composer-attachment-chip" key={attachment.id}>
                    <button
                      className="composer-attachment-preview"
                      type="button"
                      aria-label={`预览附件 ${index + 1}：${attachment.name}`}
                      onClick={() => setImagePreview(attachment)}
                    >
                      {/* Local object URL; it has not left this device. */}
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={attachment.preview} alt="" />
                      <span>{index + 1}</span>
                    </button>
                    <small title={attachment.name}>{attachment.name}</small>
                    <button
                      className="composer-attachment-remove"
                      type="button"
                      aria-label={`移除附件：${attachment.name}`}
                      onClick={() => removeAttachment(attachment.id)}
                    >
                      ×
                    </button>
                  </article>
                ))}
                <span className="composer-attachment-note">
                  {attachments.length}/4 · 可直接看图问答；生成类任务需明确说明用途
                </span>
              </div>
            ) : null}
            <label className="sr-only" htmlFor="agent-chat-input">
              输入消息
            </label>
            <textarea
              id="agent-chat-input"
              value={message}
              maxLength={4000}
              rows={2}
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="向个人助手发送消息；Shift + Enter 换行"
            />
            <div className="chat-composer-actions">
              <div>
                <input
                  ref={inputRef}
                  className="sr-only"
                  type="file"
                  multiple
                  accept="image/jpeg,image/png,image/webp"
                  onChange={chooseAttachment}
                />
                <button
                  className="composer-icon-button"
                  type="button"
                  aria-label="添加附件"
                  title="添加附件"
                  onClick={() => inputRef.current?.click()}
                >
                  <AppIcon name="paperclip" width="19" height="19" />
                  {attachments.length ? (
                    <small aria-hidden="true">{attachments.length}</small>
                  ) : null}
                </button>
                <span>{message.length} / 4000</span>
              </div>
              <button
                className="send-button"
                type="submit"
                disabled={loading || (!message.trim() && !attachments.length)}
              >
                发送
                <AppIcon name="send" width="17" height="17" />
              </button>
            </div>
          </form>
        ) : (
          <footer className="readonly-composer">
            <AppIcon name="link" width="18" height="18" />
            这是飞书会话的本地只读镜像。请在原飞书聊天中继续回复。
          </footer>
        )}
      </section>
      </div>

      {deleteTarget ? (
        <div className="conversation-delete-scrim" role="presentation">
          <section
            className="conversation-delete-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-conversation-title"
            aria-describedby="delete-conversation-description"
          >
            <span className="delete-dialog-icon" aria-hidden="true">
              <AppIcon name="trash" width="22" height="22" />
            </span>
            <h2 id="delete-conversation-title">删除“{deleteTarget.title}”？</h2>
            <p id="delete-conversation-description">
              {deleteTarget.kind === "local"
                ? "将删除这段本机会话及其消息记录，但不会删除已生成资产或显式保存的长期记忆。"
                : "只会删除电脑端的只读镜像，不会删除飞书里的原消息；收到新消息后该会话会重新出现。"}
            </p>
            <div>
              <button
                ref={deleteCancelRef}
                type="button"
                disabled={deleting}
                onClick={() => setDeleteTarget(null)}
              >
                取消
              </button>
              <button
                className="danger"
                type="button"
                disabled={deleting}
                onClick={() => void confirmConversationDelete()}
              >
                {deleting
                  ? "正在删除…"
                  : deleteTarget.kind === "local"
                    ? "删除本机会话"
                    : "删除本机镜像"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {imagePreview?.preview ? (
        <div
          className="style-lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={`${imagePreview.name}完整预览`}
          onClick={() => setImagePreview(null)}
        >
          <button
            className="style-lightbox-close"
            type="button"
            aria-label="关闭附件预览"
            onClick={() => setImagePreview(null)}
          >
            ×
          </button>
          <figure onClick={(event) => event.stopPropagation()}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={imagePreview.preview} alt={imagePreview.name} />
            <figcaption>{imagePreview.name} · 按 Esc 关闭</figcaption>
          </figure>
        </div>
      ) : null}
    </>
  );
}

function ChatWelcome({ onExample }: { onExample: (text: string) => void }) {
  return (
    <div className="chat-welcome">
      <span className="welcome-mark">
        <AppIcon name="sparkles" width="26" height="26" />
      </span>
      <h3>今天想让 Agent 做什么？</h3>
      <p>对话、图片、工具结果和执行轨迹会按当前会话集中展示。</p>
      <div className="welcome-examples">
        {examples.map((example, index) => (
          <button type="button" key={example} onClick={() => onExample(example)}>
            <span>{["组合分析", "保存记忆", "个人资产", "当前时间"][index]}</span>
            <AppIcon name="chevron" width="14" height="14" />
          </button>
        ))}
      </div>
    </div>
  );
}

function LocalMessageBubble({
  item,
  onOpenAsset,
  onOpenStyle,
  onPreviewImage,
  onApprovalDecision,
  approvalBusy,
}: {
  item: LocalMessage;
  onOpenAsset: (assetId: string) => void;
  onOpenStyle: (assetId: string) => void;
  onPreviewImage: (attachment: MessageAttachment) => void;
  onApprovalDecision: (runId: string, approved: boolean) => void;
  approvalBusy: boolean;
}) {
  return (
    <div className={`message-row ${item.role}`}>
      {item.role === "assistant" ? <div className="assistant-avatar">A</div> : null}
      <div className={`message-bubble ${item.role}`}>
        {item.attachments?.length ? (
          <div
            className="message-attachments"
            data-count={item.attachments.length}
            aria-label={`${item.attachments.length} 个图片附件`}
          >
            {item.attachments.map((attachment, index) =>
              attachment.preview ? (
                <button
                  type="button"
                  aria-label={`预览附件 ${index + 1}：${attachment.name}`}
                  onClick={() => onPreviewImage(attachment)}
                  key={`${attachment.name}-${index}`}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={attachment.preview} alt="" />
                  <span>{index + 1}</span>
                </button>
              ) : (
                <span key={`${attachment.name}-${index}`}>
                  <AppIcon name="image" width="20" height="20" />
                  <small>{attachment.name}</small>
                </span>
              ),
            )}
          </div>
        ) : null}
        <p>{cleanMessageText(item.content)}</p>
        {item.assetId ? (
          <button
            className="asset-result-button"
            type="button"
            onClick={() =>
              item.assetKind === "photo_style_transfer"
                ? onOpenStyle(item.assetId!)
                : onOpenAsset(item.assetId!)
            }
          >
            <AppIcon
              name={item.assetKind === "photo_style_transfer" ? "image" : "cube"}
              width="18"
              height="18"
            />
            {item.assetKind === "photo_style_transfer"
              ? "查看图片风格化结果"
              : "查看生成的空间照片"}
            <AppIcon name="chevron" width="15" height="15" />
          </button>
        ) : null}
        {item.run?.status === "waiting_approval" ? (
          <div className="run-approval-card">
            <div>
              <strong>需要 Root 管理员批准</strong>
              <span>
                {item.run.approval?.description ??
                  `工具 ${item.run.approval?.tool ?? "未知工具"} 请求执行`}
              </span>
              <small>
                风险等级：{item.run.approval?.risk_level ?? "未标注"}
              </small>
            </div>
            <div>
              <button
                type="button"
                disabled={approvalBusy}
                onClick={() =>
                  onApprovalDecision(item.run!.run_id, false)
                }
              >
                拒绝
              </button>
              <button
                className="approve"
                type="button"
                disabled={approvalBusy}
                onClick={() =>
                  onApprovalDecision(item.run!.run_id, true)
                }
              >
                {approvalBusy ? "处理中…" : "批准执行"}
              </button>
            </div>
          </div>
        ) : null}
        {item.run ? (
          <details className="run-details">
            <summary>
              <span>查看 LangGraph 执行轨迹</span>
              <small>{item.run.total_duration_ms} ms</small>
            </summary>
            <ol>
              {item.run.steps.map((step) => (
                <li key={`${step.index}-${step.label}`}>
                  <span>{step.index}</span>
                  <div>
                    <strong>{step.label}</strong>
                    <small>
                      {step.stage} · {step.duration_ms} ms
                    </small>
                    <p>{step.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
          </details>
        ) : null}
        <time>{formatTime(item.createdAt)}</time>
      </div>
    </div>
  );
}

function ChannelMessageBubble({
  item,
  apiBase,
}: {
  item: ChannelMessage;
  apiBase: string;
}) {
  const [imageFailed, setImageFailed] = useState(false);
  const role =
    item.direction === "inbound"
      ? "user"
      : item.direction === "outbound"
        ? "assistant"
        : "system";
  if (role === "system" || item.kind === "status") {
    return (
      <div className="channel-system-message">
        <span>{item.content}</span>
        <time>{formatTime(item.created_at)}</time>
      </div>
    );
  }
  const mediaSrc =
    item.media_url?.startsWith("/api/assets/")
      ? `${apiBase}${item.media_url}`
      : "";
  const card = item.kind === "card" ? parseFunctionCard(item) : null;
  return (
    <div className={`message-row ${role}`}>
      {role === "assistant" ? <div className="assistant-avatar">A</div> : null}
      <div className={`message-bubble ${role} channel-${item.kind}`}>
        {item.kind === "image" ? (
          mediaSrc && !imageFailed ? (
            <a
              className="channel-image-preview"
              href={mediaSrc}
              target="_blank"
              rel="noreferrer"
              aria-label="打开飞书图片原尺寸预览"
            >
              {/* The backend URL is an owner-controlled local asset route. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mediaSrc}
                alt="飞书会话中的图片"
                loading="lazy"
                onError={() => setImageFailed(true)}
              />
              <span>点击查看原图</span>
            </a>
          ) : (
            <div className="channel-image-placeholder">
              <AppIcon name="image" width="24" height="24" />
              <strong>图片预览不可用</strong>
              <span>
                {item.media_url
                  ? "本地资产可能已被删除"
                  : "旧消息未保存图片索引，请从飞书重新发送"}
              </span>
            </div>
          )
        ) : null}
        {item.kind === "file" ? (
          mediaSrc ? (
            <a
              className="channel-file-download"
              href={mediaSrc}
              download
              aria-label="下载飞书返回的结果文件"
            >
              <AppIcon name="download" width="22" height="22" />
              <span>
                <strong>风格化结果文件</strong>
                <small>点击下载到本机</small>
              </span>
            </a>
          ) : (
            <div className="channel-image-placeholder">
              <AppIcon name="download" width="24" height="24" />
              <strong>下载文件不可用</strong>
              <span>本地结果可能已经被删除</span>
            </div>
          )
        ) : null}
        {card?.variant === "menu" ? (
          <div className="channel-function-card">
            <span className="function-card-icon" aria-hidden="true">
              <AppIcon name="tools" width="19" height="19" />
            </span>
            <div>
              <small>个人数字助手</small>
              <strong>选择常用功能</strong>
              <p>在飞书中点击卡片按钮即可调用本机能力。</p>
              <ul>
                {card.items.map((label) => (
                  <li key={label}>{label}</li>
                ))}
              </ul>
            </div>
          </div>
        ) : card?.variant === "retry" ? (
          <div className="channel-retry-card">
            <span className="function-card-icon" aria-hidden="true">
              <AppIcon name="cube" width="19" height="19" />
            </span>
            <div>
              <small>空间照片任务</small>
              <strong>生成失败，可以重新生成</strong>
              <p>{card.text}</p>
              <span>请在飞书中点击“重新生成”</span>
            </div>
          </div>
        ) : item.kind !== "image" && item.kind !== "file" ? (
          <>
            {card?.variant === "action" ? (
              <span className="message-kind-label">已选择功能</span>
            ) : null}
            <p>{card?.text ?? cleanMessageText(item.content)}</p>
          </>
        ) : null}
        <div className="channel-message-foot">
          <span>
            {role === "user"
              ? `发送者 ${shortId(item.sender_id ?? "unknown")}`
              : "个人助手"}
          </span>
          <time>{formatTime(item.created_at)}</time>
        </div>
      </div>
    </div>
  );
}

function parseFunctionCard(item: ChannelMessage) {
  const cleaned = cleanMessageText(item.content);
  if (item.direction === "outbound" && cleaned.startsWith("[重试卡片]")) {
    return {
      variant: "retry" as const,
      items: [] as string[],
      text:
        cleaned
          .replace(/^\[重试卡片\]\s*/, "")
          .replace(/^空间照片生成失败、重新生成：\s*/, "") ||
        "本地任务执行失败。",
    };
  }
  const text = cleaned.replace(
    /^\[功能卡片\]\s*/,
    "",
  );
  if (item.direction === "outbound") {
    const items = text
      .split(/[、，,]/)
      .map((part) => part.trim())
      .filter(Boolean);
    return {
      variant: "menu" as const,
      items: items.length ? items : ["功能菜单"],
      text,
    };
  }
  return {
    variant: "action" as const,
    items: [] as string[],
    text: `已选择：${text || "功能卡片"}`,
  };
}

function shortId(value: string) {
  if (value.length <= 12) return value;
  return `${value.slice(0, 6)}…${value.slice(-4)}`;
}

function cleanMessageText(value: string) {
  return value
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/^---+$/gm, "")
    .split("\n")
    .filter((line) => !/^\s*\|?(?:\s*:?-+:?\s*\|)+\s*$/.test(line))
    .map((line) =>
      line.includes("|")
        ? line.replace(/^\s*\||\|\s*$/g, "").replace(/\s*\|\s*/g, " · ")
        : line,
    )
    .join("\n")
    .trim();
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function resolveApiMediaUrl(apiBase: string, value: string | undefined) {
  if (!value) return undefined;
  if (value.startsWith("/")) return `${apiBase}${value}`;
  return /^https?:\/\//i.test(value) ? value : undefined;
}

function formatRelative(value: string) {
  const elapsed = Date.now() - new Date(value).getTime();
  if (elapsed < 60_000) return "刚刚";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)} 分钟前`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)} 小时前`;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
  }).format(new Date(value));
}

async function responseError(response: Response) {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}
