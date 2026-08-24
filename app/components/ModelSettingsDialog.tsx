"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

type ProviderPreset = {
  id: string;
  name: string;
  description: string;
  base_url: string;
  default_model: string;
  models: string[];
  docs_url?: string | null;
};

export type LLMSettingsPublic = {
  enabled: boolean;
  provider_id: string;
  base_url: string;
  model: string;
  timeout_seconds: number;
  has_api_key: boolean;
  masked_api_key?: string | null;
  secret_storage: string;
};

type ProviderCatalog = {
  providers: ProviderPreset[];
  settings: LLMSettingsPublic;
  runtime?: LLMRuntimePublic | null;
};

type LLMRuntimeStatus =
  | "disabled"
  | "unconfigured"
  | "configured_not_enabled"
  | "testing"
  | "ready"
  | "degraded"
  | "error";

type LLMRuntimePublic = {
  status: LLMRuntimeStatus;
  active: boolean;
  provider_id: string;
  model: string;
  has_api_key: boolean;
  qa_available: boolean;
  tool_calling_available: boolean;
  last_tested_at?: string | null;
  last_error?: string | null;
};

type DraftSettings = {
  enabled: boolean;
  provider_id: string;
  base_url: string;
  model: string;
  timeout_seconds: number;
};

type Feedback = {
  tone: "success" | "error" | "neutral";
  message: string;
};

type Props = {
  open: boolean;
  apiBase: string;
  onClose: () => void;
  onSettingsChanged: (settings: LLMSettingsPublic) => void;
};

