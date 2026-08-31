"use client";

import { useCallback, useEffect, useState } from "react";

type ChannelMessage = {
  id: number;
  platform: "feishu";
  chat_id: string;
  sender_id?: string | null;
  direction: "inbound" | "outbound" | "system";
  kind: "text" | "markdown" | "image" | "file" | "card" | "status";
  content: string;
  created_at: string;
};

type ChannelMessageTimelineProps = {
  apiBase: string;
  connected: boolean;
  onConnectionChange: (ready: boolean) => void;
};

const kindLabels: Record<ChannelMessage["kind"], string> = {
  text: "文本",
  markdown: "回答",
  image: "图片",
  file: "文件",
  card: "功能卡片",
  status: "状态",
};

export function ChannelMessageTimeline({
  apiBase,
  connected,
  onConnectionChange,
}: ChannelMessageTimelineProps) {
  const [messages, setMessages] = useState<ChannelMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const loadMessages = useCallback(async () => {
    try {
      const response = await fetch(`${apiBase}/api/channels/messages?limit=50`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const body = (await response.json()) as { messages: ChannelMessage[] };
      setMessages(body.messages);
      setLastUpdated(new Date());
      setError("");
      onConnectionChange(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "暂时无法同步飞书消息。",
      );
    } finally {
      setLoading(false);
    }
  }, [apiBase, onConnectionChange]);

  useEffect(() => {
    const initial = window.setTimeout(() => void loadMessages(), 0);
    const interval = window.setInterval(() => void loadMessages(), 2500);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
    };
  }, [loadMessages]);

  return (
    <section className="channel-timeline-card" aria-live="polite">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">External channel</p>
          <h2>飞书消息记录</h2>
        </div>
        <div className="channel-timeline-actions">
          <span className={connected ? "is-connected" : ""}>
            {connected ? "已连接" : "未连接"}
          </span>
          <button type="button" onClick={() => void loadMessages()}>
            刷新
          </button>
        </div>
      </div>

      {loading ? (
        <div className="channel-timeline-empty">正在读取本机消息记录…</div>
      ) : error && messages.length === 0 ? (
        <div className="channel-timeline-error" role="alert">
          {error}
        </div>
      ) : messages.length === 0 ? (
        <div className="channel-timeline-empty">
          手机端向飞书机器人发送消息后，会同步显示在这里。
        </div>
      ) : (
        <ol className="channel-message-list">
          {messages.map((item) => (
            <li key={item.id} className={item.direction}>
              <div className="channel-message-meta">
                <strong>
                  {item.direction === "inbound"
                    ? "手机端"
                    : item.direction === "outbound"
                      ? "个人助手"
                      : "系统"}
                </strong>
                <span>
                  {kindLabels[item.kind]} · {formatTime(item.created_at)}
                </span>
              </div>
              <p>{item.content}</p>
            </li>
          ))}
        </ol>
      )}

      <p className="channel-timeline-footnote">
        仅保存在本机，最多保留最近 500 条
        {lastUpdated ? ` · ${formatTime(lastUpdated.toISOString())} 已同步` : ""}
      </p>
    </section>
  );
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
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
