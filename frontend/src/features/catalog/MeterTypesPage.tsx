/**
 * 管理端计量类型（`GET` / `POST` / `PATCH /api/v1/admin/usage-meter-types`，docs/api.md「AI 目录」）。
 *
 * 表格里每个类型带它的计价分量与取数字段（分量从上报事件的哪个字段取数量）。迁移预置 9 个类型、
 * 12 个分量；`LLM_TOKEN` 是唯一的多字段形态，四个分量各取一个 token 字段。
 *
 * 新建只收 `code`、`display_name`、`unit`、`quantity_kind`、`component_code`：形态固定为
 * `QUANTITY`，同一事务建出它唯一的分量（取 `quantity`）。多字段形态要改上报载荷与代码，这里做不了，
 * 表单上写明。
 *
 * 目录很短（种子 9 个），每页默认 100 条，一页看全；三种状态都画出来（spec §132 DoD 第 8 条）。
 */

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Radio,
  Space,
  Table,
  Typography,
  type TableColumnsType,
} from "antd";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import {
  METER_CODE_PATTERN,
  METER_TYPE_LISTS_QUERY_KEY,
  QUANTITY_KINDS,
  UNIT_PATTERN,
  createMeterType,
  listMeterTypes,
  meterTypeListQueryKey,
  updateMeterType,
  type CreateMeterTypeBody,
  type MeterType,
  type QuantityKind,
} from "../../api/adminCatalog";
import { MAX_PAGE_SIZE } from "../../api/adminCustomers";
import { ROUTES } from "../../routes/paths";
import {
  CatalogErrorAlert,
  CatalogStatusTag,
  CodeText,
  ConfirmWriteModal,
  EntryActions,
  FormStepModal,
  LoadingBlock,
  NoticeAlert,
  PAGE_SIZE_OPTIONS,
  RenameModal,
  StatusChangeModal,
  StatusFilterControl,
  SummaryList,
  codeRules,
  useDisplayNameRules,
  useStatusDoneMessage,
  useStatusFilteredPaging,
} from "./ProvidersPage";

