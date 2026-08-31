"use client";

import { useEffect, useMemo, useState } from "react";
import { AppIcon } from "./AppIcon";
import { HealthInfo } from "./AgentConsole";

type ToolStatus = "installed" | "testing" | "available" | "coming";
type Filter = "all" | ToolStatus;

type PhotoStyleProviderStatus = {
  name: string;
  model_name: string;
  ready: boolean | null;
  details: Record<string, unknown>;
};

type ToolLibraryProps = {
  apiBase: string;
  health: HealthInfo | null;
  onOpenSpatial: () => void;
  onOpenStyle: () => void;
  onOpenModelSettings: () => void;
  onOpenChannelSettings: () => void;
};

const catalog = [
  {
    id: "spatial-photo",
    name: "空间照片",
    description: "用单张图片生成具有拖动视差的 2.5D 空间场景。",
    status: "installed" as const,
    icon: "cube" as const,
    meta: "Depth Anything V2 · 语义主体分割 · 本地运行",
  },
  {
    id: "photo-style-transfer",
    name: "图片个性化",
    description: "用一至三张参考图迁移色彩、纹理和视觉风格。",
    status: "installed" as const,
    icon: "image" as const,
    meta: "CPU 预览 · 可选本地 SDXL + IP-Adapter",
  },
  {
    id: "text-kit",
    name: "文本工具包",
    description: "字符统计、关键词提取和组合文本分析。",
    status: "installed" as const,
    icon: "text" as const,
    meta: "轻量规则 · 无需模型下载",
  },
  {
    id: "memory",
    name: "个人记忆",
    description: "保存显式偏好，并按用户和会话隔离上下文。",
    status: "installed" as const,
    icon: "memory" as const,
    meta: "SQLite · 仅本机",
  },
  {
    id: "feishu",
    name: "飞书连接器",
    description: "从手机发送指令、图片，并把结果同步回聊天窗口。",
    status: "installed" as const,
    icon: "link" as const,
    meta: "长连接 · 白名单控制",
  },
  {
    id: "vision",
    name: "通用视觉理解",
    description: "理解图片内容并支持围绕图片继续对话，而不只是生成空间照片。",
    status: "installed" as const,
    icon: "image" as const,
    meta: "随已配置的多模态模型启用 · 图片按需发送",
  },
  {
    id: "vton",
    name: "虚拟试衣",
    description: "上传人物和衣物图片，生成保持身份一致的 2D 试衣结果。",
    status: "coming" as const,
    icon: "sparkles" as const,
    meta: "模型选型与显存评测中",
  },
  {
    id: "desktop-pet",
    name: "桌面宠物",
    description: "从宠物照片创建可交互的桌面角色和物种动作。",
    status: "coming" as const,
    icon: "sparkles" as const,
    meta: "3D 重建与动作绑定调研中",
  },
];

const filterLabels: Record<Filter, string> = {
  all: "全部",
  installed: "已安装",
  testing: "测试模式",
  available: "未安装",
  coming: "待上线",
};

const statusLabels: Record<ToolStatus, string> = {
  installed: "已安装",
  testing: "测试模式",
  available: "未安装",
  coming: "待上线",
};

