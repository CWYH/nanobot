# Teams 三角色持续讨论方案（Manager + 2 IC）

## 1. 目标与范围

### 目标
在同一个 Teams chat group 中运行 3 个 nanobot：
- `manager`：持续发起问题、推进讨论、追踪进度
- `ic_1`：偏研究/分析执行
- `ic_2`：偏实现/验证执行

形成持续消息流：当群内长时间无新消息时，`manager` 自动发起追问、催办或新议题。

### 范围边界
1. 每个 nanobot 使用各自账号（账号信息在各自 config 中）。
2. **不包含**账号分离管理机制（如租户隔离、统一账号池、自动发号）。
3. 方案重点：
     - 人格差异 + 身份/上下级关系感知
     - 持续对话（防停滞）
     - 各 bot 自主管理 memory 并记住历史

---

## 2. 总体架构

采用 **3 个独立 nanobot 实例（建议 3 个独立 Docker 容器）**，都接入同一个 Teams group：

- 进程 A：`manager-bot`（Teams 账号 A）
- 进程 B：`ic1-bot`（Teams 账号 B）
- 进程 C：`ic2-bot`（Teams 账号 C）

每个 bot：
- 有自己的 `config.json`
- 有自己的 workspace（含 memory、session、日志）
- 监听同一个 Teams 会话消息
- 根据“角色规则”判断是否该回复

> 关键点：同群可见、决策独立、记忆独立。

---

## 3. 角色人格与组织关系建模

### 3.0 identity 是否写在 SOUL.md？
建议采用 **分层策略**，而不是只放在单一位置：

1. `SOUL.md`：放“全体 bot 共享且稳定”的价值观与通用行为原则。
2. `config.json`（每个 bot 独立）：放角色身份与组织关系（`roleId`、`org`、`persona`）。
3. `ContextBuilder` 运行时注入：将 `SOUL.md + identity config` 合并成最终系统提示词。

这样做的原因：
- 角色差异是实例级配置，适合在配置层管理，便于一键复制出多个不同人格 bot。
- `SOUL.md` 保持稳定，不会因为新增一个角色就改动全局模板。
- 可避免“同一镜像不同角色”场景下的人格串线。

结论：**identity 不建议只写在 SOUL.md**；应以配置为主、SOUL 为底座、运行时拼装。

### 3.1 角色元数据（建议）
在每个 bot 的配置中定义统一字段（可放入 `agent.identity` 或自定义扩展段）：

```json
{
    "roleId": "manager",
    "displayName": "Manager",
    "persona": {
        "style": "结构化、推进式、偏提问",
        "goals": ["拆解问题", "分配任务", "追踪进度", "保持讨论活跃"],
        "dont": ["直接替下属完成全部工作"]
    },
    "org": {
        "manager": null,
        "subordinates": ["ic_1", "ic_2"],
        "peers": []
    }
}
```

`ic_1` 与 `ic_2` 使用同结构：
- `org.manager = "manager"`
- `org.subordinates = []`
- `persona` 的技能侧重点不同（研究 vs 实现）

### 3.2 注入到系统提示词（零改动方案）

`ContextBuilder` 已在 `BOOTSTRAP_FILES` 中包含 `IDENTITY.md`，启动时会自动加载 workspace 下的该文件。因此 **不需要改动 ContextBuilder 代码或引入模板变量**，只需在 Gateway 启动时根据 `config.json` 中的 identity 配置，自动生成 `{workspace}/IDENTITY.md` 即可。

#### 实施方式
在 `cli/commands.py` 的 `gateway()` 启动流程中（`sync_workspace_templates` 之后），增加一步：
1. 读取 `config.agents.defaults.identity`（§3.1 的角色元数据）。
2. 将 `roleId`、`displayName`、`org`、`persona` 渲染为结构化 Markdown。
3. 写入 `{workspace}/IDENTITY.md`。

#### 生成的 IDENTITY.md 示例
```markdown
# Role Identity

You are **Manager** (roleId: manager).

## Organization
- Your subordinates: ic_1, ic_2
- You have no manager.

## Persona
- Style: 结构化、推进式、偏提问
- Goals: 拆解问题, 分配任务, 追踪进度, 保持讨论活跃
- Don't: 直接替下属完成全部工作

## Reply Rules
- You see ALL messages in the group chat. Not every message requires your reply.
- Only reply when: you are explicitly mentioned (@Manager), the topic falls within your responsibilities, or the discussion has stalled and needs your intervention.
- When other team members are having a productive exchange, observe silently.
```

