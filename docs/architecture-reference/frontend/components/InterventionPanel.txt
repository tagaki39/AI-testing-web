import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Image,
  Input,
  List,
  Select,
  Space,
  Typography,
} from "antd";
import { useNavigate } from "react-router-dom";

import { createCorrection, executeCase } from "../services/api";
import type {
  CorrectionType,
  DOMElementSnapshot,
  InterventionRequest,
  StoredLocatorCorrection,
} from "../types/api";

type InterventionPanelProps = {
  caseId: number;
  executionId: number;
  triggeredBy: number;
  request: InterventionRequest;
};

function inferSuggestion(element: DOMElementSnapshot): { correctionType: CorrectionType; correctionValue: string } | null {
  if (element.data_testid) {
    return { correctionType: "test_id", correctionValue: element.data_testid };
  }
  if (element.xpath) {
    return { correctionType: "xpath", correctionValue: element.xpath };
  }
  if (element.css_selector) {
    return { correctionType: "css", correctionValue: element.css_selector };
  }
  return null;
}

export function InterventionPanel({
  caseId,
  executionId,
  triggeredBy,
  request,
}: InterventionPanelProps) {
  const navigate = useNavigate();
  const [correctionType, setCorrectionType] = useState<CorrectionType>("css");
  const [correctionValue, setCorrectionValue] = useState("");
  const [createdCorrection, setCreatedCorrection] = useState<StoredLocatorCorrection | null>(null);

  const createMutation = useMutation({
    mutationFn: () =>
      createCorrection({
        page_url: request.page_url,
        target_description: request.target_description,
        correction_type: correctionType,
        correction_value: correctionValue.trim(),
        source_execution_id: executionId,
        created_by: triggeredBy,
      }),
    onSuccess: (correction) => {
      setCreatedCorrection(correction);
    },
  });

  const rerunMutation = useMutation({
    mutationFn: () =>
      executeCase(caseId, {
        actor_user_id: triggeredBy,
      }),
    onSuccess: (execution) => {
      navigate(`/run/${execution.id}`);
    },
  });
  return (
    <Card size="small" title="人工干预">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Alert
          type="warning"
          showIcon
          message="当前步骤已进入人工干预"
          description="先确认失败截图和 DOM 快照，再提交一个可复用的 selector 修正。"
        />

        <Descriptions column={1} size="small" bordered>
          <Descriptions.Item label="页面 URL">{request.page_url}</Descriptions.Item>
          <Descriptions.Item label="定位目标">{request.target_description}</Descriptions.Item>
        </Descriptions>

        {request.screenshot_url ? (
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Typography.Text strong>失败截图</Typography.Text>
            <Image src={request.screenshot_url} alt={`intervention-${executionId}`} width={220} />
          </Space>
        ) : null}

        {request.ai_candidate ? (
          <Descriptions column={1} size="small" bordered>
            <Descriptions.Item label="AI 候选">
              center=[{request.ai_candidate.center.join(", ")}], bbox=[{request.ai_candidate.bbox.join(", ")}],
              confidence={request.ai_candidate.confidence}
            </Descriptions.Item>
          </Descriptions>
        ) : null}

        <div>
          <Typography.Text strong>DOM 快照候选</Typography.Text>
          <List
            size="small"
            dataSource={request.dom_snapshot}
            locale={{ emptyText: "当前没有可用的 DOM 快照候选。" }}
            renderItem={(item, index) => {
              const suggestion = inferSuggestion(item);
              return (
                <List.Item
                  actions={
                    suggestion
                      ? [
                          <Button
                            key="use"
                            size="small"
                            onClick={() => {
                              setCorrectionType(suggestion.correctionType);
                              setCorrectionValue(suggestion.correctionValue);
                            }}
                          >
                            使用该选择器
                          </Button>,
                        ]
                      : []
                  }
                >
                  <Space direction="vertical" size={2}>
                    <Typography.Text>
                      #{index + 1} {item.tag} / {item.role || "-"} / {item.text || item.aria_label || "-"}
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      css={item.css_selector || "-"} / xpath={item.xpath || "-"} / data-testid={item.data_testid || "-"}
                    </Typography.Text>
                  </Space>
                </List.Item>
              );
            }}
          />
        </div>

        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <Typography.Text strong>提交修正</Typography.Text>
          <Space wrap>
            <Select<CorrectionType>
              value={correctionType}
              style={{ width: 140 }}
              onChange={(value) => setCorrectionType(value)}
              options={[
                { label: "CSS", value: "css" },
                { label: "XPath", value: "xpath" },
                { label: "Test ID", value: "test_id" },
              ]}
            />
            <Input
              value={correctionValue}
              style={{ width: 360 }}
              placeholder="输入可复用的 selector"
              onChange={(event) => setCorrectionValue(event.target.value)}
            />
            <Button
              type="primary"
              loading={createMutation.isPending}
              disabled={!correctionValue.trim() || createMutation.isPending}
              onClick={() => createMutation.mutate()}
            >
              提交修正
            </Button>
          </Space>
          {createMutation.isError ? <Alert type="error" showIcon message={createMutation.error.message} /> : null}
          {createdCorrection ? (
            <Alert
              type="success"
              showIcon
              message="修正记录已保存到定位库"
              description="Demo 模式下修正已记录，可重新执行当前用例验证修正效果。"
              action={
                <Button type="primary" loading={rerunMutation.isPending} disabled={rerunMutation.isPending} onClick={() => rerunMutation.mutate()}>
                  重新执行当前用例
                </Button>
              }
            />
          ) : null}
          {rerunMutation.isError ? <Alert type="error" showIcon message={rerunMutation.error.message} /> : null}
        </Space>
      </Space>
    </Card>
  );
}
