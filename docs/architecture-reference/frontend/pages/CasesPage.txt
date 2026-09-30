import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Checkbox, Empty, Input, Modal, Popconfirm, Space, Tag, Typography, message } from "antd";
import { Link, useNavigate } from "react-router-dom";
import { SearchOutlined, PlusOutlined, DeleteOutlined } from "@ant-design/icons";
import { useState, useMemo } from "react";

import { ErrorBlock, LoadingBlock } from "../components/PageFeedback";
import { NotebookLMLayout } from "../layouts/NotebookLMLayout";
import {
  executeCase, getCases, deleteCase, batchDeleteCases,
  getProjects, createProject, updateProject, deleteProject,
} from "../services/api";
import type { StoredCaseSummary, ProjectSummary } from "../types/api";

const statusTags = [
  { key: "all", label: "全部" },
  { key: "pending", label: "待执行" },
  { key: "passed", label: "已通过" },
  { key: "failed", label: "已失败" },
];

export function CasesPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();

  // ── Project state ──
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [editModalOpen, setEditModalOpen] = useState(false);
  const [editProject, setEditProject] = useState<ProjectSummary | null>(null);
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // ── Case state ──
  const [searchText, setSearchText] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  // ── Queries ──
  const { data: projects = [], isLoading: projectsLoading } = useQuery<ProjectSummary[]>({
    queryKey: ["projects"],
    queryFn: getProjects,
  });

  const activeProjectId = selectedProjectId ?? projects[0]?.id ?? null;

  const casesQuery = useQuery({
    queryKey: ["cases", activeProjectId],
    queryFn: () => getCases(activeProjectId ? { project_id: activeProjectId } : undefined),
    enabled: activeProjectId != null,
  });

  // ── Mutations ──
  const executionMutation = useMutation({
    mutationFn: (caseId: number) => executeCase(caseId, { actor_user_id: 1 }),
    onSuccess: (execution) => {
      queryClient.invalidateQueries({ queryKey: ["executions"] });
      void navigate(`/run/${execution.id}`);
    },
    onError: (error: Error) => {
      void messageApi.error(error.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (caseId: number) => deleteCase(caseId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["cases"] });
      void messageApi.success("用例已删除");
    },
    onError: (error: Error) => {
      void messageApi.error("删除失败: " + error.message);
    },
  });

  const batchDeleteMutation = useMutation({
    mutationFn: (ids: number[]) => batchDeleteCases(ids),
    onSuccess: () => {
      setSelectedIds(new Set());
      queryClient.invalidateQueries({ queryKey: ["cases"] });
      void messageApi.success("批量删除成功");
    },
    onError: (error: Error) => {
      void messageApi.error("批量删除失败: " + error.message);
    },
  });

  // ── Project CRUD ──
  const handleCreateProject = async () => {
    if (!formName.trim() || submitting) return;
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
    if (!editProject || !formName.trim() || submitting) return;
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

  const handleDeleteProject = async (projectId: number) => {
    try {
      await deleteProject(projectId);
      if (selectedProjectId === projectId) setSelectedProjectId(null);
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      queryClient.invalidateQueries({ queryKey: ["cases"] });
      void messageApi.success("项目已删除");
    } catch (err) {
      void messageApi.error("删除失败: " + (err instanceof Error ? err.message : String(err)));
    }
  };

  const openEditModal = (p: ProjectSummary) => {
    setEditProject(p);
    setFormName(p.name);
    setFormDesc(p.description ?? "");
    setEditModalOpen(true);
  };

  // ── Derived data ──
  const allCases = casesQuery.data?.items ?? [];

  const filteredCases = useMemo(() => {
    let cases = allCases;
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      cases = cases.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          (c.description || "").toLowerCase().includes(q),
      );
    }
    return cases;
  }, [allCases, searchText]);

  const totalSteps = useMemo(
    () => allCases.reduce((sum, c) => sum + c.steps.length, 0),
    [allCases],
  );

  const toggleSelect = (id: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === filteredCases.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(filteredCases.map((c) => c.id)));
    }
  };

  /* ---- Left Panel ---- */
  const leftPanel = (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, flex: 1 }}>
      {/* Project list */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Typography.Text strong style={{ fontSize: 14 }}>项目</Typography.Text>
        <a
          onClick={() => { setFormName(""); setFormDesc(""); setCreateModalOpen(true); }}
          style={{ fontSize: 18, cursor: "pointer" }}
          title="新建项目"
        >
          +
        </a>
      </div>

      {projectsLoading ? (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>加载中...</Typography.Text>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {projects.map((p) => (
            <div
              key={p.id}
              onClick={() => setSelectedProjectId(p.id)}
              style={{
                padding: "6px 10px",
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
                title="确定删除此项目？删除后关联用例不可恢复。"
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
          ))}
        </div>
      )}

      <div style={{ borderBottom: "1px solid #f0f0f0", margin: "4px 0" }} />

      {/* Search + Status filter */}
      <Input
        placeholder="搜索用例..."
        prefix={<SearchOutlined />}
        value={searchText}
        onChange={(e) => setSearchText(e.target.value)}
        allowClear
        style={{
          borderRadius: 24,
          background: "#F0F4F8",
          border: "none",
          boxShadow: "none",
        }}
      />

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {statusTags.map((tag) => (
          <Tag
            key={tag.key}
            onClick={() => setStatusFilter(tag.key)}
            style={{
              cursor: "pointer",
              borderRadius: 12,
              padding: "2px 12px",
              border: "none",
              background:
                statusFilter === tag.key ? "#1a1a2e" : "#F0F4F8",
              color: statusFilter === tag.key ? "#fff" : "#666",
              fontWeight: statusFilter === tag.key ? 600 : 400,
            }}
          >
            {tag.label}
          </Tag>
        ))}
      </div>

      <div style={{ marginTop: "auto" }}>
        <Link to="/cases/new" style={{ textDecoration: "none" }}>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            block
            style={{
              background: "#1a1a2e",
              borderColor: "#1a1a2e",
              borderRadius: 8,
            }}
          >
            新建用例
          </Button>
        </Link>
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

  /* ---- Center Panel ---- */
  const centerPanel = activeProjectId ? (
    <div style={{ padding: 20, overflowY: "auto", flex: 1 }}>
      <Typography.Title level={4} style={{ margin: 0, marginBottom: 16 }}>
        {projects.find((p) => p.id === activeProjectId)?.name} — 用例
      </Typography.Title>

      {casesQuery.isLoading && <LoadingBlock />}
      {casesQuery.isError && (
        <ErrorBlock message={casesQuery.error.message} />
      )}
      {!casesQuery.isLoading && !casesQuery.isError && filteredCases.length === 0 && (
        <Empty description="暂无用例" />
      )}
      {!casesQuery.isLoading && !casesQuery.isError && filteredCases.length > 0 && (
        <>
          {/* Batch action bar */}
          {selectedIds.size > 0 && (
            <div style={{
              marginBottom: 12,
              padding: "8px 12px",
              background: "#e6f4ff",
              borderRadius: 8,
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}>
              <Checkbox
                checked={selectedIds.size === filteredCases.length}
                indeterminate={selectedIds.size > 0 && selectedIds.size < filteredCases.length}
                onChange={toggleSelectAll}
              >
                已选 {selectedIds.size} 项
              </Checkbox>
              <Popconfirm
                title={`确定删除选中的 ${selectedIds.size} 个用例？`}
                onConfirm={() => batchDeleteMutation.mutate(Array.from(selectedIds))}
                okText="删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <Button size="small" danger icon={<DeleteOutlined />}>
                  批量删除
                </Button>
              </Popconfirm>
            </div>
          )}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              gap: 12,
            }}
          >
            {filteredCases.map((c: StoredCaseSummary) => (
              <div
                key={c.id}
                className="nb-card"
                style={{
                  padding: 16,
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  border: selectedIds.has(c.id) ? "2px solid #1677ff" : undefined,
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <Checkbox
                    checked={selectedIds.has(c.id)}
                    onChange={() => toggleSelect(c.id)}
                  />
                  <Typography.Text strong style={{ fontSize: 14, flex: 1 }}>
                    {c.name}
                  </Typography.Text>
                </div>
                <Typography.Text
                  type="secondary"
                  style={{ fontSize: 12 }}
                  ellipsis
                >
                  {c.description || "未填写描述"}
                </Typography.Text>
                <Typography.Text
                  type="secondary"
                  style={{ fontSize: 12 }}
                  ellipsis
                >
                  {c.base_url || "未配置"}
                </Typography.Text>
                <Tag>{c.steps.length} steps</Tag>
                <div style={{ marginTop: "auto" }}>
                  <Space>
                    <Button
                      type="primary"
                      size="small"
                      loading={
                        executionMutation.isPending &&
                        executionMutation.variables === c.id
                      }
                      disabled={
                        executionMutation.isPending &&
                        executionMutation.variables === c.id
                      }
                      onClick={() => executionMutation.mutate(c.id)}
                    >
                      执行
                    </Button>
                    <Button type="link" size="small">
                      <Link to={`/cases/${c.id}/edit`}>编辑</Link>
                    </Button>
                    <Popconfirm
                      title="确定删除此用例？"
                      onConfirm={() => deleteMutation.mutate(c.id)}
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Button type="link" size="small" danger>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  ) : (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%" }}>
      <Empty description="请选择一个项目" />
    </div>
  );

  /* ---- Right Cards ---- */
  const rightCards = [
    <div key="stats">
      <Typography.Text strong style={{ fontSize: 14, display: "block", marginBottom: 12 }}>
        统计
      </Typography.Text>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>用例总数</Typography.Text>
          <Typography.Text strong style={{ fontSize: 14 }}>{allCases.length}</Typography.Text>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>步骤总数</Typography.Text>
          <Typography.Text strong style={{ fontSize: 14 }}>{totalSteps}</Typography.Text>
        </div>
      </div>
    </div>,

    <div key="quick-actions">
      <Typography.Text strong style={{ fontSize: 14, display: "block", marginBottom: 12 }}>
        快速操作
      </Typography.Text>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <Link to="/" style={{ fontSize: 13 }}>返回 AI 规划</Link>
        <Link to="/cases/new" style={{ fontSize: 13 }}>手动补充/编辑</Link>
      </div>
    </div>,
  ];

  return (
    <>
      {contextHolder}
      <NotebookLMLayout
        leftPanel={leftPanel}
        centerPanel={centerPanel}
        rightCards={rightCards}
      />
    </>
  );
}
