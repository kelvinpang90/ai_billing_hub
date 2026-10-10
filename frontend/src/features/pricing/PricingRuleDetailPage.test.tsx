/**
 * 规则详情：三种状态、404 与无权限；范围、客户、区间、发布人、倍数或分量（数按字符串显示，标「含税」）；按状态
 * 出现的写操作；编辑（只发改了的字段，换策略时带上新策略的字段）、发布（现在 / 预约）、停用、丢弃的二次确认、
 * 成功后重读、失败时显示后端码对应的文案与 request_id。
 *
 * 请求路径与字段名由 `api/adminPricingRules.test.ts` 管。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType } from "../../api/adminCatalog";
import type { CustomerSummary, Page } from "../../api/adminCustomers";
import type { PricingRule } from "../../api/adminPricingRules";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { PricingRuleDetailPage } from "./PricingRuleDetailPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const rules = vi.hoisted(() => ({
  getPricingRule: vi.fn(),
  updatePricingRule: vi.fn(),
  publishPricingRule: vi.fn(),
  retirePricingRule: vi.fn(),
  discardPricingRule: vi.fn(),
}));

const catalog = vi.hoisted(() => ({
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

const OCR_COMPONENT = {
  component_code: "OCR_PAGE",
  meter_type_code: "OCR_PAGE",
  unit: "PAGE",
  unit_quantity: "1000000.00000000",
  rate_amount: "999999999999.99999999",
  currency: "MYR",
  created_at: AT,
};

/** 草稿：全局默认、MARKUP。 */
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
    markup_multiplier: "1.10000000",
    status: "DRAFT",
    effective_from: null,
    effective_to: null,
    components: [],
    created_by_email: "admin@example.com",
    approved_by_email: null,
    created_at: AT,
    updated_at: AT,
    approved_at: null,
    ...overrides,
  };
}

const PUBLISHED_FIXED = rule({
  priority_scope: "CUSTOMER_PROVIDER_MODEL",
  customer_id: ID,
  provider_id: ID,
  provider_code: "anthropic",
  model_id: ID,
  model_code: "claude-x",
  strategy: "FIXED_RATE",
  markup_multiplier: null,
  status: "PUBLISHED",
  effective_from: "2026-09-29T08:30:01",
  components: [OCR_COMPONENT],
  approved_by_email: "publisher@example.com",
  approved_at: AT,
});

