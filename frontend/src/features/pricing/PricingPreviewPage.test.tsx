/**
 * 试算页：表单与「估算成本，客户不可见」、取下拉数据失败与无权限；按上报形态切换数量字段（四个 token 或
 * quantity + 只读的 unit）；交给接口的请求（token 与 quantity 都是字符串，`occurred_at` 是带时区的 RFC 3339）；
 * 计算中禁用按钮；结果（状态、命中的模型、价格版本、汇率版本、规则、分量的未舍入值、三个存储值、「含税」）与
 * 错误状态照样画出已解析到的部分；后端错误码的文案与 request_id；十进制字符串原样显示。
 *
 * 请求体的 JSON 写法由 `api/adminPricingPreview.test.ts` 管。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType, Provider } from "../../api/adminCatalog";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import type { PricingPreview } from "../../api/adminPricingPreview";
import { ApiError } from "../../api/client";
import { PricingPreviewPage } from "./PricingPreviewPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const preview = vi.hoisted(() => ({
  previewPricing: vi.fn(),
}));

const catalog = vi.hoisted(() => ({
  listProviders: vi.fn(),
  listMeterTypes: vi.fn(),
}));

const customers = vi.hoisted(() => ({
  listCustomers: vi.fn(),
}));

vi.mock("../../api/adminPricingPreview", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminPricingPreview")>(
    "../../api/adminPricingPreview",
  );
  return { ...actual, ...preview };
});

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>("../../api/adminCatalog");
  return { ...actual, ...catalog };
});

vi.mock("../../api/adminCustomers", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...customers };
});

function page<T>(items: T[]): Page<T> {
  return { items, page: 1, page_size: 100, total: items.length };
}

const CUSTOMER: CustomerSummary = {
  id: ID,
  company_name: "Fictional Trading Sdn Bhd",
  contact_name: null,
  email: "billing@example.com",
  phone: null,
  billing_status: "ACTIVE",
  billing_mode: "PREPAID",
  ai_service_enabled: true,
  status_version: 1,
  created_at: AT,
  updated_at: AT,
};

const PROVIDER: Provider = {
  id: ID,
  code: "anthropic",
  display_name: "Anthropic",
  status: "ACTIVE",
  created_at: AT,
  updated_at: AT,
};

const LLM_TOKEN: MeterType = {
  id: ID,
  code: "LLM_TOKEN",
  display_name: "LLM tokens",
  payload_shape: "LLM_TOKEN_FIELDS",
  unit: "TOKEN",
  quantity_kind: "INTEGER",
  status: "ACTIVE",
  components: ["LLM_CACHE_READ_TOKEN", "LLM_CACHE_WRITE_TOKEN", "LLM_INPUT_TOKEN", "LLM_OUTPUT_TOKEN"].map(
    (code) => ({ component_code: code, quantity_field: "x", created_at: AT }),
  ),
  created_at: AT,
  updated_at: AT,
};

const OCR_PAGE: MeterType = {
  id: ID,
  code: "OCR_PAGE",
  display_name: "OCR pages",
  payload_shape: "QUANTITY",
  unit: "PAGE",
  quantity_kind: "INTEGER",
  status: "ACTIVE",
  components: [{ component_code: "OCR_PAGE", quantity_field: "quantity", created_at: AT }],
  created_at: AT,
  updated_at: AT,
};

/** 一次成功的试算（数值是虚构的；有一个分量的未舍入值故意很长，走一遍浮点数就变样）。 */
const PRICED: PricingPreview = {
  status: "PRICED",
  error_code: null,
  model: { provider: "anthropic", model: "claude-x", matched_via: "alias" },
  provider_price_version: { id: ID, source_currency: "USD", effective_from: null, effective_to: null },
  fx_rate_version: { id: ID, rate: "4.4444444444", observed_at: AT },
  pricing_rule: { id: ID, priority_scope: "GLOBAL", strategy: "MARKUP", markup_multiplier: "2.00000000" },
  components: [
    {
      component_code: "LLM_INPUT_TOKEN",
      quantity: "1000000000000",
      provider_cost_unrounded: "12345678901234567890.12345678901234567890123456789",
      customer_price_unrounded: null,
    },
  ],
  provider_source_cost_unrounded: "0.0177",
  provider_source_cost: "0.01770000",
  estimated_provider_cost_myr_unrounded: "0.07866666666588",
  estimated_provider_cost_myr: "0.07866667",
  billable_cost_unrounded: "0.15733333333176",
  billable_cost: "0.15733333",
  tax_inclusive: true,
};

