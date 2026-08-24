"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type MemoryType =
  | "profile"
  | "preference"
  | "fact"
  | "task_state"
  | "episode"
  | "procedure"
  | "asset_relation";

type MemoryStatus = "candidate" | "active" | "superseded" | "archived";

type MemoryItem = {
  id: string;
  memory_type: MemoryType;
  scope: "user" | "channel" | "thread" | "project";
  scope_id?: string | null;
  content: string;
  source: string;
  confidence: number;
  importance: number;
  sensitivity: "normal" | "private" | "sensitive";
  valid_from: string;
  valid_to?: string | null;
  status: MemoryStatus;
  supersedes_id?: string | null;
  created_at: string;
  updated_at: string;
  last_accessed_at?: string | null;
  access_count: number;
  utility_score: number;
  metadata: Record<string, unknown>;
};

type Feedback = { tone: "success" | "error" | "neutral"; message: string };

type Props = {
  open: boolean;
  apiBase: string;
  onClose: () => void;
};

const MEMORY_TYPES: Array<{ value: MemoryType; label: string }> = [
  { value: "preference", label: "偏好" },
  { value: "profile", label: "个人资料" },
  { value: "fact", label: "事实" },
  { value: "task_state", label: "任务状态" },
  { value: "episode", label: "任务经验" },
  { value: "procedure", label: "操作流程" },
  { value: "asset_relation", label: "资产关系" },
];

const STATUS_LABELS: Record<MemoryStatus, string> = {
  candidate: "待确认",
  active: "有效",
  superseded: "已被取代",
  archived: "已归档",
};

