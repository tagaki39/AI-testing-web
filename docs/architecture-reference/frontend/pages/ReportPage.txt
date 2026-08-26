import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Spin, Empty, Typography, Tag, Popconfirm, Modal, Input, message } from "antd";

import { NotebookLMLayout } from "../layouts/NotebookLMLayout";
import {
  getProjects, getExecutionOverview, getExecutions, getExecutionDetail,
  deleteExecution, deleteProject, createProject, updateProject,
} from "../services/api";
import type { ProjectSummary, StoredCaseExecutionSummary, StepExecutionEvidence, ExecutionStatus } from "../types/api";

const { Text, Title } = Typography;

const STATUS_ICON: Record<ExecutionStatus, string> = {
  passed: "✅",
  failed: "❌",
  running: "⏳",
  needs_intervention: "⚠️",
};

function formatTime(iso: string | null) {
  if (!iso) return "-";
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function StepRow({ step }: { step: StepExecutionEvidence }) {
  const [showScreenshot, setShowScreenshot] = useState(false);
  const isFailed = step.status === "failed";

  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "6px 0",
        borderLeft: `3px solid ${isFailed ? "#ff4d4f" : "#52c41a"}`,
        paddingLeft: 8,
        marginBottom: 4,
      }}
    >
      <span style={{ fontSize: 12 }}>{isFailed ? "✗" : "✓"}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <Text style={{ fontSize: 12 }}>
            Step {step.step_index + 1}: <Text code style={{ fontSize: 11 }}>{step.action}</Text>
            {step.target && (
              <Text type="secondary" style={{ fontSize: 11 }}> {step.target}</Text>
            )}
          </Text>
          {step.duration_ms != null && (
            <Text type="secondary" style={{ fontSize: 11 }}>({step.duration_ms}ms)</Text>
          )}
        </div>

        {isFailed && step.error_message && (
          <div
            style={{
              marginTop: 4,
              padding: "4px 8px",
              background: "#fff2f0",
              borderRadius: 6,
              fontSize: 12,
              color: "#cf1322",
            }}
          >
            {step.error_message}
          </div>
        )}

        {step.locator_trace?.failure_reason && (
          <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 2 }}>
            定位失败: {step.locator_trace.failure_reason}
          </Text>
        )}

        {step.screenshot_url && (
          <div style={{ marginTop: 4 }}>
            <a
              onClick={(e) => {
                e.stopPropagation();
                setShowScreenshot(!showScreenshot);
              }}
              style={{ fontSize: 11, cursor: "pointer" }}
            >
              {showScreenshot ? "收起截图" : "查看截图"}
            </a>
            {showScreenshot && (
              <img
                src={step.screenshot_url}
                alt={`Step ${step.step_index + 1} screenshot`}
                style={{
                  maxWidth: "100%",
                  maxHeight: 300,
                  borderRadius: 8,
                  marginTop: 4,
                  border: "1px solid #f0f0f0",
                }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ExecutionRow({
  exec,
  expanded,
  onToggle,
  steps,
  onDelete,
}: {
  exec: StoredCaseExecutionSummary;
  expanded: boolean;
  onToggle: () => void;
  steps: StepExecutionEvidence[] | undefined;
  onDelete: () => void;
}) {
  return (
    <div className="nb-card" style={{ padding: 0, marginBottom: 8 }}>
      <div
        onClick={onToggle}
        style={{
          padding: "12px 16px",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 10,
          borderRadius: expanded ? "12px 12px 0 0" : 12,
        }}
      >
        <span>{STATUS_ICON[exec.status]}</span>
        <Text strong style={{ flex: 1 }}>
          {exec.case_name}
        </Text>
        {exec.failure_category && (
          <Tag color="red" style={{ marginRight: 4 }}>
            {exec.failure_category}
          </Tag>
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatTime(exec.started_at)}
        </Text>
        <Popconfirm
          title="确定删除此执行记录？"
          onConfirm={(e) => { e?.stopPropagation(); onDelete(); }}
          onCancel={(e) => e?.stopPropagation()}
          okText="删除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
        >
          <span
            onClick={(e) => e.stopPropagation()}
            style={{ fontSize: 14, cursor: "pointer", opacity: 0.5 }}
            title="删除"
          >
            🗑️
          </span>
        </Popconfirm>
      </div>

      {expanded && steps && (
        <div style={{ borderTop: "1px solid #f0f0f0", padding: "8px 16px 16px" }}>
          {steps.map((step, i) => (
            <StepRow key={i} step={step} />
          ))}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="nb-card"
      style={{ padding: 16, display: "flex", flexDirection: "column", gap: 4 }}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>{label}</Text>
      <Text strong style={{ fontSize: 20 }}>{value}</Text>
    </div>
  );
}

export function ReportPage() {
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);
  const [messageApi, contextHolder] = message.useMessage();
  const queryClient = useQueryClient();

  const { data: projects = [], isLoading: projectsLoading } = useQuery<ProjectSummary[]>({
    queryKey: ["projects"],
    queryFn: getProjects,
  });

  const activeProjectId = selectedProjectId ?? projects[0]?.id ?? null;

  const { data: overview } = useQuery({
    queryKey: ["execution-overview", activeProjectId],
    queryFn: () =>
      getExecutionOverview({ scope_type: "project", project_id: activeProjectId!, window_days: 30 }),
    enabled: activeProjectId != null,
  });

  const { data: executions = [] } = useQuery({
    queryKey: ["executions", activeProjectId],
    queryFn: () => getExecutions({ project_id: activeProjectId!, limit: 50 }),
    enabled: activeProjectId != null,
  });

  const [expandedId, setExpandedId] = useState<number | null>(null);

  // --- Project CRUD ---
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editProject, setEditProject] = useState<ProjectSummary | null>(null);
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleCreateProject = async () => {
    if (!formName.trim()) return;
    setSubmitting(true);
    try {
      await createProject({ name: formName.trim(), description: formDesc.trim() || undefined });
      setCreateModalOpen(false);
      setFormName("");
      setFormDesc("");
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      void messageApi.success("项目已创建");
    } catch (err) {
      void messageApi.error("创建失败: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setSubmitting(false);
    }
  };

  const handleEditProject = async () => {
    if (!editProject || !formName.trim()) return;
    setSubmitting(true);
    try {
      await updateProject(editProject.id, { name: formName.trim(), description: formDesc.trim() || undefined });
      setEditModalOpen(false);
      setEditProject(null);
      setFormName("");
      setFormDesc("");
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      void messageApi.success("项目已更新");
    } catch (err) {
      void messageApi.error("更新失败: " + (err instanceof Error ? err.message : String(err)));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async (id: number) => {
    await deleteExecution(id);
    if (expandedId === id) setExpandedId(null);
    queryClient.invalidateQueries({ queryKey: ["executions", activeProjectId] });
    queryClient.invalidateQueries({ queryKey: ["execution-overview", activeProjectId] });
  };

  const handleDeleteProject = async (projectId: number) => {
    try {
      await deleteProject(projectId);
      if (selectedProjectId === projectId) setSelectedProjectId(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      void messageApi.success("项目已删除");
    } catch (err) {
      void messageApi.error("删除失败: " + (err instanceof Error ? err.message : String(err)));
    }
  };

  const { data: executionDetail } = useQuery({
    queryKey: ["execution-detail", expandedId],
    queryFn: () => getExecutionDetail(expandedId!),
    enabled: expandedId != null,
  });

  const openEditModal = (p: ProjectSummary) => {
    setEditProject(p);
    setFormName(p.name);
    setFormDesc(p.description ?? "");
    setEditModalOpen(true);
  };

  const leftPanel = (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, height: "100%" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <Title level={5} style={{ margin: 0 }}>
          项目
        </Title>
        <a
          onClick={() => { setFormName(""); setFormDesc(""); setCreateModalOpen(true); }}
          style={{ fontSize: 18, cursor: "pointer" }}
          title="新建项目"
        >
          +
        </a>
      </div>
      <div style={{ flex: 1, overflowY: "auto" }}>
        {projectsLoading ? (
          <Spin />
        ) : (
          projects.map((p) => (
            <div
              key={p.id}
              onClick={() => setSelectedProjectId(p.id)}
              style={{
                padding: "8px 12px",
                borderRadius: 8,
                cursor: "pointer",
                fontSize: 13,
                display: "flex",
                alignItems: "center",
                gap: 4,
                background: p.id === activeProjectId ? "#1a1a2e" : "transparent",
                color: p.id === activeProjectId ? "#fff" : "#666",
                transition: "background 0.15s",
              }}
            >
              <span style={{ flex: 1 }}>{p.name}</span>
              <span
                onClick={(e) => { e.stopPropagation(); openEditModal(p); }}
                style={{ fontSize: 12, cursor: "pointer", opacity: 0.5 }}
                title="编辑项目"
              >
                ✏️
              </span>
              <Popconfirm
                title="确定删除此项目？删除后不可恢复。"
                onConfirm={(e) => { e?.stopPropagation(); handleDeleteProject(p.id); }}
                onCancel={(e) => e?.stopPropagation()}
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <span
                  onClick={(e) => e.stopPropagation()}
                  style={{ fontSize: 12, cursor: "pointer", opacity: 0.5 }}
                  title="删除项目"
                >
                  🗑️
                </span>
              </Popconfirm>
            </div>
          ))
        )}
      </div>

      {/* Create Project Modal */}
      <Modal
        title="新建项目"
        open={createModalOpen}
        onOk={handleCreateProject}
        onCancel={() => setCreateModalOpen(false)}
        okText="创建"
        cancelText="取消"
        confirmLoading={submitting}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
          <Input placeholder="项目名称" value={formName} onChange={(e) => setFormName(e.target.value)} />
          <Input.TextArea placeholder="项目描述（可选）" value={formDesc} onChange={(e) => setFormDesc(e.target.value)} rows={3} />
        </div>
      </Modal>

      {/* Edit Project Modal */}
      <Modal
        title="编辑项目"
        open={editModalOpen}
        onOk={handleEditProject}
        onCancel={() => { setEditModalOpen(false); setEditProject(null); }}
        okText="保存"
        cancelText="取消"
        confirmLoading={submitting}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
          <Input placeholder="项目名称" value={formName} onChange={(e) => setFormName(e.target.value)} />
          <Input.TextArea placeholder="项目描述（可选）" value={formDesc} onChange={(e) => setFormDesc(e.target.value)} rows={3} />
        </div>
      </Modal>
    </div>
  );

  const centerPanel = activeProjectId ? (
    <div style={{ padding: 20, overflowY: "auto", height: "100%" }} className="panel-scroll">
      <Title level={4} style={{ margin: 0, marginBottom: 16 }}>
        {projects.find((p) => p.id === activeProjectId)?.name} — 报告
      </Title>

      {overview && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 12, marginBottom: 24 }}>
          <StatCard label="通过率" value={`${(overview.pass_rate * 100).toFixed(1)}%`} />
          <StatCard label="失败数" value={String(overview.failed_count)} />
          <StatCard label="总执行数" value={String(overview.total_count)} />
          <StatCard
            label="平均耗时"
            value={overview.avg_duration_ms ? `${(overview.avg_duration_ms / 1000).toFixed(1)}s` : "-"}
          />
        </div>
      )}

      <Title level={5} style={{ margin: 0, marginBottom: 12 }}>执行结果</Title>
      {executions.length === 0 ? (
        <Empty description="暂无执行记录" />
      ) : (
        executions.map((exec) => (
          <ExecutionRow
            key={exec.id}
            exec={exec}
            expanded={expandedId === exec.id}
            onToggle={() => setExpandedId(expandedId === exec.id ? null : exec.id)}
            steps={
              expandedId === exec.id && executionDetail?.report
                ? executionDetail.report.steps
                : undefined
            }
            onDelete={() => handleDelete(exec.id)}
          />
        ))
      )}
    </div>
  ) : (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%" }}>
      <Empty description="请选择一个项目" />
    </div>
  );

  return (
    <>
      {contextHolder}
      <NotebookLMLayout leftPanel={leftPanel} centerPanel={centerPanel} navBottom />
    </>
  );
}