/** 有模型与价格、没有规则：`PRICING_ERROR` / `NO_PRICING_RULE`。 */
const NO_RULE: PricingPreview = {
  ...PRICED,
  status: "PRICING_ERROR",
  error_code: "NO_PRICING_RULE",
  model: { provider: "anthropic", model: "claude-x", matched_via: "code" },
  pricing_rule: null,
  components: [],
  provider_source_cost_unrounded: null,
  provider_source_cost: null,
  estimated_provider_cost_myr_unrounded: null,
  estimated_provider_cost_myr: null,
  billable_cost_unrounded: null,
  billable_cost: null,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PricingPreviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function buttonWithText(text: string, container: HTMLElement = document.body): HTMLElement {
  const button = within(container).getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

/** antd 下拉在 mousedown 时打开；选项按 title 找、用 fireEvent 点。 */
async function choose(label: string, option: string): Promise<void> {
  fireEvent.mouseDown(screen.getByLabelText(label));
  const candidates = await screen.findAllByTitle(option);
  const item = candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
}

/** 某个字段（Descriptions 的一行）的值格。 */
function field(label: string): HTMLElement {
  const th = screen.getAllByText(label).map((el) => el.closest("th")).find((el) => el !== null);
  const td = th?.nextElementSibling;
  if (!(td instanceof HTMLElement)) {
    throw new Error(`no field "${label}"`);
  }
  return td;
}

/** 金额表里某一行（按金额名找）。 */
function amountRow(label: string): HTMLElement {
  const row = screen.getByText(label).closest("tr");
  if (row === null) {
    throw new Error(`no amount row "${label}"`);
  }
  return row;
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

type User = ReturnType<typeof userEvent.setup>;

/** 填客户、供应商、模型、计量类型与 `occurred_at`（吉隆坡时间 2026-10-01 08:00）。 */
async function fillCommon(user: User, usageType: string): Promise<void> {
  await choose("Customer", "Fictional Trading Sdn Bhd");
  await choose("Provider", "anthropic");
  await user.click(screen.getByLabelText("Model"));
  await user.paste("claude-x");
  await choose("Usage type", usageType);
  fireEvent.change(screen.getByLabelText("Occurred at (Kuala Lumpur time)"), {
    target: { value: "2026-10-01T08:00" },
  });
}

/** 填一个 token 事件并提交。 */
async function submitTokens(user: User): Promise<void> {
  renderPage();
  await fillCommon(user, "LLM_TOKEN");
  await user.clear(await screen.findByLabelText("Input tokens"));
  await user.paste("1000000000000");
  await user.click(buttonWithText("Calculate"));
}

beforeEach(() => {
  vi.clearAllMocks();
  customers.listCustomers.mockResolvedValue(page([CUSTOMER]));
  catalog.listProviders.mockResolvedValue(page([PROVIDER]));
  catalog.listMeterTypes.mockResolvedValue(page([LLM_TOKEN, OCR_PAGE]));
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("PricingPreviewPage form", SLOW, () => {
  it("shows the form and says the result is an estimated cost customers never see", async () => {
    renderPage();

    expect(screen.getByText("Estimated cost, not visible to customers.")).toBeInTheDocument();
    expect(screen.getByLabelText("Customer")).toBeInTheDocument();
    expect(screen.getByLabelText("Provider")).toBeInTheDocument();
    expect(screen.getByLabelText("Model")).toBeInTheDocument();
    expect(screen.getByLabelText("Usage type")).toBeInTheDocument();
    expect(screen.getByLabelText("Occurred at (Kuala Lumpur time)")).toBeInTheDocument();
    expect(buttonWithText("Calculate")).toBeEnabled();
    // 没选计量类型之前没有数量字段。
    expect(screen.queryByLabelText("Input tokens")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Quantity")).not.toBeInTheDocument();
    await waitFor(() => expect(catalog.listMeterTypes).toHaveBeenCalled());
    expect(preview.previewPricing).not.toHaveBeenCalled();
  });

  it("shows the backend message and the request id when the lists it needs cannot be loaded", async () => {
    customers.listCustomers.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(
      await screen.findByText("The customers, providers, models or meter types could not be loaded."),
    ).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    catalog.listMeterTypes.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can manage pricing rules and preview charges.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("asks for the four token counts for LLM_TOKEN and sends them as the digit strings typed", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue(PRICED);

    await submitTokens(user);

    await waitFor(() => expect(preview.previewPricing).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText("Quantity")).not.toBeInTheDocument();
    expect(preview.previewPricing.mock.calls[0]?.[0] as unknown).toEqual({
      customer_id: ID,
      provider: "anthropic",
      model: "claude-x",
      usage_type: "LLM_TOKEN",
      input_tokens: "1000000000000",
      output_tokens: "0",
      cache_creation_input_tokens: "0",
      cache_read_input_tokens: "0",
      occurred_at: "2026-10-01T00:00:00Z",
    });
  });

  it("asks for a quantity in the meter type's own unit, refusing decimals for a whole-number type", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue(NO_RULE);

    renderPage();
    await fillCommon(user, "OCR_PAGE");
    expect(await screen.findByLabelText("Unit")).toHaveValue("PAGE");
    expect(screen.getByLabelText("Unit")).toBeDisabled();
    expect(screen.queryByLabelText("Input tokens")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Quantity"));
    await user.paste("3.0");
    await user.click(buttonWithText("Calculate"));

    // 一处是字段下的说明，一处是校验错误。
    await waitFor(() => expect(screen.getAllByText("Enter a whole number with up to 12 digits.")).toHaveLength(2));
    expect(preview.previewPricing).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText("Quantity"));
    await user.paste("3");
    await user.click(buttonWithText("Calculate"));

    await waitFor(() => expect(preview.previewPricing).toHaveBeenCalledTimes(1));
    expect(preview.previewPricing.mock.calls[0]?.[0] as unknown).toEqual({
      customer_id: ID,
      provider: "anthropic",
      model: "claude-x",
      usage_type: "OCR_PAGE",
      quantity: "3",
      unit: "PAGE",
      occurred_at: "2026-10-01T00:00:00Z",
    });
  });

  it("refuses a token count above 10^12 without sending anything", async () => {
    const user = userEvent.setup();

    renderPage();
    await fillCommon(user, "LLM_TOKEN");
    await user.clear(await screen.findByLabelText("Output tokens"));
    await user.paste("1000000000001");
    await user.click(buttonWithText("Calculate"));

    expect(await screen.findByText("Enter a whole number from 0 to 1,000,000,000,000.")).toBeInTheDocument();
    expect(preview.previewPricing).not.toHaveBeenCalled();
  });

  it("disables the button while calculating and sends once however often it is clicked", async () => {
    const user = userEvent.setup();
    const pending = deferred<PricingPreview>();
    preview.previewPricing.mockReturnValue(pending.promise);

    await submitTokens(user);
    await waitFor(() => expect(buttonWithText("Calculate")).toBeDisabled());
    // fireEvent 不管按钮是不是禁用、是不是 `pointer-events: none`：真点上去也不能再发一次。
    fireEvent.click(buttonWithText("Calculate"));
    fireEvent.submit(buttonWithText("Calculate"));
    expect(preview.previewPricing).toHaveBeenCalledTimes(1);

    pending.resolve(PRICED);
    expect(await screen.findByText("Priced")).toBeInTheDocument();
  });
});

describe("PricingPreviewPage result", SLOW, () => {
  it("shows every resolved part, each unrounded value and the three stored values exactly as sent", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue(PRICED);

    await submitTokens(user);

    expect(await screen.findByText("Priced")).toBeInTheDocument();
    expect(field("Matched model")).toHaveTextContent("anthropic / claude-x");
    expect(field("Matched model")).toHaveTextContent("By alias");
    expect(within(field("Provider price version")).getByRole("link")).toHaveAttribute(
      "href",
      `/pricing/provider-prices/${ID}`,
    );
    expect(field("Provider price version")).toHaveTextContent("USD");
    expect(field("Provider price version")).toHaveTextContent("The beginning");
    expect(field("Exchange rate version")).toHaveTextContent("4.4444444444");
    expect(field("Exchange rate version")).toHaveTextContent("MYR per 1 USD");
    expect(within(field("Pricing rule")).getByRole("link", { name: "Global default" })).toHaveAttribute(
      "href",
      `/pricing/rules/${ID}`,
    );
    expect(field("Pricing rule")).toHaveTextContent("× 2");
    // 不经 number：50 位有效数字原样（只加千分位）。
    expect(screen.getByText("12,345,678,901,234,567,890.12345678901234567890123456789")).toBeInTheDocument();
    expect(screen.getByText("1,000,000,000,000")).toBeInTheDocument();
    expect(amountRow("Estimated provider cost (MYR)")).toHaveTextContent("0.07866666666588");
    expect(amountRow("Estimated provider cost (MYR)")).toHaveTextContent("0.07866667");
    const billable = amountRow("Customer charge (MYR)");
    expect(billable).toHaveTextContent("0.15733333333176");
    expect(billable).toHaveTextContent("0.15733333");
    expect(within(billable).getByText("Tax-inclusive")).toBeInTheDocument();
    expect(screen.getAllByText("Estimated cost, not visible to customers.")).toHaveLength(2);
  });

  it("still shows what was resolved when no pricing rule applies", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue(NO_RULE);

    await submitTokens(user);

    expect(await screen.findByText("Pricing error")).toBeInTheDocument();
    expect(screen.getByText("NO_PRICING_RULE")).toBeInTheDocument();
    expect(
      screen.getByText("No pricing rule applied to this customer, provider and model at this time."),
    ).toBeInTheDocument();
    expect(field("Matched model")).toHaveTextContent("By model code");
    expect(field("Provider price version")).toHaveTextContent("USD");
    expect(field("Exchange rate version")).toHaveTextContent("4.4444444444");
    expect(field("Pricing rule")).toHaveTextContent("Not resolved");
    expect(screen.getByText("No components were calculated.")).toBeInTheDocument();
    expect(amountRow("Customer charge (MYR)")).toHaveTextContent("—");
  });

  it("shows nothing resolved for an unknown model", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue({
      ...NO_RULE,
      status: "MODEL_UNKNOWN",
      error_code: null,
      model: null,
      provider_price_version: null,
      fx_rate_version: null,
    });

    await submitTokens(user);

    expect(await screen.findByText("Model unknown")).toBeInTheDocument();
    expect(
      screen.getByText("The provider and model matched no model in the catalog at this time."),
    ).toBeInTheDocument();
    expect(field("Matched model")).toHaveTextContent("Not resolved");
    expect(field("Provider price version")).toHaveTextContent("Not resolved");
    expect(field("Exchange rate version")).toHaveTextContent("Not resolved");
  });

  it("says no exchange rate is needed when the provider price is in MYR", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockResolvedValue({
      ...PRICED,
      provider_price_version: { id: ID, source_currency: "MYR", effective_from: null, effective_to: null },
      fx_rate_version: null,
    });

    await submitTokens(user);

    await waitFor(() =>
      expect(field("Exchange rate version")).toHaveTextContent("Not needed: the source currency is MYR."),
    );
  });

  it("shows the text for a known error code with the request id", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockRejectedValue(
      new ApiError("USAGE_METER_TYPE_NOT_FOUND", "The usage meter type does not exist.", "req-404"),
    );

    await submitTokens(user);

    expect(await screen.findByText("This usage type is not in the catalog.")).toBeInTheDocument();
    expect(screen.getByText(/The usage meter type does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
  });

  it("shows the text for an amount too large to store", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockRejectedValue(new ApiError("AMOUNT_OUT_OF_RANGE", "Amount out of range.", "req-422"));

    await submitTokens(user);

    expect(await screen.findByText("One of the calculated amounts is too large to be stored.")).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
  });

  it("falls back to the backend message for a code it does not know", async () => {
    const user = userEvent.setup();
    preview.previewPricing.mockRejectedValue(new ApiError("SOMETHING_NEW", "A brand new failure.", "req-418"));

    await submitTokens(user);

    expect(await screen.findByText("The preview could not be calculated.")).toBeInTheDocument();
    expect(screen.getByText(/A brand new failure\./)).toBeInTheDocument();
    expect(screen.getByText("req-418")).toBeInTheDocument();
  });
});
