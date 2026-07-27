import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent Lab · 个人数字助手",
  description: "本地优先的空间照片、个人资产与 Web Agent 工作台。",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
