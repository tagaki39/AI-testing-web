import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { AITestPlanningPanel } from "./AITestPlanningPanel";
import * as api from "../services/api";
import * as sseModule from "../services/sseClient";
import { renderWithProviders } from "../test/test-utils";
import type { AISettings } from "../types/api";

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    createPlanningSession: vi.fn(),
    deletePlanningSession: vi.fn(),
    generatePlanningDrafts: vi.fn(),
    getPlanningSession: vi.fn(),
    listPlanningSessions: vi.fn(),
    listSessionProjects: vi.fn(),
    saveAndExecuteDrafts: vi.fn(),
    sendPlanningMessage: vi.fn(),
    updatePlanningDraftStatus: vi.fn(),
  };
});

vi.mock("../services/sseClient", () => ({
  callSSE: vi.fn(),
  cancelExecution: vi.fn(),
}));

const aiSettings: AISettings = {
  enable_ai_dsl_generate: true,
  ai_dsl_timeout_ms: 15000,
  ai_dsl_base_url: "https://api.openai.com/v1",
  ai_dsl_model: "gpt-4o-mini",
  ai_dsl_strict_mode: false,
  ai_dsl_allow_auto_repair: true,
  has_ai_dsl_api_key: true,
  enable_ai_visual_locate: false,
  ai_visual_timeout_ms: 10000,
  ai_visual_failure_threshold: 3,
  ai_visual_cooldown_seconds: 60,
  ai_visual_rate_limit_per_minute: 10,
  vlm_base_url: "https://api.openai.com/v1",
  vlm_model: "gpt-4o",
  vlm_model_family: "gpt-4o",
  has_vlm_api_key: false,
  enable_ai_planning: true,
  ai_planning_model: "gpt-4.1-mini",
  ai_planning_base_url: "https://api.openai.com/v1",
  ai_planning_timeout_ms: 30000,
  ai_planning_max_react_rounds: 5,
  has_ai_planning_api_key: true,
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.stubGlobal("confirm", vi.fn(() => true));

  vi.mocked(api.getPlanningSession).mockResolvedValue({
    session: {
      id: 5,
      actor_user_id: 1,
      projects: [],
      case_id: null,
      title: "当前会话",
      status: "collecting",
      requirements: {
        app_under_test: null,
        business_goal: null,
        entry_url_or_page: null,
        core_user_flow: null,
        main_assertions: [],
        test_data_or_account: null,
        scope_limits: null,
      },
      plan: null,
      missing_slots: ["app_under_test", "business_goal"],
      last_error_message: null,
      created_at: "2026-04-12T10:00:00",
      updated_at: "2026-04-12T10:00:00",
    },
    messages: [],
    drafts: [],
  });
  vi.mocked(api.listPlanningSessions).mockResolvedValue([
    {
      id: 5,
      title: "当前会话",
      status: "collecting",
      projects: [],
      created_at: "2026-04-12T10:00:00",
      updated_at: "2026-04-12T10:00:00",
    },
    {
      id: 9,
      title: "保留会话",
      status: "plan_ready",
      projects: [],
      created_at: "2026-04-12T09:00:00",
      updated_at: "2026-04-12T09:30:00",
    },
  ]);
  vi.mocked(api.listSessionProjects).mockResolvedValue([]);
  vi.mocked(api.deletePlanningSession).mockResolvedValue(undefined);
});

