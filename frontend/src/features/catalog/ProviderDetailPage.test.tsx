/**
 * 供应商详情页：供应商本身的三种状态（含 404 与无权限）；模型区块（三种状态、新建、改名、停用）；
 * 别名区块（三种状态、按字符串分组与当前段高亮、「改映射只影响此后发生的用量」的说明、映射与撤销的
 * 二次确认、成功后重读、错误显示）。
 *
 * 请求路径与字段名由 `api/adminCatalog.test.ts` 管。uuid 一律全零。输入一律 click + paste。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { AliasSegment, Model, Provider } from "../../api/adminCatalog";
import type { Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { ROUTES } from "../../routes/paths";
import { ProviderDetailPage, groupByAlias } from "./ProviderDetailPage";

const ID = "00000000-0000-0000-0000-000000000000";
const CREATED = "2026-09-29T08:30:00";
const SWITCHED = "2026-09-30T08:30:00";
const FUTURE_ONLY = "Changing a mapping only affects usage that happens after the change.";

const api = vi.hoisted(() => ({
  getProvider: vi.fn(),
  updateProvider: vi.fn(),
  listModels: vi.fn(),
  listAllModels: vi.fn(),
  createModel: vi.fn(),
  updateModel: vi.fn(),
  listModelAliases: vi.fn(),
  mapModelAlias: vi.fn(),
  retireModelAlias: vi.fn(),
}));

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>(
    "../../api/adminCatalog",
  );
  return { ...actual, ...api };
});

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    id: ID,
    code: "anthropic",
    display_name: "Anthropic",
    status: "ACTIVE",
    created_at: CREATED,
    updated_at: CREATED,
    ...overrides,
  };
}

function model(overrides: Partial<Model> = {}): Model {
  return {
    id: ID,
    provider_id: ID,
    provider_code: "anthropic",
    code: "claude-x",
    display_name: "Claude X",
    status: "ACTIVE",
    created_at: CREATED,
    updated_at: CREATED,
    ...overrides,
  };
}

function segment(overrides: Partial<AliasSegment> = {}): AliasSegment {
  return {
    id: ID,
    alias: "claude-x-20250929",
    model_id: ID,
    model_code: "claude-x",
    effective_from: null,
    effective_to: null,
    created_at: CREATED,
    closed_at: null,
    ...overrides,
  };
}

/**
 * 两个字符串：`claude-x-20250929` 先指向 claude-old、在 SWITCHED 改指向 claude-x（当前段）；
 * `old-name` 唯一的一段已经撤销。顺序同后端（按字符串、再按起点）。
 */