export function MeterTypesPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { status, filter, setStatus, paging } = useStatusFilteredPaging(MAX_PAGE_SIZE);
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<MeterType | null>(null);
  const [changingStatus, setChangingStatus] = useState<MeterType | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const statusDone = useStatusDoneMessage();

  const meterTypes = useQuery({
    queryKey: meterTypeListQueryKey(filter, paging.page, paging.pageSize),
    queryFn: ({ signal }) => listMeterTypes(filter, paging.page, paging.pageSize, signal),
    placeholderData: keepPreviousData,
  });

  const finish = (message: string): void => {
    void queryClient.invalidateQueries({ queryKey: METER_TYPE_LISTS_QUERY_KEY });
    setCreating(false);
    setRenaming(null);
    setChangingStatus(null);
    setNotice(message);
  };

  const open = (action: () => void): void => {
    setNotice(null);
    action();
  };

  const columns: TableColumnsType<MeterType> = [
    {
      key: "code",
      title: t("catalog.field.code"),
      render: (_: unknown, meterType) => <CodeText code={meterType.code} />,
    },
    { key: "display_name", title: t("catalog.field.displayName"), dataIndex: "display_name" },
    {
      key: "payload_shape",
      title: t("catalog.meterTypes.field.payloadShape"),
      render: (_: unknown, meterType) => <CodeText code={meterType.payload_shape} />,
    },
    {
      key: "unit",
      title: t("catalog.meterTypes.field.unit"),
      render: (_: unknown, meterType) => <CodeText code={meterType.unit} />,
    },
    {
      key: "quantity_kind",
      title: t("catalog.meterTypes.field.quantityKind"),
      render: (_: unknown, meterType) => <CodeText code={meterType.quantity_kind} />,
    },
    {
      key: "components",
      title: t("catalog.meterTypes.field.components"),
      // 一个分量一个 <li>，顺序由后端决定（`component_code` 升序）。验收脚本按 <li> 数分量。
      render: (_: unknown, meterType) => (
        <ul style={{ margin: 0, paddingLeft: 16 }}>
          {meterType.components.map((component) => (
            <li key={component.component_code}>
              <CodeText code={component.component_code} />
              {" ← "}
              <Typography.Text type="secondary">{component.quantity_field}</Typography.Text>
            </li>
          ))}
        </ul>
      ),
    },
    {
      key: "status",
      title: t("catalog.field.status"),
      render: (_: unknown, meterType) => <CatalogStatusTag status={meterType.status} />,
    },
    {
      key: "actions",
      title: t("catalog.field.actions"),
      render: (_: unknown, meterType) => (
        <EntryActions
          status={meterType.status}
          onRename={() => open(() => setRenaming(meterType))}
          onChangeStatus={() => open(() => setChangingStatus(meterType))}
        />
      ),
    },
  ];

  let body: ReactNode;
  if (meterTypes.isPending) {
    body = <LoadingBlock text={t("catalog.meterTypes.loading")} />;
  } else if (meterTypes.isError) {
    body = (
      <CatalogErrorAlert
        error={meterTypes.error}
        title={t("catalog.meterTypes.loadFailed")}
        onRetry={() => void meterTypes.refetch()}
      />
    );
  } else if (meterTypes.data.total === 0) {
    body = (
      <Empty
        description={
          status === undefined
            ? t("catalog.meterTypes.empty")
            : t("catalog.meterTypes.emptyFiltered")
        }
      />
    );
  } else {
    body = (
      <Table<MeterType>
        // 计量类型 `code` 全局唯一。
        rowKey="code"
        columns={columns}
        dataSource={meterTypes.data.items}
        loading={meterTypes.isFetching}
        locale={{ emptyText: t("catalog.meterTypes.emptyPage") }}
        pagination={{
          current: paging.page,
          pageSize: paging.pageSize,
          total: meterTypes.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("catalog.meterTypes.total", { total: count }),
          onChange: paging.goTo,
        }}
      />
    );
  }

  return (
    <Card
      title={t("catalog.meterTypes.title")}
      extra={
        <Space>
          <Link to={ROUTES.catalogProviders}>{t("catalog.meterTypes.providersLink")}</Link>
          <Button type="primary" onClick={() => open(() => setCreating(true))}>
            {t("catalog.meterTypes.create")}
          </Button>
        </Space>
      }
    >
      <Typography.Paragraph type="secondary">{t("catalog.meterTypes.intro")}</Typography.Paragraph>
      <NoticeAlert message={notice} />
      <div style={{ marginBottom: 16 }}>
        <StatusFilterControl value={status} onChange={setStatus} />
      </div>
      {body}
      {creating ? (
        <CreateMeterTypeModal
          onDone={() => finish(t("catalog.meterTypes.created"))}
          onCancel={() => setCreating(false)}
        />
      ) : null}
      {renaming === null ? null : (
        <RenameModal
          code={renaming.code}
          displayName={renaming.display_name}
          save={(patch) => updateMeterType(renaming.id, patch)}
          onDone={() => finish(t("catalog.rename.done"))}
          onCancel={() => setRenaming(null)}
        />
      )}
      {changingStatus === null ? null : (
        <StatusChangeModal
          code={changingStatus.code}
          status={changingStatus.status}
          save={(patch) => updateMeterType(changingStatus.id, patch)}
          onDone={(next) => finish(statusDone(next))}
          onCancel={() => setChangingStatus(null)}
        />
      )}
    </Card>
  );
}

interface MeterTypeFormValues {
  code: string;
  display_name: string;
  unit: string;
  quantity_kind: QuantityKind;
  component_code: string;
}

/** 新建 `QUANTITY` 类型。请求体恰好五个字段，代码与单位原样。 */
export function toCreateMeterTypeBody(values: MeterTypeFormValues): CreateMeterTypeBody {
  return {
    code: values.code,
    display_name: values.display_name.trim(),
    unit: values.unit,
    quantity_kind: values.quantity_kind,
    component_code: values.component_code,
  };
}

