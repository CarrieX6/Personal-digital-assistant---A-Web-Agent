"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

type RuntimeStatus =
  | "disabled"
  | "starting"
  | "connected"
  | "reconnecting"
  | "error";

type IdentityBinding = {
  id: string;
  provider: "feishu";
  app_id: string;
  external_id: string;
  workspace_id: string;
  workspace_name: string;
  device_id: string;
  device_name: string;
  status: "active" | "suspended" | "revoked";
  created_at: string;
  updated_at: string;
};

export type FeishuSettingsPublic = {
  enabled: boolean;
  app_id: string;
  domain: "feishu" | "lark";
  allowed_open_ids: string[];
  allow_group_mentions: boolean;
  has_app_secret: boolean;
  masked_app_secret?: string | null;
  secret_storage: string;
  runtime: {
    status: RuntimeStatus;
    last_error?: string | null;
  };
};

type DraftSettings = {
  enabled: boolean;
  app_id: string;
  domain: "feishu" | "lark";
  allowedOpenIds: string;
  allow_group_mentions: boolean;
};

type Feedback = {
  tone: "success" | "error" | "neutral";
  message: string;
};

type Props = {
  open: boolean;
  apiBase: string;
  onClose: () => void;
  onSettingsChanged: (settings: FeishuSettingsPublic) => void;
};

const statusCopy: Record<
  RuntimeStatus,
  { label: string; detail: string }
> = {
  disabled: {
    label: "未启用",
    detail: "配置会保存在本机，但不会接收飞书消息。",
  },
  starting: {
    label: "正在连接",
    detail: "电脑端正在主动建立飞书长连接。",
  },
  connected: {
    label: "已连接",
    detail: "现在可从已授权的飞书账号发送文本指令。",
  },
  reconnecting: {
    label: "正在重连",
    detail: "飞书长连接暂时中断，电脑端正在后台自动恢复。",
  },
  error: {
    label: "连接异常",
    detail: "请检查应用权限、事件订阅、网络和凭证。",
  },
};

function runtimeStatusCopy(status: string) {
  return (
    statusCopy[status as RuntimeStatus] ?? {
      label: "状态更新中",
      detail: "后端返回了新的连接状态，请稍后刷新后重试。",
    }
  );
}

async function responseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function toDraft(settings: FeishuSettingsPublic): DraftSettings {
  return {
    enabled: settings.enabled,
    app_id: settings.app_id,
    domain: settings.domain,
    allowedOpenIds: settings.allowed_open_ids.join("\n"),
    allow_group_mentions: settings.allow_group_mentions,
  };
}

function parseOpenIds(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\s,，;；]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  );
}

