"use client";

import { useMemo, useState } from "react";
import { AppIcon } from "./AppIcon";
import { HealthInfo } from "./AgentConsole";

type ToolStatus = "installed" | "available" | "coming";
type Filter = "all" | ToolStatus;

type ToolLibraryProps = {
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
  available: "未安装",
  coming: "待上线",
};

const statusLabels: Record<ToolStatus, string> = {
  installed: "已安装",
  available: "未安装",
  coming: "待上线",
};

export function ToolLibrary({
  health,
  onOpenSpatial,
  onOpenStyle,
  onOpenModelSettings,
  onOpenChannelSettings,
}: ToolLibraryProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const visible = useMemo(
    () => catalog.filter((tool) => filter === "all" || tool.status === filter),
    [filter],
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
          <strong>{catalog.filter((item) => item.status === "installed").length}</strong>
          <span>项已安装</span>
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
                : catalog.filter((tool) => tool.status === item).length}
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
                  {statusLabels[tool.status]}
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
                    打开工具
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
          <strong>“未安装”不等于现在可以下载</strong>
          <p>
            当前还没有通用安装器。工具卡会明确显示真实状态；模型下载、依赖隔离、
            许可校验和卸载能力完成后，才会开放安装按钮。
          </p>
        </div>
      </aside>
    </section>
  );
}
