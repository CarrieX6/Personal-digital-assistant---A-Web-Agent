"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AppIcon } from "./AppIcon";
import { HealthInfo } from "./AgentConsole";

type CapabilityState =
  | "not_installed"
  | "checking"
  | "incompatible"
  | "license_required"
  | "ready_to_install"
  | "downloading"
  | "installing"
  | "validating"
  | "ready"
  | "degraded"
  | "failed"
  | "update_available";

type HostFacts = {
  system: string;
  release: string;
  architecture: string;
  python_version: string;
  node_version?: string | null;
  profile: string;
  profile_label: string;
  gpu_name?: string | null;
  gpu_memory_mb?: number | null;
  nvidia_driver?: string | null;
  cuda_toolkit?: string | null;
  docker_available: boolean;
  memory_gb?: number | null;
  disk_free_gb: number;
  is_dgx_spark: boolean;
  validation_note: string;
  runtime_dependencies: HostDependency[];
  quickstart_command: string;
};

type HostDependency = {
  id: string;
  name: string;
  status: "ready" | "missing" | "outdated" | "optional";
  detected?: string | null;
  required: string;
  required_for: string;
  blocking: boolean;
  repair_command?: string | null;
  repair_steps: string[];
  docs_url?: string | null;
};

type Capability = {
  id: string;
  name: string;
  description: string;
  state: CapabilityState;
  state_label: string;
  runtime: string;
  model: string;
  installed: boolean;
  can_install: boolean;
  compatibility: "supported" | "experimental" | "manual" | "blocked";
  reason?: string | null;
  guide_path: string;
  active_job_id?: string | null;
};

type InstallStep = {
  id: string;
  title: string;
  detail: string;
  progress: number;
};

type InstallPlan = {
  capability_id: string;
  title: string;
  supported: boolean;
  compatibility: "supported" | "experimental" | "manual" | "blocked";
  state: CapabilityState;
  summary: string;
  reason?: string | null;
  download_gb?: number | null;
  disk_required_gb?: number | null;
  requires_license_acceptance: boolean;
  license_urls: string[];
  steps: InstallStep[];
  validation_checks: string[];
  recovery: string[];
  guide_path: string;
  device_profile: string;
};

type InstallJob = {
  id: string;
  capability_id: string;
  status:
    | "queued"
    | "checking"
    | "downloading"
    | "installing"
    | "validating"
    | "ready"
    | "failed"
    | "cancelled";
  progress: number;
  stage: string;
  message: string;
  error?: string | null;
  logs: string[];
  attempt: number;
};

type SetupCenterProps = {
  apiBase: string;
  health: HealthInfo | null;
  onOpenModelSettings: () => void;
  onOpenChannelSettings: () => void;
  onOpenMemorySettings: () => void;
};

const activeStates = new Set([
  "queued",
  "checking",
  "downloading",
  "installing",
  "validating",
]);

async function responseError(response: Response) {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail || `请求失败（${response.status}）`;
  } catch {
    return `请求失败（${response.status}）`;
  }
}

function formatDisk(value?: number | null) {
  return typeof value === "number" ? `${value.toFixed(1)} GiB` : "未检测";
}

function formatGpuMemory(value?: number | null) {
  return typeof value === "number" ? `${(value / 1024).toFixed(1)} GiB` : "统一/未检测";
}

function compatibilityLabel(value: Capability["compatibility"]) {
  if (value === "supported") return "已支持";
  if (value === "experimental") return "工程预览";
  if (value === "manual") return "需人工部署";
  return "需要适配";
}