export function FeishuSettingsDialog({
  open,
  apiBase,
  onClose,
  onSettingsChanged,
}: Props) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [savedSettings, setSavedSettings] =
    useState<FeishuSettingsPublic | null>(null);
  const [draft, setDraft] = useState<DraftSettings | null>(null);
  const [appSecret, setAppSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [bindings, setBindings] = useState<IdentityBinding[]>([]);
  const [bindingsLoading, setBindingsLoading] = useState(false);
  const [bindingActionId, setBindingActionId] = useState<string | null>(null);

  const loadBindings = useCallback(
    async (signal?: AbortSignal) => {
      setBindingsLoading(true);
      try {
        const response = await fetch(`${apiBase}/api/admin/identity-bindings`, {
          signal,
        });
        if (!response.ok) throw new Error(await responseError(response));
        const body = (await response.json()) as { bindings: IdentityBinding[] };
        setBindings(body.bindings);
      } finally {
        setBindingsLoading(false);
      }
    },
    [apiBase],
  );

  useEffect(() => {
    if (!open) return;

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    closeButtonRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onClose, open]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    fetch(`${apiBase}/api/settings/feishu`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response));
        return (await response.json()) as FeishuSettingsPublic;
      })
      .then((settings) => {
        setSavedSettings(settings);
        setDraft(toDraft(settings));
        setAppSecret("");
        setShowSecret(false);
        return loadBindings(controller.signal);
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setFeedback({
          tone: "error",
          message:
            error instanceof Error ? error.message : "无法读取飞书接入配置。",
        });
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [apiBase, loadBindings, open]);

  if (!open) return null;

  function requestBody() {
    if (!draft) return null;
    if (draft.enabled && !draft.app_id.trim()) {
      setFeedback({ tone: "error", message: "启用前请填写飞书 App ID。" });
      return null;
    }
    return {
      enabled: draft.enabled,
      app_id: draft.app_id.trim(),
      app_secret: appSecret.trim() || null,
      domain: draft.domain,
      allowed_open_ids: parseOpenIds(draft.allowedOpenIds),
      allow_group_mentions: draft.allow_group_mentions,
    };
  }

  async function testConnection() {
    const body = requestBody();
    if (!body) return;
    setTesting(true);
    setFeedback({ tone: "neutral", message: "正在校验应用凭证…" });
    try {
      const response = await fetch(`${apiBase}/api/settings/feishu/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = (await response.json()) as {
        message: string;
        latency_ms: number;
      };
      setFeedback({
        tone: "success",
        message: `${result.message} 延迟 ${result.latency_ms} ms。`,
      });
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "凭证测试失败。",
      });
    } finally {
      setTesting(false);
    }
  }

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const body = requestBody();
    if (!body) return;
    setSaving(true);
    setFeedback({
      tone: "neutral",
      message: body.enabled ? "正在保存并建立长连接…" : "正在保存配置…",
    });
    try {
      const response = await fetch(`${apiBase}/api/settings/feishu`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const settings = (await response.json()) as FeishuSettingsPublic;
      setSavedSettings(settings);
      setDraft(toDraft(settings));
      setAppSecret("");
      setShowSecret(false);
      const runtime = runtimeStatusCopy(settings.runtime.status);
      setFeedback({
        tone: settings.runtime.status === "error" ? "error" : "success",
        message:
          settings.runtime.last_error ||
          `配置已保存。长连接状态：${runtime.label}。`,
      });
      onSettingsChanged(settings);
      try {
        await loadBindings();
      } catch {
        setFeedback({
          tone: "neutral",
          message: "飞书配置已保存，但账号绑定列表刷新失败，请关闭弹窗后重试。",
        });
      }
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "配置保存失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  async function updateBindingStatus(binding: IdentityBinding) {
    const nextStatus = binding.status === "active" ? "suspended" : "active";
    setBindingActionId(binding.id);
    setFeedback({
      tone: "neutral",
      message: nextStatus === "active" ? "正在启用个人工作区…" : "正在停用个人工作区…",
    });
    try {
      const response = await fetch(
        `${apiBase}/api/admin/identity-bindings/${binding.id}/status`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: nextStatus }),
        },
      );
      if (!response.ok) throw new Error(await responseError(response));
      const updated = (await response.json()) as IdentityBinding;
      setBindings((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      setFeedback({
        tone: "success",
        message:
          nextStatus === "active"
            ? `${updated.workspace_name} 已启用。`
            : `${updated.workspace_name} 已停用，新的飞书指令将被拒绝。`,
      });
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "工作区状态更新失败。",
      });
    } finally {
      setBindingActionId(null);
    }
  }

  async function clearSecret() {
    if (!confirmClear) {
      setConfirmClear(true);
      setFeedback({
        tone: "neutral",
        message: "再次点击“确认清除”会关闭飞书接入并移除本机凭证。",
      });
      return;
    }
    setClearing(true);
    try {
      const response = await fetch(
        `${apiBase}/api/settings/feishu/app-secret`,
        { method: "DELETE" },
      );
      if (!response.ok) throw new Error(await responseError(response));
      const settings = (await response.json()) as FeishuSettingsPublic;
      setSavedSettings(settings);
      setDraft(toDraft(settings));
      setAppSecret("");
      setConfirmClear(false);
      setFeedback({
        tone: "success",
        message: "App Secret 已从本机安全存储清除，飞书接入已关闭。",
      });
      onSettingsChanged(settings);
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "凭证清除失败。",
      });
    } finally {
      setClearing(false);
    }
  }

  const runtime = savedSettings?.runtime ?? {
    status: "disabled" as const,
    last_error: null,
  };
  const runtimeCopy = runtimeStatusCopy(runtime.status);
  const sameSavedApp =
    Boolean(savedSettings?.has_app_secret) &&
    savedSettings?.app_id === draft?.app_id.trim();

  return (
    <div className="settings-backdrop" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="feishu-settings-title"
        aria-describedby="feishu-settings-description"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <div>
            <p className="eyebrow">External control</p>
            <h2 id="feishu-settings-title">飞书外部接入</h2>
            <p id="feishu-settings-description">
              手机发送文本指令，电脑端调用本地 Agent，并把结果回复到原会话。
              首期使用官方长连接，无需暴露电脑公网端口。
            </p>
          </div>
          <button
            ref={closeButtonRef}
            className="dialog-close"
            type="button"
            onClick={onClose}
            aria-label="关闭飞书外部接入设置"
          >
            关闭
          </button>
        </header>

        {loading ? (
          <div className="settings-loading" role="status">
            正在读取本地配置…
          </div>
        ) : draft ? (
          <form onSubmit={saveSettings}>
            <div
              className={`channel-status-card ${runtime.status}`}
              role="status"
            >
              <span className="channel-status-dot" aria-hidden="true" />
              <div>
                <strong>长连接：{runtimeCopy.label}</strong>
                <p>{runtime.last_error || runtimeCopy.detail}</p>
              </div>
            </div>

            <div className="settings-fields channel-settings-fields">
              <label>
                <span>服务区域</span>
                <select
                  value={draft.domain}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      domain: event.target.value as "feishu" | "lark",
                    })
                  }
                >
                  <option value="feishu">飞书（中国大陆）</option>
                  <option value="lark">Lark（国际版）</option>
                </select>
              </label>

              <label>
                <span>App ID</span>
                <input
                  value={draft.app_id}
                  onChange={(event) =>
                    setDraft({ ...draft, app_id: event.target.value })
                  }
                  placeholder="cli_xxxxxxxxxxxxxxxx"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>

              <label className="api-key-field">
                <span>App Secret</span>
                <div className="password-control">
                  <input
                    type={showSecret ? "text" : "password"}
                    value={appSecret}
                    onChange={(event) => setAppSecret(event.target.value)}
                    placeholder={
                      sameSavedApp
                        ? `${savedSettings?.masked_app_secret} · 留空保持不变`
                        : "输入与 App ID 匹配的 App Secret"
                    }
                    autoComplete="new-password"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    onClick={() => setShowSecret((current) => !current)}
                    aria-label={
                      showSecret ? "隐藏新输入的 App Secret" : "显示新输入的 App Secret"
                    }
                  >
                    {showSecret ? "隐藏" : "显示"}
                  </button>
                </div>
                <small>
                  已保存的凭证不会回显。{savedSettings?.secret_storage}
                </small>
              </label>

              <label className="channel-allowlist-field">
                <span>允许的用户 Open ID</span>
                <textarea
                  value={draft.allowedOpenIds}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      allowedOpenIds: event.target.value,
                    })
                  }
                  rows={4}
                  placeholder={"ou_xxxxxxxxxxxxxxxx\n每行填写一个 Open ID"}
                  spellCheck={false}
                />
                <small>
                  空白名单不会执行任何人的指令。首次私聊机器人时，系统会回复你的
                  Open ID，便于回到这里授权。
                </small>
              </label>
            </div>

            <div className="channel-controls">
              <label className="enable-control">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(event) =>
                    setDraft({ ...draft, enabled: event.target.checked })
                  }
                />
                <span>保存后启用长连接接入</span>
              </label>
              <label className="enable-control">
                <input
                  type="checkbox"
                  checked={draft.allow_group_mentions}
                  onChange={(event) =>
                    setDraft({
                      ...draft,
                      allow_group_mentions: event.target.checked,
                    })
                  }
                />
                <span>允许群聊中已授权用户通过 @机器人 发出指令</span>
              </label>
            </div>

            <section className="identity-binding-section" aria-labelledby="identity-binding-title">
              <div className="identity-binding-heading">
                <div>
                  <h3 id="identity-binding-title">账号与个人工作区</h3>
                  <p>
                    白名单中的每个飞书账号拥有独立的记忆和资产命名空间，当前都绑定到这台电脑。
                  </p>
                </div>
                <span>{bindings.length} 个绑定</span>
              </div>
              {bindingsLoading ? (
                <p className="identity-binding-empty" role="status">
                  正在读取账号绑定…
                </p>
              ) : bindings.length ? (
                <ul className="identity-binding-list">
                  {bindings.map((binding) => {
                    const isUpdating = bindingActionId === binding.id;
                    return (
                      <li key={binding.id}>
                        <div className="identity-binding-main">
                          <div>
                            <strong>{binding.workspace_name}</strong>
                            <span title={binding.external_id}>
                              Open ID · {binding.external_id.slice(0, 10)}…{binding.external_id.slice(-4)}
                            </span>
                          </div>
                          <span className={`binding-status ${binding.status}`}>
                            {binding.status === "active"
                              ? "已启用"
                              : binding.status === "suspended"
                                ? "已停用"
                                : "已撤销"}
                          </span>
                        </div>
                        <div className="identity-binding-device">
                          <span>{binding.device_name}</span>
                          <button
                            type="button"
                            onClick={() => updateBindingStatus(binding)}
                            disabled={isUpdating}
                            aria-label={`${binding.status === "active" ? "停用" : "启用"}${binding.workspace_name}`}
                          >
                            {isUpdating
                              ? "处理中…"
                              : binding.status === "active"
                                ? "停用"
                                : "启用"}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="identity-binding-empty">
                  保存包含 Open ID 的白名单后，系统会自动创建个人工作区绑定。
                </p>
              )}
            </section>

            <div className="settings-meta">
              <span className="settings-guide-links">
                <a href="/guides/feishu" target="_blank" rel="noreferrer">
                  打开完整配置指南
                </a>
                <a
                  href="https://open.feishu.cn/document/server-docs/event-subscription-guide/overview"
                  target="_blank"
                  rel="noreferrer"
                >
                  飞书官方文档
                </a>
              </span>
              {sameSavedApp ? (
                <button
                  className={confirmClear ? "danger-confirm" : "text-button"}
                  type="button"
                  onClick={clearSecret}
                  disabled={clearing}
                >
                  {clearing
                    ? "清除中…"
                    : confirmClear
                      ? "确认清除"
                      : "清除已保存凭证"}
                </button>
              ) : null}
            </div>

            {feedback ? (
              <p
                className={`settings-feedback ${feedback.tone}`}
                role={feedback.tone === "error" ? "alert" : "status"}
              >
                {feedback.message}
              </p>
            ) : null}

            <footer className="settings-actions">
              <button
                className="secondary-action"
                type="button"
                onClick={testConnection}
                disabled={testing || saving}
              >
                {testing ? "测试中…" : "测试凭证"}
              </button>
              <button
                className="primary-action"
                type="submit"
                disabled={saving || testing}
              >
                {saving ? "保存并连接中…" : "保存配置"}
              </button>
            </footer>
          </form>
        ) : null}
      </section>
    </div>
  );
}