function CreateMeterTypeModal({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [form] = Form.useForm<MeterTypeFormValues>();
  const [reviewing, setReviewing] = useState<CreateMeterTypeBody | null>(null);
  const nameRules = useDisplayNameRules();
  const codeInvalid = t("catalog.meterTypes.codeInvalid");

  return (
    <>
      <FormStepModal title={t("catalog.meterTypes.create")} form={form} onCancel={onCancel}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("catalog.meterTypes.quantityOnly")}
          description={t("catalog.meterTypes.multiFieldNeedsCode")}
        />
        <Form<MeterTypeFormValues>
          form={form}
          layout="vertical"
          initialValues={{
            code: "",
            display_name: "",
            unit: "",
            quantity_kind: "INTEGER",
            component_code: "",
          }}
          onFinish={(values) => setReviewing(toCreateMeterTypeBody(values))}
        >
          <Form.Item
            name="code"
            label={t("catalog.field.code")}
            extra={t("catalog.meterTypes.codeHint")}
            rules={codeRules(METER_CODE_PATTERN, t("catalog.form.codeRequired"), codeInvalid)}
          >
            <Input autoComplete="off" spellCheck={false} />
          </Form.Item>
          <Form.Item name="display_name" label={t("catalog.field.displayName")} rules={nameRules}>
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="unit"
            label={t("catalog.meterTypes.field.unit")}
            extra={t("catalog.meterTypes.unitHint")}
            rules={codeRules(
              UNIT_PATTERN,
              t("catalog.form.codeRequired"),
              t("catalog.meterTypes.unitInvalid"),
            )}
          >
            <Input autoComplete="off" spellCheck={false} />
          </Form.Item>
          <Form.Item
            name="quantity_kind"
            label={t("catalog.meterTypes.field.quantityKind")}
            extra={t("catalog.meterTypes.quantityKindHint")}
          >
            <Radio.Group
              optionType="button"
              options={QUANTITY_KINDS.map((kind) => ({ value: kind, label: kind }))}
            />
          </Form.Item>
          <Form.Item
            name="component_code"
            label={t("catalog.meterTypes.field.componentCode")}
            extra={t("catalog.meterTypes.componentCodeHint")}
            rules={codeRules(METER_CODE_PATTERN, t("catalog.form.codeRequired"), codeInvalid)}
          >
            <Input autoComplete="off" spellCheck={false} />
          </Form.Item>
        </Form>
      </FormStepModal>
      {reviewing === null ? null : (
        <ConfirmWriteModal
          title={t("catalog.meterTypes.confirmCreate")}
          confirmLabel={t("catalog.confirmCreate")}
          backLabel={t("catalog.backToEdit")}
          failedTitle={t("catalog.meterTypes.createFailed")}
          run={() => createMeterType(reviewing)}
          onDone={onDone}
          onBack={() => setReviewing(null)}
        >
          <SummaryList
            items={[
              { key: "code", label: t("catalog.field.code"), children: <CodeText code={reviewing.code} /> },
              { key: "display_name", label: t("catalog.field.displayName"), children: reviewing.display_name },
              { key: "payload_shape", label: t("catalog.meterTypes.field.payloadShape"), children: <CodeText code="QUANTITY" /> },
              { key: "unit", label: t("catalog.meterTypes.field.unit"), children: <CodeText code={reviewing.unit} /> },
              { key: "quantity_kind", label: t("catalog.meterTypes.field.quantityKind"), children: <CodeText code={reviewing.quantity_kind} /> },
              { key: "component_code", label: t("catalog.meterTypes.field.componentCode"), children: <CodeText code={reviewing.component_code} /> },
            ]}
          />
          <Alert type="warning" showIcon message={t("catalog.meterTypes.permanent")} />
        </ConfirmWriteModal>
      )}
    </>
  );
}