function page<T>(items: T[]): Page<T> {
  return { items, page: 1, page_size: 100, total: items.length };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/pricing/rules/${ID}`]}>
        <Routes>
          <Route path={ROUTES.pricingRuleDetail} element={<PricingRuleDetailPage />} />
        </Routes>
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

function topDialog(): HTMLElement {
  const top = screen.getAllByRole("dialog").at(-1);
  if (top === undefined) {
    throw new Error("no dialog");
  }
  return top;
}

/** 单选项（非按钮样式）：点它的 `<label>`。 */
function radioLabel(name: string, container: HTMLElement = document.body): HTMLElement {
  const label = within(container).getByRole("radio", { name }).closest("label");
  if (label === null) {
    throw new Error(`radio "${name}" is not inside a label`);
  }
  return label;
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

/** 等编辑表单出现（它要先取到计量类型）。 */
async function editForm(): Promise<HTMLElement> {
  return waitFor(() => {
    const dialog = topDialog();
    expect(within(dialog).getByText("Edit pricing rule draft")).toBeInTheDocument();
    expect(within(dialog).getByRole("radio", { name: "Markup" })).toBeInTheDocument();
    return dialog;
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  catalog.listMeterTypes.mockResolvedValue(page([OCR_PAGE]));
  customers.listCustomers.mockResolvedValue(page([CUSTOMER]));
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("PricingRuleDetailPage states", SLOW, () => {
  it("shows a loading state", () => {
    rules.getPricingRule.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading the pricing rule…")).toBeInTheDocument();
    expect(rules.getPricingRule).toHaveBeenCalledWith(ID, expect.anything());
  });

  it("says the rule does not exist on a 404", async () => {
    rules.getPricingRule.mockRejectedValue(
      new ApiError("PRICING_RULE_NOT_FOUND", "The pricing rule does not exist.", "req-404"),
    );

    renderPage();

    expect(await screen.findByText("Pricing rule not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to pricing rules" })).toHaveAttribute("href", "/pricing/rules");
  });

  it("shows the backend message and the request id when loading fails", async () => {
    rules.getPricingRule.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The pricing rule could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    rules.getPricingRule.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can manage pricing rules and preview charges.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says there are no components when a fixed-rate draft has none yet", async () => {
    rules.getPricingRule.mockResolvedValue(rule({ strategy: "FIXED_RATE", markup_multiplier: null }));

    renderPage();

    expect(await screen.findByText("This rule has no components yet.")).toBeInTheDocument();
  });
});

describe("PricingRuleDetailPage content", SLOW, () => {
  it("shows the scope, the customer, the range and every rate exactly as sent, marked tax-inclusive", async () => {
    rules.getPricingRule.mockResolvedValue(PUBLISHED_FIXED);

    renderPage();

    expect(await screen.findByText("Tax-inclusive")).toBeInTheDocument();
    expect(field("Scope")).toHaveTextContent("Customer · provider · model");
    await waitFor(() => expect(field("Customer")).toHaveTextContent("Fictional Trading Sdn Bhd"));
    expect(within(field("Customer")).getByRole("link")).toHaveAttribute("href", `/customers/${ID}`);
    expect(field("Provider")).toHaveTextContent("anthropic");
    expect(field("Model")).toHaveTextContent("claude-x");
    expect(field("Strategy")).toHaveTextContent("Fixed rate");
    expect(field("Effective from")).toHaveTextContent("2026-09-29 16:30:01");
    expect(field("Effective until")).toHaveTextContent("No end");
    expect(field("Published by")).toHaveTextContent("publisher@example.com");
    expect(screen.getByText("Rate (MYR, tax-inclusive)")).toBeInTheDocument();
    // 数按字符串显示：千分位、去掉末尾的 0，不舍入（走一遍浮点数的话已经不是它了）。
    expect(screen.getByText("1,000,000")).toBeInTheDocument();
    expect(screen.getByText("999,999,999,999.99999999")).toBeInTheDocument();
  });

  it("shows a markup rule's multiplier exactly as sent, with no components card", async () => {
    rules.getPricingRule.mockResolvedValue(rule({ markup_multiplier: "1.00000001" }));

    renderPage();

    await waitFor(() => expect(field("Markup multiplier")).toHaveTextContent("× 1.00000001"));
    expect(screen.queryByText("Components")).not.toBeInTheDocument();
  });

  it("offers edit, publish and discard on a draft, and nothing else", async () => {
    rules.getPricingRule.mockResolvedValue(rule());

    renderPage();

    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
    expect(field("Effective from")).toHaveTextContent("Not published");
  });

  it("offers only disable on the current published rule", async () => {
    rules.getPricingRule.mockResolvedValue(PUBLISHED_FIXED);

    renderPage();

    expect(await screen.findByRole("button", { name: "Disable" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Discard" })).not.toBeInTheDocument();
  });

  it("offers nothing on a rule a successor has cut off, and calls a disabled rule disabled", async () => {
    rules.getPricingRule.mockResolvedValue({
      ...PUBLISHED_FIXED,
      status: "RETIRED",
      effective_to: "2026-10-01T00:00:00",
    });

    renderPage();

    await waitFor(() => expect(field("Status")).toHaveTextContent("Disabled"));
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
  });
});

describe("PricingRuleDetailPage publish", SLOW, () => {
  it("publishes now only after the confirmation, with no time, then refreshes", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.publishPricingRule.mockResolvedValue(rule({ status: "PUBLISHED" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Publish this pricing rule?")).toBeInTheDocument();
    expect(rules.publishPricingRule).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and publish", topDialog()));

    expect(await screen.findByText("Published.")).toBeInTheDocument();
    expect(rules.publishPricingRule).toHaveBeenCalledWith(ID, undefined);
    await waitFor(() => expect(rules.getPricingRule).toHaveBeenCalledTimes(2));
  });

  it("sends a scheduled Kuala Lumpur time as RFC 3339 with a zone, on a whole second", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.publishPricingRule.mockResolvedValue(rule({ status: "PUBLISHED" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(radioLabel("At a scheduled time"));
    fireEvent.change(await screen.findByLabelText("Effective from (Kuala Lumpur time)"), {
      target: { value: "2026-10-01T08:00" },
    });
    await user.click(buttonWithText("Review", topDialog()));

    const confirm = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByText("Publish this pricing rule?")).toBeInTheDocument();
      return dialog;
    });
    expect(within(confirm).getByText("2026-10-01T00:00:00Z")).toBeInTheDocument();
    expect(within(confirm).getByText("2026-10-01 08:00:00")).toBeInTheDocument();
    await user.click(buttonWithText("Confirm and publish", confirm));

    expect(await screen.findByText("Published.")).toBeInTheDocument();
    expect(rules.publishPricingRule).toHaveBeenCalledWith(ID, "2026-10-01T00:00:00Z");
  });

  it("shows which components are missing when the backend says the draft is incomplete", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule({ strategy: "FIXED_RATE", markup_multiplier: null }));
    rules.publishPricingRule.mockRejectedValue(
      new ApiError("PRICING_RULE_INCOMPLETE", "Missing components: OCR_PAGE", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and publish"));

    expect(
      await screen.findByText(
        "This fixed-rate draft is incomplete: every component of each meter type it uses needs a rate.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Missing components: OCR_PAGE/)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(rules.getPricingRule).toHaveBeenCalledTimes(1);
  });

  it("says a scheduled time that clashes with this scope's rules is refused", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.publishPricingRule.mockRejectedValue(
      new ApiError("EFFECTIVE_FROM_CONFLICT", "effective_from conflicts.", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and publish"));

    expect(
      await screen.findByText(
        "The time does not fit the existing rules of this scope. If a scheduled rule has not started yet, choose a later time or disable that rule first.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });
});

describe("PricingRuleDetailPage disable and discard", SLOW, () => {
  it("disables with the trimmed reason only after the confirmation", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(PUBLISHED_FIXED);
    rules.retirePricingRule.mockResolvedValue({ ...PUBLISHED_FIXED, status: "RETIRED" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    await user.click(buttonWithText("Review", topDialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Customer moved to a markup  ");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Disable this pricing rule?")).toBeInTheDocument();
    expect(rules.retirePricingRule).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and disable", topDialog()));

    expect(await screen.findByText("Rule disabled.")).toBeInTheDocument();
    expect(rules.retirePricingRule).toHaveBeenCalledWith(ID, "Customer moved to a markup");
    await waitFor(() => expect(rules.getPricingRule).toHaveBeenCalledTimes(2));
  });

  it("shows the text for a rule that can no longer be disabled", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(PUBLISHED_FIXED);
    rules.retirePricingRule.mockRejectedValue(new ApiError("PRICING_RULE_NOT_RETIRABLE", "Cannot retire.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Disable" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Wrong rule");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and disable"));

    expect(
      await screen.findByText(
        "Only the latest published rule of a scope can be disabled. Drafts are discarded instead.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(screen.queryByText("Rule disabled.")).not.toBeInTheDocument();
  });

  it("discards only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.discardPricingRule.mockResolvedValue(rule({ status: "DISCARDED" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));

    expect(await screen.findByText("Discard this draft?")).toBeInTheDocument();
    expect(rules.discardPricingRule).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(await screen.findByText("Draft discarded.")).toBeInTheDocument();
    expect(rules.discardPricingRule).toHaveBeenCalledWith(ID);
    await waitFor(() => expect(rules.getPricingRule).toHaveBeenCalledTimes(2));
  });

  it("shows the text when the draft was published in the meantime", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.discardPricingRule.mockRejectedValue(new ApiError("PRICING_RULE_NOT_DRAFT", "Not a draft.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(
      await screen.findByText("This rule has already been published, so it can no longer be edited or discarded."),
    ).toBeInTheDocument();
    expect(screen.getByText("Discard this draft?")).toBeInTheDocument();
  });
});

describe("PricingRuleDetailPage edit", SLOW, () => {
  it("sends only the multiplier when only it changed, after the confirmation", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.updatePricingRule.mockResolvedValue(rule());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await editForm();
    await user.clear(within(form).getByLabelText("Markup multiplier"));
    await user.paste(" 2.50000001 ");
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Save the changes to this draft?")).toBeInTheDocument();
    expect(rules.updatePricingRule).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and save", topDialog()));

    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    expect(rules.updatePricingRule).toHaveBeenCalledWith(ID, { markup_multiplier: "2.50000001" });
    await waitFor(() => expect(rules.getPricingRule).toHaveBeenCalledTimes(2));
  });

  it("does not offer to save a multiplier that is the same number written differently", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await editForm();
    await user.clear(within(form).getByLabelText("Markup multiplier"));
    // `1.1` 与后端的 `1.10000000` 是同一个数。
    await user.paste("1.1");
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Nothing has been changed.")).toBeInTheDocument();
    expect(rules.updatePricingRule).not.toHaveBeenCalled();
  });

  it("switches a markup draft to fixed rates, sending the new strategy with its components", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.updatePricingRule.mockResolvedValue(rule({ strategy: "FIXED_RATE" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await editForm();
    await user.click(radioLabel("Fixed rate", form));
    fireEvent.mouseDown(await within(form).findByLabelText("Add a meter type"));
    fireEvent.click(await screen.findByTitle("OCR_PAGE"));
    await user.click(await within(form).findByLabelText("Per quantity for OCR_PAGE"));
    await user.paste("1");
    await user.click(within(form).getByLabelText("Rate for OCR_PAGE"));
    await user.paste("0.10000001");
    await user.click(buttonWithText("Review", form));
    await user.click(await screen.findByText("Confirm and save"));

    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    expect(rules.updatePricingRule).toHaveBeenCalledWith(ID, {
      strategy: "FIXED_RATE",
      components: [{ component_code: "OCR_PAGE", unit_quantity: "1", rate_amount: "0.10000001" }],
    });
  });

  it("shows the text and the request id when the backend refuses the edit, and does not refresh", async () => {
    const user = userEvent.setup();
    rules.getPricingRule.mockResolvedValue(rule());
    rules.updatePricingRule.mockRejectedValue(new ApiError("PRICING_RULE_FINAL", "The rule is final.", "req-409"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await editForm();
    await user.clear(within(form).getByLabelText("Markup multiplier"));
    await user.paste("3");
    await user.click(buttonWithText("Review", form));
    await user.click(await screen.findByText("Confirm and save"));

    expect(await screen.findByText("This rule has been disabled or discarded and cannot be changed.")).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(screen.queryByText("Draft saved.")).not.toBeInTheDocument();
    expect(rules.getPricingRule).toHaveBeenCalledTimes(1);
  });
});
