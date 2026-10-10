/**
 * 价格分量的录入：按计量类型成组（docs/api.md「管理端供应商价格」的完整性规则）。
 *
 * 选中一个计量类型，就列出它在目录里的**全部**分量，每个分量一行「每多少个单位」与「单价」；
 * 只能整组移除，不能单删一个分量。所以表单里出现的计量类型永远是完整的，与后端发布时的规则一致：
 * 「出现的计量类型，它的全部分量都必须出现」（例如选了 `LLM_TOKEN` 就是输入、输出、缓存写入、
 * 缓存读取四行）。编辑一个不完整的旧草稿时，缺的分量照样列出来、空着，填满才许提交 —— 见
 * {@link ratesProblem}，表单的校验就是它。
 *
 * 只有启用中的计量类型可以新加进来（停用的建草稿时是 409 `CATALOG_ITEM_RETIRED`）；旧草稿里已经有
 * 的停用类型照样显示，由后端说它不行。
 *
 * ⚠️ 数量与单价一律是用户输入的**字符串**，原样提交（INV-10），不 trim 以外的任何加工；
 * 校验只用正则与 {@link isZeroDecimal}，不经过 number。分量上的 `metadata`（备注）这里不编辑，
 * 但编辑旧草稿时原样带回去：`components` 是整体替换，丢了就真丢了。
 */

import { useQuery } from "@tanstack/react-query";
import {
  Button,
  Card,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Typography,
  type FormRule,
  type TableColumnsType,
} from "antd";
import { useId } from "react";
import { useTranslation } from "react-i18next";

import {
  METER_TYPE_LISTS_QUERY_KEY,
  listMeterTypes,
  type MeterType,
} from "../../api/adminCatalog";
import { MAX_PAGE_SIZE } from "../../api/adminCustomers";
import {
  MAX_COMPONENTS,
  PRICE_DECIMAL_PATTERN,
  type ComponentRateBody,
  type PriceComponent,
} from "../../api/adminProviderPrices";
import { isZeroDecimal } from "../../components/DecimalText";
import { CatalogStatusTag, CodeText } from "../catalog/ProvidersPage";

/** 表单里的一行：一个分量的两个数，以及它属于哪个计量类型（成组显示与完整性检查用）。 */
export interface RateRow {
  component_code: string;
  meter_type_code: string;
  unit: string;
  unit_quantity: string;
  rate_amount: string;
  /** 旧草稿带来的备注，原样带回；新加的行是 `null`。 */
  metadata: Record<string, unknown> | null;
}

/** 目录里的全部计量类型（含已停用的），逐页取完。 */
export async function listAllMeterTypes(signal?: AbortSignal): Promise<MeterType[]> {
  const meterTypes: MeterType[] = [];
  for (let page = 1; ; page += 1) {
    const result = await listMeterTypes({}, page, MAX_PAGE_SIZE, signal);
    meterTypes.push(...result.items);
    if (result.items.length === 0 || meterTypes.length >= result.total) {
      return meterTypes;
    }
  }
}

/** 挂在计量类型列表的前缀下：目录页新建、停用计量类型之后它跟着过期。 */
export const ALL_METER_TYPES_QUERY_KEY = [...METER_TYPE_LISTS_QUERY_KEY, "all"] as const;

export function useAllMeterTypes() {
  return useQuery({
    queryKey: ALL_METER_TYPES_QUERY_KEY,
    queryFn: ({ signal }) => listAllMeterTypes(signal),
  });
}

/** 一个计量类型的全部分量，每行空着等人填。 */
export function rowsForMeterType(meterType: MeterType): RateRow[] {
  return meterType.components.map((component) => ({
    component_code: component.component_code,
    meter_type_code: meterType.code,
    unit: meterType.unit,
    unit_quantity: "",
    rate_amount: "",
    metadata: null,
  }));
}

/**
 * 旧草稿的分量 → 表单行。按计量类型成组：组的顺序是它在草稿里第一次出现的顺序；组内按目录列出
 * 全部分量，草稿里有的填上原值，缺的空着。目录里找不到的计量类型（不该发生）原样列出草稿里的行。
 */