export function SetupCenter({
  apiBase,
  health,
  onOpenModelSettings,
  onOpenChannelSettings,
  onOpenMemorySettings,
}: SetupCenterProps) {
  const [host, setHost] = useState<HostFacts | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [jobs, setJobs] = useState<InstallJob[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [plan, setPlan] = useState<InstallPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [planning, setPlanning] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [licensesAccepted, setLicensesAccepted] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [copiedCommand, setCopiedCommand] = useState<string | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const [hostResponse, capabilitiesResponse, jobsResponse] = await Promise.all([
      fetch(`${apiBase}/api/setup/host`, { signal }),
      fetch(`${apiBase}/api/setup/capabilities`, { signal }),
      fetch(`${apiBase}/api/setup/jobs`, { signal }),
    ]);
    if (!hostResponse.ok) throw new Error(await responseError(hostResponse));
    if (!capabilitiesResponse.ok) {
      throw new Error(await responseError(capabilitiesResponse));
    }
    if (!jobsResponse.ok) throw new Error(await responseError(jobsResponse));
    setHost((await hostResponse.json()) as HostFacts);
    setCapabilities((await capabilitiesResponse.json()) as Capability[]);
    setJobs((await jobsResponse.json()) as InstallJob[]);
  }, [apiBase]);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal)
      .catch((error) => {
        if (
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError")
        ) {
          return;
        }
        setFeedback(
          error instanceof Error ? error.message : "无法读取本机安装状态。",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [refresh]);

  const hasActiveJob = jobs.some((job) => activeStates.has(job.status));
  useEffect(() => {
    if (!hasActiveJob) return;
    const timer = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, 1200);
    return () => window.clearInterval(timer);
  }, [hasActiveJob, refresh]);

  const selectedCapability = useMemo(
    () => capabilities.find((item) => item.id === selectedId) ?? null,
    [capabilities, selectedId],
  );
  const selectedJob = useMemo(
    () => jobs.find((job) => job.capability_id === selectedId) ?? null,
    [jobs, selectedId],
  );
  const selectedJobIsActive = Boolean(
    selectedJob && activeStates.has(selectedJob.status),
  );
  const readyCount = capabilities.filter((item) => item.installed).length;

  async function openPlan(capability: Capability) {
    setSelectedId(capability.id);
    setPlan(null);
    setFeedback(null);
    setConfirmed(false);
    setLicensesAccepted(false);
    setPlanning(true);
    try {
      const response = await fetch(
        `${apiBase}/api/setup/capabilities/${capability.id}/plan`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error(await responseError(response));
      setPlan((await response.json()) as InstallPlan);
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "无法生成安装计划。");
    } finally {
      setPlanning(false);
    }
  }

  async function startInstall() {
    if (!plan) return;
    setInstalling(true);
    setFeedback(null);
    try {
      const response = await fetch(
        `${apiBase}/api/setup/capabilities/${plan.capability_id}/install`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            confirm_install: confirmed,
            accept_licenses: licensesAccepted,
          }),
        },
      );
      if (!response.ok) throw new Error(await responseError(response));
      const job = (await response.json()) as InstallJob;
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setFeedback("安装已经在当前设备上开始，可以留在本页查看进度。");
      await refresh();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "无法开始安装。 ");
    } finally {
      setInstalling(false);
    }
  }

  async function cancelJob(jobId: string) {
    try {
      const response = await fetch(`${apiBase}/api/setup/jobs/${jobId}/cancel`, {
        method: "POST",
      });
      if (!response.ok) throw new Error(await responseError(response));
      await refresh();
    } catch (error) {
      setFeedback(error instanceof Error ? error.message : "无法停止安装任务。");
    }
  }

  async function copyCommand(id: string, command: string) {
    try {
      await navigator.clipboard.writeText(command);
      setCopiedCommand(id);
      window.setTimeout(() => setCopiedCommand((current) => current === id ? null : current), 1800);
    } catch {
      setFeedback("浏览器没有允许复制，请手动选中命令复制到终端。 ");
    }
  }

  return (
    <section className="setup-center" aria-labelledby="setup-center-title">
      <header className="setup-hero">
        <div>
          <p className="eyebrow">One device · complete node</p>
          <h1 id="setup-center-title">设置与模型安装</h1>
          <p>
            当前设备同时运行 Web Agent、飞书连接、记忆与资产、Viewer
            和模型服务。这里不会把私人数据或模型任务转交给另一台控制电脑。
          </p>
        </div>
        <div className="setup-readiness" aria-label="当前节点就绪情况">
          <strong>{readyCount}/{capabilities.length || "—"}</strong>
          <span>核心能力就绪</span>
        </div>
      </header>

      <div className="setup-quick-grid">
        <button type="button" onClick={onOpenModelSettings}>
          <span className="setup-icon"><AppIcon name="sparkles" width="20" height="20" /></span>
          <span><strong>对话模型</strong><small>{health?.llm_configured ? `${health.model} 已启用` : "需要配置供应商与 API Key"}</small></span>
          <AppIcon name="chevron" width="16" height="16" />
        </button>
        <button type="button" onClick={onOpenChannelSettings}>
          <span className="setup-icon"><AppIcon name="link" width="20" height="20" /></span>
          <span><strong>飞书入口</strong><small>{health?.feishu_status === "connected" ? "长连接已建立" : "尚未连接手机端"}</small></span>
          <AppIcon name="chevron" width="16" height="16" />
        </button>
        <button type="button" onClick={onOpenMemorySettings}>
          <span className="setup-icon"><AppIcon name="memory" width="20" height="20" /></span>
          <span><strong>记忆与数据</strong><small>本机 SQLite、资产和备份</small></span>
          <AppIcon name="chevron" width="16" height="16" />
        </button>
      </div>

      <section className="host-panel" aria-labelledby="host-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Host preflight</p>
            <h2 id="host-title">这台设备</h2>
          </div>
          <button type="button" onClick={() => void refresh()} aria-label="重新检测设备">
            <AppIcon name="refresh" width="17" height="17" />
            重新检测
          </button>
        </div>
        {host ? (
          <>
            <div className="host-facts">
              <div><span>运行档位</span><strong>{host.profile_label}</strong></div>
              <div><span>系统</span><strong>{host.system} {host.release}</strong></div>
              <div><span>处理器架构</span><strong>{host.architecture}</strong></div>
              <div><span>Python / Node</span><strong>{host.python_version} / {host.node_version || "未检测"}</strong></div>
              <div><span>GPU</span><strong>{host.gpu_name || "未检测到"}</strong></div>
              <div><span>显存</span><strong>{formatGpuMemory(host.gpu_memory_mb)}</strong></div>
              <div><span>CUDA / 驱动</span><strong>{host.cuda_toolkit || "—"} / {host.nvidia_driver || "—"}</strong></div>
              <div><span>内存</span><strong>{formatDisk(host.memory_gb)}</strong></div>
              <div><span>可用磁盘</span><strong>{formatDisk(host.disk_free_gb)}</strong></div>
              <div><span>容器环境</span><strong>{host.docker_available ? "已检测" : "未检测"}</strong></div>
            </div>
            <p className={`host-note ${host.is_dgx_spark ? "attention" : ""}`}>
              <AppIcon name={host.is_dgx_spark ? "alert" : "shield"} width="18" height="18" />
              {host.validation_note}
            </p>
            <div className="runtime-checks" aria-labelledby="runtime-checks-title">
              <div className="runtime-checks-heading">
                <div>
                  <strong id="runtime-checks-title">启动环境</strong>
                  <p>缺失项会显示对应系统的修复命令；安装完成后点击“重新检测”。</p>
                </div>
                <code>{host.quickstart_command}</code>
              </div>
              <div className="runtime-check-list">
                {host.runtime_dependencies.map((dependency) => {
                  const ready = dependency.status === "ready";
                  return (
                    <article key={dependency.id} className={ready ? "ready" : "needs-action"}>
                      <span className="runtime-check-icon" aria-hidden="true">
                        <AppIcon name={ready ? "check" : "alert"} width="15" height="15" />
                      </span>
                      <div className="runtime-check-copy">
                        <div>
                          <strong>{dependency.name}</strong>
                          <em>{ready ? "已就绪" : dependency.status === "outdated" ? "版本需更新" : "缺失"}</em>
                        </div>
                        <p>{dependency.required_for}</p>
                        <small>当前：{dependency.detected || "未检测到"} · 要求：{dependency.required}</small>
                        {!ready && dependency.repair_steps.length > 0 && (
                          <ol>{dependency.repair_steps.map((step) => <li key={step}>{step}</li>)}</ol>
                        )}
                        {!ready && dependency.repair_command && (
                          <div className="repair-command">
                            <code>{dependency.repair_command}</code>
                            <button type="button" onClick={() => void copyCommand(dependency.id, dependency.repair_command!)}>
                              {copiedCommand === dependency.id ? "已复制" : "复制命令"}
                            </button>
                          </div>
                        )}
                        {!ready && dependency.docs_url && <a href={dependency.docs_url} target="_blank" rel="noreferrer">查看官方安装说明</a>}
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          </>
        ) : (
          <p className="setup-empty">{loading ? "正在检测设备…" : "设备信息不可用。"}</p>
        )}
      </section>

      <section className="capability-installer" aria-labelledby="capability-installer-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Capability installer</p>
            <h2 id="capability-installer-title">本机能力</h2>
          </div>
          <span className="owner-only"><AppIcon name="shield" width="15" height="15" />仅本机所有者可安装</span>
        </div>

        <div className={`installer-layout ${selectedId ? "has-detail" : ""}`}>
          <div className="install-catalog">
            {capabilities.map((capability) => (
              <button
                key={capability.id}
                type="button"
                className={selectedId === capability.id ? "selected" : ""}
                onClick={() => void openPlan(capability)}
              >
                <span className={`capability-state ${capability.state}`}>
                  {capability.state === "ready" ? (
                    <AppIcon name="check" width="15" height="15" />
                  ) : capability.state === "incompatible" ? (
                    <AppIcon name="alert" width="15" height="15" />
                  ) : (
                    <AppIcon name="download" width="15" height="15" />
                  )}
                </span>
                <span className="capability-copy">
                  <span><strong>{capability.name}</strong><em className={capability.state}>{capability.state_label}</em></span>
                  <small>{capability.model}</small>
                  <small>{capability.runtime} · {compatibilityLabel(capability.compatibility)}</small>
                </span>
                <AppIcon name="chevron" width="16" height="16" />
              </button>
            ))}
          </div>

          <aside className="install-detail" aria-live="polite">
            {!selectedId ? (
              <div className="install-placeholder">
                <AppIcon name="download" width="24" height="24" />
                <strong>先选择一项能力</strong>
                <p>系统会先生成计划，不会因为打开页面而下载模型。</p>
              </div>
            ) : planning || !plan || !selectedCapability ? (
              <div className="install-placeholder"><strong>正在生成本机安装计划…</strong></div>
            ) : (
              <>
                <div className="install-detail-heading">
                  <div>
                    <span className={`compatibility ${plan.compatibility}`}>{compatibilityLabel(plan.compatibility)}</span>
                    <h3>{plan.title}</h3>
                    <p>{plan.summary}</p>
                  </div>
                  <button className="detail-close" type="button" onClick={() => { setSelectedId(null); setPlan(null); }} aria-label="关闭安装计划">
                    <AppIcon name="close" width="18" height="18" />
                  </button>
                </div>

                {(plan.download_gb || plan.disk_required_gb) && (
                  <div className="install-costs">
                    <span>预计下载<strong>{formatDisk(plan.download_gb)}</strong></span>
                    <span>所需磁盘<strong>{formatDisk(plan.disk_required_gb)}</strong></span>
                    <span>当前可用<strong>{formatDisk(host?.disk_free_gb)}</strong></span>
                  </div>
                )}

                {plan.reason && <p className="plan-reason"><AppIcon name="alert" width="17" height="17" />{plan.reason}</p>}

                {selectedJobIsActive && selectedJob ? (
                  <div className="install-progress">
                    <div><strong>{selectedJob.message}</strong><span>{selectedJob.progress}%</span></div>
                    <progress max="100" value={selectedJob.progress}>{selectedJob.progress}%</progress>
                    <small>第 {selectedJob.attempt} 次执行 · 页面关闭后任务仍保存在本机</small>
                    <button type="button" onClick={() => void cancelJob(selectedJob.id)}>停止安装</button>
                  </div>
                ) : (
                  <ol className="install-steps">
                    {plan.steps.map((step, index) => (
                      <li key={step.id}><span>{index + 1}</span><div><strong>{step.title}</strong><p>{step.detail}</p></div></li>
                    ))}
                  </ol>
                )}

                {selectedJob?.status === "failed" && (
                  <div className="job-error">
                    <strong>上一次没有安装成功</strong>
                    <p>{selectedJob.error || selectedJob.message}</p>
                    <small>可以修复提示的问题后重新执行；安装器不会自动运行未知命令。</small>
                  </div>
                )}
                {selectedJob?.status === "ready" && (
                  <div className="job-success"><AppIcon name="check" width="17" height="17" /><span>{selectedJob.message}</span></div>
                )}

                {plan.validation_checks.length > 0 && (
                  <div className="validation-list">
                    <strong>完成前必须通过</strong>
                    <ul>{plan.validation_checks.map((item) => <li key={item}><AppIcon name="check" width="13" height="13" />{item}</li>)}</ul>
                  </div>
                )}

                {plan.supported && plan.steps.length > 0 && selectedJob?.status !== "ready" && !selectedJobIsActive && (
                  <div className="install-confirmation">
                    <label><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />我已查看磁盘、网络与运行影响，确认在当前设备安装。</label>
                    {plan.requires_license_acceptance && (
                      <label><input type="checkbox" checked={licensesAccepted} onChange={(event) => setLicensesAccepted(event.target.checked)} />我已阅读第三方模型许可证，并由本人接受这些条款。</label>
                    )}
                    {plan.license_urls.length > 0 && (
                      <div className="license-links">{plan.license_urls.map((url) => <a key={url} href={url} target="_blank" rel="noreferrer">查看许可证来源</a>)}</div>
                    )}
                    <button className="primary-install" type="button" disabled={installing || !confirmed || (plan.requires_license_acceptance && !licensesAccepted)} onClick={() => void startInstall()}>
                      <AppIcon name={selectedJob?.status === "failed" ? "refresh" : "download"} width="17" height="17" />
                      {installing ? "正在创建任务…" : selectedJob?.status === "failed" ? "重新安装" : "开始安装"}
                    </button>
                  </div>
                )}

                {!plan.supported && (
                  <div className="manual-gate">
                    <strong>当前不会自动执行</strong>
                    <p>这不是功能缺失提示，而是兼容性与许可证门禁。完成目标设备实测后再开放一键安装。</p>
                    <code>{plan.guide_path}</code>
                  </div>
                )}

                <details className="recovery-note">
                  <summary>如果安装中断怎么办</summary>
                  <ul>{plan.recovery.map((item) => <li key={item}>{item}</li>)}</ul>
                </details>

                {selectedJob && selectedJob.logs.length > 0 && (
                  <details className="install-logs"><summary>查看最近安装日志</summary><pre>{selectedJob.logs.slice(-40).join("\n")}</pre></details>
                )}
              </>
            )}
          </aside>
        </div>
        {feedback && <p className="setup-feedback">{feedback}</p>}
      </section>

      <section className="migration-panel" aria-labelledby="migration-title">
        <div className="section-heading"><div><p className="eyebrow">Full migration</p><h2 id="migration-title">换电脑时完整迁移</h2></div></div>
        <div className="migration-flow">
          <article><span>1</span><strong>旧电脑导出</strong><p>暂停写入后，导出 SQLite、资产、配置清单和校验值；Secret 不明文打包。</p></article>
          <article><span>2</span><strong>目标电脑部署</strong><p>在目标机克隆仓库并启动 Web Agent，由本页识别系统、架构、GPU 与磁盘。</p></article>
          <article><span>3</span><strong>恢复数据与密钥</strong><p>校验备份后恢复数据；LLM Key 与飞书 Secret 在新设备重新录入。</p></article>
          <article><span>4</span><strong>本机安装模型</strong><p>按目标机档位重新安装模型，完成离线、质量、显存和真实飞书验收。</p></article>
        </div>
        <p className="migration-rule"><AppIcon name="shield" width="17" height="17" />迁移完成后，旧 Mac 可以关机；目标节点仍能独立接收飞书指令、执行 Agent、保存记忆并返回结果。</p>
      </section>
    </section>
  );
}
