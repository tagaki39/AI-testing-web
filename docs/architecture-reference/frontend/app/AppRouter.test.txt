import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntdApp, ConfigProvider } from "antd";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { AppRouter } from "./AppRouter";

vi.mock("../pages/PlanningPage", () => ({
  PlanningPage: () => <div>Planning Mock</div>,
}));
vi.mock("../pages/CasesPage", () => ({
  CasesPage: () => <div>Cases Mock</div>,
}));
vi.mock("../pages/ExecutionDetailPage", () => ({
  ExecutionDetailPage: () => <div>Execution Detail Mock</div>,
}));
vi.mock("../pages/ReportPage", () => ({
  ReportPage: () => <div>Report Mock</div>,
}));

function renderRouter(initialEntries: string[]) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return render(
    <ConfigProvider>
      <AntdApp>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter initialEntries={initialEntries}>
            <AppRouter />
          </MemoryRouter>
        </QueryClientProvider>
      </AntdApp>
    </ConfigProvider>,
  );
}

test("root route renders planning page without auth guard", async () => {
  renderRouter(["/"]);
  expect(await screen.findByText("Planning Mock")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "步骤 1" })).toHaveAttribute("href", "/");
});

test("/cases renders cases page", async () => {
  renderRouter(["/cases"]);
  expect(await screen.findByText("Cases Mock")).toBeInTheDocument();
});

test("/run/:id renders execution detail page", async () => {
  renderRouter(["/run/12"]);
  expect(await screen.findByText("Execution Detail Mock")).toBeInTheDocument();
});

test("legacy execution detail path redirects to /run/:id", async () => {
  renderRouter(["/executions/12"]);
  expect(await screen.findByText("Execution Detail Mock")).toBeInTheDocument();
});

test("/dashboard redirects to root", async () => {
  renderRouter(["/dashboard"]);
  expect(await screen.findByText("Planning Mock")).toBeInTheDocument();
});

test("/login redirects to root", async () => {
  renderRouter(["/login"]);
  expect(await screen.findByText("Planning Mock")).toBeInTheDocument();
});

test("/executions redirects to /cases", async () => {
  renderRouter(["/executions"]);
  expect(await screen.findByText("Cases Mock")).toBeInTheDocument();
});

test("step navigation shows three demo steps", async () => {
  renderRouter(["/"]);
  expect(screen.getByRole("link", { name: "步骤 1" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "步骤 2" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "步骤 3" })).toBeInTheDocument();
});
