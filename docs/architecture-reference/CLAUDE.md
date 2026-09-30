# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Rules

Read and follow all instructions in AGENTS.md in this repository. Key rules inlined here:

- **Language**: Respond in Chinese unless user requests otherwise. Final responses include Summary, Changes, How to run, Tests, Notes sections.
- **Git**: Single-owner repo, direct push preferred over PRs. Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`). One focused commit per task.
- **Task logging**: Append to `docs/execution-log.md` after completing tasks. Append to `docs/bug-log.md` for defects found. Ask user about GitHub sync after completing requirements.
- **Boundaries**: Frontend must not contain test execution logic. Backend runner is the only source of truth for results. AI generation cannot bypass DSL validation.

## Commands

### Backend (from `backend/`)

```bash
cp .env.example .env                          # First-time: create env config
uv sync                                       # Install dependencies
uv run alembic upgrade head                   # Run database migrations
uv run alembic revision --autogenerate -m "description"  # Create new migration
uv run backend-dev                            # Start dev server (http://127.0.0.1:8000)
uv run pytest tests/unit -q                   # Run unit tests (505 tests)
uv run pytest tests/integration -m browser_integration    # Browser regression tests (needs Playwright + chromium)
uv run pytest tests/integration/test_platform_api_chain.py -v  # API chain integration tests
uv run pytest tests/unit/test_dsl_validation.py -k "test_name"  # Run single test
```

### Frontend (from `frontend/`)

```bash
npm install          # Install dependencies
npm run dev          # Start dev server (http://127.0.0.1:5173, proxies /api → backend:8000)
npm run build        # Production build (tsc --noEmit && vite build)
npm test -- --run    # Run Vitest tests
```

## Architecture

Monorepo with Python backend, TypeScript frontend, and docs:

```
backend/app/
  main.py              # FastAPI app factory, Uvicorn entry
  api/router.py        # Route assembly (auth, cases, executions, corrections, dsl, ai-planning, etc.)
  api/routes/          # Thin route handlers
  services/            # Business logic
    ai_planning.py              # AI planning core logic (largest service)
    ai_planning_streaming.py    # SSE streaming helpers (CancellationManager, event formatter)
    executions.py, dsl.py, cases.py, corrections.py, auth.py, settings.py
  models/              # SQLAlchemy 2.x ORM
    test_case.py, test_case_run.py, locator_correction.py
    ai_planning_session.py, ai_planning_draft.py, ai_planning_message.py
    project.py, dsl_generation_run.py
  runners/
    playwright_runner.py    # Execution engine: sync + streaming modes, artifact collection
  locators/            # 5-tier hybrid locator system
    corrections.py     # Tier 0: historical manual corrections (priority match)
    semantic.py        # Tier 1-2: A11y candidates + DOM semantic (text_parent_chain, element_id, CSS/XPath)
    ai_visual.py       # Tier 3: VLM-based visual locate (disabled by default)
    fallback.py        # Tier 4: raise InterventionNeededError, collect DOM snapshot
  ai/
    test_planning_agent.py   # ReAct-style conversational test planning agent
    dsl_generator.py         # NL→DSL with governance, auto-repair, rejection tracking
    page_explorer.py         # Page structure exploration
    planning_tools.py        # Agent tool implementations
  schemas/dsl.py       # Pydantic DSL models (GotoStep, ClickStep, InputStep, etc.)
  reporters/json_report.py   # JSON report generation
  core/config.py       # Settings from env vars (.env file)

frontend/src/
  app/AppRouter.tsx          # React Router v6, lazy-loaded pages
  pages/                     # PlanningPage, CasesPage, ReportPage, ExecutionDetailPage, CaseEditPage
  components/                # AITestPlanningPanel, ChatInput, StepList, InterventionPanel
  services/
    api.ts                   # REST API client (fetch wrappers)
    sseClient.ts             # Generic SSE client (POST + ReadableStream, with AbortSignal)
  layouts/                   # AppLayout, NotebookLMLayout (three-column layout)
  types/api.ts               # TypeScript type definitions for API contracts
```

## Environment Setup

- Backend requires Python 3.12+. Uses `uv` for dependency management.
- `AUTH_SESSION_SECRET` is **required** — backend crashes without it. Set in `backend/.env` (copy from `.env.example`).
- `get_settings()` uses `@lru_cache` — in tests, the `reset_cached_state` autouse fixture clears caches on `get_settings`, `get_engine`, `get_session_factory`.
- Test fixtures in `backend/tests/conftest.py` auto-set `AUTH_SESSION_SECRET` and provide `db_session`, `client`, `anonymous_client` fixtures using in-memory SQLite.

## Key Data Flows

**Execution flow**: Case DSL → `playwright_runner` → per-step locator fallback chain → evidence (screenshot, console, network) → `TestCaseRun` with step-level results.

**SSE streaming flow**: All AI planning operations use SSE over POST (not WebSocket):
- Frontend `callSSE()` sends POST with JSON body → backend `StreamingResponse` with `text/event-stream`
- Endpoints: `/chat`, `/drafts`, `/execute` under `/api/v1/ai-planning/sessions/{id}/`
- Cancellation: frontend `AbortController` → `POST .../cancel` → backend `CancellationManager`

**AI Planning flow**: User conversation → `test_planning_agent` (ReAct + tool calls) → DSL draft → user review → save as TestCase → trigger execution → stream progress.

**Correction flow**: Failed step → `needs_intervention` → user submits correction → stored as `LocatorCorrection` → Tier 0 priority match on future runs.

## Conventions

- **Backend**: FastAPI + SQLAlchemy 2.x + Alembic. Route handlers thin, logic in services. SQLite for local dev, PostgreSQL for production design.
- **Frontend**: React + TypeScript + Vite + Ant Design + TanStack Query. Vite dev server proxies `/api` and `/artifacts` to backend. No execution logic in frontend.
- **Streaming**: Use SSE (fetch-based `sseClient.ts`), not WebSocket. All streaming endpoints are POST with JSON body.
- **Testing**: `tests/unit/` for unit tests, `tests/integration/` for integration tests. `browser_integration` pytest marker for browser-level tests. All meaningful features need tests.
- **DSL**: All test cases must be structured DSL. No free-form NL into executor. Validate before execution. Every step produces evidence.
- **AI**: AI generation cannot bypass DSL validation. AI visual is opt-in (disabled by default). DSL generator outputs governance metadata (warnings, normalization_notes, generation_meta).
- **Design docs**: Specs and plans live in `docs/superpowers/specs/` and `docs/superpowers/plans/` (date-prefixed filenames). Read the relevant spec before implementing a feature.
- **Structured logging**: JSONL format in `backend_structured.log`. 4 categories: `ai_thinking`, `tool_call`, `dsl_execution`, `locator_fallback`. Query with `jq 'select(.category == "ai_thinking")' backend_structured.log`.

## Project Skills

- **e2e-testing-workflow** (`.claude/skills/e2e-testing-workflow.md`): E2E 手动测试完整链路 — 启动系统 → AI 会话规划 → 保存执行 DSL → 分析报告 → 用户反馈迭代。当用户说 "测试平台"、"E2E 测试"、"手动测试" 时自动触发。
- **e2e-brand-filter-cart** (`.claude/skills/e2e-brand-filter-cart.md`): 品牌筛选购物车 E2E 测试场景。
