import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Card, Descriptions, Empty, Modal, Skeleton, Table, Tag, Typography, type TableColumnsType } from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { type Project } from "../../api/adminCustomers";
import {
  AI_CLIENT_KEY_LIMIT,
  AI_CLIENT_KEY_REVOKED,
  issueAiClientKey,
  listAiClientKeys,
  projectAiAccessQueryKey,
  revokeAiClientKey,
  type AiClientKey,
  type AiClientKeyStatus,
  type IssuedAiClientKey,
} from "../../api/aiAccess";
import { ApiError } from "../../api/client";
import { DateTimeText } from "../../components/DateTimeText";
import { RequestReference } from "../../components/RequestReference";
import { CustomerErrorAlert } from "./CustomerForm";

const NO_VALUE = "—";

function StatusTag({ status }: { status: AiClientKeyStatus }) {
  const { t } = useTranslation();
  return status === "ACTIVE" ? <Tag color="green">{t("aiAccess.status.active")}</Tag> : <Tag>{t("aiAccess.status.revoked")}</Tag>;
}

function MutationError({ error, title }: { error: Error | null; title: string }) {
  const { t } = useTranslation();
  if (error instanceof ApiError && error.code === AI_CLIENT_KEY_LIMIT) {
    return <Alert type="warning" showIcon message={t("aiAccess.error.limit")} description={<RequestReference error={error} />} />;
  }
  if (error instanceof ApiError && error.code === AI_CLIENT_KEY_REVOKED) {
    return <Alert type="warning" showIcon message={t("aiAccess.error.revoked")} description={<RequestReference error={error} />} />;
  }
  return error === null ? null : <CustomerErrorAlert error={error} title={title} />;
}

export function AiAccessPanel({ customerId, project }: { customerId: string; project: Project }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [revoking, setRevoking] = useState<AiClientKey | null>(null);
  const inFlight = useRef(false);
  const projectId = project.id;
  const keys = useQuery({
    queryKey: projectAiAccessQueryKey(customerId, projectId),
    queryFn: ({ signal }) => listAiClientKeys(customerId, projectId, signal),
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: projectAiAccessQueryKey(customerId, projectId) });
  const issue = useMutation({
    mutationFn: () => issueAiClientKey(customerId, projectId),
    gcTime: 0,
    onSuccess: refresh,
    onSettled: () => { inFlight.current = false; },
  });
  const revoke = useMutation({
    mutationFn: (clientId: string) => revokeAiClientKey(customerId, projectId, clientId),
    onSuccess: refresh,
    onError: (error) => {
      if (error instanceof ApiError && error.code === AI_CLIENT_KEY_REVOKED) refresh();
    },
    onSettled: () => { inFlight.current = false; setRevoking(null); },
  });
  const clearOutcomes = () => { issue.reset(); revoke.reset(); };
  const startIssue = () => {
    if (inFlight.current) return;
    clearOutcomes();
    inFlight.current = true;
    issue.mutate();
  };
  const startRevoke = (key: AiClientKey) => { clearOutcomes(); setRevoking(key); };
  const confirmRevoke = () => {
    if (inFlight.current || revoking === null) return;
    inFlight.current = true;
    revoke.mutate(revoking.client_id);
  };
  const discardToken = () => issue.reset();
  const busy = issue.isPending || revoke.isPending;
  const columns: TableColumnsType<AiClientKey> = [
    { key: "client_id", title: t("aiAccess.field.clientId"), dataIndex: "client_id" },
    { key: "status", title: t("aiAccess.field.status"), render: (_: unknown, key) => <StatusTag status={key.status} /> },
    { key: "created_at", title: t("aiAccess.field.createdAt"), render: (_: unknown, key) => <DateTimeText value={key.created_at} /> },
    { key: "revoked_at", title: t("aiAccess.field.revokedAt"), render: (_: unknown, key) => key.revoked_at === null ? NO_VALUE : <DateTimeText value={key.revoked_at} /> },
    { key: "actions", title: t("aiAccess.field.actions"), render: (_: unknown, key) => <Button size="small" danger disabled={busy || key.status === "REVOKED"} onClick={() => startRevoke(key)}>{t("aiAccess.revoke.open")}</Button> },
  ];
  let body: ReactNode;
  if (keys.isPending) body = <><Skeleton active title={false} paragraph={{ rows: 2 }} /><Typography.Text type="secondary">{t("aiAccess.loading")}</Typography.Text></>;
  else if (keys.isError) body = <CustomerErrorAlert error={keys.error} title={t("aiAccess.loadFailed")} onRetry={() => void keys.refetch()} />;
  else if (keys.data.length === 0) body = <Empty description={t("aiAccess.empty")} />;
  else body = <Table<AiClientKey> rowKey="client_id" columns={columns} dataSource={keys.data} pagination={false} />;
  const issued: IssuedAiClientKey | undefined = issue.data;
  return <Card role="region" aria-label={t("aiAccess.title")} title={t("aiAccess.title")} extra={<Button disabled={busy} loading={issue.isPending} onClick={startIssue}>{t("aiAccess.issue")}</Button>}>
    <Typography.Paragraph type="secondary">{t("aiAccess.opsNotice")}</Typography.Paragraph>
    <MutationError error={issue.error ?? revoke.error} title={issue.error === null ? t("aiAccess.revoke.failed") : t("aiAccess.issue.failed")} />
    {body}
    {issued === undefined ? null : <TokenDialog issued={issued} onClose={discardToken} />}
    {revoking === null ? null : <Modal open title={t("aiAccess.revoke.confirmTitle")} okText={t("aiAccess.revoke.confirm")} cancelText={t("aiAccess.cancel")} onOk={confirmRevoke} onCancel={() => !revoke.isPending && setRevoking(null)} okButtonProps={{ danger: true, loading: revoke.isPending, disabled: revoke.isPending }} cancelButtonProps={{ disabled: revoke.isPending }} maskClosable={false} closable={!revoke.isPending} keyboard={!revoke.isPending}><Typography.Paragraph>{t("aiAccess.revoke.body", { clientId: revoking.client_id })}</Typography.Paragraph></Modal>}
  </Card>;
}

function TokenDialog({ issued, onClose }: { issued: IssuedAiClientKey; onClose: () => void }) {
  const { t } = useTranslation();
  return <Modal open title={t("aiAccess.issued.title")} footer={<Button type="primary" onClick={onClose}>{t("aiAccess.issued.close")}</Button>} onCancel={onClose} maskClosable={false}>
    <Alert type="warning" showIcon message={t("aiAccess.issued.warning")} style={{ marginBottom: 16 }} />
    <Descriptions column={1} items={[{ key: "client", label: t("aiAccess.field.clientId"), children: <Typography.Text code copyable>{issued.client_id}</Typography.Text> }, { key: "token", label: t("aiAccess.issued.token"), children: <Typography.Text code copyable>{issued.token}</Typography.Text> }]} />
  </Modal>;
}
