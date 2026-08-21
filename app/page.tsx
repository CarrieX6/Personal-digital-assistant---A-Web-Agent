"use client";

import { useCallback, useEffect, useState } from "react";
import { AgentConsole, HealthInfo } from "./components/AgentConsole";
import { AppIcon } from "./components/AppIcon";
import { FeishuSettingsDialog } from "./components/FeishuSettingsDialog";
import { ModelSettingsDialog } from "./components/ModelSettingsDialog";
import { PhotoStyleStudio } from "./components/PhotoStyleStudio";
import { SpatialStudio } from "./components/SpatialStudio";
import { ToolLibrary } from "./components/ToolLibrary";

type View = "agent" | "tools" | "spatial" | "style";

const API_BASE =
  process.env.NEXT_PUBLIC_AGENT_API_URL ?? "http://localhost:8000";

export default function Home() {
  const [activeView, setActiveView] = useState<View>("agent");
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [backendReady, setBackendReady] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [externalSettingsOpen, setExternalSettingsOpen] = useState(false);
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
      await toolResponse.json();
      setBackendReady(true);
    } catch {
      setBackendReady(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshStatus(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshStatus]);

  function openSpatial(assetId?: string) {
    setRequestedSpatialAssetId(assetId ?? null);
    setActiveView("spatial");
  }

  function openStyle(assetId?: string) {
    setRequestedStyleAssetId(assetId ?? null);
    setActiveView("style");
  }

  return (
    <main className="app-shell assistant-shell">
      <a className="skip-link" href="#primary-content">
        跳到主要内容
      </a>
      <header className="topbar assistant-topbar">
        <div className="brand">
          <span className="brand-mark">A</span>
          <span>Agent Lab</span>
          <span className="version">Personal · 0.5</span>
        </div>
        <div className="topbar-actions">
          <div
            className={`connection ${backendReady ? "is-ready" : ""}`}
            aria-label={backendReady ? "本地服务已连接" : "本地服务未连接"}
          >
            <span className="connection-dot" aria-hidden="true" />
            <span className="connection-copy">
              {backendReady
                ? health?.llm_configured
                  ? health.model
                  : "本地服务"
                : "连接中"}
            </span>
          </div>
          <button
            className={`settings-trigger icon-settings ${
              health?.feishu_status === "connected" ? "is-connected" : ""
            }`}
            type="button"
            aria-label={
              health?.feishu_status === "connected"
                ? "管理飞书连接"
                : "配置外部接入"
            }
            onClick={() => setExternalSettingsOpen(true)}
          >
            <AppIcon name="link" width="17" height="17" />
            <span>
              {health?.feishu_status === "connected" ? "飞书已连接" : "外部接入"}
            </span>
          </button>
          <button
            className="settings-trigger icon-settings"
            type="button"
            aria-label="打开模型设置"
            onClick={() => setSettingsOpen(true)}
          >
            <AppIcon name="settings" width="17" height="17" />
            <span>设置</span>
          </button>
        </div>
      </header>

      <section className="assistant-workspace">
        <aside className="app-rail" aria-label="主要导航">
          <div className="app-rail-nav">
            <button
              type="button"
              className={activeView === "agent" ? "active" : ""}
              onClick={() => setActiveView("agent")}
            >
              <AppIcon name="chat" width="21" height="21" />
              <span>对话</span>
            </button>
            <button
              type="button"
              className={
                activeView === "tools" ||
                activeView === "spatial" ||
                activeView === "style"
                  ? "active"
                  : ""
              }
              onClick={() => setActiveView("tools")}
            >
              <AppIcon name="tools" width="21" height="21" />
              <span>工具库</span>
            </button>
          </div>
          <div className="rail-privacy" title="图片、记忆和生成资产默认保存在本机">
            <span />
            <small>本地优先</small>
          </div>
        </aside>

        <div className="assistant-main" id="primary-content">
          {activeView === "agent" ? (
            <AgentConsole
              apiBase={API_BASE}
              health={health}
              onConnectionChange={setBackendReady}
              onSpatialSceneReady={(assetId) => openSpatial(assetId)}
              onPhotoStyleReady={(assetId) => openStyle(assetId)}
            />
          ) : activeView === "tools" ? (
            <ToolLibrary
              health={health}
              onOpenSpatial={() => openSpatial()}
              onOpenStyle={() => openStyle()}
              onOpenModelSettings={() => setSettingsOpen(true)}
              onOpenChannelSettings={() => setExternalSettingsOpen(true)}
            />
          ) : activeView === "spatial" ? (
            <div className="tool-detail-view">
              <button
                className="back-to-library"
                type="button"
                onClick={() => setActiveView("tools")}
              >
                <span aria-hidden="true">←</span>
                返回工具库
              </button>
              <SpatialStudio
                apiBase={API_BASE}
                onConnectionChange={setBackendReady}
                requestedAssetId={requestedSpatialAssetId}
              />
            </div>
          ) : (
            <div className="tool-detail-view">
              <button
                className="back-to-library"
                type="button"
                onClick={() => setActiveView("tools")}
              >
                <span aria-hidden="true">←</span>
                返回工具库
              </button>
              <PhotoStyleStudio
                apiBase={API_BASE}
                onConnectionChange={setBackendReady}
                requestedAssetId={requestedStyleAssetId}
              />
            </div>
          )}
        </div>
      </section>

      <ModelSettingsDialog
        open={settingsOpen}
        apiBase={API_BASE}
        onClose={() => setSettingsOpen(false)}
        onSettingsChanged={() => void refreshStatus()}
      />
      <FeishuSettingsDialog
        open={externalSettingsOpen}
        apiBase={API_BASE}
        onClose={() => setExternalSettingsOpen(false)}
        onSettingsChanged={() => void refreshStatus()}
      />
    </main>
  );
}
