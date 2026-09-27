/**
 * 客户详情里的项目区块（`GET` / `POST /api/v1/admin/customers/{customer_id}/projects`）。
 *
 * 列表分页，顺序由后端决定（**最早在前**），前端不重排。三种状态都画出来（spec §132
 * DoD 第 8 条）：加载中、失败（带 request_id）、空列表。页码只放在组件状态里 ——
 * 地址栏已经归客户详情用，项目区块只是详情页的一部分。
 *
 * 建项目**不幂等**：提交进行中禁用按钮，并用一个 ref 挡住第二次提交。成功后让这个客户的
 * 所有项目列表页过期、重新读，不在前端拼一行进去。
 *
 * `renderProjectDetails` 是给 AIH-TASK-018（集成凭据）留的挂载点：传了它，每一行就能展开，
 * 展开的内容由调用方决定。本组件自己不碰凭据，也不发凭据接口的请求。
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Skeleton,
  Space,
  Table,
  Typography,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  createProject,
  customerProjectsQueryKey,
  listProjects,
  projectListQueryKey,
  type CreateProjectBody,
  type Project,
} from "../../api/adminCustomers";
import { DateTimeText } from "../../components/DateTimeText";
import { CustomerErrorAlert } from "./CustomerForm";

/** 与后端 `app/schemas/customers.py` 的上限一致。 */
const NAME_MAX = 255;
const DESCRIPTION_MAX = 1000;

const PAGE_SIZE_OPTIONS = [10, 20, 50, MAX_PAGE_SIZE];

/** 空的描述显示成一道横线，与详情页的空字段一致。 */
const NO_VALUE = "—";

export interface ProjectFormValues {
  name: string;
  description?: string;
}

/** 建项目的请求体：名称去首尾空白；描述去首尾空白后为空就不带（后端本来也存成 `null`）。 */
export function toCreateProjectBody(values: ProjectFormValues): CreateProjectBody {
  const body: CreateProjectBody = { name: values.name.trim() };
  const description = (values.description ?? "").trim();
  if (description !== "") {
    body.description = description;
  }
  return body;
}

/** 按码点数，与 Python 的 `len()` 一致（`.length` 数的是 UTF-16 单元，emoji 算 2）。 */
function trimmedLength(value: unknown): number {
  return typeof value === "string" ? [...value.trim()].length : 0;
}

function maxTrimmed(limit: number, message: string): FormRule {
  return {
    validator: (_rule, value: unknown) =>
      trimmedLength(value) > limit ? Promise.reject(new Error(message)) : Promise.resolve(),
  };
}

