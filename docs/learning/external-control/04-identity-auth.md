# 身份、认证、授权与设备绑定

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：标准原则已核对，平台实现待 PoC  
关联任务：`LEARN-CHANNEL-001`、`CHANNEL-001`、`SEC-001`

外部聊天控制本地电脑时，最危险的误区是“平台告诉我一个用户 ID，所以这个人可以
调用电脑上的全部功能”。平台事件真实性、用户登录、账号绑定和资源授权是四个不同
问题。

## 1. 先区分四件事

| 问题 | 回答 | 常用机制 |
| --- | --- | --- |
| Event Authenticity | 事件是否来自真实平台、是否被重放 | TLS、签名/加密、时间窗、event ID |
| Authentication | 当前主体是谁 | 平台会话、OIDC、设备凭证 |
| Account Linking | 平台账号对应哪个本地用户 | 一次性绑定码、用户确认 |
| Authorization | 该用户可对哪个资源做什么 | 服务端 Policy、scope、角色、所有权、审批 |

OAuth 2.0 是委托授权框架；OpenID Connect 在 OAuth 2.0 之上增加身份层和 ID Token，
用于验证终端用户身份
（[OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0-errata2.html)）。
因此“使用 OAuth”不自动等于完成登录，“拿到用户 ID”也不自动等于完成授权。

## 2. 本项目需要区分的身份

```text
Channel App / Tenant
        ↓ signed event
Platform User ── account link ── Local User
                                     ↓ policy
                     Agent Instance / Device
                                     ↓
                       Capability / Action / Asset
```

建议所有 Run、Job、Asset、Memory 和 Outbound Message 都记录：

- `local_user_id`；
- 来源 `channel/tenant/conversation/platform_user_id`；
- 执行 `device_id/agent_instance_id`；
- `capability_id/action/resource_id/risk_level`；
- Policy 和审批结果。

平台 ID 只在对应 `channel + tenant` 命名空间内解释，不能把不同平台的同名字符串当成
同一个用户。

## 3. 三条实际认证链路

### 聊天平台事件

飞书/企业微信 Adapter 按各平台官方机制验证事件，检查时间窗和 event/message ID，
然后映射到本地绑定。首期不需要为了“接收机器人消息”自行发明 OIDC 登录。

事件进入后：

1. 先验证平台和去重；
2. 查找显式 `ChannelIdentity → LocalUser` 绑定；
3. 检查会话、Capability、资源和风险级权限；
4. 未绑定用户只获得绑定引导，不能进入 Agent Tool Loop。

### Companion App 用户登录

若未来开发 iOS/HarmonyOS/Android Companion App，使用 Authorization Code + PKCE，
在系统浏览器/安全浏览器标签页完成授权。RFC 8252 要求原生应用通过外部 user-agent
授权，并要求公共原生客户端使用 PKCE；静态打包在 App 内的 shared secret 不能视为
机密
（[RFC 8252](https://www.rfc-editor.org/rfc/rfc8252.html)）。

不应：

- 在 WebView 中收集用户密码；
- 把长期 Client Secret 写进安装包；
- 使用 Implicit Grant；
- 把 access token 放在 Viewer URL；
- 只靠可伪造的自定义 URL 参数完成绑定。

### 本地电脑设备注册

Cloud Gateway 或 Companion App 与本地 Worker 建立关系时：

1. 本地电脑显示短时一次性绑定码；
2. 已登录用户在手机确认具体设备名和权限；
3. 服务端颁发每设备独立、可撤销凭证；
4. 本地安全存储凭证，只通过出站认证通道连接；
5. 丢失手机或电脑时可单独吊销设备，不影响其他设备。

设备凭证不能替代最终用户授权；多用户共享电脑时仍需 Local User 隔离。

## 4. OAuth/OIDC 安全基线

RFC 9700 是 OAuth 2.0 当前安全最佳实践，更新并扩展了早期 OAuth 安全建议
（[RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)）。

本项目采用的基线：

- Authorization Code + PKCE，使用 `S256`；
- redirect URI 精确匹配；
- `state`/OIDC `nonce` 和授权事务绑定；
- 验证 issuer、audience、签名、过期时间和 nonce；
- access token 限定 audience 和 scope；
- refresh token 轮换或检测重放；
- 不使用 Resource Owner Password Credentials Grant；
- 不把 bearer token 写入 URL、日志、Prompt 或 Trace；
- Provider metadata、JWKS 和时钟偏差有缓存及失败策略；
- 高价值场景再评估 sender-constrained token，例如 DPoP。

PKCE 防止授权码被截获后直接兑换，不负责本项目的 Capability 权限，也不替代 Token
签名、issuer/audience 和过期验证。

## 5. Viewer 与短时资产访问

聊天中发送的 Viewer URL 应携带短时、只读、单 Asset grant，而不是用户主 access
token。服务端检查：

- 随机高熵 token 或签名；
- asset、owner/audience、scope；
- `exp` 和可选 `nbf`；
- 是否已撤销；
- 下载/预览次数策略；
- 不允许由 URL 参数指定任意磁盘路径。

高敏感资产可要求用户在 Viewer 再登录；普通演示资产可以使用短时 capability URL。
URL 可能进入聊天记录、浏览器历史和平台扫描系统，因此默认按“可能泄露”设计。

## 6. 授权与审批

建议 Policy 键：

```text
local_user × channel × conversation × device
           × capability × action × resource × risk_level
```

- L0/L1 可在白名单和资源所有权满足时自动执行；
- L2/L3 生成精确审批对象，包括参数摘要、目标、费用/外发影响和过期时间；
- 审批 token 一次性使用，绑定参数 hash；
- Tool 执行服务再次检查 Policy，不能只在 Agent Prompt 检查；
- 群聊默认比私聊更严格，敏感结果不自动发回群。

## 7. Threat Checklist

- [ ] 伪造或重放平台事件；
- [ ] 不同 tenant 的平台用户 ID 碰撞；
- [ ] 绑定码被截屏或重复使用；
- [ ] OAuth authorization code 被其他 App 截获；
- [ ] Token issuer/audience 验证缺失；
- [ ] refresh token 或设备凭证泄露；
- [ ] Viewer URL 转发给未授权用户；
- [ ] 群成员借机器人访问私有 Asset；
- [ ] 审批后替换 Tool 参数；
- [ ] 删除用户后残留 Channel/Device 凭证。

## 8. PoC 验收

- 未绑定平台用户不能创建 Run；
- 同一平台 ID 在不同 tenant 不会映射到同一用户；
- 一次性绑定码过期、已用和输错均安全失败；
- Token/Viewer URL 过期和撤销测试通过；
- Policy 拒绝不进入模型自由判断；
- 审批参数改变后必须重新审批；
- 日志和错误响应不包含 Secret、完整 Token 或签名 URL。

## 9. 来源

- [RFC 9700：OAuth 2.0 Security Best Current Practice](https://www.rfc-editor.org/rfc/rfc9700.html)
- [RFC 8252：OAuth 2.0 for Native Apps](https://www.rfc-editor.org/rfc/rfc8252.html)
- [RFC 7636：Proof Key for Code Exchange](https://www.rfc-editor.org/rfc/rfc7636.html)
- [OpenID Connect Core 1.0 with errata set 2](https://openid.net/specs/openid-connect-core-1_0-errata2.html)
- [RFC 9449：OAuth 2.0 Demonstrating Proof of Possession](https://www.rfc-editor.org/rfc/rfc9449.html)

返回[外部控制与移动端知识地图](README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