export function rowsFromVersion(
  components: readonly PriceComponent[],
  meterTypes: readonly MeterType[],
): RateRow[] {
  const groups: string[] = [];
  for (const component of components) {
    if (!groups.includes(component.meter_type_code)) {
      groups.push(component.meter_type_code);
    }
  }
  return groups.flatMap((code) => {
    const existing = components.filter((component) => component.meter_type_code === code);
    const fromDraft = (component: PriceComponent): RateRow => ({
      component_code: component.component_code,
      meter_type_code: component.meter_type_code,
      unit: component.unit,
      unit_quantity: component.unit_quantity,
      rate_amount: component.rate_amount,
      metadata: component.metadata,
    });
    const meterType = meterTypes.find((candidate) => candidate.code === code);
    if (meterType === undefined) {
      return existing.map(fromDraft);
    }
    return rowsForMeterType(meterType).map((row) => {
      const found = existing.find((component) => component.component_code === row.component_code);
      return found === undefined ? row : fromDraft(found);
    });
  });
}

export type RatesProblem =
  | { kind: "empty" }
  | { kind: "tooMany" }
  | { kind: "missing"; codes: string[] }
  | { kind: "invalid" };

function validAmount(value: string): boolean {
  return PRICE_DECIMAL_PATTERN.test(value) && !isZeroDecimal(value);
}

/**
 * 能不能提交，不能的话第一个问题是什么。顺序：没有分量 → 太多 → 某个已选计量类型缺分量 → 某个数不合法。
 * 「缺分量」与后端发布时的完整性规则是同一条（这里在建草稿时就要求，比后端严）。
 */
export function ratesProblem(rows: readonly RateRow[], meterTypes: readonly MeterType[]): RatesProblem | null {
  if (rows.length === 0) {
    return { kind: "empty" };
  }
  if (rows.length > MAX_COMPONENTS) {
    return { kind: "tooMany" };
  }
  const present = new Set(rows.map((row) => row.component_code));
  const missing: string[] = [];
  for (const code of new Set(rows.map((row) => row.meter_type_code))) {
    const meterType = meterTypes.find((candidate) => candidate.code === code);
    for (const component of meterType?.components ?? []) {
      if (!present.has(component.component_code)) {
        missing.push(component.component_code);
      }
    }
  }
  if (missing.length > 0) {
    return { kind: "missing", codes: missing };
  }
  if (rows.some((row) => !validAmount(row.unit_quantity) || !validAmount(row.rate_amount))) {
    return { kind: "invalid" };
  }
  return null;
}

/** 放在 `Form.Item name="components"` 上的校验：{@link ratesProblem} 的每一种问题一句话。 */
export function useComponentRatesRules(meterTypes: readonly MeterType[]): FormRule[] {
  const { t } = useTranslation();
  return [
    {
      validator: (_rule, value: unknown) => {
        const problem = ratesProblem(Array.isArray(value) ? (value as RateRow[]) : [], meterTypes);
        if (problem === null) {
          return Promise.resolve();
        }
        let message = t("pricing.components.invalid");
        if (problem.kind === "empty") {
          message = t("pricing.components.required");
        } else if (problem.kind === "tooMany") {
          message = t("pricing.components.tooMany");
        } else if (problem.kind === "missing") {
          message = t("pricing.components.missing", { codes: problem.codes.join(", ") });
        }
        return Promise.reject(new Error(message));
      },
    },
  ];
}

/** 表单行 → 请求体。没有备注的行不带 `metadata`。两个数原样（已由校验保证合法）。 */
export function toComponentBodies(rows: readonly RateRow[]): ComponentRateBody[] {
  return rows.map((row) => ({
    component_code: row.component_code,
    unit_quantity: row.unit_quantity,
    rate_amount: row.rate_amount,
    ...(row.metadata === null ? {} : { metadata: row.metadata }),
  }));
}

/** 按计量类型分组，保持出现顺序。 */
function groupRows(rows: readonly RateRow[]): { code: string; unit: string; rows: RateRow[] }[] {
  const groups: { code: string; unit: string; rows: RateRow[] }[] = [];
  for (const row of rows) {
    const group = groups.find((candidate) => candidate.code === row.meter_type_code);
    if (group === undefined) {
      groups.push({ code: row.meter_type_code, unit: row.unit, rows: [row] });
    } else {
      group.rows.push(row);
    }
  }
  return groups;
}

/**
 * 受控组件：放在 antd 的 `Form.Item` 里，由它注入 `value` / `onChange`。
 *
 * `meterTypes` 是目录里的全部计量类型（{@link useAllMeterTypes}），由调用方取好传进来：表单的校验
 * 也要用同一份。
 *
 * `rateTitle` 换掉「单价」一列的标题：定价规则（AIH-TASK-037）在那里写明「MYR，含税」。不给就是价格页的「Rate」。
 */
