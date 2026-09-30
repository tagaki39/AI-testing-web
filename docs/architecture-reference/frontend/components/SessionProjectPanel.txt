import { useState } from "react";
import { Button, Select, Tag, Input, Modal, Space, message } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getProjects,
  linkProjectToSession,
  unlinkProjectFromSession,
  createProjectInSession,
  listSessionProjects,
} from "../services/api";

interface SessionProjectPanelProps {
  sessionId: number;
  onProjectsChange?: () => void;
}

export function SessionProjectPanel({ sessionId, onProjectsChange }: SessionProjectPanelProps) {
  const queryClient = useQueryClient();
  const [linking, setLinking] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [creating, setCreating] = useState(false);

  const projectsQuery = useQuery({
    queryKey: ["session-projects", sessionId],
    queryFn: () => listSessionProjects(sessionId),
  });

  const allProjectsQuery = useQuery({
    queryKey: ["projects"],
    queryFn: getProjects,
  });

  const linkedIds = new Set((projectsQuery.data ?? []).map((p) => p.id));
  const unlinkedProjects = (allProjectsQuery.data ?? []).filter((p) => !linkedIds.has(p.id));

  async function handleLink(projectId: number) {
    setLinking(true);
    try {
      await linkProjectToSession(sessionId, { project_id: projectId });
      queryClient.invalidateQueries({ queryKey: ["session-projects", sessionId] });
      onProjectsChange?.();
      void message.success("项目已关联");
    } catch (err) {
      void message.error(err instanceof Error ? err.message : "关联失败");
    } finally {
      setLinking(false);
    }
  }

  async function handleUnlink(projectId: number) {
    try {
      await unlinkProjectFromSession(sessionId, projectId);
      queryClient.invalidateQueries({ queryKey: ["session-projects", sessionId] });
      onProjectsChange?.();
      void message.success("已取消关联");
    } catch (err) {
      void message.error(err instanceof Error ? err.message : "取消关联失败");
    }
  }

  async function handleCreate() {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      await createProjectInSession(sessionId, { name: newName.trim(), description: newDesc.trim() || null });
      queryClient.invalidateQueries({ queryKey: ["session-projects", sessionId] });
      queryClient.invalidateQueries({ queryKey: ["projects"] });
      setShowCreate(false);
      setNewName("");
      setNewDesc("");
      onProjectsChange?.();
      void message.success("项目已创建并关联");
    } catch (err) {
      void message.error(err instanceof Error ? err.message : "创建失败");
    } finally {
      setCreating(false);
    }
  }

  const projects = projectsQuery.data ?? [];

  return (
    <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      {projects.map((p) => (
        <Tag
          key={p.id}
          closable
          onClose={(e) => { e.preventDefault(); handleUnlink(p.id); }}
          color="blue"
        >
          {p.name}
        </Tag>
      ))}
      {projects.length === 0 && <Tag color="default">未关联项目</Tag>}

      {unlinkedProjects.length > 0 && (
        <Select
          size="small"
          placeholder="关联已有项目"
          style={{ width: 160 }}
          loading={linking}
          value={undefined}
          onChange={(val: number) => handleLink(val)}
          options={unlinkedProjects.map((p) => ({ value: p.id, label: p.name }))}
        />
      )}

      <Button size="small" icon={<PlusOutlined />} onClick={() => setShowCreate(true)}>
        新建项目
      </Button>

      <Modal
        open={showCreate}
        title="创建新项目"
        onCancel={() => setShowCreate(false)}
        onOk={handleCreate}
        confirmLoading={creating}
        okText="创建并关联"
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input placeholder="项目名称" value={newName} onChange={(e) => setNewName(e.target.value)} />
          <Input.TextArea placeholder="项目描述（可选）" value={newDesc} onChange={(e) => setNewDesc(e.target.value)} rows={3} />
        </Space>
      </Modal>
    </div>
  );
}