#### 优势
- ContextBuilder 零行改动，`IDENTITY.md` 天然被 `_load_bootstrap_files()` 加载。
- 角色内容由 config 驱动、启动时生成，不会出现"人格串线"。
- 可通过编辑 IDENTITY.md 做热调试，无需重启。

---

## 4. 持续对话机制（核心）

### 4.1 事件驱动 + 定时驱动双轨
1. **事件驱动**：群内有新消息时，相关 bot 正常响应。
2. **定时驱动（防停滞）**：定期检查该群“最后消息时间”，若超过阈值，触发 manager 主动发言。

### 4.2 空闲超时策略
建议阈值（可配置）：
- `idleWarnAfterSec = 300`（5 分钟）
- `idleNudgeAfterSec = 900`（15 分钟）
- `idleNewTopicAfterSec = 1800`（30 分钟）

动作分级：
1. 5 分钟无消息：manager 询问当前卡点（轻提醒）
2. 15 分钟无消息：manager 点名各 IC 汇报阶段进展（强提醒）
3. 30 分钟无消息：manager 提出新问题或切换子话题（重启讨论）

### 4.3 manager 发言守则（避免刷屏）
- 单次 idle 只触发一种动作
- 连续 2 次 manager 主动发言后，如果仍无人响应，降低频率（如退避到 30 分钟）
- 同一主题的催办消息需“换角度”，避免重复

### 4.4 IC 回复守则（防抢答与防死锁）
- **明确指向优先**：收到 manager 点名（@）或明确分配自身的任务时才立即响应，避免开放式问题引发广播风暴。
- **防抢答机制**：对全局问题，IC 内部可引入小范围随机延迟（Jitter），若在此期间监听到其他成员已认领或解答，则评估后保持静默。
- **标准汇报格式**：响应内容包含：当前状态、下一步、阻塞项。
- **反向驱动（防单点故障）**：IC 遇到耗时较长或卡点阻塞时，应主动配置内部 Cron/定时器向 manager 求助或同步进度，不纯粹依赖 manager 的单向轮询。

### 4.5 主动检索与总结（强烈建议而非硬性要求）

适用对象：`manager`、`ic_1`、`ic_2`（全员）。

基本原则：
- 在回答任务型问题时，每个 agent 默认先进行外部信息检索，再输出结论。
- 输出应包含“结论 + 检索摘要 + 不确定性说明（如有）”，避免只给主观判断。

触发条件（满足任一即触发检索）：
1. 涉及外部事实、最新信息、技术方案对比、官方文档说明。
2. 涉及不在当前对话与 memory 中的知识缺口。
3. manager 发起“请给依据/请给来源”的明确要求。

建议执行流程（把工具决策权交还给大模型）：
1. 当判断问题超出上下文、涉及事实性缺失时，请求且建议调用 `web_search` 补充外部信息。
2. 视问题复杂度由模型动态决定检索与拉取页面（`web_fetch`）的深度，不强制锁死固定轮次，降低 Tool 调用的无谓耗时。
3. 先在内部形成要点摘要，再在群里给出结构化回复。

例外（强烈建议直接跳过检索以防延迟）：
- 纯进度汇报类消息（如“我正在做 X，预计 Y 完成”）。
- 对话中刚刚给出、且无需外部验证的重复确认。
- 本地文件/本地代码可直接得出结论的问题。

输出格式建议：
- `结论`：先给可执行结论。
- `依据摘要`：2~4 条要点（来自检索内容）。
- `下一步`：建议动作或待确认项。

### 4.6 Manager 选题原则与示例

Manager 发起的问题质量直接决定讨论深度与 IC 协作效果。好的问题应满足以下原则：

#### 选题原则

1. **需要分布式认知，非单点可解**：问题必须同时需要"调研/分析"和"验证/建模"两种思维才能得出结论，使两个 IC 有明确的分工空间。若一个 IC 独立 30 秒就能给出最终答案，说明问题过于简单。
2. **答案非唯一确定，存在权衡空间**：最佳议题的结论不是"对或错"，而是"在 A/B/C 方案中选一个并论证"。纯数学计算题、LeetCode 算法题有唯一正确答案，不适合此场景。
3. **需要外部信息检索**：问题中包含 IC 不可能仅凭内部知识回答的事实性缺口（如最新数据、行业案例、法规细节），迫使 IC 使用 `web_search` + `web_fetch` 补充信息，与 §4.5 联动。
4. **天然支持多轮迭代**：一个 IC 的中间产出会改变另一个 IC 的工作方向。例如 IC_1 的调研结果推翻了 IC_2 的初始假设，需要 IC_2 修正方案后再交叉验证。
5. **可拆解为子任务**：Manager 发出问题后，IC 可以主动将其拆为"我负责 X，你负责 Y"的认领模式，而非两人做同一件事。

