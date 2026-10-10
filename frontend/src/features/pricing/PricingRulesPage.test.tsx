/**
 * 规则列表页：三种状态与无权限、表格（范围、客户、策略、MYR 含税价）、筛选（范围、客户、状态）；建草稿先选范围、
 * 只显示这一级要的字段，MARKUP 只填倍数、FIXED_RATE 成组录入分量；二次确认、成功后重读、失败时显示后端码对应的
 * 文案（未知码回落到后端 message）与 request_id、确认中防重复提交；倍数与单价原样是字符串。
 *
 * 请求路径与字段名由 `api/adminPricingRules.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType, Provider } from "../../api/adminCatalog";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import type { PricingRule } from "../../api/adminPricingRules";
import { ApiError } from "../../api/client";
import { PricingRulesPage } from "./PricingRulesPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const rules = vi.hoisted(() => ({
  listPricingRules: vi.fn(),
  createPricingRule: vi.fn(),
}));

const catalog = vi.hoisted(() => ({
  listProviders: vi.fn(),
  listAllModels: vi.fn(),
  listMeterTypes: vi.fn(),
}));

const customers = vi.hoisted(() => ({
  listCustomers: vi.fn(),
}));

vi.mock("../../api/adminPricingRules", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminPricingRules")>(
    "../../api/adminPricingRules",
  );
  return { ...actual, ...rules };
});

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>("../../api/adminCatalog");
  return { ...actual, ...catalog };
});

vi.mock("../../api/adminCustomers", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...customers };
});

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, page: 1, page_size: 20, total };
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

function rule(overrides: Partial<PricingRule> = {}): PricingRule {
  return {
    id: ID,
    priority_scope: "GLOBAL",
    customer_id: null,
    provider_id: null,
    provider_code: null,
    model_id: null,
    model_code: null,
    strategy: "MARKUP",
    markup_multiplier: "1.10000001",
    status: "PUBLISHED",
    effective_from: null,
    effective_to: null,
    components: [],
    created_by_email: "admin@example.com",
    approved_by_email: "admin@example.com",
    created_at: AT,
    updated_at: AT,
    approved_at: AT,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <PricingRulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 按按钮上的文字找按钮；loading 时按钮名字前面多一个图标名，`getByRole` 的精确名字对不上。 */
function buttonWithText(text: string, container: HTMLElement = document.body): HTMLElement {
  const button = within(container).getByText(text).closest("button");
  if (button === null) {
    throw new Error(`"${text}" is not inside a button`);
  }
  return button;
}

/** 单选项：真人点的是外面的 `<label>`，这里也点它。 */
function radioLabel(name: string, container: HTMLElement = document.body): HTMLElement {
  const label = within(container).getByRole("radio", { name }).closest("label");
  if (label === null) {
    throw new Error(`radio "${name}" is not inside a label`);
  }
  return label;
}

/** 最上面的那个对话框（确认框叠在表单框上面）。 */
function topDialog(): HTMLElement {
  const top = screen.getAllByRole("dialog").at(-1);
  if (top === undefined) {
    throw new Error("no dialog");
  }
  return top;
}

