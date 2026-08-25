# R4 — Control Flow Simplification（纯减法）

> 目标：**每一个问题只能有一个 owner**。凡是一个判断出现在第二个模块里，就删。
> 用已确立的三原则反过来删除旧机制：Restrict, don't repair / Execute, don't predict / One source of truth。

## 一、唯一负责人表（判断标准）

| 问题 | 唯一负责人 |
|---|---|
| 当前在哪个状态？ | `ObservationStore.current_obs` |
| 这个状态允许做什么？ | `ActionSpace` |
| LLM 选哪个动作？ | `Policy / Planner` |
| 动作到底能不能执行？ | `ActionExecutor` |
| 这个 ref 是否属于当前状态？ | `G3` |
| ref 转成什么定位描述？ | `Compiler` |
| 运行时到底是哪一个元素？ | `Resolver` |
| 正式测试是否成功？ | `Runner` |

## 二、现状审计（2026-08-18）

**已删除（前几轮完成）**：
- ✅ `_detect_modal_hint`（未引入）
- ✅ `REPEATED_FAILED_ACTION` 主流程
- ✅ `exploration_stalled` / consecutive-rejection 机制
- ✅ 观察期 Playwright full trial（改 elementFromPoint 毫秒级）
- ✅ explore_flow.py 单文件 → explore/ 包（observation / action_space / policy / explorer）
- ✅ 探索异常静默降级 legacy（R4 本轮：Explore fail → generate fail，已改并 123 测试绿）

**待删除 / 退出主链**：
- ⬜ anti-pattern 负例注入重生 prompt（保留 record 诊断，重生只带 error）
- ⬜ Preflight hard gate → 降级为 optional diagnostics（`GENERATE_PREFLIGHT` 开关）
- ⬜ policy.py 合并回 explorer.py（Policy 非领域对象，唯一调用者是主循环）

## 三、删除清单 vs 保留清单

**删除或退出主链**：
```
anti-pattern 负例注入重生
G3 专属 repair prompt（合并进统一 replan 提示）
多轮 GQ semantic repair（bounded ×1 已达成，不再加）
missing_wait_for 自动修复（保持 warning 级别）
silent legacy fallback（已改 fail honestly）
Preflight hard gate（降级 diagnostics）
重复 actionability classifier（ActionSpace 唯一 cheap filter）
```

**保留**：
```
ObservationStore（current_obs 唯一事实源）
ActionSpace（唯一选择边界 + failed_actions filter）
StateGraph（成功转移唯一事实）
refs-only Planner（LLM 语义选择）
G3 + reachability（唯一状态合法性闸门）
Compiler（ref → LocatorSpec，保持极纯：不 resolve/repair/score/访问浏览器）
Resolver（0/1/N + confidence，不追 5-tier）
Corrections（持久化修正）
ActionExecutor（唯一执行权威）
Runner（正式测试 + evidence）
Metrics / Cache / API（infrastructure）
```

## 四、目标模块结构

```
backend/
├─ main.py
├─ generation/            # service.py / planner.py（可选拆分）
├─ explore/
│   ├─ observation.py
│   ├─ action_space.py
│   └─ explorer.py        # policy 合并进来
├─ grounding.py
├─ compiler.py
├─ locator/               # resolver.py / corrections.py
├─ execution/
│   ├─ browser_actions.py # 最薄 Playwright primitive（Explorer/Runner 共享）
│   └─ runner.py
├─ dsl.py
├─ metrics.py
└─ tests/
```

## 五、执行顺序

1. **重生简化**：`_build_retry_hint` 去 patterns 参数；重生 prompt 只带 error（+ grounding 专属可达状态提示保留）
2. **policy 合并**：policy.py 函数并入 explorer.py（删 explore/policy.py，__init__ re-export 调整）
3. **Preflight 降级**：`GENERATE_PREFLIGHT` 常量（默认 False），保留代码供调试
4. **（可选）generation/ 拆分 + browser_actions 共享**：视需要

## 六、验收标准

做完后遇到 bug，只检查五个问题（不再在十几个 guard/retry 里排查）：
```
Observation 错？
ActionSpace 错？
Transition 错？
Resolver 错？
Planner 语义错（LLM 限制，fail honestly）？
```

## 七、三问判断标准（以后加机制前）

1. 现在哪一层应该负责这个问题？已有 owner → 不加新层
2. 能不能通过缩小 action space 解决？可以 → 不写 repair
3. 失败是否真的需要恢复？最多 retry 一次仍失败 → fail honestly