#### 不适合的问题类型

| 类型 | 原因 |
|---|---|
| 纯数学计算（求解方程、证明定理） | 答案唯一，单 agent 深度推理效率更高 |
| 标准算法题（LeetCode、ACM） | 推理链线性，无需分工和检索 |
| 简单事实查询（"X 的首都是哪"） | 一轮检索即可完成，无需讨论 |
| 主观闲聊（"你觉得什么颜色好看"） | 无法收敛，讨论无终止条件 |

#### 示例问题库（供 Manager 参考）

**技术架构决策类**

1. "我们有一个日均 5000 万事件的实时数据管道，目前用 Kafka + Flink，团队在考虑换成 Redpanda + RisingWave。请评估迁移的收益、风险和分阶段策略。"
   - IC_1 调研性能对比与社区案例，IC_2 构建成本模型与灰度方案，需多轮校验吞吐量数据和 exactly-once 语义差异。

2. "公司要在东南亚部署 SaaS 产品，需要满足印尼 GR 71 和新加坡 PDPA 数据驻留要求。请设计一套多区域数据架构，兼顾合规与性能。"
   - IC_1 研究各国法规细节，IC_2 设计 DB 分片策略和跨区同步机制。IC_1 发现印尼"原始数据不出境"要求后，IC_2 需重新调整读写分离方案。

3. "评估将一个 200 万行 Java 单体逐步迁移到 Go 微服务的可行性。团队有 20 名 Java 开发者，Go 经验为零。给出 18 个月路线图。"
   - IC_1 调研业界大规模语言迁移案例，IC_2 分析模块耦合度与拆分顺序。IC_2 发现核心交易模块 ORM 深度耦合后，IC_1 补充 Go ORM 替代方案调研。

**商业战略分析类**

4. "一家年营收 2 亿美元的中型 B2B SaaS 公司，核心产品是 CRM，想要评估是否应该自研 AI 功能还是集成第三方 LLM API。请从技术、商业、竞争三个维度给出完整分析。"
   - IC_1 研究竞品 AI 策略和市场数据，IC_2 估算自研成本与 API 集成架构。IC_1 发现竞品 AI 功能留存率提升 15% 后，IC_2 重新计算 ROI。

5. "分析 Costco 和山姆会员店在中国市场的竞争策略差异，预判未来 3 年谁将占据更大的市场份额，并解释原因。"
   - IC_1 调研开店节奏与本土化策略，IC_2 对比财报数据并构建定量模型。IC_1 发现山姆线上占比远超 Costco 后，IC_2 修正模型加入电商权重。

**复杂工程可行性类**

6. "一座人口 50 万的北方城市想将全部公交系统替换为自动驾驶电动巴士。请给出 5 年实施路线图，包括技术选型、基础设施改造、法规审批和成本估算。"
   - IC_1 调研国内无人巴士落地案例，IC_2 测算车辆采购和充电设施费用。IC_1 发现北方冬季低温对电池衰减严重后，IC_2 需重新计算续航模型。

**跨学科开放研究类**

7. "全球海运集装箱的空箱调配每年造成约 200 亿美元浪费。请分析目前最前沿的优化方案，评估可折叠集装箱的大规模商业化前景。"
   - IC_1 研究折叠集装箱技术现状，IC_2 建立成本模型。IC_1 发现折叠率 4:1 但单箱重量增加 15% 后，IC_2 需重新评估载重对航线的影响。

8. "比较核聚变发电的三条技术路线——托卡马克、仿星器、惯性约束——在 2035 年前实现商业发电的概率，并推荐一条路线进行投资。"
   - IC_1 调研各路线最新里程碑，IC_2 构建投资分析框架。多轮讨论私营融合公司（如 Commonwealth Fusion）是否改变格局。

---

## 5. 记忆设计（每个 bot 自主管理）

### 5.1 独立存储
每个 bot 指向独立 workspace，例如：
- `~/.nanobot/workspace/manager/`
- `~/.nanobot/workspace/ic1/`
- `~/.nanobot/workspace/ic2/`

每个 workspace 内各自维护：
- conversation history
- memory entries
- consolidation state

### 5.2 共享上下文来源（防 Session 碎片化）
虽然 memory 独立，但三者都在同一 Teams 群读取公开消息，因此：
- 群消息是"共享事实来源"
- 每个 bot 对共享事实的归纳、抽象、偏好记忆是"私有解释层"

#### 关键约束：三 Bot 必须使用 `group_policy: "open"`

