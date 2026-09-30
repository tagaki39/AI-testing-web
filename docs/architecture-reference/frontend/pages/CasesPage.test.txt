import { Route } from "react-router-dom";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { CasesPage } from "./CasesPage";
import * as api from "../services/api";
import { renderWithProviders } from "../test/test-utils";

vi.mock("../services/api", async () => {
  const actual = await vi.importActual<typeof import("../services/api")>("../services/api");
  return {
    ...actual,
    getCases: vi.fn(),
    executeCase: vi.fn(),
  };
});

test("渲染用例列表并支持执行后跳转", async () => {
  vi.mocked(api.getCases).mockResolvedValue({
    items: [
      {
        id: 1,
        project_id: 1,
        name: "登录冒烟",
        description: "检查登录链路",
        input_contract: [],
        output_contract: [],
        steps: [{ action: "goto", value: "/login" }],
        created_by: 1,
        updated_by: 1,
        created_at: "2026-03-09T10:00:00",
        updated_at: "2026-03-09T10:00:00",
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
    total_pages: 1,
    has_next: false,
    has_prev: false,
  });
  vi.mocked(api.executeCase).mockResolvedValue({
    id: 88,
    case_id: 1,
    case_name: "登录冒烟",
    project_id: 1,
    triggered_by: 1,
    status: "passed",
    error_message: null,
    started_at: "2026-03-09T10:00:00",
    finished_at: "2026-03-09T10:00:01",
    duration_ms: 1000,
    total_steps: 0,
    failed_step_index: null,
    failure_category: null,
    failure_step_action: null,
    latest_url: null,
    latest_screenshot_url: null,
    report: {
      status: "passed",
      steps: [],
    },
  });

  renderWithProviders(<CasesPage />, {
    route: "/cases",
    path: "/cases",
    extraRoutes: [<Route key="detail" path="/run/:executionId" element={<div>detail-view</div>} />],
  });

  expect(await screen.findByText("登录冒烟")).toBeInTheDocument();

  const caseRow = screen.getByText("登录冒烟").closest("tr");
  expect(caseRow).not.toBeNull();
  await userEvent.click(within(caseRow as HTMLElement).getByRole("button", { name: /执\s*行/ }));

  await waitFor(() => {
    expect(api.executeCase).toHaveBeenCalledWith(1, { actor_user_id: 1 });
  });
  expect(await screen.findByText("detail-view")).toBeInTheDocument();
});
