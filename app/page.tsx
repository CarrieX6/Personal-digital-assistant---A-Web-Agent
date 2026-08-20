"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AgentConsole,
  HealthInfo,
} from "./components/AgentConsole";
import { ModelSettingsDialog } from "./components/ModelSettingsDialog";
import { PhotoStyleStudio } from "./components/PhotoStyleStudio";
import { SpatialStudio } from "./components/SpatialStudio";

type ToolInfo = {
  name: string;
  description: string;
};

type View = "spatial" | "style" | "agent";

const API_BASE =
  process.env.NEXT_PUBLIC_AGENT_API_URL ?? "http://127.0.0.1:8000";

const fallbackTools: ToolInfo[] = [
  { name: "text_stats", description: "文本统计" },
  { name: "extract_keywords", description: "关键词提取" },
  { name: "current_time", description: "当前时间" },
  { name: "list_personal_assets", description: "个人资产" },
  { name: "create_spatial_scene", description: "生成空间照片" },
  { name: "create_photo_style_transfer", description: "图片风格化" },
];

export default function Home() {
  const [activeView, setActiveView] = useState<View>("spatial");
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [backendReady, setBackendReady] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [requestedSpatialAssetId, setRequestedSpatialAssetId] = useState<
    string | null
  >(null);
  const [requestedStyleAssetId, setRequestedStyleAssetId] = useState<
    string | null
  >(null);

  const refreshStatus = useCallback(async () => {
    try {
      const [healthResponse, toolResponse] = await Promise.all([
        fetch(`${API_BASE}/health`),
        fetch(`${API_BASE}/api/tools`),
      ]);
      if (!healthResponse.ok || !toolResponse.ok) {
        throw new Error("local service unavailable");
      }
      setHealth((await healthResponse.json()) as HealthInfo);
      setTools((await toolResponse.json()) as ToolInfo[]);
      setBackendReady(true);
    } catch {
      setBackendReady(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshStatus(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshStatus]);

  const availableTools = tools.length ? tools : fallbackTools;

  return (
    <main className="app-shell">
      <a className="skip-link" href="#primary-content">
        跳到主要内容
      </a>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">A</span>
          <span>Agent Lab</span>
          <span className="version">Personal · 0.5</span>
        </div>
        <div className="topbar-actions">
          <div className={`connection ${backendReady ? "is-ready" : ""}`}>
            <span className="connection-dot" aria-hidden="true" />
            {backendReady
              ? health?.llm_configured
                ? `本地服务 · ${health.model}`
                : "本地服务已连接"
              : "等待本地服务"}
          </div>
          <button
            className="settings-trigger"
            type="button"
            onClick={() => setSettingsOpen(true)}
          >
            模型设置
          </button>
        </div>
      </header>

      <section className="workspace">
        <aside className="sidebar product-sidebar">
          <div>
            <p className="eyebrow">Personal AI studio</p>
            <h1>个人数字助手</h1>
            <p className="sidebar-copy">
              一套本地优先的生成框架，逐步承载空间照片、图片风格化与更多个人内容能力。
            </p>
          </div>

          <nav className="product-nav" aria-label="产品功能">
            <button
              type="button"
              className={activeView === "spatial" ? "active" : ""}
              onClick={() => setActiveView("spatial")}
            >
              <span>01</span>
              <div>
                <strong>空间照片</strong>
                <small>2D → 可动视角</small>
              </div>
            </button>
            <button
              type="button"
              className={activeView === "style" ? "active" : ""}
              onClick={() => setActiveView("style")}
            >
              <span>02</span>
              <div>
                <strong>图片风格化</strong>
                <small>参考图 → 风格迁移</small>
              </div>
            </button>
            <button type="button" disabled>
              <span>03</span>
              <div>
                <strong>虚拟试衣</strong>
                <small>下一阶段 · 先 2D</small>
              </div>
            </button>
            <button type="button" disabled>
              <span>04</span>
              <div>
                <strong>桌面宠物</strong>
                <small>规划中 · 3D 动作</small>
              </div>
            </button>
            <button
              type="button"
              className={activeView === "agent" ? "active" : ""}
              onClick={() => setActiveView("agent")}
            >
              <span>05</span>
              <div>
                <strong>Agent 控制台</strong>
                <small>{availableTools.length} 个本地工具</small>
              </div>
            </button>
          </nav>

          <div className="privacy-card">
            <span className="privacy-mark" aria-hidden="true" />
            <div>
              <strong>本地优先</strong>
              <p>默认图片与生成物仅保存在 backend/data；远端 Provider 必须显式启用。</p>
            </div>
          </div>
        </aside>

        <div className="main-panel personal-panel" id="primary-content">
          <nav className="mobile-tabs" aria-label="移动端功能切换">
            <button
              type="button"
              className={activeView === "spatial" ? "active" : ""}
              onClick={() => setActiveView("spatial")}
            >
              空间照片
            </button>
            <button
              type="button"
              className={activeView === "style" ? "active" : ""}
              onClick={() => setActiveView("style")}
            >
              图片风格
            </button>
            <button
              type="button"
              className={activeView === "agent" ? "active" : ""}
              onClick={() => setActiveView("agent")}
            >
              Agent
            </button>
          </nav>

          {activeView === "spatial" ? (
            <SpatialStudio
              apiBase={API_BASE}
              onConnectionChange={setBackendReady}
              requestedAssetId={requestedSpatialAssetId}
            />
          ) : activeView === "style" ? (
            <PhotoStyleStudio
              apiBase={API_BASE}
              onConnectionChange={setBackendReady}
              requestedAssetId={requestedStyleAssetId}
            />
          ) : (
            <AgentConsole
              apiBase={API_BASE}
              health={health}
              onConnectionChange={setBackendReady}
              onSpatialSceneReady={(assetId) => {
                setRequestedSpatialAssetId(assetId);
                setActiveView("spatial");
              }}
              onPhotoStyleReady={(assetId) => {
                setRequestedStyleAssetId(assetId);
                setActiveView("style");
              }}
            />
          )}
        </div>
      </section>

      <ModelSettingsDialog
        open={settingsOpen}
        apiBase={API_BASE}
        onClose={() => setSettingsOpen(false)}
        onSettingsChanged={() => void refreshStatus()}
      />
    </main>
  );
}
