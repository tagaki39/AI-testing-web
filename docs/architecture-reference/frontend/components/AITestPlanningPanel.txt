import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Checkbox, Input, Progress, Select, Tag, Typography, message } from "antd";
import { DeleteOutlined, SendOutlined, CheckCircleFilled, LoadingOutlined, ClockCircleOutlined } from "@ant-design/icons";
import { useQueryClient } from "@tanstack/react-query";

import {
  createPlanningSession,
  deletePlanningDraft,
  deletePlanningSession,
  generatePlanningDrafts,
  getPlanningSession,
  getSessionEvents,
  listPlanningSessions,
  saveAndExecuteDrafts,
  sendPlanningMessage,
  updatePlanningDraftStatus,
} from "../services/api";
import { callSSE, cancelExecution } from "../services/sseClient";
import type {
  AIPlanningDraft,
  AIPlanningMessage,
  AIPlanningPlan,
  AIPlanningRequirements,
  AIPlanningSessionSummary,
  AIPlanningToolCall,
  AISettings,
  DSLCaseInputContract,
  DSLCaseOutputContract,
  DSLCasePayload,
  DSLStep,
  ExecutionStreamEvent,
  ExecutionSummaryResult,
} from "../types/api";
import { NotebookLMLayout } from "../layouts/NotebookLMLayout";
import { SessionProjectPanel } from "./SessionProjectPanel";

type AITestPlanningPanelProps = {
  aiSettings?: AISettings | null;
  sessionId: number;
  currentCase?: DSLCasePayload | null;
  currentSteps?: DSLStep[] | null;
  currentInputContract?: DSLCaseInputContract[] | null;
  currentOutputContract?: DSLCaseOutputContract[] | null;
  onImportDraft: (draft: AIPlanningDraft) => void | Promise<void>;
  draftImportLabel?: string;
};

type RequirementFieldMeta = {
  key: keyof AIPlanningRequirements;
  label: string;
};

const REQUIREMENT_FIELDS: RequirementFieldMeta[] = [
  { key: "app_under_test", label: "被测系统" },
  { key: "business_goal", label: "业务目标" },
  { key: "entry_url_or_page", label: "入口页面或 URL" },
  { key: "core_user_flow", label: "核心流程" },
  { key: "main_assertions", label: "关键断言" },
  { key: "test_data_or_account", label: "测试数据或账号" },
  { key: "scope_limits", label: "范围限制" },
];

const DEFAULT_REQUIREMENTS: AIPlanningRequirements = {
  app_under_test: null,
  business_goal: null,
  entry_url_or_page: null,
  core_user_flow: null,
  main_assertions: [],
  test_data_or_account: null,
  scope_limits: null,
};

function formatRequirementValue(value: AIPlanningRequirements[keyof AIPlanningRequirements]) {
  if (Array.isArray(value)) {
    return value.length ? value.join("、") : null;
  }
  return value?.trim() ? value : null;
}

function createOptimisticMessage(
  sessionId: number,
  role: AIPlanningMessage["role"],
  turnType: AIPlanningMessage["turn_type"],
  content: string,
  structuredPayload?: Record<string, unknown> | null,
): AIPlanningMessage {
  return {
    id: -Date.now() - Math.floor(Math.random() * 1000),
    session_id: sessionId,
    role,
    turn_type: turnType,
    content,
    structured_payload: structuredPayload ?? null,
    created_at: new Date().toISOString(),
  };
}

function buildToolMessages(sessionId: number, toolCalls: AIPlanningToolCall[]) {
  return toolCalls.map((toolCall) =>
    createOptimisticMessage(
      sessionId,
      "assistant",
      "tool_call",
      `调用工具：${toolCall.tool}`,
      { type: "tool_call", ...toolCall },
    ),
  );
}

function applyStreamEventToContent(currentContent: string, event: ExecutionStreamEvent): string {
  switch (event.type) {
    case "save_progress":
      return `已保存 ${event.saved_count}/${event.total} 个用例…`;
    case "case_start":
      return `正在执行：${event.case_name}（${event.total_steps}步）`;
    case "step_start":
      return `步骤 ${event.step_index + 1}：${event.action}…`;
    case "step_complete":
      return `步骤 ${event.step_index + 1}：${event.action} — ${event.status === "passed" ? "✅" : "❌"}（${event.duration_ms}ms）`;
    default:
      return currentContent;
  }
}

function applyStreamEventToPayload(
  current: Record<string, unknown> | null,
  event: ExecutionStreamEvent,
): Record<string, unknown> {
  const base = current ?? { type: "execution_progress", saved_count: 0, total: 0, cases: [] };
  switch (event.type) {
    case "save_progress":
      return { ...base, saved_count: event.saved_count, total: event.total };
    case "case_start":
      return {
        ...base,
        cases: [
          ...((base.cases as unknown[]) ?? []),
          { case_id: event.case_id, case_name: event.case_name, total_steps: event.total_steps, steps: [] },
        ],
      };
    case "step_start":
    case "step_complete":
      return base;
    default:
      return base;
  }
}

const phaseColorMap: Record<string, string> = {
  thinking: "processing",
  generating: "warning",
  tool_calling: "warning",
  draft_generating: "warning",
  executing: "success",
};