export function ProjectsPanel({
  customerId,
  renderProjectDetails,
}: {
  customerId: string;
  /** AIH-TASK-018 的挂载点：每个项目展开后显示什么。不传就没有展开。 */
  renderProjectDetails?: ((project: Project) => ReactNode) | undefined;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState(false);
  const [form] = Form.useForm<ProjectFormValues>();
  // 与建客户同一个理由：`isPending` 要等下一次渲染，而 antd 的校验是异步的，
  // 双击时两次 onFinish 可能都赶在按钮禁用之前。
  const inFlight = useRef(false);

  const projects = useQuery({
    queryKey: projectListQueryKey(customerId, page, pageSize),
    queryFn: ({ signal }) => listProjects(customerId, page, pageSize, signal),
    // 翻页时先留着上一页，不让整张表闪成骨架屏。
    placeholderData: keepPreviousData,
  });

  const create = useMutation({
    mutationFn: (body: CreateProjectBody) => createProject(customerId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: customerProjectsQueryKey(customerId) });
      form.resetFields();
      setCreating(false);
      setCreated(true);
    },
    onSettled: () => {
      inFlight.current = false;
    },
  });

  const startCreating = (): void => {
    create.reset();
    setCreated(false);
    setCreating(true);
  };

  const submit = (values: ProjectFormValues): void => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    create.mutate(toCreateProjectBody(values));
  };

  const goTo = (nextPage: number, nextPageSize: number): void => {
    // 换了每页条数，原来的页码就没有意义了，回到第一页。
    setPage(nextPageSize === pageSize ? nextPage : 1);
    setPageSize(nextPageSize);
  };

  const columns: TableColumnsType<Project> = [
    { key: "name", title: t("customers.projects.field.name"), dataIndex: "name" },
    {
      key: "description",
      title: t("customers.projects.field.description"),
      render: (_: unknown, project) => project.description ?? NO_VALUE,
    },
    {
      key: "created_at",
      title: t("customers.field.createdAt"),
      render: (_: unknown, project) => <DateTimeText value={project.created_at} />,
    },
  ];

  const createButton = creating ? null : (
    <Button onClick={startCreating}>{t("customers.projects.create")}</Button>
  );

  const submitting = create.isPending;
  const createForm = creating ? (
    <Card type="inner" title={t("customers.projects.create")} style={{ marginBottom: 16 }}>
      <Form<ProjectFormValues>
        form={form}
        layout="vertical"
        initialValues={{ name: "", description: "" }}
        onFinish={submit}
        disabled={submitting}
      >
        <CustomerErrorAlert error={create.error} title={t("customers.projects.createFailed")} />
        <Form.Item
          name="name"
          label={t("customers.projects.field.name")}
          rules={[
            { required: true, whitespace: true, message: t("customers.projects.nameRequired") },
            maxTrimmed(NAME_MAX, t("customers.projects.nameTooLong")),
          ]}
        >
          <Input />
        </Form.Item>
        <Form.Item
          name="description"
          label={t("customers.projects.field.description")}
          rules={[maxTrimmed(DESCRIPTION_MAX, t("customers.projects.descriptionTooLong"))]}
        >
          <Input.TextArea rows={3} />
        </Form.Item>
        <Space>
          {/* ⚠️ 建项目**不幂等**：提交进行中必须禁用，否则双击就是两个项目。 */}
          <Button type="primary" htmlType="submit" loading={submitting} disabled={submitting}>
            {t("customers.projects.submit")}
          </Button>
          <Button onClick={() => setCreating(false)}>{t("customers.form.cancel")}</Button>
        </Space>
      </Form>
    </Card>
  ) : null;

  const createdNotice = created ? (
    <Alert
      type="success"
      showIcon
      style={{ marginBottom: 16 }}
      message={t("customers.projects.created")}
    />
  ) : null;

  // `exactOptionalPropertyTypes` 下不能传 `expandable={undefined}`：没有挂载内容就不带这个属性。
  const expandable =
    renderProjectDetails === undefined
      ? {}
      : { expandable: { expandedRowRender: (project: Project) => renderProjectDetails(project) } };

  let body: ReactNode;
  if (projects.isPending) {
    body = (
      <>
        <Skeleton active title={false} paragraph={{ rows: 3 }} />
        <Typography.Text type="secondary">{t("customers.projects.loading")}</Typography.Text>
      </>
    );
  } else if (projects.isError) {
    body = (
      <CustomerErrorAlert
        error={projects.error}
        title={t("customers.projects.loadFailed")}
        onRetry={() => void projects.refetch()}
      />
    );
  } else if (projects.data.total === 0) {
    body = <Empty description={t("customers.projects.empty")} />;
  } else {
    body = (
      <Table<Project>
        rowKey="id"
        columns={columns}
        dataSource={projects.data.items}
        loading={projects.isFetching}
        // 页码超过末页时后端回空 items、total 照报：说清楚是「这一页没有」。
        locale={{ emptyText: t("customers.projects.emptyPage") }}
        pagination={{
          current: page,
          pageSize,
          total: projects.data.total,
          showSizeChanger: true,
          pageSizeOptions: PAGE_SIZE_OPTIONS,
          showTotal: (count) => t("customers.projects.total", { total: count }),
          onChange: goTo,
        }}
        {...expandable}
      />
    );
  }

  return (
    <Card title={t("customers.projects.title")} extra={createButton}>
      {createdNotice}
      {createForm}
      {body}
    </Card>
  );
}