async function responseError(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    return body.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function runtimeCopy(runtime: LLMRuntimePublic | null) {
  if (!runtime) {
    return {
      title: "正在确认模型状态",
      detail: "尚未读取后端实际运行模式。",
    };
  }
  switch (runtime.status) {
    case "ready":
      return {
        title: `${runtime.model} 已启用`,
        detail: "普通问答与 Tool Calling 均可用。",
      };
    case "degraded":
      return {
        title: `${runtime.model} 部分可用`,
        detail: "普通问答可用，但 Tool Calling 尚未通过验证。",
      };
    case "configured_not_enabled":
      return {
        title: "模型已配置但尚未启用",
        detail: "API Key 已安全保存；点击“保存并启用”后才会用于回答。",
      };
    case "testing":
      return {
        title: "正在验证模型",
        detail: "正在检查普通问答和 Tool Calling。",
      };
    case "error":
      return {
        title: "模型配置异常",
        detail: runtime.last_error || "请检查模型 ID、接口地址和 API Key。",
      };
    case "disabled":
      return {
        title: "真实模型已停用",
        detail: "当前使用 Demo 模式，已保存的配置不会被删除。",
      };
    default:
      return {
        title: "尚未完成模型配置",
        detail: "填写 API Key 后测试连接，再保存并启用。",
      };
  }
}

export function ModelSettingsDialog({
  open,
  apiBase,
  onClose,
  onSettingsChanged,
}: Props) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [providers, setProviders] = useState<ProviderPreset[]>([]);
  const [savedSettings, setSavedSettings] = useState<LLMSettingsPublic | null>(
    null,
  );
  const [draft, setDraft] = useState<DraftSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [disabling, setDisabling] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [runtime, setRuntime] = useState<LLMRuntimePublic | null>(null);

  const selectedProvider = useMemo(
    () => providers.find((provider) => provider.id === draft?.provider_id),
    [draft?.provider_id, providers],
  );
  const hasKeyForDraft =
    Boolean(savedSettings?.has_api_key) &&
    savedSettings?.provider_id === draft?.provider_id;
  const currentRuntimeCopy = runtimeCopy(runtime);

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
          'button:not([disabled]), a[href], input:not([disabled]), summary, textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
    fetch(`${apiBase}/api/settings/providers`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response));
        return (await response.json()) as ProviderCatalog;
      })
      .then((catalog) => {
        setProviders(catalog.providers);
        setSavedSettings(catalog.settings);
        setRuntime(catalog.runtime ?? null);
        setDraft({
          enabled: catalog.settings.enabled,
          provider_id: catalog.settings.provider_id,
          base_url: catalog.settings.base_url,
          model: catalog.settings.model,
          timeout_seconds: catalog.settings.timeout_seconds,
        });
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setFeedback({
          tone: "error",
          message:
            error instanceof Error ? error.message : "无法读取模型配置。",
        });
      })
      .finally(() => setLoading(false));

    return () => controller.abort();
  }, [apiBase, open]);

  if (!open) return null;

  function chooseProvider(provider: ProviderPreset) {
    setDraft((current) => ({
      enabled: current?.enabled ?? true,
      provider_id: provider.id,
      base_url: provider.base_url,
      model: provider.default_model,
      timeout_seconds: current?.timeout_seconds ?? 30,
    }));
    setApiKey("");
    setShowKey(false);
    setFeedback(null);
    setConfirmClear(false);
  }

  function requestBody() {
    if (!draft) return null;
    return {
      ...draft,
      enabled: true,
      api_key: apiKey.trim() || null,
    };
  }

  async function testConnection() {
    const body = requestBody();
    if (!body) return;
    setTesting(true);
    setFeedback({
      tone: "neutral",
      message: "正在验证普通问答与 Tool Calling…",
    });
    try {
      const response = await fetch(`${apiBase}/api/settings/llm/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = (await response.json()) as {
        message: string;
        latency_ms: number;
        answer_preview: string;
      };
      setFeedback({
        tone: "success",
        message: `${result.message} 问答响应：“${result.answer_preview}” · 延迟 ${result.latency_ms} ms。`,
      });
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "连接测试失败。",
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
      message: "正在验证配置并启用真实模型…",
    });
    try {
      const response = await fetch(`${apiBase}/api/settings/llm`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const settings = (await response.json()) as LLMSettingsPublic;
      const runtimeResponse = await fetch(
        `${apiBase}/api/settings/llm/status`,
      ).catch(() => null);
      const activeRuntime = runtimeResponse?.ok
        ? ((await runtimeResponse.json()) as LLMRuntimePublic)
        : null;
      setSavedSettings(settings);
      setRuntime(
        activeRuntime ?? {
          status: "ready",
          active: true,
          provider_id: settings.provider_id,
          model: settings.model,
          has_api_key: settings.has_api_key,
          qa_available: true,
          tool_calling_available: true,
          last_tested_at: new Date().toISOString(),
          last_error: null,
        },
      );
      setDraft({
        enabled: settings.enabled,
        provider_id: settings.provider_id,
        base_url: settings.base_url,
        model: settings.model,
        timeout_seconds: settings.timeout_seconds,
      });
      setApiKey("");
      setShowKey(false);
      setFeedback({
        tone: "success",
        message:
          activeRuntime?.status === "degraded"
            ? "真实模型已启用，基础问答可用；当前模型未通过 Tool Calling 验证。"
            : "普通问答与 Tool Calling 已验证，真实模型现已启用。",
      });
      onSettingsChanged(settings);
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "配置保存失败。",
      });
    } finally {
      setSaving(false);
    }
  }

  async function disableModel() {
    if (!draft) return;
    setDisabling(true);
    setFeedback({ tone: "neutral", message: "正在停用真实模型…" });
    try {
      const response = await fetch(`${apiBase}/api/settings/llm`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...draft,
          enabled: false,
          api_key: null,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const settings = (await response.json()) as LLMSettingsPublic;
      setSavedSettings(settings);
      setDraft({ ...draft, enabled: false });
      setRuntime({
        status: "disabled",
        active: false,
        provider_id: settings.provider_id,
        model: settings.model,
        has_api_key: settings.has_api_key,
        qa_available: false,
        tool_calling_available: false,
        last_error: null,
      });
      setFeedback({
        tone: "success",
        message: "真实模型已停用，配置与 API Key 仍保存在本机。",
      });
      onSettingsChanged(settings);
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "停用模型失败。",
      });
    } finally {
      setDisabling(false);
    }
  }

  async function clearApiKey() {
    if (!confirmClear) {
      setConfirmClear(true);
      setFeedback({
        tone: "neutral",
        message: "再次点击“确认清除”将从本机安全存储中移除密钥。",
      });
      return;
    }

    setClearing(true);
    try {
      const response = await fetch(`${apiBase}/api/settings/llm/api-key`, {
        method: "DELETE",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const settings = (await response.json()) as LLMSettingsPublic;
      setSavedSettings(settings);
      setRuntime({
        status: "unconfigured",
        active: false,
        provider_id: settings.provider_id,
        model: settings.model,
        has_api_key: false,
        qa_available: false,
        tool_calling_available: false,
        last_error: null,
      });
      setDraft((current) => (current ? { ...current, enabled: false } : current));
      setApiKey("");
      setConfirmClear(false);
      setFeedback({
        tone: "success",
        message: "API Key 已清除，系统已切换到 Demo 模式。",
      });
      onSettingsChanged(settings);
    } catch (error) {
      setFeedback({
        tone: "error",
        message: error instanceof Error ? error.message : "密钥清除失败。",
      });
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="settings-backdrop" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="settings-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-settings-title"
        aria-describedby="model-settings-description"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <div>
            <p className="eyebrow">Model connection</p>
            <h2 id="model-settings-title">模型供应商设置</h2>
            <p id="model-settings-description">
              选择供应商并同时验证基础问答与 Tool Calling。只有点击“保存并启用”
              后，真实模型才会接管对话；看图问答还要求所填模型支持图片输入。
            </p>
          </div>
          <button
            ref={closeButtonRef}
            className="dialog-close"
            type="button"
            onClick={onClose}
            aria-label="关闭模型设置"
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
            <div className={`model-runtime-card ${runtime?.status ?? "unconfigured"}`}>
              <span className="model-runtime-dot" aria-hidden="true" />
              <div>
                <strong>{currentRuntimeCopy.title}</strong>
                <p>{currentRuntimeCopy.detail}</p>
              </div>
            </div>
            <fieldset className="provider-fieldset">
              <legend>选择供应商</legend>
              <div className="provider-grid">
                {providers.map((provider) => (
                  <button
                    className={`provider-card ${
                      draft.provider_id === provider.id ? "selected" : ""
                    }`}
                    type="button"
                    key={provider.id}
                    onClick={() => chooseProvider(provider)}
                    aria-pressed={draft.provider_id === provider.id}
                  >
                    <span className="provider-name">{provider.name}</span>
                    <span>{provider.description}</span>
                  </button>
                ))}
              </div>
            </fieldset>

            <div className="settings-fields">
              <label>
                <span>模型名称</span>
                <input
                  list="provider-models"
                  value={draft.model}
                  onChange={(event) =>
                    setDraft({ ...draft, model: event.target.value })
                  }
                  placeholder="输入支持 Tool Calling；看图时还需支持视觉"
                  required
                />
                <datalist id="provider-models">
                  {selectedProvider?.models.map((model) => (
                    <option value={model} key={model} />
                  ))}
                </datalist>
              </label>

              <label>
                <span>Base URL</span>
                <input
                  type="url"
                  value={draft.base_url}
                  onChange={(event) =>
                    setDraft({ ...draft, base_url: event.target.value })
                  }
                  placeholder="https://provider.example/v1"
                  spellCheck={false}
                  required
                />
              </label>

              <label className="api-key-field">
                <span>API Key</span>
                <div className="password-control">
                  <input
                    type={showKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(event) => setApiKey(event.target.value)}
                    placeholder={
                      hasKeyForDraft
                        ? `${savedSettings.masked_api_key} · 留空保持不变`
                        : savedSettings?.has_api_key
                          ? "已切换供应商，请输入对应的 API Key"
                          : "输入供应商 API Key"
                    }
                    autoComplete="new-password"
                    spellCheck={false}
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((current) => !current)}
                    aria-label={showKey ? "隐藏新输入的 API Key" : "显示新输入的 API Key"}
                  >
                    {showKey ? "隐藏" : "显示"}
                  </button>
                </div>
                <small>
                  已保存的密钥不会回显。{savedSettings?.secret_storage}
                </small>
              </label>
            </div>

            <details className="advanced-settings">
              <summary>高级设置</summary>
              <div className="advanced-row">
                <label>
                  <span>请求超时（秒）</span>
                  <input
                    type="number"
                    min={5}
                    max={120}
                    value={draft.timeout_seconds}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        timeout_seconds: Number(event.target.value),
                      })
                    }
                  />
                </label>
                <p className="advanced-hint">
                  模型启用状态由底部操作明确控制，不再随高级配置隐式切换。
                </p>
              </div>
            </details>

            <div className="settings-meta">
              {selectedProvider?.docs_url ? (
                <a
                  href={selectedProvider.docs_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看供应商官方文档
                </a>
              ) : (
                <span>
                  自定义服务需兼容 Chat Completions；视觉模型还需支持 image_url。
                </span>
              )}
              {hasKeyForDraft ? (
                <button
                  className={confirmClear ? "danger-confirm" : "text-button"}
                  type="button"
                  onClick={clearApiKey}
                  disabled={clearing}
                >
                  {clearing
                    ? "清除中…"
                    : confirmClear
                      ? "确认清除"
                      : "清除已保存密钥"}
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
              {runtime?.active || savedSettings?.enabled ? (
                <button
                  className="danger-secondary-action"
                  type="button"
                  onClick={disableModel}
                  disabled={testing || saving || disabling}
                >
                  {disabling ? "停用中…" : "停用真实模型"}
                </button>
              ) : null}
              <button
                className="secondary-action"
                type="button"
                onClick={testConnection}
                disabled={testing || saving || disabling}
              >
                {testing ? "测试中…" : "测试连接"}
              </button>
              <button
                className="primary-action"
                type="submit"
                disabled={saving || testing || disabling}
              >
                {saving ? "验证并启用中…" : "保存并启用"}
              </button>
            </footer>
          </form>
        ) : null}
      </section>
    </div>
  );
}