若 Bot 使用 `group_policy: "mention"`，则只有被 `@` 时才会将消息纳入 session history。由于 Teams mention 能力（§11）暂不实现，Manager 发出的纯文本 `@IC-1` 不会被 Channel 层识别为有效 mention，导致 IC 的 session history 出现大段空白（碎片化），下次被激活时严重缺乏上下文。

**MVP 阶段方案：Channel 层全放行，Prompt 层控回复。**

```json
{
    "channels": {
        "teams": {
            "group_policy": "open"
        }
    }
}
```

- **Channel 层（open）**：所有群消息都进入每个 Bot 的 AgentLoop，确保 session history 完整。
- **Prompt 层（IDENTITY.md）**：通过角色人格规则约束"何时应该回复、何时应该保持沉默"。Agent 收到消息后会经过 LLM 推理决定是否需要发言——不需要发言时，LLM 的回复不会被发送到群里。
- **防广播风暴**：此约束与 §4.4 的防抢答守则配合生效。每个 Bot 的 IDENTITY.md 中包含"非指向性消息保持沉默"的硬约束。

> 当 §11 Teams mention 能力实现后，可将 IC 切回 `group_policy: "mention"` 以降低 LLM 调用量。过渡期的成本折中是：多出的 LLM 调用换来完整的 session 上下文。

### 5.3 建议记忆分类
- `task_state`: 当前任务阶段、owner、deadline
- `facts`: 已确认事实或结论
- `open_questions`: 待解问题
- `interaction_prefs`: 其他 bot 的沟通偏好（如 ic_1 偏简短、ic_2 偏技术细节）

---

## 6. 最小落地实施步骤（MVP）

1. 为 3 个 bot 准备 3 份独立配置（账号、workspace、role identity）。
2. 明确 prompt 分层：`SOUL.md` 放通用原则；角色 identity 仅放配置层。
3. 在 gateway 启动时根据 config 中的 identity 配置自动生成 `IDENTITY.md` 到 workspace（ContextBuilder 零改动）。
4. 以 Docker Compose 启动 3 个独立容器（manager/ic1/ic2），分别挂载各自配置与 workspace。
5. 增加“群空闲检测任务”（cron/service）：只由 manager-bot 执行。
6. 实现 idle 分级动作（轻提醒/强提醒/新话题）。
7. 在 IC 的角色规则中固定汇报格式（状态/下一步/阻塞）。
8. 为三角色加入“主动检索与总结”规则（web_search + web_fetch 最小步骤）。
9. 观察 24h 消息流，调参（阈值、退避间隔、发言长度）。

---

## 7. 配置建议（示例结构）

> 仅示例关键字段，真实字段名按 `nanobot/config/schema.py` 对齐。

```json
{
    "agent": {
        "name": "manager-bot",
        "identity": {
            "roleId": "manager",
            "displayName": "Manager",
            "org": { "manager": null, "subordinates": ["ic_1", "ic_2"] },
            "persona": {
                "style": "推进式",
                "goals": ["发起问题", "追踪进度", "维持活跃"]
            }
        }
    },
    "channels": {
        "teams": {
            "enabled": true,
            "targetThread": "<same-thread-id>"
        }
    },
    "workspace": "~/.nanobot/workspace/manager",
    "discussion": {
        "idleWarnAfterSec": 300,
        "idleNudgeAfterSec": 900,
        "idleNewTopicAfterSec": 1800,
        "maxConsecutiveManagerNudges": 2
    }
}
```

`ic1/ic2` 配置改 `roleId`、`persona`、`workspace` 即可。

---

## 8. 风险与控制

1. **风险：自说自话循环**
     - 控制：manager 触发加退避；要求 IC 回复必须包含新信息。

2. **风险：重复话题导致噪音**
     - 控制：manager 在发新话题前先查询最近 N 条消息，避免重复。

3. **风险：记忆漂移（长期后角色失真）**
     - 控制：在系统提示中保留不可变角色规则；定期 consolidation。

4. **风险：容器间配置误挂载（角色串线）**
         - 控制：每个服务使用只读配置挂载 + 独立 workspace 挂载；启动时打印 roleId 自检。

5. **风险：未检索直接回答导致幻觉**
         - 控制：将“主动检索与总结”设为角色硬约束，并在验收中抽样检查是否含检索依据。

---

## 9. Docker 部署建议（每 bot 一个容器）

### 9.1 目录约定

```text
deploy/
    manager/config.json
    ic1/config.json
    ic2/config.json
state/
    manager/
    ic1/
    ic2/
```

### 9.2 docker-compose 示例

