/**
 * 价格版本详情：三种状态、404 与无权限；区间、发布人与分量（数按字符串显示）；按状态出现的写操作；
 * 编辑（只发改了的字段）、发布（现在 / 预约，吉隆坡时间换成带时区的 RFC 3339）、退役、丢弃的二次确认、
 * 成功后重读、失败时显示后端码对应的文案与 request_id。
 *
 * 请求路径与字段名由 `api/adminProviderPrices.test.ts` 管。uuid 一律全零。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterType } from "../../api/adminCatalog";
import type { Page } from "../../api/adminCustomers";
import type { PriceVersion } from "../../api/adminProviderPrices";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { ProviderPriceDetailPage } from "./ProviderPriceDetailPage";

const ID = "00000000-0000-0000-0000-000000000000";
const AT = "2026-09-29T08:30:00";

const prices = vi.hoisted(() => ({
  getProviderPrice: vi.fn(),
  updateProviderPrice: vi.fn(),
  publishProviderPrice: vi.fn(),
  retireProviderPrice: vi.fn(),
  discardProviderPrice: vi.fn(),
}));

const catalog = vi.hoisted(() => ({
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

function component(code: string, rate: string) {
  return {
    component_code: code,
    meter_type_code: "LLM_TOKEN",
    unit: "TOKEN",
    unit_quantity: "1000000.00000000",
    rate_amount: rate,
    metadata: null,
    created_at: AT,
  };
}

const FULL = [
  component("LLM_CACHE_READ_TOKEN", "0.10000001"),
  component("LLM_CACHE_WRITE_TOKEN", "3.75000000"),
  component("LLM_INPUT_TOKEN", "3.00000000"),
  component("LLM_OUTPUT_TOKEN", "999999999999.99999999"),
];

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
    status: "DRAFT",
    effective_from: null,
    effective_to: null,
    components: FULL,
    created_by_email: "admin@example.com",
    approved_by_email: null,
    created_at: AT,
    updated_at: AT,
    approved_at: null,
    ...overrides,
  };
}

const PUBLISHED = version({
  status: "PUBLISHED",
  effective_from: "2026-09-29T08:30:01",
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
      <MemoryRouter initialEntries={[`/pricing/provider-prices/${ID}`]}>
        <Routes>
          <Route path={ROUTES.providerPriceDetail} element={<ProviderPriceDetailPage />} />
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
function radioLabel(name: string): HTMLElement {
  const label = screen.getByRole("radio", { name }).closest("label");
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

beforeEach(() => {
  vi.clearAllMocks();
  catalog.listMeterTypes.mockResolvedValue(page([LLM_TOKEN]));
});

const SLOW = { timeout: 20_000 };
configure({ asyncUtilTimeout: 5_000 });

describe("ProviderPriceDetailPage states", SLOW, () => {
  it("shows a loading state", () => {
    prices.getProviderPrice.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading the price version…")).toBeInTheDocument();
    expect(prices.getProviderPrice).toHaveBeenCalledWith(ID, expect.anything());
  });

  it("says the version does not exist on a 404", async () => {
    prices.getProviderPrice.mockRejectedValue(
      new ApiError("PRICE_VERSION_NOT_FOUND", "The price version does not exist.", "req-404"),
    );

    renderPage();

    expect(await screen.findByText("Price version not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to provider prices" })).toHaveAttribute(
      "href",
      "/pricing/provider-prices",
    );
  });

  it("shows the backend message and the request id when loading fails", async () => {
    prices.getProviderPrice.mockRejectedValue(new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"));

    renderPage();

    expect(await screen.findByText("The price version could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    prices.getProviderPrice.mockRejectedValue(new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"));

    renderPage();

    expect(await screen.findByText("Only administrators can manage prices and exchange rates.")).toBeInTheDocument();
    expect(screen.getByText("req-403")).toBeInTheDocument();
  });

  it("says there are no components when a version has none", async () => {
    prices.getProviderPrice.mockResolvedValue(version({ components: [] }));

    renderPage();

    expect(await screen.findByText("This version has no components.")).toBeInTheDocument();
  });
});

describe("ProviderPriceDetailPage content", SLOW, () => {
  it("shows the range, the publisher and every component exactly as the backend sent it", async () => {
    prices.getProviderPrice.mockResolvedValue(PUBLISHED);

    renderPage();

    expect(await screen.findByText("Cost prices, in the source currency, not visible to customers.")).toBeInTheDocument();
    expect(field("Effective from")).toHaveTextContent("2026-09-29 16:30:01");
    expect(field("Effective until")).toHaveTextContent("No end");
    expect(field("Published by")).toHaveTextContent("publisher@example.com");
    expect(field("Created by")).toHaveTextContent("admin@example.com");
    // 数按字符串显示：千分位、去掉末尾的 0，不舍入（走一遍浮点数的话最后一个已经不是它了）。
    expect(screen.getAllByText("1,000,000")).toHaveLength(4);
    expect(screen.getByText("0.10000001")).toBeInTheDocument();
    expect(screen.getByText("3.75")).toBeInTheDocument();
    expect(screen.getByText("999,999,999,999.99999999")).toBeInTheDocument();
  });

  it("shows a withdrawn schedule as never in effect", async () => {
    prices.getProviderPrice.mockResolvedValue(
      version({ status: "RETIRED", effective_from: "2026-10-01T00:00:00", effective_to: "2026-10-01T00:00:00" }),
    );

    renderPage();

    expect(await screen.findByText("Never in effect")).toBeInTheDocument();
  });

  it("offers edit, publish and discard on a draft, and nothing else", async () => {
    prices.getProviderPrice.mockResolvedValue(version());

    renderPage();

    expect(await screen.findByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retire" })).not.toBeInTheDocument();
    expect(field("Effective from")).toHaveTextContent("Not published");
  });

  it("offers only retire on the current published version", async () => {
    prices.getProviderPrice.mockResolvedValue(PUBLISHED);

    renderPage();

    expect(await screen.findByRole("button", { name: "Retire" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Discard" })).not.toBeInTheDocument();
  });

  it("offers nothing on a version a successor has cut off", async () => {
    prices.getProviderPrice.mockResolvedValue({ ...PUBLISHED, effective_to: "2026-10-01T00:00:00" });

    renderPage();

    await screen.findByText("Fictional price sheet, viewed 2026-09-29");
    expect(screen.queryByRole("button", { name: "Retire" })).not.toBeInTheDocument();
  });
});

describe("ProviderPriceDetailPage publish", SLOW, () => {
  it("publishes now only after the confirmation, with no time, then refreshes", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.publishProviderPrice.mockResolvedValue(PUBLISHED);

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Publish this version?")).toBeInTheDocument();
    expect(prices.publishProviderPrice).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and publish", topDialog()));

    expect(await screen.findByText("Published.")).toBeInTheDocument();
    expect(prices.publishProviderPrice).toHaveBeenCalledWith(ID, undefined);
    await waitFor(() => expect(prices.getProviderPrice).toHaveBeenCalledTimes(2));
  });

  it("sends a scheduled Kuala Lumpur time as RFC 3339 with a zone, on a whole second", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.publishProviderPrice.mockResolvedValue(PUBLISHED);

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(radioLabel("At a scheduled time"));
    fireEvent.change(await screen.findByLabelText("Effective from (Kuala Lumpur time)"), {
      target: { value: "2026-10-01T08:00" },
    });
    await user.click(buttonWithText("Review", topDialog()));

    const confirm = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByText("Publish this version?")).toBeInTheDocument();
      return dialog;
    });
    expect(within(confirm).getByText("2026-10-01T00:00:00Z")).toBeInTheDocument();
    expect(within(confirm).getByText("2026-10-01 08:00:00")).toBeInTheDocument();
    await user.click(buttonWithText("Confirm and publish", confirm));

    expect(await screen.findByText("Published.")).toBeInTheDocument();
    expect(prices.publishProviderPrice).toHaveBeenCalledWith(ID, "2026-10-01T00:00:00Z");
  });

  it("refuses a scheduled time that is not a real one", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(radioLabel("At a scheduled time"));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Enter a complete, real date and time.")).toBeInTheDocument();
    expect(screen.queryByText("Publish this version?")).not.toBeInTheDocument();
  });

  it("shows which components are missing when the backend says the draft is incomplete", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.publishProviderPrice.mockRejectedValue(
      new ApiError("PRICE_VERSION_INCOMPLETE", "Missing components: LLM_CACHE_READ_TOKEN", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and publish"));

    expect(
      await screen.findByText(
        "This draft is incomplete: every component of each meter type it uses needs a price.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(/Missing components: LLM_CACHE_READ_TOKEN/)).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(prices.getProviderPrice).toHaveBeenCalledTimes(1);
  });

  it("says a scheduled time in the past is refused", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.publishProviderPrice.mockRejectedValue(
      new ApiError("EFFECTIVE_FROM_IN_PAST", "effective_from is in the past.", "req-422"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await user.click(radioLabel("At a scheduled time"));
    fireEvent.change(await screen.findByLabelText("Effective from (Kuala Lumpur time)"), {
      target: { value: "2026-01-01T08:00" },
    });
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and publish"));

    expect(await screen.findByText("The scheduled time is in the past.")).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
  });
});

describe("ProviderPriceDetailPage retire and discard", SLOW, () => {
  it("retires with the trimmed reason only after the confirmation", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(PUBLISHED);
    prices.retireProviderPrice.mockResolvedValue({ ...PUBLISHED, status: "RETIRED" });

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(buttonWithText("Review", topDialog()));
    expect(await screen.findByText("Enter the reason.")).toBeInTheDocument();

    await user.click(screen.getByLabelText("Reason"));
    await user.paste("  Provider changed its prices  ");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Retire this version?")).toBeInTheDocument();
    expect(prices.retireProviderPrice).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("Retired.")).toBeInTheDocument();
    expect(prices.retireProviderPrice).toHaveBeenCalledWith(ID, "Provider changed its prices");
    await waitFor(() => expect(prices.getProviderPrice).toHaveBeenCalledTimes(2));
  });

  it("shows the text for a version that can no longer be retired", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(PUBLISHED);
    prices.retireProviderPrice.mockRejectedValue(
      new ApiError("PRICE_VERSION_NOT_RETIRABLE", "Cannot retire.", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(screen.getByLabelText("Reason"));
    await user.paste("Wrong price");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and retire"));

    expect(
      await screen.findByText("Only the latest published version can be retired. Drafts are discarded instead."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });

  it("discards only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.discardProviderPrice.mockResolvedValue(version({ status: "DISCARDED" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));

    expect(await screen.findByText("Discard this draft?")).toBeInTheDocument();
    expect(prices.discardProviderPrice).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(await screen.findByText("Draft discarded.")).toBeInTheDocument();
    expect(prices.discardProviderPrice).toHaveBeenCalledWith(ID);
    await waitFor(() => expect(prices.getProviderPrice).toHaveBeenCalledTimes(2));
  });

  it("shows the text when the draft was published in the meantime", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.discardProviderPrice.mockRejectedValue(
      new ApiError("PRICE_VERSION_NOT_DRAFT", "Not a draft.", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await user.click(buttonWithText("Confirm and discard", topDialog()));

    expect(
      await screen.findByText(
        "This version has already been published, so it can no longer be edited or discarded.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Discard this draft?")).toBeInTheDocument();
  });
});

describe("ProviderPriceDetailPage edit", SLOW, () => {
  it("sends only the field that changed, after the confirmation", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());
    prices.updateProviderPrice.mockResolvedValue(version());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByLabelText("Source reference")).toBeInTheDocument();
      return dialog;
    });
    await user.clear(within(form).getByLabelText("Source reference"));
    await user.paste(" Updated price sheet ");
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Save the changes to this draft?")).toBeInTheDocument();
    expect(prices.updateProviderPrice).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and save", topDialog()));

    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    // 分量没动（`1000000.00000000` 原样在框里），不出现在请求体里。
    expect(prices.updateProviderPrice).toHaveBeenCalledWith(ID, { source_reference: "Updated price sheet" });
  });

  it("does not offer to save a draft that did not change", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByLabelText("Source reference")).toBeInTheDocument();
      return dialog;
    });
    await user.click(buttonWithText("Review", form));

    expect(await screen.findByText("Nothing has been changed.")).toBeInTheDocument();
    expect(prices.updateProviderPrice).not.toHaveBeenCalled();
  });

  it("lists the components an old draft is missing and replaces the whole set once they are filled", async () => {
    const user = userEvent.setup();
    prices.getProviderPrice.mockResolvedValue(version({ components: FULL.slice(1) }));
    prices.updateProviderPrice.mockResolvedValue(version());

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const form = await waitFor(() => {
      const dialog = topDialog();
      expect(within(dialog).getByLabelText("Rate for LLM_CACHE_READ_TOKEN")).toBeInTheDocument();
      return dialog;
    });
    expect(within(form).getByLabelText("Rate for LLM_CACHE_READ_TOKEN")).toHaveValue("");
    await user.click(buttonWithText("Review", form));
    expect(
      await screen.findByText(
        "Enter each quantity and rate as a positive number with up to 12 digits before the decimal point and up to 8 after it, with no sign or exponent.",
      ),
    ).toBeInTheDocument();

    await user.click(within(form).getByLabelText("Per quantity for LLM_CACHE_READ_TOKEN"));
    await user.paste("1000000");
    await user.click(within(form).getByLabelText("Rate for LLM_CACHE_READ_TOKEN"));
    await user.paste("0.30000000");
    await user.click(buttonWithText("Review", form));
    await user.click(await screen.findByText("Confirm and save"));

    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
    expect(prices.updateProviderPrice).toHaveBeenCalledWith(ID, {
      components: [
        { component_code: "LLM_CACHE_READ_TOKEN", unit_quantity: "1000000", rate_amount: "0.30000000" },
        { component_code: "LLM_CACHE_WRITE_TOKEN", unit_quantity: "1000000.00000000", rate_amount: "3.75000000" },
        { component_code: "LLM_INPUT_TOKEN", unit_quantity: "1000000.00000000", rate_amount: "3.00000000" },
        {
          component_code: "LLM_OUTPUT_TOKEN",
          unit_quantity: "1000000.00000000",
          rate_amount: "999999999999.99999999",
        },
      ],
    });
  });
});
