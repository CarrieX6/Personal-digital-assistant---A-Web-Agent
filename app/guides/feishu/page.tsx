import Link from "next/link";

const steps = [
  {
    title: "创建企业自建应用",
    detail:
      "在飞书开放平台创建企业自建应用，启用机器人能力，并在“凭证与基础信息”复制 App ID 与 App Secret。",
  },
  {
    title: "申请消息与图片权限",
    detail:
      "申请接收消息、以机器人身份发送消息，以及 im:resource（获取与上传图片或文件资源）。权限变化后需要重新发布版本。",
  },
  {
    title: "配置消息事件",
    detail:
      "进入“事件与回调 → 事件配置”，选择长连接并添加 im.message.receive_v1。这一步负责文字和图片消息接收。",
  },
  {
    title: "配置卡片回调（可选）",
    detail:
      "进入“事件与回调 → 回调配置”，选择长连接并添加 card.action.trigger。它不在事件配置中；只有卡片按钮需要。",
  },
  {
    title: "发布并设置可用范围",
    detail:
      "创建应用版本，把测试成员或部门加入可用范围，完成发布与管理员审核。未进入可用范围的用户无法正常使用。",
  },
  {
    title: "连接本机 Agent",
    detail:
      "在首页“外部接入”填写 App ID 与 App Secret，先测试凭证，再启用并保存，等待状态变为“已连接”。",
  },
  {
    title: "获取 Open ID 并授权",
    detail:
      "白名单为空时先私聊机器人，复制它返回的 ou_... Open ID，添加到 Web 控制台白名单后重新保存。",
  },
  {
    title: "验证私聊、群聊和图片",
    detail:
      "私聊发送“现在几点？”。群聊需开启群聊开关、把机器人入群并 @机器人；单图可生成空间照片，图片风格化会返回预览图和可下载文件。",
  },
];

const issues = [
  ["测试成功但未连接", "检查是否启用、服务区域、长连接配置和网络。"],
  ["已连接但不回复", "检查 im.message.receive_v1、版本发布、可用范围和 Open ID 白名单。"],
  ["群聊无反应", "检查机器人是否入群、群聊开关、用户白名单，并确认消息中 @机器人。"],
  ["图片失败", "检查 im:resource 权限，重新发布版本后再测试。"],
  ["卡片按钮无效", "在“回调配置”添加 card.action.trigger，不要在“事件配置”中寻找。"],
];

export default function FeishuSetupGuide() {
  return (
    <main className="guide-page">
      <article className="guide-shell">
        <header className="guide-hero">
          <Link className="guide-back" href="/">
            ← 返回 Web Agent
          </Link>
          <p className="eyebrow">External control guide</p>
          <h1>飞书机器人配置指南</h1>
          <p>
            从创建应用到私聊、群聊和图片验证，按顺序完成即可。预计首次配置需要
            15–30 分钟。
          </p>
          <div className="guide-warning">
            <strong>数据可见性说明</strong>
            <span>
              Web 控制台是运行电脑的 Root 管理员视图，可以查看本机记录的所有 Web
              和飞书会话。普通用户只应使用飞书，不应获得 Web 控制台访问权。
            </span>
          </div>
        </header>

        <section className="guide-section" aria-labelledby="before-start">
          <h2 id="before-start">开始前</h2>
          <ul className="guide-checklist">
            <li>运行 Agent 的电脑可以持续联网，并已启动后端和 Web 控制台。</li>
            <li>你拥有飞书企业自建应用的开发或管理权限。</li>
            <li>
              用户既要进入飞书应用“可用范围”，也要进入本项目 Open ID 白名单。
            </li>
            <li>App Secret 只填写在本机，不发送到聊天或提交到 Git。</li>
          </ul>
        </section>

        <section className="guide-section" aria-labelledby="setup-steps">
          <h2 id="setup-steps">配置步骤</h2>
          <div className="guide-steps">
            {steps.map((step, index) => (
              <section className="guide-step" key={step.title}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.detail}</p>
                </div>
              </section>
            ))}
          </div>
        </section>

        <section className="guide-section" aria-labelledby="troubleshooting">
          <h2 id="troubleshooting">快速排障</h2>
          <div className="guide-table-wrap">
            <table className="guide-table">
              <thead>
                <tr>
                  <th>现象</th>
                  <th>优先检查</th>
                </tr>
              </thead>
              <tbody>
                {issues.map(([issue, check]) => (
                  <tr key={issue}>
                    <td>{issue}</td>
                    <td>{check}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="guide-section guide-links" aria-labelledby="references">
          <h2 id="references">官方资料与完整文档</h2>
          <a
            href="https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case"
            target="_blank"
            rel="noreferrer"
          >
            飞书：使用长连接接收事件
          </a>
          <a
            href="https://open.feishu.cn/document/server-docs/application-scope/introduction?lang=zh-CN"
            target="_blank"
            rel="noreferrer"
          >
            飞书：应用权限管理
          </a>
          <a
            href="https://open.feishu.cn/document/develop-a-card-interactive-bot/faqs"
            target="_blank"
            rel="noreferrer"
          >
            飞书：卡片回调常见问题
          </a>
        </section>
      </article>
    </main>
  );
}