function history(): AliasSegment[] {
  return [
    segment({ model_code: "claude-old", effective_to: SWITCHED, closed_at: SWITCHED }),
    segment({ effective_from: SWITCHED }),
    segment({ alias: "old-name", model_code: "claude-old", effective_to: SWITCHED, closed_at: SWITCHED }),
  ];
}

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, page: 1, page_size: 20, total };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/catalog/providers/${ID}`]}>
        <Routes>
          <Route path={ROUTES.catalogProviderDetail} element={<ProviderDetailPage />} />
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

/** 文字里含 `text` 的那一个数据行。 */
function rowWith(text: string): HTMLElement {
  const rows = screen.getAllByRole("row").filter((row) => row.textContent?.includes(text));
  const [row] = rows;
  if (rows.length !== 1 || row === undefined) {
    throw new Error(`expected exactly one row with ${text}, found ${String(rows.length)}`);
  }
  return row;
}

/** 别名区块里以 `alias` 为标题的那一组（段表格里只显示模型代码，字符串本身只出现在组的标题上）。 */
function aliasGroup(alias: string): HTMLElement {
  const card = screen.getByText(alias, { selector: "code" }).closest(".ant-card");
  if (!(card instanceof HTMLElement)) {
    throw new Error(`no alias group ${alias}`);
  }
  return card;
}

/** 默认：供应商存在、没有模型、没有别名。 */
function givenEmptyProvider(): void {
  api.getProvider.mockResolvedValue(provider());
  api.listModels.mockResolvedValue(page<Model>([]));
  api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));
}

beforeEach(() => {
  vi.clearAllMocks();
  // 映射别名的模型下拉取的是全部模型（listAllModels 自己逐页取，分页逻辑在 adminCatalog.test.ts）。
  api.listAllModels.mockResolvedValue([model()]);
});

describe("groupByAlias", () => {
  it("groups adjacent segments of the same string without reordering", () => {
    const groups = groupByAlias(history());

    expect(groups.map((group) => [group.alias, group.segments.length])).toEqual([
      ["claude-x-20250929", 2],
      ["old-name", 1],
    ]);
  });
});

// antd 的表格与对话框在 jsdom 里渲染慢，全量并行时 5 秒不够（同 AdjustmentModal.test.tsx）。
const SLOW = { timeout: 20_000 };
// 同理，findBy / waitFor 默认只等 1 秒，不够三个查询依次落地后 antd 重画；只改本文件（每个测试文件各自隔离）。
configure({ asyncUtilTimeout: 5_000 });

describe("ProviderDetailPage provider", SLOW, () => {
  it("shows a loading state while the provider is on its way", () => {
    api.getProvider.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading the provider…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the provider fails to load", async () => {
    api.getProvider.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The provider could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says the provider does not exist on a 404", async () => {
    api.getProvider.mockRejectedValue(
      new ApiError("AI_PROVIDER_NOT_FOUND", "The provider does not exist.", "req-404"),
    );

    renderPage();

    expect(await screen.findByText("Provider not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to providers" })).toHaveAttribute(
      "href",
      "/catalog/providers",
    );
  });

  it("says there is no permission on a 403", async () => {
    api.getProvider.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(
      await screen.findByText("Only administrators can manage the AI catalog."),
    ).toBeInTheDocument();
  });

  it("shows the provider and empty models and aliases, with the mapping rule spelled out", async () => {
    givenEmptyProvider();

    renderPage();

    expect(await screen.findByText("This provider has no models yet.")).toBeInTheDocument();
    expect(await screen.findByText("This provider has no aliases yet.")).toBeInTheDocument();
    expect(screen.getByText(FUTURE_ONLY)).toBeInTheDocument();
    expect(screen.getByText("anthropic", { selector: "code" })).toBeInTheDocument();
    expect(api.getProvider).toHaveBeenCalledWith(ID, expect.anything());
    expect(api.listModels).toHaveBeenCalledWith(ID, {}, 1, 20, expect.anything());
    expect(api.listModelAliases).toHaveBeenCalledWith(ID, {}, 1, 100, expect.anything());
  });

  it("renames the provider after the confirmation and reloads it", async () => {
    const user = userEvent.setup();
    givenEmptyProvider();
    api.updateProvider.mockResolvedValue(provider({ display_name: "Anthropic PBC" }));

    renderPage();
    await screen.findByText("This provider has no models yet.");
    await user.click(screen.getByRole("button", { name: "Rename" }));
    await user.clear(screen.getByLabelText("Display name"));
    await user.paste("Anthropic PBC");
    await user.click(buttonWithText("Review", topDialog()));
    expect(api.updateProvider).not.toHaveBeenCalled();
    await user.click(await screen.findByText("Confirm and save"));

    expect(await screen.findByText("Display name saved.")).toBeInTheDocument();
    expect(api.updateProvider).toHaveBeenCalledWith(ID, { display_name: "Anthropic PBC" });
    await waitFor(() => expect(api.getProvider).toHaveBeenCalledTimes(2));
  });
});

describe("ProviderDetailPage models", SLOW, () => {
  it("shows its own loading and failure states", async () => {
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );
    api.listModelAliases.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(await screen.findByText("The models could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByText("Loading aliases…")).toBeInTheDocument();
  });

  it("creates a model after the confirmation with the code exactly as typed, then refreshes", async () => {
    const user = userEvent.setup();
    givenEmptyProvider();
    api.createModel.mockResolvedValue(model({ code: "GPT-4o" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New model" }));
    await user.click(screen.getByLabelText("Code"));
    await user.paste("GPT-4o");
    await user.click(screen.getByLabelText("Display name"));
    await user.paste("GPT-4o");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Create this model?")).toBeInTheDocument();
    expect(api.createModel).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Model created.")).toBeInTheDocument();
    expect(api.createModel).toHaveBeenCalledWith(ID, { code: "GPT-4o", display_name: "GPT-4o" });
    await waitFor(() => expect(api.listModels).toHaveBeenCalledTimes(2));
  });

  it("refuses a model code with a space before asking the backend", async () => {
    const user = userEvent.setup();
    givenEmptyProvider();

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New model" }));
    await user.click(screen.getByLabelText("Code"));
    await user.paste("gpt 4o");
    await user.click(screen.getByLabelText("Display name"));
    await user.paste("GPT-4o");
    await user.click(buttonWithText("Review", topDialog()));

    expect(
      await screen.findByText(
        "Use up to 128 letters, digits and . _ : / @ - with no spaces, starting with a letter or digit.",
      ),
    ).toBeInTheDocument();
    expect(api.createModel).not.toHaveBeenCalled();
  });

  it("shows the text for a model code that clashes with an alias", async () => {
    const user = userEvent.setup();
    givenEmptyProvider();
    api.createModel.mockRejectedValue(
      new ApiError("AI_MODEL_CODE_TAKEN", "The model code is already in use.", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New model" }));
    await user.click(screen.getByLabelText("Code"));
    await user.paste("claude-x");
    await user.click(screen.getByLabelText("Display name"));
    await user.paste("Claude X");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and create"));

    expect(
      await screen.findByText(
        "This provider already has a model with this code, or the code has been used as an alias.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(api.listModels).toHaveBeenCalledTimes(1);
  });

  it("retires a model only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page([model()]));
    api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));
    api.updateModel.mockResolvedValue(model({ status: "RETIRED" }));

    renderPage();
    await screen.findByText("Claude X");
    await user.click(within(rowWith("Claude X")).getByRole("button", { name: "Retire" }));

    expect(await screen.findByText("Retire claude-x?")).toBeInTheDocument();
    expect(api.updateModel).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("Retired.")).toBeInTheDocument();
    expect(api.updateModel).toHaveBeenCalledWith(ID, ID, { status: "RETIRED" });
    await waitFor(() => expect(api.listModels).toHaveBeenCalledTimes(2));
  });
});

describe("ProviderDetailPage aliases", SLOW, () => {
  it("shows its own failure state with the request id", async () => {
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page<Model>([]));
    api.listModelAliases.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The aliases could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("groups the segments by string and highlights the current one", async () => {
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page<Model>([]));
    api.listModelAliases.mockResolvedValue(page(history()));

    renderPage();
    await screen.findByText("old-name", { selector: "code" });

    const switched = aliasGroup("claude-x-20250929");
    const rows = within(switched).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("The beginning");
    expect(rows[0]).toHaveTextContent("claude-old");
    expect(rows[0]).toHaveTextContent("Ended");
    expect(rows[1]).toHaveTextContent("No end");
    expect(rows[1]).toHaveTextContent("claude-x");
    expect(rows[1]).toHaveTextContent("Current");
    expect((rows[1] as HTMLElement).style.background).not.toBe("");
    expect((rows[0] as HTMLElement).style.background).toBe("");
    expect(within(switched).getByRole("button", { name: "Remove mapping" })).toBeInTheDocument();

    // 撤销过、没有当前段的字符串：只能重新映射，没有可撤销的。
    const retired = aliasGroup("old-name");
    expect(within(retired).queryByText("Current")).not.toBeInTheDocument();
    expect(within(retired).queryByRole("button", { name: "Remove mapping" })).not.toBeInTheDocument();
    expect(within(retired).getByRole("button", { name: "Change mapping" })).toBeInTheDocument();
  });

  it("maps a new alias after the confirmation, alias untouched, then refreshes", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page([model()]));
    api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));
    api.mapModelAlias.mockResolvedValue(segment({ alias: "Claude-X-Latest" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Map an alias" }));
    expect(within(topDialog()).getByText(FUTURE_ONLY)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Alias"));
    await user.paste("Claude-X-Latest");
    // antd 下拉在 mousedown 时打开；它的 input 只是占位，不直接点（同下面选项用 fireEvent）。
    fireEvent.mouseDown(screen.getByLabelText("Model"));
    fireEvent.click(await screen.findByTitle("claude-x"));
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Save this mapping?")).toBeInTheDocument();
    expect(api.mapModelAlias).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and map", topDialog()));

    expect(await screen.findByText("Mapping saved.")).toBeInTheDocument();
    expect(api.mapModelAlias).toHaveBeenCalledTimes(1);
    expect(api.mapModelAlias).toHaveBeenCalledWith(ID, { alias: "Claude-X-Latest", model_id: ID });
    await waitFor(() => expect(api.listModelAliases).toHaveBeenCalledTimes(2));
    // 下拉要的是本供应商的全部模型，不只是第一页。
    expect(api.listAllModels).toHaveBeenCalledWith(ID, expect.anything());
  });

  it("maps to a model beyond the first page of 100", async () => {
    const user = userEvent.setup();
    // 101 个模型：最后一个只在第二页上，旧做法（只取一页）选不到它。id 只换末尾几位，仍是全零占位。
    const models = Array.from({ length: 101 }, (_, index) =>
      model({
        id: `00000000-0000-0000-0000-${String(index + 1).padStart(12, "0")}`,
        code: `model-${String(index + 1).padStart(3, "0")}`,
      }),
    );
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page<Model>([]));
    api.listAllModels.mockResolvedValue(models);
    api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));
    api.mapModelAlias.mockResolvedValue(segment({ alias: "late-alias" }));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Map an alias" }));
    await user.click(screen.getByLabelText("Alias"));
    await user.paste("late-alias");
    // 下拉是虚拟列表，只渲染开头几项；按代码搜到第 101 个再点。
    fireEvent.mouseDown(screen.getByLabelText("Model"));
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "model-101" } });
    fireEvent.click(await screen.findByTitle("model-101"));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and map"));

    expect(await screen.findByText("Mapping saved.")).toBeInTheDocument();
    expect(api.mapModelAlias).toHaveBeenCalledWith(ID, {
      alias: "late-alias",
      model_id: "00000000-0000-0000-0000-000000000101",
    });
  });

  it("does not ask the backend before a model is chosen", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page([model()]));
    api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Map an alias" }));
    await user.click(screen.getByLabelText("Alias"));
    await user.paste("claude-x-latest");
    await user.click(buttonWithText("Review", topDialog()));

    expect(await screen.findByText("Choose the model.")).toBeInTheDocument();
    expect(api.mapModelAlias).not.toHaveBeenCalled();
  });

  it("changes the mapping of an existing string with the string fixed", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page([model()]));
    api.listModelAliases.mockResolvedValue(page(history()));

    renderPage();
    await screen.findByText("old-name", { selector: "code" });
    await user.click(
      within(aliasGroup("old-name")).getByRole("button", { name: "Change mapping" }),
    );

    expect(await screen.findByText("Change the mapping of old-name")).toBeInTheDocument();
    expect(screen.getByLabelText("Alias")).toHaveValue("old-name");
    expect(screen.getByLabelText("Alias")).toBeDisabled();
  });

  it("shows the text for an alias that is a model code", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page([model()]));
    api.listModelAliases.mockResolvedValue(page<AliasSegment>([]));
    api.mapModelAlias.mockRejectedValue(
      new ApiError("AI_MODEL_ALIAS_TAKEN", "The alias is a model code.", "req-409"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Map an alias" }));
    await user.click(screen.getByLabelText("Alias"));
    await user.paste("claude-x");
    fireEvent.mouseDown(screen.getByLabelText("Model"));
    fireEvent.click(await screen.findByTitle("claude-x"));
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and map"));

    expect(
      await screen.findByText(
        "This string is the code of one of this provider's models, so it cannot be mapped as an alias.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(api.listModelAliases).toHaveBeenCalledTimes(1);
  });

  it("removes the current mapping only after the confirmation, then refreshes", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page<Model>([]));
    api.listModelAliases.mockResolvedValue(page(history()));
    api.retireModelAlias.mockResolvedValue(segment({ effective_to: CREATED }));

    renderPage();
    await screen.findByText("old-name", { selector: "code" });
    await user.click(
      within(aliasGroup("claude-x-20250929")).getByRole("button", { name: "Remove mapping" }),
    );

    expect(await screen.findByText("Remove the mapping of claude-x-20250929?")).toBeInTheDocument();
    expect(screen.getByText(/kept as MODEL_UNKNOWN and not charged/)).toBeInTheDocument();
    expect(api.retireModelAlias).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and remove", topDialog()));

    expect(await screen.findByText("Mapping removed.")).toBeInTheDocument();
    expect(api.retireModelAlias).toHaveBeenCalledTimes(1);
    expect(api.retireModelAlias).toHaveBeenCalledWith(ID, ID);
    await waitFor(() => expect(api.listModelAliases).toHaveBeenCalledTimes(2));
  });

  it("refreshes and explains when the segment is no longer current", async () => {
    const user = userEvent.setup();
    api.getProvider.mockResolvedValue(provider());
    api.listModels.mockResolvedValue(page<Model>([]));
    api.listModelAliases.mockResolvedValue(page(history()));
    api.retireModelAlias.mockRejectedValue(
      new ApiError("AI_MODEL_ALIAS_NOT_FOUND", "The alias segment does not exist.", "req-404"),
    );

    renderPage();
    await screen.findByText("old-name", { selector: "code" });
    await user.click(
      within(aliasGroup("claude-x-20250929")).getByRole("button", { name: "Remove mapping" }),
    );
    await user.click(buttonWithText("Confirm and remove", topDialog()));

    expect(
      await screen.findByText(
        "This mapping is no longer the current one. The list has been refreshed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    await waitFor(() => expect(api.listModelAliases).toHaveBeenCalledTimes(2));
  });
});
