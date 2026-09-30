import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  List,
  Row,
  Space,
  Tag,
  Timeline,
  Typography,
} from "antd";
import { Link, useLocation, useParams } from "react-router-dom";

import { InterventionPanel } from "../components/InterventionPanel";
import { ErrorBlock, LoadingBlock } from "../components/PageFeedback";
import { renderExecutionStatus } from "../components/executionPresentation";
import {
  classifyLocatorStrategy,
  formatDuration,
  formatPassRate,
} from "../components/executionMetrics";
import type { LocatorStrategyBucket } from "../components/executionMetrics";
import { NotebookLMLayout } from "../layouts/NotebookLMLayout";
import { getExecutionDetail, getExecutionOverview } from "../services/api";
import type {
  ConsoleEvent,
  ExecutionsOverview,
  NetworkEvent,
  StepExecutionEvidence,
} from "../types/api";

const DEFAULT_EVENT_PREVIEW_COUNT = 2;
const DEFAULT_EXECUTIONS_PATH = "/executions";

const STRATEGY_LABEL: Record<LocatorStrategyBucket, string> = {
  dom: "DOM 定位",
  vlm: "VLM 视觉定位",
  correction: "修正定位",
  manual: "人工干预",
  not_applicable: "不适用",
};

const STRATEGY_COLOR: Record<LocatorStrategyBucket, string> = {
  dom: "blue",
  vlm: "purple",
  correction: "orange",
  manual: "red",
  not_applicable: "default",
};

function formatStepTarget(step: StepExecutionEvidence): string {
  const parts: string[] = [];
  if (step.target) parts.push(step.target);
  if (step.value) parts.push(step.value);
  return parts.join(" → ");
}

function isVariableRef(text: string): boolean {
  return /^\$\{.+\}$/.test(text);
}

function computeStrategyDistribution(steps: StepExecutionEvidence[]) {
  const counts: Record<LocatorStrategyBucket, number> = {
    dom: 0,
    vlm: 0,
    correction: 0,
    manual: 0,
    not_applicable: 0,
  };
  for (const step of steps) {
    const bucket = classifyLocatorStrategy(step);
    counts[bucket]++;
  }
  return counts;
}

type ExecutionDetailLocationState = {
  fromExecutions?: string;
};

function isBaseUrlError(message: string | null) {
  if (!message) {
    return false;
  }
  return message.includes("case.base_url") || message.includes("Relative goto step requires");
}