export function AITestPlanningPanel({
  aiSettings,
  sessionId: sessionIdProp,
  currentCase,
  currentSteps,
  currentInputContract,
  currentOutputContract,
  onImportDraft,
  draftImportLabel,
}: AITestPlanningPanelProps) {
  const [messageApi, contextHolder] = message.useMessage();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [transcript, setTranscript] = useState<AIPlanningMessage[]>([]);
  const [requirements, setRequirements] = useState<AIPlanningRequirements>(DEFAULT_REQUIREMENTS);
  const [missingSlots, setMissingSlots] = useState<string[]>([]);
  const [suggestedQuestions, setSuggestedQuestions] = useState<string[]>([]);
  const [plan, setPlan] = useState<AIPlanningPlan | null>(null);
  const [drafts, setDrafts] = useState<AIPlanningDraft[]>([]);
  const [selectedScenarioKeys, setSelectedScenarioKeys] = useState<string[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isBootstrapping, setIsBootstrapping] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [isGenerating, setIsGenerating] = useState(false);
  const [sessionList, setSessionList] = useState<AIPlanningSessionSummary[]>([]);
  const activeAssistantMessageIdRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [isLoadingHistory, setIsLoadingHistory] = useState(false);

  const planningEnabled = Boolean(aiSettings?.enable_ai_planning);
  const isDisabled = !planningEnabled;

  function clearStreamingOnMessage(targetId: number | null) {
    if (targetId == null) return;
    setTranscript((current) =>
      current.map((msg) => {
        if (msg.id !== targetId) return msg;
        const payload = msg.structured_payload as Record<string, unknown> | null;
        if (!payload?._streaming) return msg;
        return { ...msg, structured_payload: { ...payload, _streaming: false } };
      }),
    );
  }

  function clearAllStreaming() {
    setTranscript((current) =>
      current.map((msg) => {
        const payload = msg.structured_payload as Record<string, unknown> | null;
        if (!payload?._streaming) return msg;
        return { ...msg, structured_payload: { ...payload, _streaming: false } };
      }),
    );
  }

  async function loadSessionList() {
    setIsLoadingHistory(true);
    try {
      const list = await listPlanningSessions();
      setSessionList(list);
    } catch {
      // silently fail — session list is non-critical
    } finally {
      setIsLoadingHistory(false);
    }
  }

  function applySessionDetail(detail: Awaited<ReturnType<typeof getPlanningSession>>) {
    setSessionId(detail.session.id);
    setRequirements(detail.session.requirements);
    setMissingSlots(detail.session.missing_slots);
    setSuggestedQuestions([]);
    setPlan(detail.session.plan ?? null);
    setTranscript(
      detail.messages.map((msg) => {
        const payload = msg.structured_payload as Record<string, unknown> | null;
        // Messages with turn_type "streaming" were persisted mid-stream but never
        // finalized — keep them flagged so the UI can show a recovery indicator.
        if (msg.turn_type === "streaming") {
          return { ...msg, structured_payload: { ...(payload ?? {}), _streaming: true, _interrupted: true } };
        }
        if (payload?._streaming) {
          return { ...msg, structured_payload: { ...payload, _streaming: false } };
        }
        return msg;
      }),
    );
    setDrafts(detail.drafts);
  }

  /**
   * Enhanced version of applySessionDetail that also replays missed SSE events
   * from the event log when an interrupted streaming message is detected.
   */
  async function applySessionDetailWithRecovery(
    detail: Awaited<ReturnType<typeof getPlanningSession>>,
  ) {
    // 1. Apply the DB state first (shows whatever was last flushed).
    applySessionDetail(detail);

    // 2. Find any interrupted streaming message.
    const interruptedMsg = detail.messages.find((m) => m.turn_type === "streaming");
    if (!interruptedMsg) return;

    // 3. Replay events from the event log to recover latest state.
    try {
      const events = await getSessionEvents(detail.session.id, 0);

      let recoveredContent = interruptedMsg.content;
      let recoveredPhase: string | null = null;
      let recoveredPhaseMessage: string | null = null;
      let hasCompleted = false;
      let thinkingContent = "";

      for (const evt of events) {
        // Only process events for this specific message.
        if (evt.message_id !== interruptedMsg.id && evt.message_id !== null) continue;

        const data = evt.event_data as Record<string, unknown>;
        switch (evt.event_type) {
          case "text_chunk":
            if (!data.thinking) {
              recoveredContent += (data.text as string) || "";
            } else {
              thinkingContent += (data.text as string) || "";
            }
            break;
          case "status":
            recoveredPhase = (data.phase as string) || recoveredPhase;
            recoveredPhaseMessage = (data.message as string) || recoveredPhaseMessage;
            break;
          case "tool_call_start":
            recoveredPhase = "tool_calling";
            recoveredPhaseMessage = `正在调用工具: ${data.tool || ""}`;
            break;
          case "tool_call_end":
            recoveredPhase = "thinking";
            recoveredPhaseMessage = "正在分析需求...";
            break;
          case "turn_complete":
            hasCompleted = true;
            // Use the final assistant message from the event if available.
            const payload = data.payload as Record<string, unknown> | undefined;
            if (payload?.assistant_message) {
              recoveredContent = payload.assistant_message as string;
            }
            break;
        }
      }

      // 4. Update the transcript with recovered content.
      setTranscript((current) =>
        current.map((msg) => {
          if (msg.id !== interruptedMsg.id) return msg;
          if (hasCompleted) {
            // Stream actually completed — mark as done.
            return {
              ...msg,
              turn_type: "followup" as const,
              content: recoveredContent,
              structured_payload: {
                ...(msg.structured_payload ?? {}),
                _streaming: false,
                _interrupted: false,
                _recovered: true,
                ...(thinkingContent ? { _thinkingContent: thinkingContent } : {}),
              },
            };
          }
          // Stream is still in progress on the server — show recovered content
          // with a "recovering" indicator so the user knows it's being restored.
          return {
            ...msg,
            content: recoveredContent || msg.content,
            structured_payload: {
              ...(msg.structured_payload ?? {}),
              _streaming: true,
              _interrupted: false,
              _recovered: true,
              _phase: recoveredPhase || "thinking",
              _phaseMessage: recoveredPhaseMessage || "正在恢复...",
              ...(thinkingContent ? { _thinkingContent: thinkingContent } : {}),
            },
          };
        }),
      );
    } catch {
      // Event replay failed — keep the _interrupted flag from applySessionDetail.
    }
  }

  async function loadSessionDetail(sessionIdToLoad: number) {
    try {
      const detail = await getPlanningSession(sessionIdToLoad);
      await applySessionDetailWithRecovery(detail);
      return detail;
    } catch (err: unknown) {
      // If session not found, refresh the session list and show error
      void messageApi.error("加载会话失败: " + (err instanceof Error ? err.message : String(err)));
      await loadSessionList();
      throw err;
    }
  }

  async function createAndSelectSession() {
    try {
      const detail = await createPlanningSession({});
      applySessionDetail(detail);
      return detail;
    } catch (err: unknown) {
      void messageApi.error("创建会话失败: " + (err instanceof Error ? err.message : String(err)));
      throw err;
    }
  }

  async function handleSessionDeleted(deletedSessionId: number) {
    const nextList = await listPlanningSessions();
    setSessionList(nextList);

    if (deletedSessionId !== sessionId) {
      return;
    }

    const nextSession = nextList[0];
    if (nextSession) {
      await loadSessionDetail(nextSession.id);
      return;
    }

    await createAndSelectSession();
  }

  useEffect(() => {
    let cancelled = false;

    async function init() {
      setIsBootstrapping(true);
      try {
        await loadSessionDetail(sessionIdProp);
        if (!cancelled) await loadSessionList();
      } catch (err: unknown) {
        if (!cancelled) {
          void messageApi.error(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setIsBootstrapping(false);
      }
    }

    void init();
    return () => {
      cancelled = true;
    };
  }, [sessionIdProp]);

  function handleStreamEvent(event: ExecutionStreamEvent) {
    if (
      event.type === "status" ||
      event.type === "text_chunk" ||
      event.type === "tool_call_start" ||
      event.type === "tool_call_end"
    ) {
      const targetId = activeAssistantMessageIdRef.current;
      if (targetId == null) return;

      setTranscript((current) =>
        current.map((msg) => {
          if (msg.id !== targetId) return msg;
          const payload = (msg.structured_payload ?? {}) as Record<string, unknown>;
          if (event.type === "status") {
            return {
              ...msg,
              structured_payload: { ...payload, _phase: event.phase, _phaseMessage: event.message, _streaming: true },
            };
          }
          if (event.type === "text_chunk") {
            if (event.thinking) {
              const payload = (msg.structured_payload ?? {}) as Record<string, unknown>;
              const prev = (payload._thinkingContent as string) ?? "";
              return {
                ...msg,
                structured_payload: { ...payload, _thinkingContent: prev + event.text, _streaming: true },
              };
            }
            return { ...msg, content: msg.content + event.text };
          }
          if (event.type === "tool_call_start") {
            return {
              ...msg,
              structured_payload: { ...payload, _phase: "tool_calling", _phaseMessage: `正在调用工具: ${event.tool}` },
            };
          }
          if (event.type === "tool_call_end") {
            return {
              ...msg,
              structured_payload: { ...payload, _phase: "thinking", _phaseMessage: "正在分析需求..." },
            };
          }
          return msg;
        }),
      );
      return;
    }

    if (event.type === "draft_generating") {
      const targetId = activeAssistantMessageIdRef.current;
      if (targetId == null) return;
      setTranscript((current) =>
        current.map((msg) =>
          msg.id === targetId
            ? {
                ...msg,
                structured_payload: {
                  ...(msg.structured_payload ?? {}),
                  _phase: "draft_generating",
                  _phaseMessage: event.message,
                  _streaming: true,
                },
              }
            : msg,
        ),
      );
      return;
    }

    if (
      event.type === "save_progress" ||
      event.type === "case_start" ||
      event.type === "step_start" ||
      event.type === "step_complete"
    ) {
      const targetId = activeAssistantMessageIdRef.current;
      if (targetId == null) return;
      setTranscript((current) =>
        current.map((msg) =>
          msg.id === targetId
            ? {
                ...msg,
                content: applyStreamEventToContent(msg.content, event),
                structured_payload: applyStreamEventToPayload(
                  msg.structured_payload as Record<string, unknown> | null,
                  event,
                ),
              }
            : msg,
        ),
      );
      return;
    }

    if (event.type === "turn_complete") {
      const targetId = activeAssistantMessageIdRef.current;
      const thinkingContent = targetId != null
        ? transcript.find((m) => m.id === targetId)?.structured_payload as Record<string, unknown> | undefined
        : undefined;
      if (targetId != null) {
        setTranscript((current) =>
          current.map((msg) => {
            if (msg.id !== targetId) return msg;
            const payload = msg.structured_payload as Record<string, unknown> | null;
            return {
              ...msg,
              content: event.payload.assistant_message || msg.content,
              structured_payload: {
                ...payload,
                _streaming: false,
                todo_list: event.payload.todo_list,
                missing_slots: event.payload.missing_slots,
                suggested_questions: event.payload.suggested_questions,
              },
            };
          }),
        );
      }
      if (sessionId) {
        const savedThinking = thinkingContent?._thinkingContent as string | undefined;
        loadSessionDetail(sessionId).then((detail) => {
          if (savedThinking) {
            setTranscript((msgs) =>
              msgs.map((msg) => {
                if (msg.id !== targetId) return msg;
                return {
                  ...msg,
                  structured_payload: {
                    ...(msg.structured_payload as Record<string, unknown> ?? {}),
                    _thinkingContent: savedThinking,
                  },
                };
              }),
            );
          }
          setSessionId(detail.session.id);
          setRequirements(detail.session.requirements);
          setMissingSlots(detail.session.missing_slots);
          setPlan(detail.session.plan ?? null);
          setDrafts(detail.drafts);
        }).catch(() => {});
        loadSessionList().catch(() => {});
      }
      setIsSending(false);
      setIsGenerating(false);
      return;
    }

    if (event.type === "done" || event.type === "cancelled" || event.type === "error") {
      if (event.type === "error") {
        const targetId = activeAssistantMessageIdRef.current;
        if (targetId != null) {
          const phaseLabel = event.phase ? `[${event.phase}] ` : "";
          const errorTypeLabel = event.error_type ? ` (${event.error_type})` : "";
          const tracebackSection = event.traceback
            ? `\n\n<details><summary>错误追踪</summary>\n\n\`\`\`\n${event.traceback}\n\`\`\`\n</details>`
            : "";
          setTranscript((current) =>
            current.map((msg) => {
              if (msg.id !== targetId) return msg;
              const payload = msg.structured_payload as Record<string, unknown> | null;
              return {
                ...msg,
                content: `❌ **${phaseLabel}错误${errorTypeLabel}**\n\n${event.message}${tracebackSection}`,
                structured_payload: {
                  ...payload,
                  _streaming: false,
                  _phase: "error",
                  _phaseMessage: event.message,
                  error_type: event.error_type,
                  error_phase: event.phase,
                },
              };
            }),
          );
        }
        void messageApi.error("执行错误: " + event.message);
      }
      clearStreamingOnMessage(activeAssistantMessageIdRef.current);
      if (sessionId) {
        loadSessionDetail(sessionId).catch(() => {});
        loadSessionList().catch(() => {});
      }
      if (event.type === "done" || event.type === "cancelled") {
        void queryClient.invalidateQueries({ queryKey: ["cases"] });
        void queryClient.invalidateQueries({ queryKey: ["executions"] });
      }
      setIsExecuting(false);
      if (event.type === "cancelled") {
        void messageApi.info("执行已取消");
      }
    }
  }

  const collectedEntries = useMemo(
    () =>
      REQUIREMENT_FIELDS.flatMap((field) => {
        const value = formatRequirementValue(requirements[field.key]);
        return value ? [{ label: field.label, value }] : [];
      }),
    [requirements],
  );

  const progressCount = collectedEntries.length;
  const progressPercent = Math.round((progressCount / REQUIREMENT_FIELDS.length) * 100);

  async function handleSendMessage() {
    if (!sessionId) {
      return;
    }
    const trimmed = inputValue.trim();
    if (!trimmed) {
      return;
    }

    // Validate session exists before sending
    try {
      await getPlanningSession(sessionId);
    } catch (err: unknown) {
      void messageApi.error("会话不存在，请刷新页面");
      await loadSessionList();
      return;
    }

    setIsSending(true);
    clearAllStreaming();
    const optimisticUser = createOptimisticMessage(sessionId, "user", "user", trimmed);
    const optimisticAssistant = createOptimisticMessage(sessionId, "assistant", "followup", "", {
      _phase: "thinking",
      _phaseMessage: "正在分析需求...",
      _streaming: true,
    });
    activeAssistantMessageIdRef.current = optimisticAssistant.id;
    setTranscript((current) => [...current, optimisticUser, optimisticAssistant]);
    setInputValue("");

    try {
      const controller = new AbortController();
      abortRef.current = controller;
      await callSSE({
        url: `/api/v1/ai-planning/sessions/${sessionId}/chat`,
        body: { content: trimmed },
        onEvent: (_type, data) => handleStreamEvent(data as ExecutionStreamEvent),
        onDone: () => {
          // If stream ended without a terminal event, clean up streaming state
          clearStreamingOnMessage(activeAssistantMessageIdRef.current);
          if (sessionId) {
            loadSessionDetail(sessionId).catch(() => {});
            loadSessionList().catch(() => {});
          }
          setIsSending(false);
        },
        signal: controller.signal,
      });
    } catch (error) {
      clearStreamingOnMessage(activeAssistantMessageIdRef.current);
      if ((error as Error).name !== "AbortError") {
        void messageApi.error((error as Error).message);
        // Refresh session list on error
        await loadSessionList();
      }
    } finally {
      abortRef.current = null;
      setIsSending(false);
    }
  }

  async function handleGenerateDrafts() {
    if (!sessionId || !selectedScenarioKeys.length) {
      return;
    }

    // Validate session exists before generating
    try {
      await getPlanningSession(sessionId);
    } catch (err: unknown) {
      void messageApi.error("会话不存在，请刷新页面");
      await loadSessionList();
      return;
    }

    setIsGenerating(true);
    clearAllStreaming();

    const optimisticAssistant = createOptimisticMessage(sessionId, "assistant", "plan", "", {
      _phase: "generating",
      _phaseMessage: "正在生成 DSL...",
      _streaming: true,
    });
    activeAssistantMessageIdRef.current = optimisticAssistant.id;
    setTranscript((current) => [...current, optimisticAssistant]);

    try {
      const controller = new AbortController();
      abortRef.current = controller;
      await callSSE({
        url: `/api/v1/ai-planning/sessions/${sessionId}/drafts`,
        body: {
          scenario_keys: selectedScenarioKeys,
          current_case: currentCase ?? null,
          current_steps: currentSteps ?? null,
          current_input_contract: currentInputContract ?? null,
          current_output_contract: currentOutputContract ?? null,
          preserve_contracts: true,
        },
        onEvent: (_type, data) => handleStreamEvent(data as ExecutionStreamEvent),
        onDone: () => {
          clearStreamingOnMessage(activeAssistantMessageIdRef.current);
          if (sessionId) {
            loadSessionDetail(sessionId).catch(() => {});
            loadSessionList().catch(() => {});
          }
          setIsGenerating(false);
        },
        signal: controller.signal,
      });
    } catch (error) {
      clearStreamingOnMessage(activeAssistantMessageIdRef.current);
      if ((error as Error).name !== "AbortError") {
        void messageApi.error((error as Error).message);
        // Refresh session list on error
        await loadSessionList();
      }
    } finally {
      abortRef.current = null;
      setIsGenerating(false);
    }
  }

  async function handleImportDraft(draft: AIPlanningDraft) {
    await onImportDraft(draft);
    const updatedDraft = await updatePlanningDraftStatus(draft.id, { status: "imported" });
    setDrafts((current) => current.map((item) => (item.id === draft.id ? updatedDraft : item)));
  }

  function renderLeftPanel() {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 12, flex: 1, overflow: "hidden" }}>
        <div style={{ fontWeight: 700, fontSize: 14 }}>Requirements</div>
        <Progress percent={progressPercent} size="small" />
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          已收集 {progressCount} / {REQUIREMENT_FIELDS.length} 项
        </Typography.Text>
        <div style={{ flex: 1, overflowY: "auto" }} className="panel-scroll">
          {collectedEntries.length ? (
            collectedEntries.map((entry) => (
              <div key={entry.label} className="step-item">
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {entry.label}
                </Typography.Text>
                <div style={{ fontSize: 13, color: "#555", marginTop: 2 }}>{entry.value}</div>
              </div>
            ))
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              当前还没有收集到明确的规划信息。
            </Typography.Text>
          )}
        </div>
        {missingSlots.length ? (
          <Alert
            type="info"
            showIcon
            message="待补充信息"
            description={missingSlots.join("、")}
            style={{ fontSize: 12 }}
          />
        ) : null}
      </div>
    );
  }

  function renderCenterPanel() {
    return (
      <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
        {contextHolder}
        {/* Top area with title and status */}
        <div style={{ padding: "20px 40px 0" }}>
          <Typography.Title level={4} style={{ margin: 0 }}>
            AI Planning
          </Typography.Title>
          {aiSettings && planningEnabled ? (
            <Alert
              type="info"
              showIcon
              style={{ marginTop: 12, fontSize: 13 }}
              message={`模型：${aiSettings.ai_planning_model ?? "未配置"}，最多 ${aiSettings.ai_planning_max_react_rounds ?? 5} 轮`}
            />
          ) : null}
          <div style={{ marginTop: 8 }}>
            <SessionProjectPanel sessionId={sessionId ?? 0} onProjectsChange={() => {
              queryClient.invalidateQueries({ queryKey: ["planning-sessions"] });
            }} />
          </div>
        </div>

        {/* Session switcher */}
        <div style={{ display: "flex", gap: 8, padding: "8px 40px 0", alignItems: "center" }}>
          <Select
            style={{ flex: 1 }}
            size="small"
            placeholder="选择会话"
            value={sessionId ?? undefined}
            loading={isLoadingHistory}
            onChange={async (id: number) => {
              setIsBootstrapping(true);
              try {
                await loadSessionDetail(id);
              } catch (err: unknown) {
                void messageApi.error("加载会话失败: " + (err instanceof Error ? err.message : String(err)));
              } finally {
                setIsBootstrapping(false);
              }
            }}
            options={sessionList.map((s) => ({
              value: s.id,
              label: s.title || `会话 #${s.id} (${new Date(s.created_at).toLocaleString()})`,
            }))}
          />
          {sessionId ? (
            <Button
              size="small"
              danger
              icon={<DeleteOutlined />}
              aria-label={`删除会话 ${sessionList.find((item) => item.id === sessionId)?.title ?? `#${sessionId}`}`}
              onClick={async () => {
                // Abort any ongoing SSE stream before deleting
                if (abortRef.current) {
                  abortRef.current.abort();
                  abortRef.current = null;
                }
                setIsSending(false);
                setIsGenerating(false);
                setIsExecuting(false);

                const currentSession = sessionList.find((item) => item.id === sessionId);
                const label = currentSession?.title ?? `会话 #${sessionId}`;
                if (!window.confirm(`确认删除"${label}"吗？此操作不可恢复。`)) {
                  return;
                }

                setIsBootstrapping(true);
                try {
                  await deletePlanningSession(sessionId);
                  await handleSessionDeleted(sessionId);
                  void messageApi.success("会话已删除");
                } catch (err: unknown) {
                  void messageApi.error("删除会话失败: " + (err instanceof Error ? err.message : String(err)));
                } finally {
                  setIsBootstrapping(false);
                }
              }}
            />
          ) : null}
          <Button
            type="primary"
            size="small"
            onClick={async () => {
              setIsBootstrapping(true);
              try {
                await createAndSelectSession();
                await loadSessionList();
              } catch (err: unknown) {
                void messageApi.error("创建会话失败: " + (err instanceof Error ? err.message : String(err)));
              } finally {
                setIsBootstrapping(false);
              }
            }}
          >
            新建会话
          </Button>
        </div>

        {/* Scrollable message area */}
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "24px 40px",
          }}
          className="panel-scroll"
        >
          {transcript.map((item) => (
            <div
              key={`${item.id}-${item.turn_type}`}
              style={{
                display: "flex",
                justifyContent: item.role === "user" ? "flex-end" : "flex-start",
                marginBottom: 12,
              }}
            >
              <div className={item.role === "user" ? "chat-bubble-user" : "chat-bubble-ai"}>
                {item.role === "assistant" && item.turn_type === "tool_call" ? (
                  <>
                    <span style={{ fontWeight: 600 }}>🔧 工具调用</span>
                    <div style={{ marginTop: 4 }}>{item.content}</div>
                    {item.structured_payload?.result_summary ? (
                      <details style={{ fontSize: 12, color: "#666", background: "#fafafa",
                                        borderRadius: 6, padding: "4px 8px", marginTop: 4 }}>
                        <summary style={{ cursor: "pointer", fontWeight: 500 }}>
                          查看摘要
                          {item.structured_payload.result_summary &&
                            typeof item.structured_payload.result_summary === "object" &&
                            "page_title" in (item.structured_payload.result_summary as Record<string, unknown>)
                            ? ` — ${(item.structured_payload.result_summary as Record<string, unknown>).page_title}`
                            : ""}
                        </summary>
                        <pre style={{ whiteSpace: "pre-wrap", marginTop: 4, maxHeight: 200,
                                      overflowY: "auto", fontSize: 11 }}>
                          {JSON.stringify(item.structured_payload.result_summary, null, 2)}
                        </pre>
                      </details>
                    ) : null}
                  </>
                ) : item.role === "assistant" &&
                  item.structured_payload?.type === "execution_progress" ? (
                  <div>
                    <span style={{ fontWeight: 600 }}>⚡ 执行进度</span>
                    <div style={{ marginTop: 4, whiteSpace: "pre-wrap" }}>{item.content}</div>
                    {isExecuting && (
                      <div style={{ marginTop: 4 }}>
                        <Button
                          size="small"
                          danger
                          onClick={() => {
                            abortRef.current?.abort();
                            abortRef.current = null;
                            if (sessionId) void cancelExecution(sessionId);
                          }}
                        >
                          取消执行
                        </Button>
                      </div>
                    )}
                  </div>
                ) : item.role === "assistant" &&
                  item.structured_payload?.type === "execution_summary" &&
                  Array.isArray(item.structured_payload.execution_summaries) ? (
                  <div>
                    <div style={{ whiteSpace: "pre-wrap" }}>{item.content}</div>
                    {(item.structured_payload.execution_summaries as ExecutionSummaryResult[]).map((ex) => (
                      <div
                        key={ex.execution_id}
                        style={{ marginTop: 8, display: "flex", alignItems: "center", gap: 8 }}
                      >
                        {ex.status === "passed" ? "✅" : "❌"}
                        <span>{ex.case_name}</span>
                        <span style={{ color: "#888" }}>
                          {ex.passed_steps}/{ex.total_steps}步
                        </span>
                        {ex.duration_ms ? (
                          <span style={{ color: "#888" }}>{(ex.duration_ms / 1000).toFixed(1)}s</span>
                        ) : null}
                        <a href={ex.report_url} target="_blank" rel="noopener noreferrer">
                          查看报告
                        </a>
                      </div>
                    ))}
                  </div>
                ) : item.role === "assistant" &&
                  Array.isArray(item.structured_payload?.todo_list) &&
                  (item.structured_payload?.todo_list as Array<{ item: string; status: string }>).length > 0 ? (
                  <div>
                    <div style={{ whiteSpace: "pre-wrap", marginBottom: 8 }}>{item.content}</div>
                    <div style={{
                      background: "rgba(0,0,0,0.03)",
                      borderRadius: 8,
                      padding: "8px 12px",
                    }}>
                      {(item.structured_payload!.todo_list as Array<{ item: string; status: string }>).map((todo, idx) => (
                        <div key={idx} style={{ display: "flex", alignItems: "center", gap: 8, padding: "3px 0" }}>
                          {todo.status === "done" ? (
                            <CheckCircleFilled style={{ color: "#52c41a", fontSize: 14 }} />
                          ) : todo.status === "in_progress" ? (
                            <LoadingOutlined style={{ color: "#1677ff", fontSize: 14 }} />
                          ) : (
                            <ClockCircleOutlined style={{ color: "#d9d9d9", fontSize: 14 }} />
                          )}
                          <span style={{
                            textDecoration: todo.status === "done" ? "line-through" : "none",
                            color: todo.status === "pending" ? "#aaa" : todo.status === "done" ? "#888" : "#333",
                          }}>
                            {todo.item}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : item.role === "assistant" && (item.structured_payload as Record<string, unknown>)?._phase ? (
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    <Tag color={phaseColorMap[((item.structured_payload as Record<string, unknown>)._phase as string) ?? ""] ?? "processing"}>
                      {String((item.structured_payload as Record<string, unknown>)._phaseMessage ?? "处理中...")}
                    </Tag>
                    {((item.structured_payload as Record<string, unknown>)._thinkingContent as string) ? (
                      <details
                        style={{ fontSize: 12, color: "#666", background: "#fafafa",
                                 borderRadius: 6, padding: "4px 8px" }}
                        open={Boolean((item.structured_payload as Record<string, unknown>)._streaming)}
                      >
                        <summary style={{ cursor: "pointer", fontWeight: 500 }}>
                          {((item.structured_payload as Record<string, unknown>)._thinkingContent as string).length > 500
                            ? `思考过程（${((item.structured_payload as Record<string, unknown>)._thinkingContent as string).length} 字，已折叠）`
                            : "思考过程"}
                        </summary>
                        <div style={{
                          whiteSpace: "pre-wrap",
                          marginTop: 4,
                          maxHeight: 200,
                          overflowY: "auto",
                          opacity: (item.structured_payload as Record<string, unknown>)._streaming ? 1 : 0.7,
                        }}>
                          {((item.structured_payload as Record<string, unknown>)._thinkingContent as string).length > 500
                            ? ((item.structured_payload as Record<string, unknown>)._thinkingContent as string).slice(0, 500) + "..."
                            : ((item.structured_payload as Record<string, unknown>)._thinkingContent as string)}
                        </div>
                      </details>
                    ) : null}
                    <div style={{ whiteSpace: "pre-wrap" }}>
                      {item.content}
                      {(item.structured_payload as Record<string, unknown>)?._interrupted ? (
                        <span style={{ color: "#faad14", fontSize: 12, marginLeft: 8 }}>⏸ 回复中断</span>
                      ) : (item.structured_payload as Record<string, unknown>)?._recovered ? (
                        <span style={{ color: "#52c41a", fontSize: 12, marginLeft: 8 }}>✓ 已恢复</span>
                      ) : (item.structured_payload as Record<string, unknown>)?._streaming ? (
                        <span className="typing-cursor">▊</span>
                      ) : null}
                    </div>
                  </div>
                ) : (
                  item.content
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Suggested questions */}
        {suggestedQuestions.length ? (
          <div style={{ padding: "0 40px 8px", display: "flex", flexWrap: "wrap", gap: 8 }}>
            {suggestedQuestions.map((question) => (
              <Tag
                key={question}
                className="action-grid-item"
                style={{ cursor: "pointer" }}
                onClick={() => {
                  setInputValue(question);
                }}
              >
                {question}
              </Tag>
            ))}
          </div>
        ) : null}

        {/* Bottom input bar */}
        <div style={{ padding: "16px 32px 20px", borderTop: "1px solid #f5f5f5" }}>
          <div
            style={{
              display: "flex",
              alignItems: "flex-end",
              gap: 8,
              background: "#F0F4F8",
              borderRadius: 24,
              padding: "8px 8px 8px 16px",
            }}
          >
            <Input.TextArea
              aria-label="测试规划对话输入"
              autoSize={{ minRows: 1, maxRows: 4 }}
              variant="borderless"
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSendMessage();
                }
              }}
              disabled={isDisabled || isBootstrapping}
              placeholder="描述业务目标、入口页面、核心流程、断言和测试数据…"
              style={{ background: "transparent", resize: "none", flex: 1 }}
            />
            {(isSending || isGenerating || isExecuting) ? (
              <Button
                danger
                shape="circle"
                onClick={() => {
                  abortRef.current?.abort();
                  abortRef.current = null;
                  if (isExecuting && sessionId) void cancelExecution(sessionId);
                }}
                style={{
                  width: 40,
                  height: 40,
                  minWidth: 40,
                  flexShrink: 0,
                }}
              >
                ■
              </Button>
            ) : (
              <Button
                type="primary"
                shape="circle"
                icon={<SendOutlined />}
                onClick={() => void handleSendMessage()}
                disabled={isDisabled || isBootstrapping || !sessionId || !inputValue.trim()}
                style={{
                  background: "#1a1a2e",
                  borderColor: "#1a1a2e",
                  width: 40,
                  height: 40,
                  minWidth: 40,
                  flexShrink: 0,
                }}
              />
            )}
          </div>
        </div>
      </div>
    );
  }

  function renderRightCards() {
    const cards: React.ReactNode[] = [];

    // Card 1: 规划进度
    cards.push(
      <div key="plan-progress">
        <Typography.Text strong style={{ fontSize: 14 }}>
          规划进度
        </Typography.Text>
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
          {plan ? (
            <>
              <Alert type="success" showIcon message={plan.summary} style={{ fontSize: 13 }} />
              {plan.scenarios.map((scenario) => (
                <div key={scenario.scenario_key} style={{ padding: "8px 0" }}>
                  <Checkbox
                    aria-label={`选择场景 ${scenario.title}`}
                    checked={selectedScenarioKeys.includes(scenario.scenario_key)}
                    onChange={(event) =>
                      setSelectedScenarioKeys((current) =>
                        event.target.checked
                          ? [...current, scenario.scenario_key]
                          : current.filter((item) => item !== scenario.scenario_key),
                      )
                    }
                  >
                    {scenario.title}
                  </Checkbox>
                  <Typography.Text style={{ display: "block", fontSize: 12, color: "#555", marginTop: 2, paddingLeft: 24 }}>
                    {scenario.goal}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, paddingLeft: 24 }}>
                    数据需求：{scenario.test_data_requirements.length ? scenario.test_data_requirements.map((item) => item.label).join("、") : "无"}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, paddingLeft: 24 }}>
                    关键断言：{scenario.assertions.length ? scenario.assertions.join("、") : "无"}
                  </Typography.Text>
                </div>
              ))}
              <Button
                onClick={() => void handleGenerateDrafts()}
                loading={isGenerating}
                disabled={!selectedScenarioKeys.length || isGenerating}
                type="primary"
                block
              >
                生成选中草案
              </Button>
            </>
          ) : (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              尚未生成规划方案，请在对话中描述测试需求。
            </Typography.Text>
          )}
        </div>
      </div>,
    );

    // Card 2: DSL 草案列表
    if (drafts.length > 0) {
      cards.push(
        <div key="drafts-list">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <Typography.Text strong style={{ fontSize: 14 }}>
              测试用例草案
            </Typography.Text>
            <div style={{ display: "flex", gap: 8 }}>
              <Button
                size="small"
                disabled={selectedScenarioKeys.length === 0 || isSending}
                onClick={async () => {
                  if (!sessionId || selectedScenarioKeys.length === 0) return;
                  setIsSending(true);
                  try {
                    const resp = await saveAndExecuteDrafts(
                      sessionId,
                      drafts.filter((d) => selectedScenarioKeys.includes(d.scenario_key)).map((d) => d.id),
                      false,
                    );
                    await queryClient.invalidateQueries({ queryKey: ["cases"] });
                    await loadSessionDetail(sessionId);
                    void messageApi.success(`已保存 ${resp.saved_cases?.length ?? 0} 个用例`);
                    await loadSessionList();
                  } catch (err: unknown) {
                    void messageApi.error("保存失败: " + (err instanceof Error ? err.message : String(err)));
                  } finally {
                    setIsSending(false);
                  }
                }}
              >
                仅保存
              </Button>
              <Button
                type="primary"
                size="small"
                loading={isExecuting}
                disabled={selectedScenarioKeys.length === 0 || isExecuting}
                onClick={async () => {
                    if (!sessionId || selectedScenarioKeys.length === 0) return;
                    const draftIds = drafts
                      .filter((d) => selectedScenarioKeys.includes(d.scenario_key))
                      .map((d) => d.id);
                    setIsExecuting(true);

                    const progressMessage = createOptimisticMessage(
                      sessionId,
                      "assistant",
                      "followup",
                      "正在保存并执行已选草案…",
                      { type: "execution_progress", saved_count: 0, total: 0, cases: [] },
                    );
                    activeAssistantMessageIdRef.current = progressMessage.id;
                    setTranscript((current) => [...current, progressMessage]);

                    try {
                      const controller = new AbortController();
                      abortRef.current = controller;
                      await callSSE({
                        url: `/api/v1/ai-planning/sessions/${sessionId}/execute`,
                        body: { draft_ids: draftIds },
                        onEvent: (_type, data) => handleStreamEvent(data as ExecutionStreamEvent),
                        onDone: () => {
                          clearStreamingOnMessage(activeAssistantMessageIdRef.current);
                          if (sessionId) {
                            loadSessionDetail(sessionId).catch(() => {});
                            loadSessionList().catch(() => {});
                          }
                          setIsExecuting(false);
                        },
                        signal: controller.signal,
                      });
                    } catch (error) {
                      clearStreamingOnMessage(activeAssistantMessageIdRef.current);
                      if ((error as Error).name !== "AbortError") {
                        void messageApi.error("执行失败: " + (error instanceof Error ? error.message : String(error)));
                      }
                      setIsExecuting(false);
                    } finally {
                      abortRef.current = null;
                    }
                  }}
              >
                保存并执行
              </Button>
            </div>
          </div>
          <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            {drafts.map((draft) => (
              <div key={draft.id} style={{ padding: "8px 0", borderBottom: "1px solid #f5f5f5" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Checkbox
                    checked={selectedScenarioKeys.includes(draft.scenario_key)}
                    onChange={(e) => {
                      setSelectedScenarioKeys((prev) =>
                        e.target.checked
                          ? [...prev, draft.scenario_key]
                          : prev.filter((k) => k !== draft.scenario_key),
                      );
                    }}
                    disabled={draft.status !== "generated" || !draft.dsl_case}
                  />
                  <Typography.Text strong style={{ fontSize: 13, flex: 1 }}>
                    {draft.title}
                  </Typography.Text>
                  <Tag>{draft.status}</Tag>
                  <DeleteOutlined
                    style={{ fontSize: 12, color: "#999", cursor: "pointer" }}
                    title="删除草案"
                    onClick={async () => {
                      try {
                        await deletePlanningDraft(draft.id);
                        setDrafts((prev) => prev.filter((d) => d.id !== draft.id));
                        setSelectedScenarioKeys((prev) => prev.filter((k) => k !== draft.scenario_key));
                        void messageApi.success("草案已删除");
                      } catch (err) {
                        void messageApi.error("删除失败: " + (err instanceof Error ? err.message : String(err)));
                      }
                    }}
                  />
                </div>
                {draft.error_message ? (
                  <Alert type="error" showIcon message={draft.error_message} style={{ marginTop: 4, fontSize: 12 }} />
                ) : null}
                {draft.dsl_case ? (
                  <div style={{ marginLeft: 30, color: "#888", fontSize: 12, marginTop: 4 }}>
                    {draft.dsl_case.steps.map((s) => s.action).join(" → ")}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>,
      );
    }

    // Card 3 (optional): AI settings status
    if (aiSettings) {
      cards.push(
        <div key="ai-settings-info">
          <Typography.Text strong style={{ fontSize: 14 }}>
            AI 设置
          </Typography.Text>
          <div style={{ marginTop: 8, fontSize: 12, color: "#888" }}>
            <div>状态：{planningEnabled ? "已启用" : "未启用"}</div>
            {aiSettings.ai_planning_model ? <div>模型：{aiSettings.ai_planning_model}</div> : null}
            <div>最大轮数：{aiSettings.ai_planning_max_react_rounds ?? 5}</div>
          </div>
        </div>,
      );
    }

    return cards;
  }

  return (
    <NotebookLMLayout
      leftPanel={renderLeftPanel()}
      centerPanel={renderCenterPanel()}
      rightCards={renderRightCards()}
    />
  );
}