test("展示动态进度、工具调用并支持直接生成方案", async () => {
  // Mock SSE call to immediately emit turn_complete, triggering session reload
  vi.mocked(sseModule.callSSE).mockImplementation(async (opts) => {
    opts.onEvent("turn_complete", { type: "turn_complete", session_status: "collecting", payload: { assistant_message: "", missing_slots: [], suggested_questions: [], plan: null, tool_calls: [], todo_list: [] } });
  });

  // getPlanningSession: called after turn_complete reloads session, return final state with plan
  vi.mocked(api.getPlanningSession).mockResolvedValueOnce({
      session: {
        id: 5,
        actor_user_id: 1,
        projects: [],
        case_id: null,
        title: "当前会话",
        status: "plan_ready",
        requirements: {
          app_under_test: "商城后台",
          business_goal: "验证管理员登录",
          entry_url_or_page: "https://shop.example.com/login",
          core_user_flow: "输入账号密码并点击登录",
          main_assertions: ["跳转到 dashboard"],
          test_data_or_account: null,
          scope_limits: null,
        },
        plan: {
          summary: "商城后台登录测试方案",
          assumptions: ["入口页面为 /login"],
          risks: ["未覆盖忘记密码"],
          scenarios: [
            {
              scenario_key: "login_success",
              title: "登录成功",
              goal: "验证管理员可以登录后台",
              preconditions: ["准备管理员账号"],
              priority: "high",
              test_data_requirements: [
                { key: "username", label: "管理员账号", value_type: "string", required: true, source_hint: "seed" },
              ],
              assertions: ["跳转到 dashboard"],
              draft_prompt: "为登录成功场景生成 DSL",
            },
          ],
        },
        missing_slots: [],
        last_error_message: null,
        created_at: "2026-04-12T10:00:00",
        updated_at: "2026-04-12T10:05:00",
      },
      messages: [
        {
          id: 1,
          session_id: 5,
          role: "user",
          turn_type: "user",
          content: "请先整理后台登录测试方案",
          structured_payload: null,
          created_at: "2026-04-12T10:01:00",
        },
        {
          id: 2,
          session_id: 5,
          role: "assistant",
          turn_type: "tool_call",
          content: "调用工具：list_test_cases",
          structured_payload: { type: "tool_call", tool: "list_test_cases", params: { search: "登录" }, result: { cases: [{ id: 1, name: "后台登录成功" }] } },
          created_at: "2026-04-12T10:01:01",
        },
        {
          id: 3,
          session_id: 5,
          role: "assistant",
          turn_type: "plan",
          content: "信息已经足够，我先给出结构化测试方案。",
          structured_payload: null,
          created_at: "2026-04-12T10:01:02",
        },
      ],
      drafts: [],
    });

  renderWithProviders(
    <AITestPlanningPanel aiSettings={aiSettings} sessionId={5} onImportDraft={vi.fn()} />,
  );

  expect(await screen.findByText("AI Planning")).toBeInTheDocument();
  expect(screen.getByText("已收集 0 / 7 项")).toBeInTheDocument();

  await userEvent.type(screen.getByLabelText("测试规划对话输入"), "请先整理后台登录测试方案{enter}");

  await waitFor(() => {
    expect(screen.getByText(/list_test_cases/)).toBeInTheDocument();
  }, { timeout: 3000 });
  await waitFor(() => {
    expect(screen.getByText("已收集 5 / 7 项")).toBeInTheDocument();
  }, { timeout: 3000 });
  expect(screen.getByText("商城后台登录测试方案")).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "选择场景 登录成功" })).toBeInTheDocument();
});

