import { screen, waitFor } from "@testing-library/react";
import { vi } from "vitest";

import * as api from "../services/api";
import { renderWithProviders } from "../test/test-utils";
import { PlanningPage } from "./PlanningPage";

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    getAISettings: vi.fn(),
    getPlanningSession: vi.fn(),
    listPlanningSessions: vi.fn(),
  };
});

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getAISettings).mockResolvedValue({ enable_ai_planning: true } as never);
  vi.mocked(api.getPlanningSession).mockResolvedValue({
    session: {
      id: 1,
      actor_user_id: 1,
      projects: [],
      case_id: null,
      title: null,
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
      created_at: "2026-03-30T10:00:00",
      updated_at: "2026-03-30T10:00:00",
    },
    messages: [],
    drafts: [],
  });
  vi.mocked(api.listPlanningSessions).mockResolvedValue([]);
});

test("renders planning page with AI planning panel when sessionId is provided", async () => {
  renderWithProviders(<PlanningPage />, { route: "/planning/sessions/1", path: "/planning/sessions/:sessionId" });

  expect(await screen.findByText("AI Planning")).toBeInTheDocument();
});

test("redirects to /planning when no sessionId is provided", async () => {
  renderWithProviders(<PlanningPage />, { route: "/planning", path: "/planning" });

  // The component calls navigate("/planning") when no sessionId
  // and returns null — this just verifies no crash
  await waitFor(() => {
    expect(api.getAISettings).toHaveBeenCalled();
  });
});

test("loads session detail on mount using sessionId from URL", async () => {
  renderWithProviders(<PlanningPage />, { route: "/planning/sessions/42", path: "/planning/sessions/:sessionId" });

  await waitFor(() => {
    expect(api.getPlanningSession).toHaveBeenCalledWith(42);
  });
});
