/**
 * 价格列表页：三种状态与无权限、表格、筛选（供应商、状态）、「成本价，原币种，客户不可见」；建草稿的二次确认、
 * 成功后重读、失败时显示后端码对应的文案（未知码回落到后端 message）与 request_id、确认中防重复提交；
 * 金额原样是字符串。
 *
 * 请求路径与字段名由 `api/adminProviderPrices.test.ts` 管，这里只看页面把什么交给了它。uuid 一律全零。
 * 输入一律 click + paste：逐字 `user.type` 每按一键 antd 表单就重渲染一次，全量并行时会超时。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType, Model, Provider } from "../../api/adminCatalog";
import type { Page } from "../../api/adminCustomers";
import type { PriceVersion } from "../../api/adminProviderPrices";
import { ApiError } from "../../api/client";
import { ProviderPricesPage } from "./ProviderPricesPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const prices = vi.hoisted(() => ({
  listProviderPrices: vi.fn(),
  createProviderPrice: vi.fn(),
}));

const catalog = vi.hoisted(() => ({
  listProviders: vi.fn(),
  listAllModels: vi.fn(),
  listMeterTypes: vi.fn(),
}));

vi.mock("../../api/adminProviderPrices", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminProviderPrices")>(
    "../../api/adminProviderPrices",
  );
  return { ...actual, ...prices };
});

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>("../../api/adminCatalog");
  return { ...actual, ...catalog };
});

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, page: 1, page_size: 20, total };
}

const PROVIDER: Provider = {
  id: ID,
  code: "anthropic",
  display_name: "Anthropic",
  status: "ACTIVE",
  created_at: AT,
  updated_at: AT,
};

const MODEL: Model = {
  id: ID,
  provider_id: ID,
  provider_code: "anthropic",
  code: "claude-x",
  display_name: "Claude X",
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

function version(overrides: Partial<PriceVersion> = {}): PriceVersion {
  return {
    id: ID,
    provider_id: ID,
    provider_code: "anthropic",
    model_id: ID,
    model_code: "claude-x",
    source_currency: "USD",
    source_type: "MANUAL",
    source_reference: "Fictional price sheet, viewed 2026-09-29",
    status: "PUBLISHED",
    effective_from: null,
    effective_to: null,
    components: [
      {
        component_code: "OCR_PAGE",
        meter_type_code: "OCR_PAGE",
        unit: "PAGE",
        unit_quantity: "1000.00000000",
        rate_amount: "1.50000000",
        metadata: null,
        created_at: AT,
      },
    ],
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
        <ProviderPricesPage />
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

/** 按钮样式的单选项：真人点的是外面的 `<label>`，这里也点它。 */
function radioLabel(name: string): HTMLElement {
  const label = screen.getByRole("radio", { name }).closest("label");
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

/** 填建草稿的表单：供应商、模型、来源说明，加一组 OCR_PAGE 并填两个数，然后点「Review」。 */
async function fillDraft(user: User, quantity: string, rate: string): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New draft" }));
  const form = topDialog();
  // antd 下拉在 mousedown 时打开；选项用 fireEvent 点（同目录页的用例）。
  fireEvent.mouseDown(within(form).getByLabelText("Provider"));
  fireEvent.click(await screen.findByTitle("anthropic"));
  // 模型下拉跟着 `Form.useWatch` 解禁，而 useWatch 是异步通知的：等它拿到供应商、开始取模型再点，
  // 否则 mousedown 落在仍禁用的下拉上，什么也不打开。
  await waitFor(() => expect(catalog.listAllModels).toHaveBeenCalled());
  await waitFor(() =>
    expect(within(form).getByLabelText("Model").closest(".ant-select")).not.toHaveClass("ant-select-disabled"),
  );
  fireEvent.mouseDown(within(form).getByLabelText("Model"));
  fireEvent.click(await screen.findByTitle("claude-x"));
  await user.click(within(form).getByLabelText("Source reference"));
  await user.paste("  Fictional price sheet, viewed 2026-09-29  ");
  fireEvent.mouseDown(within(form).getByLabelText("Add a meter type"));
  fireEvent.click(await screen.findByTitle("OCR_PAGE"));
  await user.click(await within(form).findByLabelText("Per quantity for OCR_PAGE"));
  await user.paste(quantity);
  await user.click(within(form).getByLabelText("Rate for OCR_PAGE"));
  await user.paste(rate);
  await user.click(buttonWithText("Review", form));
}