test("可以生成草案并展示审阅操作", async () => {
  // Mock SSE call for chat to immediately emit turn_complete
  vi.mocked(sseModule.callSSE).mockImplementation(async (opts) => {
    opts.onEvent("turn_complete", { type: "turn_complete", session_status: "collecting", payload: { assistant_message: "", missing_slots: [], suggested_questions: [], plan: null, tool_calls: [], todo_list: [] } });
  });

  // getPlanningSession: after chat turn_complete returns plan, after drafts turn_complete returns drafts
  vi.mocked(api.getPlanningSession)
    .mockResolvedValueOnce({
      session: {
        id: 5,
        actor_user_id: 1,
        projects: [],
        case_id: null,
        title: "当前会话",
        status: "plan_ready",
        requirements: {
          app_under_test: "商城后台",
          business_goal: "验证管理员登录",
          entry_url_or_page: "https://shop.example.com/login",
          core_user_flow: "输入账号密码并点击登录",
          main_assertions: ["跳转到 dashboard"],
          test_data_or_account: "admin@example.com",
          scope_limits: "不覆盖忘记密码",
        },
        plan: {
          summary: "商城后台登录测试方案",
          assumptions: ["入口页面为 /login"],
          risks: ["未覆盖忘记密码"],
          scenarios: [
            {
              scenario_key: "login_success",
              title: "登录成功",
              goal: "验证管理员可以登录后台",
              preconditions: ["准备管理员账号"],
              priority: "high",
              test_data_requirements: [],
              assertions: ["跳转到 dashboard"],
              draft_prompt: "为登录成功场景生成 DSL",
            },
          ],
        },
        missing_slots: [],
        last_error_message: null,
        created_at: "2026-04-12T10:00:00",
        updated_at: "2026-04-12T10:05:00",
      },
      messages: [
        {
          id: 1,
          session_id: 5,
          role: "user",
          turn_type: "user",
          content: "请先整理后台登录测试方案",
          structured_payload: null,
          created_at: "2026-04-12T10:01:00",
        },
        {
          id: 2,
          session_id: 5,
          role: "assistant",
          turn_type: "plan",
          content: "信息已经足够，我先给出结构化测试方案。",
          structured_payload: null,
          created_at: "2026-04-12T10:01:02",
        },
      ],
      drafts: [],
    })
    .mockResolvedValueOnce({
      session: {
        id: 5,
        actor_user_id: 1,
        projects: [],
        case_id: null,
        title: "当前会话",
        status: "drafts_ready",
        requirements: {
          app_under_test: "商城后台",
          business_goal: "验证管理员登录",
          entry_url_or_page: "https://shop.example.com/login",
          core_user_flow: "输入账号密码并点击登录",
          main_assertions: ["跳转到 dashboard"],
          test_data_or_account: "admin@example.com",
          scope_limits: "不覆盖忘记密码",
        },
        plan: {
          summary: "商城后台登录测试方案",
          assumptions: ["入口页面为 /login"],
          risks: ["未覆盖忘记密码"],
          scenarios: [
            {
              scenario_key: "login_success",
              title: "登录成功",
              goal: "验证管理员可以登录后台",
              preconditions: ["准备管理员账号"],
              priority: "high",
              test_data_requirements: [],
              assertions: ["跳转到 dashboard"],
              draft_prompt: "为登录成功场景生成 DSL",
            },
          ],
        },
        missing_slots: [],
        last_error_message: null,
        created_at: "2026-04-12T10:00:00",
        updated_at: "2026-04-12T10:10:00",
      },
      messages: [
        {
          id: 1,
          session_id: 5,
          role: "user",
          turn_type: "user",
          content: "请先整理后台登录测试方案",
          structured_payload: null,
          created_at: "2026-04-12T10:01:00",
        },
        {
          id: 2,
          session_id: 5,
          role: "assistant",
          turn_type: "plan",
          content: "信息已经足够，我先给出结构化测试方案。",
          structured_payload: null,
          created_at: "2026-04-12T10:01:02",
        },
        {
          id: 3,
          session_id: 5,
          role: "assistant",
          turn_type: "plan",
          content: "已根据所选场景生成 DSL 草案。",
          structured_payload: null,
          created_at: "2026-04-12T10:05:00",
        },
      ],
      drafts: [
        {
          id: 11,
          session_id: 5,
          scenario_key: "login_success",
          title: "登录成功",
          status: "generated",
          dsl_generation_id: 33,
          dsl_case: {
            name: "登录成功",
            description: "草案",
            base_url: "https://shop.example.com",
            input_contract: [],
            output_contract: [],
            steps: [{ action: "goto", value: "/login" }],
          },
          warnings: [],
          normalization_notes: [],
          error_message: null,
          created_at: "2026-03-30T10:00:00",
          updated_at: "2026-03-30T10:00:00",
        },
      ],
    });

  vi.mocked(api.updatePlanningDraftStatus).mockResolvedValue({
    id: 11,
    session_id: 5,
    scenario_key: "login_success",
    title: "登录成功",
    status: "imported",
    dsl_generation_id: 33,
    dsl_case: {
      name: "登录成功",
      description: "草案",
      base_url: "https://shop.example.com",
      input_contract: [],
      output_contract: [],
      steps: [{ action: "goto", value: "/login" }],
    },
    warnings: [],
    normalization_notes: [],
    error_message: null,
    created_at: "2026-03-30T10:00:00",
    updated_at: "2026-03-30T10:00:00",
  });

  renderWithProviders(
    <AITestPlanningPanel aiSettings={aiSettings} sessionId={5} onImportDraft={vi.fn()} />,
  );

  expect(await screen.findByText("AI Planning")).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("测试规划对话输入"), "请先整理后台登录测试方案{enter}");

  await userEvent.click(await screen.findByRole("checkbox", { name: "选择场景 登录成功" }));
  await userEvent.click(screen.getByRole("button", { name: "生成选中草案" }));

  await waitFor(() => {
    expect(sseModule.callSSE).toHaveBeenCalledWith(
      expect.objectContaining({ url: "/api/v1/ai-planning/sessions/5/drafts" }),
    );
  });
  expect(await screen.findByText("测试用例草案")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "仅保存" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存并执行" })).toBeInTheDocument();
});