/** antd 下拉在 mousedown 时打开；选项按 title 找、用 fireEvent 点（同价格页的用例）。 */
async function choose(field: HTMLElement, option: string): Promise<void> {
  fireEvent.mouseDown(field);
  const candidates = await screen.findAllByTitle(option);
  const item = candidates.find((candidate) => candidate.closest(".ant-select-item") !== null) ?? candidates[0];
  if (item === undefined) {
    throw new Error(`no option "${option}"`);
  }
  fireEvent.click(item);
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

/** 开建草稿的表单，选全局默认、填倍数，然后点「Review」。 */
async function fillMarkupDraft(user: User, multiplier: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New draft" }));
  const form = topDialog();
  await choose(within(form).getByLabelText("Scope"), "Global default");
  await user.click(await within(form).findByLabelText("Markup multiplier"));
  await user.paste(multiplier);
  await user.click(buttonWithText("Review", form));
}

beforeEach(() => {
  vi.clearAllMocks();
  customers.listCustomers.mockResolvedValue(page([CUSTOMER]));
  catalog.listProviders.mockResolvedValue(page([PROVIDER]));
  catalog.listAllModels.mockResolvedValue([]);
  catalog.listMeterTypes.mockResolvedValue(page([OCR_PAGE]));
});

// antd 的表格与对话框在 jsdom 里渲染慢，全量并行时 5 秒不够（同 AdjustmentModal.test.tsx）。
const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("PricingRulesPage states", SLOW, () => {
  it("shows a loading state while the first page is on its way", () => {
    rules.listPricingRules.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading pricing rules…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    rules.listPricingRules.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The pricing rules could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    rules.listPricingRules.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(await screen.findByText("Only administrators can manage pricing rules and preview charges.")).toBeInTheDocument();
    expect(screen.queryByText("The pricing rules could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says so when there are no rules, and says the prices are tax-inclusive MYR", async () => {
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText("No pricing rules yet.")).toBeInTheDocument();
    expect(screen.getByText("Tax-inclusive")).toBeInTheDocument();
    expect(
      screen.getByText("Rule prices are in MYR and include tax. Markups and rates are shown to administrators only."),
    ).toBeInTheDocument();
    expect(rules.listPricingRules).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });
});

describe("PricingRulesPage table and filters", SLOW, () => {
  // 每张表只放一行：uuid 一律全零，两行就是两个相同的行键。
  it("shows a global markup rule with its multiplier exactly as sent, and links it to its detail page", async () => {
    rules.listPricingRules.mockResolvedValue(page([rule()]));

    renderPage();

    const link = await screen.findByRole("link", { name: "Global default" });
    expect(link).toHaveAttribute("href", `/pricing/rules/${ID}`);
    expect(screen.getByText("Price (MYR, tax-inclusive)")).toBeInTheDocument();
    const [row] = screen.getAllByRole("row").slice(1);
    expect(row).toHaveTextContent("Markup");
    // 不经 number：`1.10000001` 原样（只去掉末尾的 0）。
    expect(row).toHaveTextContent("× 1.10000001");
    expect(row).toHaveTextContent("Published");
    expect(row).toHaveTextContent("The beginning");
    expect(row).toHaveTextContent("No end");
  });

  it("shows a customer's fixed-rate rule with the customer's name, the codes and the components", async () => {
    rules.listPricingRules.mockResolvedValue(
      page([
        rule({
          priority_scope: "CUSTOMER_PROVIDER_MODEL",
          customer_id: ID,
          provider_id: ID,
          provider_code: "anthropic",
          model_id: ID,
          model_code: "claude-x",
          strategy: "FIXED_RATE",
          markup_multiplier: null,
          status: "RETIRED",
          effective_from: "2026-10-01T00:00:00",
          effective_to: "2026-10-02T00:00:00",
          components: [
            {
              component_code: "OCR_PAGE",
              meter_type_code: "OCR_PAGE",
              unit: "PAGE",
              unit_quantity: "1000.00000000",
              rate_amount: "0.10000001",
              currency: "MYR",
              created_at: AT,
            },
          ],
        }),
      ]),
    );

    renderPage();

    await screen.findByRole("link", { name: "Customer · provider · model" });
    const [row] = screen.getAllByRole("row").slice(1);
    await waitFor(() => expect(row).toHaveTextContent("Fictional Trading Sdn Bhd"));
    expect(row).toHaveTextContent("anthropic");
    expect(row).toHaveTextContent("claude-x");
    expect(row).toHaveTextContent("Fixed rate");
    expect(row).toHaveTextContent("OCR_PAGE");
    expect(row).toHaveTextContent("Disabled");
  });

  it("asks only for one scope when it is chosen", async () => {
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("No pricing rules yet.");
    await choose(screen.getByLabelText("Scope"), "Global default");

    await waitFor(() =>
      expect(rules.listPricingRules).toHaveBeenLastCalledWith({ priority_scope: "GLOBAL" }, 1, 20, expect.anything()),
    );
    expect(await screen.findByText("No pricing rules match these filters.")).toBeInTheDocument();
  });

  it("asks only for one customer's rules when the customer is chosen", async () => {
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("No pricing rules yet.");
    await waitFor(() => expect(customers.listCustomers).toHaveBeenCalled());
    await choose(screen.getByLabelText("Customer"), "Fictional Trading Sdn Bhd");

    await waitFor(() =>
      expect(rules.listPricingRules).toHaveBeenLastCalledWith({ customer_id: ID }, 1, 20, expect.anything()),
    );
  });

  it("asks only for one provider's rules, and only for drafts, when those are chosen", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("No pricing rules yet.");
    await choose(screen.getByLabelText("Provider"), "anthropic");
    await user.click(radioLabel("Draft"));

    await waitFor(() =>
      expect(rules.listPricingRules).toHaveBeenLastCalledWith(
        { provider_id: ID, status: "DRAFT" },
        1,
        20,
        expect.anything(),
      ),
    );
  });
});

describe("PricingRulesPage new draft", SLOW, () => {
  it("shows only the scope fields the chosen scope needs", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const form = topDialog();
    expect(within(form).queryByLabelText("Customer")).not.toBeInTheDocument();

    await choose(within(form).getByLabelText("Scope"), "Customer · provider · model");
    expect(await within(form).findByLabelText("Customer")).toBeInTheDocument();
    expect(within(form).getByLabelText("Provider")).toBeInTheDocument();
    expect(within(form).getByLabelText("Model")).toBeInTheDocument();

    await choose(within(form).getByLabelText("Scope"), "Global default");
    await waitFor(() => expect(within(form).queryByLabelText("Customer")).not.toBeInTheDocument());
    expect(within(form).queryByLabelText("Provider")).not.toBeInTheDocument();
    expect(within(form).queryByLabelText("Model")).not.toBeInTheDocument();
  });

  it("creates a markup rule only after the confirmation, with the multiplier exactly as typed, then refreshes", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockResolvedValue(rule({ status: "DRAFT" }));

    renderPage();
    await fillMarkupDraft(user, "1.10000001");

    expect(await screen.findByText("Create this pricing rule draft?")).toBeInTheDocument();
    expect(rules.createPricingRule).not.toHaveBeenCalled();
    const confirm = topDialog();
    expect(within(confirm).getByText("1.10000001")).toBeInTheDocument();

    await user.click(buttonWithText("Confirm and create", confirm));

    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
    expect(rules.createPricingRule).toHaveBeenCalledTimes(1);
    // 全局默认：不带任何范围字段；MARKUP：不带分量。
    expect(rules.createPricingRule).toHaveBeenCalledWith({
      priority_scope: "GLOBAL",
      strategy: "MARKUP",
      markup_multiplier: "1.10000001",
    });
    await waitFor(() => expect(rules.listPricingRules).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("creates a customer's fixed-rate rule with the customer and the rates as typed", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockResolvedValue(rule({ status: "DRAFT" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    const form = topDialog();
    await choose(within(form).getByLabelText("Scope"), "Customer (all providers)");
    await waitFor(() => expect(customers.listCustomers).toHaveBeenCalled());
    await choose(await within(form).findByLabelText("Customer"), "Fictional Trading Sdn Bhd");
    await user.click(radioLabel("Fixed rate", form));
    await waitFor(() => expect(within(form).queryByLabelText("Markup multiplier")).not.toBeInTheDocument());
    await choose(await within(form).findByLabelText("Add a meter type"), "OCR_PAGE");
    // 单价一列写明「MYR，含税」。
    expect(await within(form).findByText("Rate (MYR, tax-inclusive)")).toBeInTheDocument();
    await user.click(await within(form).findByLabelText("Per quantity for OCR_PAGE"));
    await user.paste("1000");
    await user.click(within(form).getByLabelText("Rate for OCR_PAGE"));
    await user.paste("999999999999.99999999");
    await user.click(buttonWithText("Review", form));

    const confirm = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByText("Create this pricing rule draft?")).toBeInTheDocument();
      return dialog;
    });
    expect(within(confirm).getByText("999,999,999,999.99999999")).toBeInTheDocument();
    await user.click(buttonWithText("Confirm and create", confirm));

    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
    expect(rules.createPricingRule).toHaveBeenCalledWith({
      priority_scope: "CUSTOMER",
      customer_id: ID,
      strategy: "FIXED_RATE",
      components: [{ component_code: "OCR_PAGE", unit_quantity: "1000", rate_amount: "999999999999.99999999" }],
    });
  });

  it("refuses a zero multiplier, or one with more than 8 decimal places, instead of rounding it", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));

    renderPage();
    await fillMarkupDraft(user, "1.123456789");

    const invalid =
      "Enter a positive number with up to 12 digits before the decimal point and up to 8 after it, with no sign or exponent.";
    expect(await screen.findByText(invalid)).toBeInTheDocument();
    const form = topDialog();
    await user.clear(within(form).getByLabelText("Markup multiplier"));
    await user.paste("0.00000000");
    await user.click(buttonWithText("Review", form));
    expect(await screen.findByText(invalid)).toBeInTheDocument();
    expect(screen.queryByText("Create this pricing rule draft?")).not.toBeInTheDocument();
    expect(rules.createPricingRule).not.toHaveBeenCalled();
  });

  it("shows the text for a known 404 code with the request id, and does not refresh", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404"),
    );

    renderPage();
    await fillMarkupDraft(user, "2");
    await user.click(await screen.findByText("Confirm and create"));

    expect(await screen.findByText("This customer does not exist.")).toBeInTheDocument();
    expect(screen.getByText(/The customer does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(rules.listPricingRules).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Draft created.")).not.toBeInTheDocument();
  });

  it("shows the text for a 422 together with the backend's field list", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request fields: body.markup_multiplier", "req-422"),
    );

    renderPage();
    await fillMarkupDraft(user, "2");
    await user.click(await screen.findByText("Confirm and create"));

    expect(await screen.findByText("Some fields were not accepted.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request fields: body\.markup_multiplier/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
  });

  it("falls back to the backend message for a code it does not know", async () => {
    const user = userEvent.setup();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockRejectedValue(new ApiError("SOMETHING_NEW", "A brand new failure.", "req-418"));

    renderPage();
    await fillMarkupDraft(user, "2");
    await user.click(await screen.findByText("Confirm and create"));

    expect(await screen.findByText("The draft was not created.")).toBeInTheDocument();
    expect(screen.getByText(/A brand new failure\./)).toBeInTheDocument();
    expect(screen.getByText("req-418")).toBeInTheDocument();
  });

  it("sends once however often the confirm button is clicked while it is running", async () => {
    const user = userEvent.setup();
    const pending = deferred<PricingRule>();
    rules.listPricingRules.mockResolvedValue(page([]));
    rules.createPricingRule.mockReturnValue(pending.promise);

    renderPage();
    await fillMarkupDraft(user, "2");
    await screen.findByText("Create this pricing rule draft?");
    await user.dblClick(buttonWithText("Confirm and create", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and create", topDialog())).toBeDisabled());
    expect(buttonWithText("Back to edit", topDialog())).toBeDisabled();
    expect(rules.createPricingRule).toHaveBeenCalledTimes(1);

    pending.resolve(rule({ status: "DRAFT" }));
    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
  });
});