beforeEach(() => {
  vi.clearAllMocks();
  catalog.listProviders.mockResolvedValue(page([PROVIDER]));
  catalog.listAllModels.mockResolvedValue([MODEL]);
  catalog.listMeterTypes.mockResolvedValue(page([OCR_PAGE]));
});

// antd 的表格与对话框在 jsdom 里渲染慢，全量并行时 5 秒不够（同 AdjustmentModal.test.tsx）。
const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("ProviderPricesPage states", SLOW, () => {
  it("shows a loading state while the first page is on its way", () => {
    prices.listProviderPrices.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading provider prices…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    prices.listProviderPrices.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The provider prices could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403 instead of a generic failure", async () => {
    prices.listProviderPrices.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(
      await screen.findByText("Only administrators can manage prices and exchange rates."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The provider prices could not be loaded.")).not.toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says so when there are no prices, and says these are hidden cost prices", async () => {
    prices.listProviderPrices.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText("No provider prices yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Cost prices, in the source currency, not visible to customers."),
    ).toBeInTheDocument();
    expect(prices.listProviderPrices).toHaveBeenCalledWith({}, 1, 20, expect.anything());
  });
});

describe("ProviderPricesPage table and filters", SLOW, () => {
  // 每张表只放一行：uuid 一律全零，两行就是两个相同的行键。
  it("shows a published version with its open range and links it to its detail page", async () => {
    prices.listProviderPrices.mockResolvedValue(page([version()]));

    renderPage();

    const link = await screen.findByRole("link", { name: "claude-x" });
    expect(link).toHaveAttribute("href", `/pricing/provider-prices/${ID}`);
    const [row] = screen.getAllByRole("row").slice(1);
    expect(row).toHaveTextContent("anthropic");
    expect(row).toHaveTextContent("USD");
    expect(row).toHaveTextContent("Published");
    expect(row).toHaveTextContent("The beginning");
    expect(row).toHaveTextContent("No end");
    expect(row).toHaveTextContent("OCR_PAGE");
  });

  it("shows a draft as not published yet", async () => {
    prices.listProviderPrices.mockResolvedValue(
      page([version({ status: "DRAFT", approved_by_email: null, approved_at: null })]),
    );

    renderPage();

    await screen.findByRole("link", { name: "claude-x" });
    const [draft] = screen.getAllByRole("row").slice(1);
    expect(draft).toHaveTextContent("Draft");
    expect(draft).toHaveTextContent("Not published");
  });

  it("asks only for drafts when that filter is chosen", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValueOnce(page([version()])).mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("claude-x");
    await user.click(radioLabel("Draft"));

    await waitFor(() =>
      expect(prices.listProviderPrices).toHaveBeenLastCalledWith({ status: "DRAFT" }, 1, 20, expect.anything()),
    );
    expect(await screen.findByText("No provider prices match these filters.")).toBeInTheDocument();
  });

  it("asks only for one provider's prices when it is chosen", async () => {
    prices.listProviderPrices.mockResolvedValue(page([]));

    renderPage();
    await screen.findByText("No provider prices yet.");
    fireEvent.mouseDown(screen.getByLabelText("Provider"));
    fireEvent.click(await screen.findByTitle("anthropic"));

    await waitFor(() =>
      expect(prices.listProviderPrices).toHaveBeenLastCalledWith({ provider_id: ID }, 1, 20, expect.anything()),
    );
  });
});

describe("ProviderPricesPage new draft", SLOW, () => {
  it("creates only after the confirmation, with the amounts exactly as typed, then refreshes", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));
    prices.createProviderPrice.mockResolvedValue(version({ status: "DRAFT" }));

    renderPage();
    await fillDraft(user, "1000000", "0.10000001");

    // 第一步只是检查：还没发请求。确认框里的数按字符串显示（千分位，不舍入）。
    expect(await screen.findByText("Create this draft?")).toBeInTheDocument();
    expect(prices.createProviderPrice).not.toHaveBeenCalled();
    const confirm = topDialog();
    expect(within(confirm).getByText("1,000,000")).toBeInTheDocument();
    expect(within(confirm).getByText("0.10000001")).toBeInTheDocument();

    await user.click(buttonWithText("Confirm and create", confirm));

    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
    expect(prices.createProviderPrice).toHaveBeenCalledTimes(1);
    expect(prices.createProviderPrice).toHaveBeenCalledWith({
      provider_id: ID,
      model_id: ID,
      source_currency: "USD",
      source_reference: "Fictional price sheet, viewed 2026-09-29",
      components: [{ component_code: "OCR_PAGE", unit_quantity: "1000000", rate_amount: "0.10000001" }],
    });
    await waitFor(() => expect(prices.listProviderPrices).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("refuses a draft without any meter type", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New draft" }));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Add at least one meter type.")).toBeInTheDocument();
    expect(screen.queryByText("Create this draft?")).not.toBeInTheDocument();
  });

  it("refuses an amount with more than 8 decimal places instead of rounding it", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));

    renderPage();
    await fillDraft(user, "1000000", "0.123456789");

    expect(
      await screen.findByText(
        "Enter each quantity and rate as a positive number with up to 12 digits before the decimal point and up to 8 after it, with no sign or exponent.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Create this draft?")).not.toBeInTheDocument();
    expect(prices.createProviderPrice).not.toHaveBeenCalled();
  });

  it("shows the text for a known 409 code with the request id, and does not refresh", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));
    prices.createProviderPrice.mockRejectedValue(
      new ApiError("CATALOG_ITEM_RETIRED", "A catalog item is retired.", "req-409"),
    );

    renderPage();
    await fillDraft(user, "1", "1");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(
      await screen.findByText("The provider, the model or one of the meter types has been retired."),
    ).toBeInTheDocument();
    expect(screen.getByText(/A catalog item is retired\./)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(prices.listProviderPrices).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Draft created.")).not.toBeInTheDocument();
  });

  it("shows the text for a 422 together with the backend's field list", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));
    prices.createProviderPrice.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid request fields: body.components", "req-422"),
    );

    renderPage();
    await fillDraft(user, "1", "1");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Some fields were not accepted.")).toBeInTheDocument();
    expect(screen.getByText(/Invalid request fields: body\.components/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
  });

  it("falls back to the backend message for a code it does not know", async () => {
    const user = userEvent.setup();
    prices.listProviderPrices.mockResolvedValue(page([]));
    prices.createProviderPrice.mockRejectedValue(new ApiError("SOMETHING_NEW", "A brand new failure.", "req-418"));

    renderPage();
    await fillDraft(user, "1", "1");
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("The draft was not created.")).toBeInTheDocument();
    expect(screen.getByText(/A brand new failure\./)).toBeInTheDocument();
    expect(screen.getByText("req-418")).toBeInTheDocument();
  });

  it("sends once however often the confirm button is clicked while it is running", async () => {
    const user = userEvent.setup();
    const pending = deferred<PriceVersion>();
    prices.listProviderPrices.mockResolvedValue(page([]));
    prices.createProviderPrice.mockReturnValue(pending.promise);

    renderPage();
    await fillDraft(user, "1", "1");
    await user.dblClick(buttonWithText("Confirm and create", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and create", topDialog())).toBeDisabled());
    expect(buttonWithText("Back to edit", topDialog())).toBeDisabled();
    expect(prices.createProviderPrice).toHaveBeenCalledTimes(1);

    pending.resolve(version({ status: "DRAFT" }));
    expect(await screen.findByText("Draft created.")).toBeInTheDocument();
  });
});