test("删除当前会话后会切换到剩余会话", async () => {
  vi.mocked(api.listPlanningSessions)
    .mockResolvedValueOnce([
      {
        id: 5,
        title: "当前会话",
        status: "collecting",
        projects: [],
        created_at: "2026-04-12T10:00:00",
        updated_at: "2026-04-12T10:00:00",
      },
      {
        id: 9,
        title: "保留会话",
        status: "plan_ready",
        projects: [],
        created_at: "2026-04-12T09:00:00",
        updated_at: "2026-04-12T09:30:00",
      },
    ])
    .mockResolvedValueOnce([
      {
        id: 9,
        title: "保留会话",
        status: "plan_ready",
        projects: [],
        created_at: "2026-04-12T09:00:00",
        updated_at: "2026-04-12T09:30:00",
      },
    ]);

  vi.mocked(api.getPlanningSession).mockImplementation(async (sessionId: number) => ({
    session: {
      id: sessionId,
      actor_user_id: 1,
      projects: [],
      case_id: null,
      title: sessionId === 9 ? "保留会话" : "当前会话",
      status: "collecting",
      requirements: {
        app_under_test: null,
        business_goal: null,
        entry_url_or_page: null,
        core_user_flow: null,
        main_assertions: [],
        test_data_or_account: null,
        scope_limits: null,
      },
      plan: null,
      missing_slots: [],
      last_error_message: null,
      created_at: "2026-04-12T10:00:00",
      updated_at: "2026-04-12T10:00:00",
    },
    messages: [],
    drafts: [],
  }));

  renderWithProviders(
    <AITestPlanningPanel aiSettings={aiSettings} sessionId={5} onImportDraft={vi.fn()} />,
  );

  await waitFor(() => {
    expect(screen.getByRole("button", { name: "删除会话 当前会话" })).toBeInTheDocument();
  });
  await userEvent.click(screen.getByRole("button", { name: "删除会话 当前会话" }));

  await waitFor(() => {
    expect(api.deletePlanningSession).toHaveBeenCalledWith(5);
    expect(api.getPlanningSession).toHaveBeenLastCalledWith(9);
  });
});

test("保存并执行后会重新加载会话详情并展示持久化的执行摘要", async () => {
  // Mock SSE streaming — immediately emit done when callSSE is called
  vi.mocked(sseModule.callSSE).mockImplementation(async (opts) => {
    // Simulate backend immediately sending done
    opts.onEvent("done", { type: "done" });
  });
  vi.mocked(api.getPlanningSession).mockResolvedValue({
    session: {
      id: 5,
      actor_user_id: 1,
      projects: [],
      case_id: null,
      title: "当前会话",
      status: "drafts_ready",
      requirements: {
        app_under_test: "商城后台",
        business_goal: "验证管理员登录",
        entry_url_or_page: "https://shop.example.com/login",
        core_user_flow: "输入账号密码并点击登录",
        main_assertions: ["跳转到 dashboard"],
        test_data_or_account: "admin@example.com",
        scope_limits: "不覆盖忘记密码",
      },
      plan: {
        summary: "商城后台登录测试方案",
        assumptions: [],
        risks: [],
        scenarios: [
          {
            scenario_key: "login_success",
            title: "登录成功",
            goal: "验证管理员可以登录后台",
            preconditions: [],
            priority: "high",
            test_data_requirements: [],
            assertions: ["跳转到 dashboard"],
            draft_prompt: "为登录成功场景生成 DSL",
          },
        ],
      },
      missing_slots: [],
      last_error_message: null,
      created_at: "2026-04-13T10:00:00",
      updated_at: "2026-04-13T10:00:00",
    },
    messages: [],
    drafts: [
      {
        id: 11,
        session_id: 5,
        scenario_key: "login_success",
        title: "登录成功",
        status: "generated",
        dsl_generation_id: 33,
        dsl_case: {
          name: "登录成功",
          description: "草案",
          base_url: "https://shop.example.com",
          input_contract: [],
          output_contract: [],
          steps: [{ action: "goto", value: "/login" }],
        },
        warnings: [],
        normalization_notes: [],
        error_message: null,
        created_at: "2026-04-13T10:00:00",
        updated_at: "2026-04-13T10:00:00",
      },
    ],
  });

  renderWithProviders(
    <AITestPlanningPanel aiSettings={aiSettings} sessionId={5} onImportDraft={vi.fn()} />,
  );

  expect(await screen.findByText("AI Planning")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: "选择场景 登录成功" }));
  await userEvent.click(screen.getByRole("button", { name: "保存并执行" }));

  await waitFor(() => {
    expect(sseModule.callSSE).toHaveBeenCalledWith(
      expect.objectContaining({ url: "/api/v1/ai-planning/sessions/5/execute" }),
    );
    expect(api.getPlanningSession).toHaveBeenCalledWith(5);
  }, { timeout: 3000 });
});