function EventList<T>({
  title,
  events,
  renderItem,
  emptyDescription,
}: {
  title: string;
  events: T[];
  renderItem: (event: T) => ReactNode;
  emptyDescription: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const visibleEvents = expanded ? events : events.slice(0, DEFAULT_EVENT_PREVIEW_COUNT);

  return (
    <>
      <Typography.Title level={5} style={{ marginTop: 16 }}>
        {title}
      </Typography.Title>
      {events.length ? (
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <List size="small" dataSource={visibleEvents} renderItem={(event) => <List.Item>{renderItem(event)}</List.Item>} />
          {events.length > DEFAULT_EVENT_PREVIEW_COUNT ? (
            <Typography.Link onClick={() => setExpanded((value) => !value)}>
              {expanded ? "收起" : `显示全部 ${events.length} 条`}
            </Typography.Link>
          ) : null}
        </Space>
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={emptyDescription} />
      )}
    </>
  );
}

function StepEvidenceBody({
  step,
  caseId,
  executionId,
  triggeredBy,
}: {
  step: StepExecutionEvidence;
  caseId: number;
  executionId: number;
  triggeredBy: number;
}) {
  const locatorTrace = step.locator_trace;
  const isAssert = step.action.startsWith("assert");

  return (
    <div id={`step-${step.step_index + 1}`}>
      {/* Step summary strip */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        <Tag color={step.status === "passed" ? "success" : "error"} style={{ fontWeight: 600 }}>
          {step.status === "passed" ? "PASS" : "FAIL"}
        </Tag>
        <Typography.Text strong>{step.action}</Typography.Text>
        {step.target && (
          <>
            <Typography.Text type="secondary">→</Typography.Text>
            <Typography.Text>{step.target}</Typography.Text>
          </>
        )}
        {step.value && (
          <>
            <Typography.Text type="secondary">
              {step.action === "input" ? "输入" : step.action === "goto" ? "URL" : "值"}
            </Typography.Text>
            {isVariableRef(step.value) ? (
              <Tag color="blue">{step.value}</Tag>
            ) : (
              <Typography.Text code>{step.value}</Typography.Text>
            )}
          </>
        )}
        {isAssert && (
          <Tag
            color={step.status === "passed" ? "#f6ffed" : "#fff2f0"}
            style={{ color: step.status === "passed" ? "#52c41a" : "#ff4d4f", fontWeight: 600, marginLeft: 4 }}
          >
            {step.status === "passed" ? "断言通过 ✓" : "断言失败 ✗"}
          </Tag>
        )}
        <Typography.Text type="secondary" style={{ marginLeft: "auto" }}>
          {step.duration_ms != null ? `${step.duration_ms} ms` : ""}
        </Typography.Text>
      </div>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={11}>
          <Card size="small" title="页面信息" className="evidence-card">
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="URL">{step.url || "-"}</Descriptions.Item>
                <Descriptions.Item label="页面标题">{step.page_title || "-"}</Descriptions.Item>
                <Descriptions.Item label="耗时">{step.duration_ms ?? "-"} ms</Descriptions.Item>
                <Descriptions.Item label="视口">
                  {step.viewport ? `${step.viewport.width} x ${step.viewport.height}` : "-"}
                </Descriptions.Item>
                <Descriptions.Item label="DOM 摘要">
                  {step.dom_summary ? (
                    <Space direction="vertical" size={2}>
                      <Typography.Text>{step.dom_summary.text_preview || "-"}</Typography.Text>
                      <Typography.Text type="secondary">
                        button {step.dom_summary.button_count} / input {step.dom_summary.input_count} / link{" "}
                        {step.dom_summary.link_count}
                      </Typography.Text>
                    </Space>
                  ) : (
                    "-"
                  )}
                </Descriptions.Item>
              </Descriptions>
              {step.screenshot_url ? (
                <Space direction="vertical" size="small" style={{ width: "100%" }}>
                  <div className="step-screenshot-frame">
                    <img
                      className="step-screenshot-image"
                      src={step.screenshot_url}
                      alt={`step-${step.step_index + 1}`}
                    />
                  </div>
                  <Typography.Text type="secondary">
                    截图按固定展示框缩放，避免与其他证据面板重叠。{" "}
                    <a href={step.screenshot_url} target="_blank" rel="noreferrer">
                      打开原图
                    </a>
                  </Typography.Text>
                </Space>
              ) : (
                <div className="screenshot-empty">该步骤没有截图</div>
              )}
            </Space>
          </Card>
        </Col>
        <Col xs={24} lg={13}>
          <Space direction="vertical" size="middle" style={{ width: "100%" }}>
            <Card size="small" title="定位信息" className="evidence-card">
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="目标">{step.target || locatorTrace?.target || "-"}</Descriptions.Item>
                <Descriptions.Item label="命中策略">
                  {locatorTrace?.match_strategy || step.resolved_by || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="命中说明">
                  {locatorTrace?.selection_reason || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="失败原因">
                  {locatorTrace?.failure_reason || step.error_message || "-"}
                </Descriptions.Item>
                <Descriptions.Item label="最终命中">
                  {locatorTrace?.selected_candidate
                    ? `${locatorTrace.selected_candidate.strategy} / ${
                        locatorTrace.selected_candidate.preview_text || locatorTrace.selected_candidate.role || "-"
                      }`
                    : "-"}
                </Descriptions.Item>
              </Descriptions>
              <Typography.Title level={5} style={{ marginTop: 16 }}>
                候选列表
              </Typography.Title>
              {locatorTrace?.candidates.length ? (
                <List
                  size="small"
                  dataSource={locatorTrace.candidates}
                  renderItem={(candidate, index) => (
                    <List.Item>
                      <Space direction="vertical" size={0}>
                        <Typography.Text>
                          #{index + 1} {candidate.strategy} / {candidate.preview_text || candidate.role || "-"} /
                          score={candidate.score}
                        </Typography.Text>
                        <Typography.Text type="secondary">
                          role={candidate.role || "-"} / visible={candidate.visible ? "true" : "false"} / enabled=
                          {candidate.enabled ? "true" : "false"} / matched=
                          {candidate.matched_rules.length ? candidate.matched_rules.join(", ") : "-"}
                        </Typography.Text>
                        <Typography.Text type="secondary">
                          rejected={candidate.rejected_reasons.length ? candidate.rejected_reasons.join(", ") : "-"}
                        </Typography.Text>
                      </Space>
                    </List.Item>
                  )}
                />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有候选元素证据" />
              )}
            </Card>

            <Card size="small" title="运行信息" className="evidence-card">
              <Descriptions column={1} size="small" bordered>
                <Descriptions.Item label="断言结果">
                  {isAssert ? (
                    <Tag color={step.status === "passed" ? "success" : "error"}>
                      {step.status === "passed" ? "通过 ✓" : "失败 ✗"}
                    </Tag>
                  ) : (
                    "非断言步骤"
                  )}
                </Descriptions.Item>
                <Descriptions.Item label="错误信息">{step.error_message || "-"}</Descriptions.Item>
              </Descriptions>

              <EventList<ConsoleEvent>
                title="Console 事件"
                events={step.console_events}
                emptyDescription="没有 console 告警或错误"
                renderItem={(event) => (
                  <Space direction="vertical" size={0}>
                    <Typography.Text>
                      {event.level} / {event.text}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      {event.source_url || "-"} {event.line_number ?? ""}
                    </Typography.Text>
                  </Space>
                )}
              />

              <EventList<NetworkEvent>
                title="Network 事件"
                events={step.network_events}
                emptyDescription="没有失败请求证据"
                renderItem={(event) => (
                  <Space direction="vertical" size={0}>
                    <Typography.Text>
                      {event.method} {event.url}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      status={event.status ?? "-"} / resource={event.resource_type || "-"} / failure=
                      {event.failure_text || "-"}
                    </Typography.Text>
                  </Space>
                )}
              />
            </Card>
          </Space>
        </Col>
      </Row>
      {step.intervention_request ? (
        <div style={{ marginTop: 16 }}>
          <InterventionPanel
            caseId={caseId}
            executionId={executionId}
            triggeredBy={triggeredBy}
            request={step.intervention_request}
          />
        </div>
      ) : null}
    </div>
  );
}

export function ExecutionDetailPage() {
  const params = useParams<{ executionId: string }>();
  const location = useLocation();
  const state = location.state as ExecutionDetailLocationState | null;
  const executionId = Number(params.executionId);
  const query = useQuery({
    queryKey: ["execution-detail", executionId],
    queryFn: () => getExecutionDetail(executionId),
    enabled: Number.isFinite(executionId),
  });

  const [overviewReady, setOverviewReady] = useState(false);
  const [overviewData, setOverviewData] = useState<ExecutionsOverview | null>(null);
  useEffect(() => {
    if (!query.data?.case_id) return;
    let cancelled = false;
    getExecutionOverview({ scope_type: "case", case_id: query.data.case_id })
      .then((data) => {
        if (!cancelled) {
          setOverviewData(data);
          setOverviewReady(true);
        }
      })
      .catch(() => {
        if (!cancelled) setOverviewReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [query.data?.case_id]);

  const failedStepKeys = useMemo(
    () =>
      (query.data?.report?.steps ?? [])
        .filter((step) => step.status === "failed")
        .map((step) => String(step.step_index)),
    [query.data?.report?.steps],
  );
  const [activeKeys, setActiveKeys] = useState<string[]>([]);
  const [activeStepIndex, setActiveStepIndex] = useState(0);

  useEffect(() => {
    setActiveKeys(failedStepKeys);
  }, [failedStepKeys]);

  useEffect(() => {
    const steps = query.data?.report?.steps ?? [];
    if (!steps.length) {
      setActiveStepIndex(0);
      return;
    }
    const failedStep = steps.find((step) => step.status === "failed");
    setActiveStepIndex(failedStep?.step_index ?? 0);
  }, [query.data?.id, query.data?.report?.steps]);

  useEffect(() => {
    if (!query.data || !location.hash) {
      return;
    }
    const element = document.querySelector(location.hash);
    if (element && "scrollIntoView" in element) {
      element.scrollIntoView({ block: "start" });
    }
  }, [location.hash, query.data]);

  if (query.isLoading) {
    return <LoadingBlock />;
  }
  if (query.isError) {
    return <ErrorBlock message={query.error.message} />;
  }
  if (!query.data) {
    return <Empty description="执行详情不存在。" />;
  }

  const detail = query.data;
  const steps = detail.report?.steps ?? [];
  const backHref =
    typeof state?.fromExecutions === "string" && state.fromExecutions ? state.fromExecutions : DEFAULT_EXECUTIONS_PATH;
  const activeStep = steps[activeStepIndex] ?? steps[0];
  const strategyDistribution = computeStrategyDistribution(steps);

  const leftPanel = (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, height: "100%" }}>
      <div>
        <Typography.Title level={4} style={{ margin: 0 }}>
          {detail.case_name}
        </Typography.Title>
        <div style={{ marginTop: 4 }}>
          <Typography.Text type="secondary">查看步骤时间线、定位候选、截图证据、URL 与失败原因。</Typography.Text>
        </div>
      </div>
      <Space wrap>
        <Button>
          <Link to={backHref}>返回执行中心</Link>
        </Button>
        <Button>
          <Link to={`/cases/${detail.case_id}/edit`}>返回用例</Link>
        </Button>
      </Space>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
        <div className="nb-card" style={{ padding: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            执行状态
          </Typography.Text>
          <div style={{ marginTop: 4 }}>{renderExecutionStatus(detail.status)}</div>
        </div>
        <div className="nb-card" style={{ padding: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            执行编号
          </Typography.Text>
          <div style={{ marginTop: 4, fontWeight: 700 }}>{`#${detail.id}`}</div>
        </div>
        <div className="nb-card" style={{ padding: 12 }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            步骤数
          </Typography.Text>
          <div style={{ marginTop: 4, fontWeight: 700 }}>{steps.length}</div>
        </div>
      </div>
      <div style={{ flex: 1, overflowY: "auto", paddingRight: 4 }} className="panel-scroll">
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          {steps.map((step) => (
            <div
              key={step.step_index}
              className={`step-item ${step.step_index === activeStepIndex ? "step-item-active" : ""}`}
              onClick={() => {
                setActiveStepIndex(step.step_index);
                setActiveKeys([String(step.step_index)]);
              }}
              style={{ fontSize: 11, marginBottom: 2 }}
            >
              <span style={{ marginRight: 4, color: step.status === "passed" ? "#52c41a" : "#ff4d4f", fontWeight: 700 }}>
                {step.status === "passed" ? "PASS" : "FAIL"}
              </span>
              <strong>{`${step.step_index + 1}`}</strong>
              <span style={{ marginLeft: 4, color: "#666" }}>{step.action}</span>
              {step.target && (
                <span style={{ marginLeft: 4, color: "#999" }}>
                  → {step.target.length > 18 ? step.target.slice(0, 18) + "…" : step.target}
                </span>
              )}
            </div>
          ))}
        </Space>
      </div>
    </div>
  );

  const rightCards = [
    <div key="overview">
      <Typography.Text strong style={{ fontSize: 14, display: "block", marginBottom: 12 }}>
        执行概览
      </Typography.Text>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Typography.Text strong>{`通过率 ${overviewData ? formatPassRate(overviewData.pass_rate) : "-"}`}</Typography.Text>
        <Typography.Text strong>{`平均耗时 ${overviewData ? formatDuration(overviewData.avg_duration_ms) : "-"}`}</Typography.Text>
        <Typography.Text strong>{`干预率 ${overviewData ? formatPassRate(overviewData.intervention_rate) : "-"}`}</Typography.Text>
      </Space>
    </div>,
    <div key="strategy">
      <Typography.Text strong style={{ fontSize: 14, display: "block", marginBottom: 12 }}>
        定位策略分布
      </Typography.Text>
      <Space wrap>
        {(Object.keys(strategyDistribution) as LocatorStrategyBucket[])
          .filter((key) => strategyDistribution[key] > 0)
          .map((key) => (
            <Tag key={key} color={STRATEGY_COLOR[key]}>
              {`${STRATEGY_LABEL[key]} x${strategyDistribution[key]}`}
            </Tag>
          ))}
      </Space>
    </div>,
    <div key="candidates">
      <Typography.Text strong style={{ fontSize: 14, display: "block", marginBottom: 12 }}>
        候选元素
      </Typography.Text>
      {activeStep?.locator_trace?.candidates.length ? (
        <List
          size="small"
          dataSource={activeStep.locator_trace.candidates}
          renderItem={(candidate, index) => (
            <List.Item>
              <Space direction="vertical" size={0}>
                <Typography.Text>{`#${index + 1} ${candidate.strategy} / ${candidate.preview_text || candidate.role || "-"}`}</Typography.Text>
                <Typography.Text type="secondary">{`评分 ${candidate.score}`}</Typography.Text>
              </Space>
            </List.Item>
          )}
        />
      ) : (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有候选元素证据" />
      )}
    </div>,
  ];

  return (
    <NotebookLMLayout
      leftPanel={leftPanel}
      rightCards={rightCards}
      centerPanel={
        <div style={{ overflowY: "auto", padding: "20px 24px 24px" }} className="panel-scroll">
          <Space direction="vertical" size="large" style={{ width: "100%" }}>
            {isBaseUrlError(detail.error_message) ? (
              <Alert
                type="warning"
                showIcon
                message="该用例的相对路径 goto 缺少用例 Base URL"
                description="请回到用例工作台补充 Base URL，或在调试执行接口时显式传入 base_url。"
              />
            ) : null}

            <div className="summary-strip">
              <div className="summary-tile">
                <div className="summary-label">执行状态</div>
                <div className="summary-value">{renderExecutionStatus(detail.status)}</div>
              </div>
              <div className="summary-tile">
                <div className="summary-label">执行编号</div>
                <div className="summary-value">#{detail.id}</div>
              </div>
              <div className="summary-tile">
                <div className="summary-label">步骤数量</div>
                <div className="summary-value">{steps.length}</div>
              </div>
            </div>

            <Card>
              <Descriptions bordered column={2}>
          <Descriptions.Item label="用例名称">{detail.case_name}</Descriptions.Item>
          <Descriptions.Item label="项目 ID">{detail.project_id}</Descriptions.Item>
          <Descriptions.Item label="开始时间">{new Date(detail.started_at).toLocaleString()}</Descriptions.Item>
          <Descriptions.Item label="结束时间">
            {detail.finished_at ? new Date(detail.finished_at).toLocaleString() : "-"}
          </Descriptions.Item>
          <Descriptions.Item label="总耗时">{detail.duration_ms ?? "-"} ms</Descriptions.Item>
          <Descriptions.Item label="失败步骤">
            {detail.failed_step_index === null || detail.failed_step_index === undefined
              ? "-"
              : `Step ${detail.failed_step_index + 1}`}
          </Descriptions.Item>
          <Descriptions.Item label="错误摘要" span={2}>
            {detail.error_message || "-"}
          </Descriptions.Item>
              </Descriptions>
            </Card>

      {overviewReady && detail.case_id && (
        <>
          <Card title="执行报告总览" size="small">
            <div className="summary-strip">
              <div className="summary-tile">
                <div className="summary-label">通过率</div>
                <div className="summary-value">
                  {overviewData ? formatPassRate(overviewData.pass_rate) : "-"}
                </div>
              </div>
              <div className="summary-tile">
                <div className="summary-label">平均耗时</div>
                <div className="summary-value">
                  {overviewData ? formatDuration(overviewData.avg_duration_ms) : "-"}
                </div>
              </div>
              <div className="summary-tile">
                <div className="summary-label">干预率</div>
                <div className="summary-value">
                  {overviewData ? formatPassRate(overviewData.intervention_rate) : "-"}
                </div>
              </div>
            </div>
          </Card>
          <Card title="定位策略总览" size="small">
            <Space wrap>
              {(() => {
                const dist = computeStrategyDistribution(steps);
                return (Object.keys(dist) as LocatorStrategyBucket[])
                  .filter((key) => dist[key] > 0)
                  .map((key) => (
                    <Tag key={key} color={STRATEGY_COLOR[key]}>
                      {STRATEGY_LABEL[key]}: {dist[key]}
                    </Tag>
                  ));
              })()}
            </Space>
          </Card>
        </>
      )}

            <Card title="步骤时间线">
              {steps.length ? (
                <Space direction="vertical" size="large" style={{ width: "100%" }}>
                  <Timeline
                    items={steps.map((step) => {
                      const desc = formatStepTarget(step);
                      return {
                        color: step.status === "passed" ? "green" : "red",
                        children: `步骤 ${step.step_index + 1}: ${step.action}${desc ? " — " + desc : ""}`,
                      };
                    })}
                  />
                  <Collapse
                    activeKey={activeKeys}
                    onChange={(keys) => setActiveKeys(Array.isArray(keys) ? keys : [keys])}
                    items={steps.map((step) => {
                      const isAssert = step.action.startsWith("assert");
                      const desc = formatStepTarget(step);
                      return {
                        key: String(step.step_index),
                        label: (
                          <Space>
                            <Typography.Text strong>{`步骤 ${step.step_index + 1}: ${step.action}`}</Typography.Text>
                            {desc && <Typography.Text type="secondary">{desc}</Typography.Text>}
                            {isAssert && (
                              <Tag color={step.status === "passed" ? "success" : "error"}>
                                {step.status === "passed" ? "✓" : "✗"}
                              </Tag>
                            )}
                            {!isAssert && renderExecutionStatus(step.status)}
                          </Space>
                        ),
                        children: (
                          <StepEvidenceBody
                            step={step}
                            caseId={detail.case_id}
                            executionId={detail.id}
                            triggeredBy={detail.triggered_by}
                          />
                        ),
                      };
                    })}
                  />
                </Space>
              ) : (
                <Empty description="当前执行没有步骤证据。" />
              )}
            </Card>
          </Space>
        </div>
      }
    />
  );
}
