# 前端设计风格与排版

## 文档定位

本文件从属于 [AI 自动化测试增强项目规划](./AI%20自动化测试增强项目规划.md)，用于描述前端目标形态与页面设计，不单独定义产品主线。

- 前端设计必须服务于核心规划中的 Planner、Locator、Reporter 与项目级资产展示需求。
- 正式执行能力以后端 Runner 为准，前端不承载官方执行逻辑。

## 当前落地状态（截至 2026-04-06）

### 整体设计风格：NotebookLM 三栏浮岛

前端采用类似 Google NotebookLM 的三栏浮岛布局风格：

- **外层容器**：`height: 100vh`，浅灰底色 `#f8f9fa`，内间距 `16px`，栏间距 `16px`
- **左侧栏**（280px）：白色圆角卡片（`border-radius: 16px, box-shadow: 0 2px 10px rgba(0,0,0,0.03)`），底部包含页面导航
- **中间栏**（flex:1）：白色圆角卡片，承载主内容
- **右侧栏**（340px）：透明背景，垂直 flex 容器（gap: 12px），存放多张分离的小卡片
- **导航**：侧边栏底部固定区域，替代传统顶部 header

### 主题 Token

通过 Ant Design ConfigProvider 全局注入：

| 组件 | 关键 Token |
|------|-----------|
| 全局 | `colorPrimary: #1a1a2e`, `borderRadius: 8`, `fontFamily: Inter, PingFang SC, Microsoft YaHei` |
| Button | `borderRadius: 8`, `colorPrimary: #1a1a2e` |
| Input | `borderRadius: 12`, `borderWidth: 0`, `activeShadow` |
| Card | `borderRadius: 16`, `boxShadowTertiary: 0 2px 10px rgba(0,0,0,0.03)` |
| Table | `borderWidth: 0`, `borderRadius: 12` |
| Select | `borderRadius: 12`, `borderWidth: 0` |
| Tag | `borderRadiusSM: 12` |

### 全局 CSS 基础类

| 类名 | 用途 |
|------|------|
| `.nb-card` | 白色圆角卡片基础样式（border-radius: 16px） |
| `.chat-bubble-user` | 用户消息气泡（深色背景 #1a1a2e） |
| `.chat-bubble-ai` | AI 消息气泡（浅灰蓝背景 #f0f4f8） |
| `.step-item` / `.step-item-active` | 步骤列表项（hover 灰底 / 选中蓝底 + 左边框） |
| `.action-grid-item` | 右栏操作网格方块 |
| `.panel-scroll` | 面板内滚动条美化（4px 细滚动条） |

### 已落地页面

| 页面 | 路由 | 布局 | 说明 |
|------|------|------|------|
| PlanningPage | `/` | 三栏 | AI 规划对话：左栏需求进度，中栏 AI 对话 + 底部输入框，右栏规划进度 + DSL 草案 |
| CasesPage | `/cases` | 三栏 | 用例中心：左栏搜索筛选，中栏用例卡片网格，右栏统计面板 |
| ReportPage | `/reports` | 两栏 | 项目报告：左栏项目列表，中栏概览统计卡片 + 可展开执行结果列表含步骤证据 |
| ExecutionDetailPage | `/run/:executionId` | 三栏 | 执行详情：左栏步骤时间线，中栏截图 + 证据，右栏统计 + 定位策略 + 候选元素 |

### 辅助组件

| 组件 | 文件 | 用途 |
|------|------|------|
| NotebookNav | `components/NotebookNav.tsx` | 侧边栏底部页面导航（AI 规划 / 用例中心 / 报告） |
| NotebookLMLayout | `layouts/NotebookLMLayout.tsx` | 三栏浮岛布局容器，支持 `leftPanel / centerPanel / rightCards` |
| ChatMessage | `components/ChatMessage.tsx` | AI 对话消息气泡 |
| ChatInput | `components/ChatInput.tsx` | 圆角悬挂式 AI 输入框 |
| StepList | `components/StepList.tsx` | 测试步骤列表（搜索 + 列表 + Add Action） |
| StatCard | ReportPage 内联 | 报告页概览统计卡片 |
| ExecutionRow | ReportPage 内联 | 可展开执行结果行 |
| StepRow | ReportPage 内联 | 步骤证据行（含截图、定位信息、错误信息） |

### 旧页面迁移

| 旧页面 | 旧路由 | 现状 |
|--------|--------|------|
| DashboardPage | `/dashboard` | 重定向到 `/` |
| LoginPage | `/login` | 重定向到 `/`，认证改为 `require_demo_user` |
| ExecutionsPage | `/executions` | 重定向到 `/cases` |
| CaseWorkbenchPage | `/cases/new` | 已删除，功能由 ReportPage 和 AI 规划覆盖 |
| AISettingsPage | `/settings/ai` | 已删除 |
| CorrectionsPage | `/corrections` | 已删除 |
| ReportCenterPage | `/reports/center` | 已删除，报告能力合并到 ReportPage |

## 可视化重点

1. 步骤流转：左侧步骤时间线或列表，点击联动中间和右侧面板
2. 元素定位：target、候选元素、最终命中，通过定位策略 Tag 展示
3. 证据记录：截图大图预览、URL、Console/Network 日志、断言结果
4. 测试资产：项目级聚合统计（通过率、失败数、平均耗时）、执行趋势

## 交互原则

- 正式测试执行由后端 Runner 完成，前端只做触发与展示。
- AI 对话采用气泡样式，底部圆角输入框，建议标签引导。
- 报告页优先保证排障效率——概览统计一目了然，步骤证据可展开查看。
- 三栏布局中，左栏和右栏为辅助信息区，中栏始终为主操作/展示区。