test("保存并执行改为流式 WebSocket 并在 done 后回读会话详情", async () => {
  // getPlanningSession: first call (on mount) returns plan + drafts, subsequent calls return completed
  vi.mocked(api.getPlanningSession)
    .mockResolvedValueOnce({
      session: {
        id: 5,
        actor_user_id: 1,
        projects: [],
        case_id: null,
        title: "流式测试会话",
        status: "drafts_ready",
        requirements: {
          app_under_test: "商城后台",
          business_goal: "验证登录",
          entry_url_or_page: "https://shop.example.com/login",
          core_user_flow: "输入账号密码并点击登录",
          main_assertions: ["跳转到 dashboard"],
          test_data_or_account: "admin",
          scope_limits: null,
        },
        plan: {
          summary: "登录测试方案",
          assumptions: [],
          risks: [],
          scenarios: [
            {
              scenario_key: "login_success",
              title: "登录成功",
              goal: "验证登录",
              preconditions: [],
              priority: "high",
              test_data_requirements: [],
              assertions: ["跳转到 dashboard"],
              draft_prompt: "为登录成功场景生成 DSL",
            },
          ],
        },
        missing_slots: [],
        last_error_message: null,
        created_at: "2026-04-15T10:00:00",
        updated_at: "2026-04-15T10:00:00",
      },
      messages: [],
      drafts: [
        {
          id: 11,
          session_id: 5,
          scenario_key: "login_success",
          title: "登录成功",
          status: "generated",
          dsl_generation_id: 33,
          dsl_case: {
            name: "登录成功",
            description: "草案",
            base_url: "https://shop.example.com",
            input_contract: [],
            output_contract: [],
            steps: [{ action: "goto", value: "/login" }],
          },
          warnings: [],
          normalization_notes: [],
          error_message: null,
          created_at: "2026-04-15T10:00:00",
          updated_at: "2026-04-15T10:00:00",
        },
      ],
    })
    .mockResolvedValue({
      session: {
        id: 5,
        actor_user_id: 1,
        projects: [],
        case_id: null,
        title: "流式测试会话",
        status: "completed",
        requirements: {
          app_under_test: "商城后台",
          business_goal: "验证登录",
          entry_url_or_page: "https://shop.example.com/login",
          core_user_flow: "输入账号密码并点击登录",
          main_assertions: ["跳转到 dashboard"],
          test_data_or_account: "admin",
          scope_limits: null,
        },
        plan: null,
        missing_slots: [],
        last_error_message: null,
        created_at: "2026-04-15T10:00:00",
        updated_at: "2026-04-15T10:01:00",
      },
      messages: [],
      drafts: [],
    });

  // Mock SSE client
  let capturedOnEvent: ((eventType: string, data: unknown) => void) | null = null;

  vi.mocked(sseModule.callSSE).mockImplementation(async (opts) => {
    capturedOnEvent = opts.onEvent;
  });

  renderWithProviders(
    <AITestPlanningPanel aiSettings={aiSettings} sessionId={5} onImportDraft={vi.fn()} />,
  );

  expect(await screen.findByText("AI Planning")).toBeInTheDocument();

  // Select scenario checkbox, then click execute
  await userEvent.click(screen.getByRole("checkbox", { name: "选择场景 登录成功" }));
  await userEvent.click(screen.getByRole("button", { name: "保存并执行" }));

  await waitFor(() => {
    expect(sseModule.callSSE).toHaveBeenCalledWith(
      expect.objectContaining({ url: "/api/v1/ai-planning/sessions/5/execute" }),
    );
  });

  // Verify callSSE was called with execute body
  expect(sseModule.callSSE).toHaveBeenCalledWith(
    expect.objectContaining({
      url: "/api/v1/ai-planning/sessions/5/execute",
      body: expect.objectContaining({ draft_ids: [11] }),
    }),
  );

  // Simulate receiving events via the captured onEvent callback
  act(() => {
    capturedOnEvent?.("message", { type: "save_progress", saved_count: 1, total: 1, case_name: "登录成功" });
    capturedOnEvent?.("message", { type: "done" });
  });

  // After done, should reload session detail
  await waitFor(() => {
    expect(api.getPlanningSession).toHaveBeenCalled();
  }, { timeout: 3000 });
});