export function ComponentRatesInput({
  value,
  onChange,
  meterTypes,
  rateTitle,
}: {
  value?: RateRow[];
  onChange?: (next: RateRow[]) => void;
  meterTypes: readonly MeterType[];
  rateTitle?: string | undefined;
}) {
  const { t } = useTranslation();
  const addId = useId();
  const rows = value ?? [];
  const chosen = new Set(rows.map((row) => row.meter_type_code));

  const change = (next: RateRow[]): void => {
    onChange?.(next);
  };

  const add = (code: string): void => {
    const meterType = meterTypes.find((candidate) => candidate.code === code);
    if (meterType !== undefined && !chosen.has(code)) {
      change([...rows, ...rowsForMeterType(meterType)]);
    }
  };

  const remove = (code: string): void => {
    change(rows.filter((row) => row.meter_type_code !== code));
  };

  const edit = (componentCode: string, field: "unit_quantity" | "rate_amount", next: string): void => {
    change(
      rows.map((row) => {
        if (row.component_code !== componentCode) {
          return row;
        }
        return field === "unit_quantity" ? { ...row, unit_quantity: next } : { ...row, rate_amount: next };
      }),
    );
  };

  const addable = meterTypes
    .filter((meterType) => meterType.status === "ACTIVE" && !chosen.has(meterType.code))
    .map((meterType) => ({ value: meterType.code, label: meterType.code }));

  const amountStatus = (amount: string): "error" | "" =>
    amount !== "" && !validAmount(amount) ? "error" : "";

  const columns: TableColumnsType<RateRow> = [
    {
      key: "component_code",
      title: t("pricing.components.componentCode"),
      render: (_: unknown, row) => <CodeText code={row.component_code} />,
    },
    {
      key: "unit_quantity",
      title: t("pricing.components.unitQuantity"),
      render: (_: unknown, row) => (
        <Input
          aria-label={t("pricing.components.unitQuantityFor", { code: row.component_code })}
          inputMode="decimal"
          autoComplete="off"
          value={row.unit_quantity}
          status={amountStatus(row.unit_quantity)}
          suffix={row.unit}
          onChange={(event) => edit(row.component_code, "unit_quantity", event.target.value.trim())}
        />
      ),
    },
    {
      key: "rate_amount",
      title: rateTitle ?? t("pricing.components.rateAmount"),
      render: (_: unknown, row) => (
        <Input
          aria-label={t("pricing.components.rateAmountFor", { code: row.component_code })}
          inputMode="decimal"
          autoComplete="off"
          value={row.rate_amount}
          status={amountStatus(row.rate_amount)}
          onChange={(event) => edit(row.component_code, "rate_amount", event.target.value.trim())}
        />
      ),
    },
  ];

  const groups = groupRows(rows);

  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Typography.Text type="secondary">{t("pricing.components.addHint")}</Typography.Text>
      {groups.length === 0 ? <Empty description={t("pricing.components.none")} /> : null}
      {groups.map((group) => {
        const meterType = meterTypes.find((candidate) => candidate.code === group.code);
        return (
          <Card
            key={group.code}
            type="inner"
            size="small"
            title={
              <Space>
                <CodeText code={group.code} />
                {meterType === undefined || meterType.status === "ACTIVE" ? null : (
                  <CatalogStatusTag status={meterType.status} />
                )}
              </Space>
            }
            extra={
              <Button
                size="small"
                aria-label={t("pricing.components.removeGroup", { code: group.code })}
                onClick={() => remove(group.code)}
              >
                {t("pricing.components.remove")}
              </Button>
            }
          >
            <Table<RateRow>
              size="small"
              rowKey="component_code"
              columns={columns}
              dataSource={group.rows}
              pagination={false}
            />
          </Card>
        );
      })}
      <label htmlFor={addId}>
        <Typography.Text strong>{t("pricing.components.addMeterType")}</Typography.Text>
      </label>
      <Select<string>
        // 选中即加一组；换了组数就重新挂载，下拉回到空白，不留着上一次选的值。
        key={`add-${String(chosen.size)}`}
        id={addId}
        style={{ width: "100%" }}
        showSearch
        optionFilterProp="label"
        options={addable}
        notFoundContent={t("pricing.components.noneLeft")}
        onChange={add}
      />
    </Space>
  );
}