export function ToolLibrary({
  apiBase,
  health,
  onOpenSpatial,
  onOpenStyle,
  onOpenModelSettings,
  onOpenChannelSettings,
}: ToolLibraryProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [photoStyleProvider, setPhotoStyleProvider] =
    useState<PhotoStyleProviderStatus | null>(null);
  const [photoStyleChecked, setPhotoStyleChecked] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void fetch(`${apiBase}/api/photo-style-transfers/provider`, {
      signal: controller.signal,
    })
      .then((response) => {
        if (!response.ok) throw new Error("provider status unavailable");
        return response.json() as Promise<PhotoStyleProviderStatus>;
      })
      .then((status) => setPhotoStyleProvider(status))
      .catch(() => setPhotoStyleProvider(null))
      .finally(() => setPhotoStyleChecked(true));
    return () => controller.abort();
  }, [apiBase]);

  const resolvedCatalog = useMemo(() => {
    const details = photoStyleProvider?.details ?? {};
    const productionQuality = details.production_quality === true;
    const upstreamProvider =
      typeof details.upstream_provider === "string"
        ? details.upstream_provider
        : null;
    return catalog.map((tool) => {
      if (tool.id !== "photo-style-transfer") return tool;
      if (!photoStyleChecked) {
        return {
          ...tool,
          status: "available" as const,
          statusLabel: "检测中",
          meta: "正在检查独立图片风格化服务",
        };
      }
      if (photoStyleProvider?.ready !== true) {
        return {
          ...tool,
          status: "available" as const,
          statusLabel: "未部署",
          meta: "独立 SDXL + IP-Adapter 服务未连接",
        };
      }
      if (!productionQuality) {
        return {
          ...tool,
          status: "testing" as const,
          statusLabel: "测试模式",
          meta: `${upstreamProvider ?? "Fake Provider"} · 仅验证调用链路`,
        };
      }
      return {
        ...tool,
        status: "installed" as const,
        statusLabel: "SDXL 已就绪",
        meta: `${upstreamProvider ?? "SDXL + IP-Adapter"} · 独立服务`,
      };
    });
  }, [photoStyleChecked, photoStyleProvider]);
  const visible = useMemo(
    () =>
      resolvedCatalog.filter(
        (tool) => filter === "all" || tool.status === filter,
      ),
    [filter, resolvedCatalog],
  );

  return (
    <section className="tool-library" aria-labelledby="tool-library-title">
      <header className="library-header">
        <div>
          <p className="eyebrow">Capability library</p>
          <h1 id="tool-library-title">工具库</h1>
          <p>
            Agent 只会调用已经安装并获授权的工具。未安装能力不会被模型静默启用。
          </p>
        </div>
        <div className="library-summary" aria-label="工具状态概览">
          <strong>
            {resolvedCatalog.filter((item) => item.status === "installed").length}
          </strong>
          <span>项生产就绪</span>
        </div>
      </header>

      <div className="library-filters" role="tablist" aria-label="筛选工具状态">
        {(Object.keys(filterLabels) as Filter[]).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={filter === item}
            className={filter === item ? "active" : ""}
            onClick={() => setFilter(item)}
          >
            {filterLabels[item]}
            <span>
              {item === "all"
                ? catalog.length
                : resolvedCatalog.filter((tool) => tool.status === item).length}
            </span>
          </button>
        ))}
      </div>

      <div className="tool-catalog-grid">
        {visible.map((tool) => {
          const isFeishuReady =
            tool.id === "feishu" && health?.feishu_status === "connected";
          return (
            <article className={`tool-catalog-card ${tool.status}`} key={tool.id}>
              <div className="tool-card-topline">
                <span className="tool-card-icon">
                  <AppIcon name={tool.icon} width="22" height="22" />
                </span>
                <span className={`tool-status ${tool.status}`}>
                  {tool.status === "installed" ? (
                    <AppIcon name="check" width="13" height="13" />
                  ) : tool.status === "available" ? (
                    <AppIcon name="download" width="13" height="13" />
                  ) : (
                    <AppIcon name="clock" width="13" height="13" />
                  )}
                  {"statusLabel" in tool
                    ? tool.statusLabel
                    : statusLabels[tool.status]}
                </span>
              </div>
              <h2>{tool.name}</h2>
              <p>{tool.description}</p>
              <span className="tool-meta">
                {isFeishuReady ? "已连接 · " : ""}
                {tool.meta}
              </span>
              <div className="tool-card-actions">
                {tool.id === "spatial-photo" ? (
                  <button type="button" onClick={onOpenSpatial}>
                    打开工具
                    <AppIcon name="chevron" width="15" height="15" />
                  </button>
                ) : tool.id === "photo-style-transfer" ? (
                  <button type="button" onClick={onOpenStyle}>
                    {tool.status === "installed"
                      ? "打开工具"
                      : tool.status === "testing"
                        ? "打开链路测试"
                        : "查看部署状态"}
                    <AppIcon name="chevron" width="15" height="15" />
                  </button>
                ) : tool.id === "feishu" ? (
                  <button type="button" onClick={onOpenChannelSettings}>
                    {isFeishuReady ? "管理连接" : "完成配置"}
                    <AppIcon name="chevron" width="15" height="15" />
                  </button>
                ) : tool.id === "vision" ? (
                  <button type="button" onClick={onOpenModelSettings}>
                    配置视觉模型
                    <AppIcon name="chevron" width="15" height="15" />
                  </button>
                ) : tool.status === "coming" ? (
                  <button type="button" disabled>
                    尚未开放
                  </button>
                ) : (
                  <span>Agent 可直接调用</span>
                )}
              </div>
            </article>
          );
        })}
      </div>

      <aside className="library-notice">
        <AppIcon name="download" width="20" height="20" />
        <div>
          <strong>模型能力按真实运行状态展示</strong>
          <p>
            图片风格化可用部署管理器完成环境隔离、固定版本、健康检查和迁移配置。
            真实模型仍要求审阅许可证和执行 GPU 质量门禁，不会被普通聊天静默下载。
          </p>
        </div>
      </aside>
    </section>
  );
}