```yaml
services:
    nanobot-manager:
        image: nanobot:latest
        container_name: nanobot-manager
        command: ["nanobot", "gateway", "--config", "/etc/nanobot/config.json"]
        volumes:
            - ./deploy/manager/config.json:/etc/nanobot/config.json:ro
            - ./state/manager:/home/nanobot/.nanobot/workspace
        restart: unless-stopped

    nanobot-ic1:
        image: nanobot:latest
        container_name: nanobot-ic1
        command: ["nanobot", "gateway", "--config", "/etc/nanobot/config.json"]
        volumes:
            - ./deploy/ic1/config.json:/etc/nanobot/config.json:ro
            - ./state/ic1:/home/nanobot/.nanobot/workspace
        restart: unless-stopped

    nanobot-ic2:
        image: nanobot:latest
        container_name: nanobot-ic2
        command: ["nanobot", "gateway", "--config", "/etc/nanobot/config.json"]
        volumes:
            - ./deploy/ic2/config.json:/etc/nanobot/config.json:ro
            - ./state/ic2:/home/nanobot/.nanobot/workspace
        restart: unless-stopped
```

### 9.3 运行约束
- 3 个容器必须使用不同 Teams 凭据。
- 3 个容器都指向同一个 Teams chat/thread。
- 仅 manager 容器开启 idle 主动唤醒任务，IC 容器不执行该任务。

---

## 10. 验收标准

满足以下条件即认为方案达标：

1. 三个 bot 在同一 Teams 群可稳定互相对话。
2. 任意 30 分钟窗口内，若无人发言，manager 能主动恢复讨论。
3. IC 能基于历史对话延续上下文，不出现“失忆式重复提问”。
4. 三个 bot 的发言风格和职责可区分（manager/研究 IC/实现 IC）。
5. 三个 bot 的容器彼此独立重启，且不会互相污染 memory/workspace。
6. 抽样任务回复中，三角色均能体现“先检索后回答”，且回复包含依据摘要。

---

## 11. Teams 发送 @ 能力需求（暂不实现）

### 11.1 需求目标
- bot 在 Teams 群聊/频道发送消息时，支持真正的 @ 指定成员（触发 Teams 通知），而不是仅发送纯文本 `@名字`。
- 该能力用于 manager 对下属点名催办、IC 对 manager 定向汇报。

### 11.2 范围
- 本阶段只写入 spec，不做代码实现。
- 仅覆盖“发送时 @ 他人”，不扩展到复杂批量群发策略。

### 11.3 预期行为
1. 当 agent 输出中包含目标 mention 指令时，Teams channel 发送层应构造 Graph mention 消息体。
2. 支持单条消息 @1~N 个成员，且正文中能按顺序显示对应 `@显示名`。
3. 若目标用户无法解析（缺少映射或 ID 无效），应降级为普通文本并记录 warning 日志，不阻断消息发送。

### 11.4 别名映射与动态学习（替代纯静态配置）
为避免跨环境硬编码 AAD User IDs 带来的维护灾难，采用**被动学习**优先的机制：

在 `channels.teams` 下增加配置：

```json
{
    "teams": {
        "mention_enabled": true,
        "auto_learn_mentions": true
    }
}
```

机制说明：
- **动态推断**：Channel 适配器在监听到群内其他成员（人类或其他 Bot）发言时，从 payload 自动抓取并缓存 `{DisplayName: UserID}` 到系统内存或内部 kv 库。
- **动态转换**：当 Bot 输出内容包含类似 `@IC-1` 时，拦截层直接从缓存表中精确或模糊匹配对应的 UserID 进行 Graph Api 转换，无需人工维护清单。
- 仅在首次启动无任何发信记录时，可保留极少量的静态配置字典作为 fallback 手段。

### 11.5 发送协议草案（实现约束）
- 发送层应使用 Graph API mention 规范（HTML `<at id="...">` + `mentions[]`），而非仅纯文本。
- `body.contentType` 应支持 `html`。
- mention 解析逻辑应在 channel 发送层完成，不要求 agent 直接拼装 Graph 原始结构。

### 11.6 验收标准（未来实现时）
1. bot 发送 `@目标成员` 后，目标成员可在 Teams 客户端收到 mention 通知。
2. 同一条消息可稳定包含多个 mention，显示与通知均正确。
3. 用户映射缺失时，消息仍可发出且系统有可检索告警日志。
4. 不影响现有非 mention 消息发送路径。

---

## 12. 本方案不覆盖

- 多账号生命周期管理（申请、轮换、回收）
- 多团队/多群组大规模编排
- 成本控制与模型动态路由策略

以上内容可在 MVP 稳定后作为下一阶段扩展。