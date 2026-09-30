import { Alert, Empty, Skeleton } from "antd";

export function LoadingBlock() {
  return <Skeleton active paragraph={{ rows: 6 }} />;
}

export function ErrorBlock({ message }: { message: string }) {
  return <Alert type="error" showIcon message="加载失败" description={message} />;
}

export function EmptyBlock({ description }: { description: string }) {
  return <Empty description={description} />;
}