async function responseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function formatDate(value: string | null | undefined) {
  if (!value) return "尚未使用";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function typeLabel(value: MemoryType) {
  return MEMORY_TYPES.find((item) => item.value === value)?.label ?? value;
}

export function MemoryManagerDialog({ open, apiBase, onClose }: Props) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<MemoryType | "all">("all");
  const [showInactive, setShowInactive] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [newType, setNewType] = useState<MemoryType>("preference");
  const [newImportance, setNewImportance] = useState(0.65);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState("");
  const [editingImportance, setEditingImportance] = useState(0.65);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  const loadMemories = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    try {
      const parameters = new URLSearchParams({ limit: "200" });
      if (query.trim()) parameters.set("query", query.trim());
      if (typeFilter !== "all") parameters.set("type", typeFilter);
      if (showInactive) parameters.set("include_inactive", "true");
      const response = await fetch(`${apiBase}/api/memories?${parameters}`);
      if (!response.ok) throw new Error(await responseError(response));
      const body = (await response.json()) as { memories: MemoryItem[] };
      setMemories(body.memories);
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "无法读取记忆。",
      });
    } finally {
      setLoading(false);
    }
  }, [apiBase, open, query, showInactive, typeFilter]);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => void loadMemories(), 220);
    return () => window.clearTimeout(timer);
  }, [loadMemories, open]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    closeButtonRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose, open]);

  if (!open) return null;

  async function createMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!newContent.trim()) return;
    setSaving(true);
    try {
      const response = await fetch(`${apiBase}/api/memories`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: newContent.trim(),
          memory_type: newType,
          importance: newImportance,
          scope: "user",
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setNewContent("");
      setFeedback({ tone: "success", message: "记忆已保存并建立来源记录。" });
      await loadMemories();
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "保存失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  function beginEdit(memory: MemoryItem) {
    setEditingId(memory.id);
    setEditingContent(memory.content);
    setEditingImportance(memory.importance);
    setConfirmDeleteId(null);
    setFeedback(null);
  }

  async function saveEdit(memory: MemoryItem) {
    if (!editingContent.trim()) return;
    setSaving(true);
    try {
      const response = await fetch(`${apiBase}/api/memories/${memory.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: editingContent.trim(),
          importance: editingImportance,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setEditingId(null);
      setFeedback({ tone: "success", message: "记忆已更新，旧版本操作已审计。" });
      await loadMemories();
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "更新失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  async function archiveMemory(memory: MemoryItem) {
    setSaving(true);
    try {
      const response = await fetch(`${apiBase}/api/memories/${memory.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: "archived" }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      setFeedback({ tone: "success", message: "记忆已归档，不再进入 Agent 上下文。" });
      await loadMemories();
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "归档失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  async function deleteMemory(memory: MemoryItem) {
    if (confirmDeleteId !== memory.id) {
      setConfirmDeleteId(memory.id);
      return;
    }
    setSaving(true);
    try {
      const response = await fetch(`${apiBase}/api/memories/${memory.id}`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(await responseError(response));
      setConfirmDeleteId(null);
      setFeedback({ tone: "success", message: "记忆及其使用记录已永久删除。" });
      await loadMemories();
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "删除失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  async function exportMemories() {
    try {
      const response = await fetch(`${apiBase}/api/memories/export`);
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json();
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
      );
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `agent-memory-${new Date().toISOString().slice(0, 10)}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      setFeedback({ tone: "success", message: "记忆已导出为本地 JSON 文件。" });
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "导出失败。",
      });
    }
  }

  return (
    <div className="settings-backdrop" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="settings-dialog memory-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-manager-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <div>
            <p className="eyebrow">Evidence-aware memory</p>
            <h2 id="memory-manager-title">记忆中心</h2>
            <p>
              管理本机长期记忆。每条记忆都有类型、来源、有效状态和使用反馈；
              归档后不会进入 Agent 上下文。
            </p>
          </div>
          <button
            ref={closeButtonRef}
            className="dialog-close"
            type="button"
            onClick={onClose}
          >
            关闭
          </button>
        </header>

        <form className="memory-create" onSubmit={createMemory}>
          <label>
            <span>新增记忆</span>
            <textarea
              value={newContent}
              onChange={(event) => setNewContent(event.target.value)}
              placeholder="例如：我偏好在本机处理私人图片"
              maxLength={2000}
              required
            />
          </label>
          <div className="memory-create-options">
            <label>
              <span>类型</span>
              <select
                value={newType}
                onChange={(event) => setNewType(event.target.value as MemoryType)}
              >
                {MEMORY_TYPES.filter((item) => item.value !== "episode").map(
                  (item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ),
                )}
              </select>
            </label>
            <label>
              <span>重要度 {Math.round(newImportance * 100)}%</span>
              <input
                type="range"
                min="0"
                max="1"
                step="0.05"
                value={newImportance}
                onChange={(event) => setNewImportance(Number(event.target.value))}
              />
            </label>
            <button className="primary-action" type="submit" disabled={saving}>
              保存记忆
            </button>
          </div>
        </form>

        <div className="memory-toolbar">
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索记忆内容"
            aria-label="搜索记忆"
          />
          <select
            value={typeFilter}
            onChange={(event) =>
              setTypeFilter(event.target.value as MemoryType | "all")
            }
            aria-label="按类型筛选"
          >
            <option value="all">全部类型</option>
            {MEMORY_TYPES.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <label className="memory-inactive-control">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={(event) => setShowInactive(event.target.checked)}
            />
            显示历史状态
          </label>
          <button className="secondary-action" type="button" onClick={exportMemories}>
            导出 JSON
          </button>
        </div>

        {feedback ? (
          <p className={`settings-feedback ${feedback.tone}`} role="status">
            {feedback.message}
          </p>
        ) : null}

        <div className="memory-list" aria-busy={loading}>
          {loading ? (
            <div className="memory-empty">正在读取本地记忆…</div>
          ) : memories.length === 0 ? (
            <div className="memory-empty">没有符合条件的记忆。</div>
          ) : (
            memories.map((memory) => (
              <article className={`memory-card status-${memory.status}`} key={memory.id}>
                <div className="memory-card-heading">
                  <div>
                    <span className="memory-type">{typeLabel(memory.memory_type)}</span>
                    <span className="memory-status">{STATUS_LABELS[memory.status]}</span>
                    {memory.sensitivity !== "normal" ? (
                      <span className="memory-sensitive">{memory.sensitivity}</span>
                    ) : null}
                  </div>
                  <small>{formatDate(memory.updated_at)}</small>
                </div>

                {editingId === memory.id ? (
                  <div className="memory-editor">
                    <textarea
                      value={editingContent}
                      onChange={(event) => setEditingContent(event.target.value)}
                      maxLength={2000}
                    />
                    <label>
                      重要度 {Math.round(editingImportance * 100)}%
                      <input
                        type="range"
                        min="0"
                        max="1"
                        step="0.05"
                        value={editingImportance}
                        onChange={(event) =>
                          setEditingImportance(Number(event.target.value))
                        }
                      />
                    </label>
                  </div>
                ) : (
                  <p className="memory-content">{memory.content}</p>
                )}

                <dl className="memory-evidence">
                  <div>
                    <dt>来源</dt>
                    <dd>{memory.source}</dd>
                  </div>
                  <div>
                    <dt>使用</dt>
                    <dd>{memory.access_count} 次</dd>
                  </div>
                  <div>
                    <dt>效用</dt>
                    <dd>{Math.round(memory.utility_score * 100)}%</dd>
                  </div>
                  <div>
                    <dt>最近召回</dt>
                    <dd>{formatDate(memory.last_accessed_at)}</dd>
                  </div>
                </dl>

                <div className="memory-card-actions">
                  {editingId === memory.id ? (
                    <>
                      <button type="button" onClick={() => void saveEdit(memory)}>
                        保存修改
                      </button>
                      <button type="button" onClick={() => setEditingId(null)}>
                        取消
                      </button>
                    </>
                  ) : (
                    <button type="button" onClick={() => beginEdit(memory)}>
                      编辑
                    </button>
                  )}
                  {memory.status === "active" ? (
                    <button type="button" onClick={() => void archiveMemory(memory)}>
                      归档
                    </button>
                  ) : null}
                  <button
                    className="danger"
                    type="button"
                    onClick={() => void deleteMemory(memory)}
                  >
                    {confirmDeleteId === memory.id ? "确认永久删除" : "删除"}
                  </button>
                </div>
              </article>
            ))
          )}
        </div>
      </section>
    </div>
  );
}
