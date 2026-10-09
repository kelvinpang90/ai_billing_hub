/**
 * 计量类型页：三种状态与无权限、表格里的分量与取数字段（迁移种子的 9 个类型，`LLM_TOKEN` 四个分量）、
 * 新建 `QUANTITY` 类型（只收五个字段、代码原样、表单写明多字段形态要改代码）、改名与停用的二次确认、
 * 成功后重读、失败时的错误显示。
 *
 * 请求路径与字段名由 `api/adminCatalog.test.ts` 管。uuid 一律全零。输入一律 click + paste。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { configure, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { MeterComponent, MeterType } from "../../api/adminCatalog";
import type { Page } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { MeterTypesPage } from "./MeterTypesPage";

const ID = "00000000-0000-0000-0000-000000000000";
const CREATED = "2026-09-29T08:30:00";

const api = vi.hoisted(() => ({
  listMeterTypes: vi.fn(),
  createMeterType: vi.fn(),
  updateMeterType: vi.fn(),
}));

vi.mock("../../api/adminCatalog", async () => {
  const actual = await vi.importActual<typeof import("../../api/adminCatalog")>(
    "../../api/adminCatalog",
  );
  return { ...actual, ...api };
});

function component(code: string, field = "quantity"): MeterComponent {
  return { component_code: code, quantity_field: field, created_at: CREATED };
}

function quantityType(code: string, unit: string, kind: "INTEGER" | "DECIMAL"): MeterType {
  return {
    id: ID,
    code,
    display_name: code,
    payload_shape: "QUANTITY",
    unit,
    quantity_kind: kind,
    status: "ACTIVE",
    components: [component(code)],
    created_at: CREATED,
    updated_at: CREATED,
  };
}

/** 迁移种子：9 个类型、12 个分量（docs/design/AIH-TASK-025-ai-catalog.md §2），按 `code` 升序。 */
function seeds(): MeterType[] {
  return [
    quantityType("AUDIO_MINUTE", "MINUTE", "DECIMAL"),
    quantityType("AUDIO_SECOND", "SECOND", "DECIMAL"),
    quantityType("CUSTOM", "UNIT", "DECIMAL"),
    quantityType("DOCUMENT_PAGE", "PAGE", "INTEGER"),
    quantityType("EMBEDDING_TOKEN", "TOKEN", "INTEGER"),
    quantityType("IMAGE_GENERATION", "IMAGE", "INTEGER"),
    {
      ...quantityType("LLM_TOKEN", "TOKEN", "INTEGER"),
      payload_shape: "LLM_TOKEN_FIELDS",
      components: [
        component("LLM_CACHE_READ_TOKEN", "cache_read_input_tokens"),
        component("LLM_CACHE_WRITE_TOKEN", "cache_creation_input_tokens"),
        component("LLM_INPUT_TOKEN", "input_tokens"),
        component("LLM_OUTPUT_TOKEN", "output_tokens"),
      ],
    },
    quantityType("OCR_PAGE", "PAGE", "INTEGER"),
    quantityType("TTS_CHARACTER", "CHARACTER", "INTEGER"),
  ];
}

function page(items: MeterType[], total = items.length): Page<MeterType> {
  return { items, page: 1, page_size: 100, total };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MeterTypesPage />
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

/** 按钮样式的单选项：里面的 `<input>` 是 `pointer-events: none`，点外面的 `<label>`（真人也点它）。 */
function radioLabel(name: string): HTMLElement {
  const label = screen.getByRole("radio", { name }).closest("label");
  if (label === null) {
    throw new Error(`radio "${name}" is not inside a label`);
  }
  return label;
}

/** 代码那一格恰好是 `code` 的数据行。 */
function rowOf(code: string): HTMLElement {
  const row = screen
    .getAllByRole("row")
    .find((candidate) => within(candidate).queryAllByRole("cell")[0]?.textContent === code);
  if (row === undefined) {
    throw new Error(`no row for ${code}`);
  }
  return row;
}

/**
 * 等到 `code` 那一行画出来。不用 `findByText(code, { selector: "code" })`：`QUANTITY` 类型唯一的
 * 分量与类型同名，同一行里有两个 `<code>`，那样的查询会一直报「找到多个」直到超时。
 */
async function waitForRow(code: string): Promise<void> {
  await waitFor(() => rowOf(code));
}

type User = ReturnType<typeof userEvent.setup>;

async function paste(user: User, label: string, text: string): Promise<void> {
  await user.click(screen.getByLabelText(label));
  await user.paste(text);
}

async function fillCreateForm(user: User): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "New meter type" }));
  await paste(user, "Code", "VIDEO_SECOND");
  await paste(user, "Display name", " Video seconds ");
  await paste(user, "Unit", "SECOND");
  await user.click(radioLabel("DECIMAL"));
  await paste(user, "Component code", "VIDEO_SECOND");
  await user.click(buttonWithText("Review", topDialog()));
}

beforeEach(() => {
  vi.clearAllMocks();
});

// antd 的表格与对话框在 jsdom 里渲染慢，全量并行时 5 秒不够（同 AdjustmentModal.test.tsx）。
const SLOW = { timeout: 15_000 };
// 同理，findBy / waitFor 默认只等 1 秒，不够 antd 重画；只改本文件（每个测试文件各自隔离）。
configure({ asyncUtilTimeout: 5_000 });

describe("MeterTypesPage states", SLOW, () => {
  it("shows a loading state while the list is on its way", () => {
    api.listMeterTypes.mockReturnValue(new Promise(() => undefined));

    renderPage();

    expect(screen.getByText("Loading meter types…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listMeterTypes.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPage();

    expect(await screen.findByText("The meter types could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says there is no permission on a 403", async () => {
    api.listMeterTypes.mockRejectedValue(
      new ApiError("ADMIN_REQUIRED", "Administrator access is required.", "req-403"),
    );

    renderPage();

    expect(
      await screen.findByText("Only administrators can manage the AI catalog."),
    ).toBeInTheDocument();
    expect(screen.queryByText("The meter types could not be loaded.")).not.toBeInTheDocument();
  });

  it("says so when the list is empty", async () => {
    api.listMeterTypes.mockResolvedValue(page([]));

    renderPage();

    expect(await screen.findByText("No meter types yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI providers" })).toHaveAttribute(
      "href",
      "/catalog/providers",
    );
  });
});

describe("MeterTypesPage table", SLOW, () => {
  it("shows the nine seeded types on one page, each with its components", async () => {
    api.listMeterTypes.mockResolvedValue(page(seeds()));

    renderPage();

    await waitForRow("TTS_CHARACTER");
    // 目录很短：一页 100 条，不带 status。
    expect(api.listMeterTypes).toHaveBeenCalledWith({}, 1, 100, expect.anything());
    for (const seed of seeds()) {
      expect(rowOf(seed.code)).toBeInTheDocument();
    }

    const llm = rowOf("LLM_TOKEN");
    expect(within(llm).getAllByRole("listitem")).toHaveLength(4);
    expect(llm).toHaveTextContent("LLM_TOKEN_FIELDS");
    expect(within(llm).getByText("LLM_CACHE_WRITE_TOKEN")).toBeInTheDocument();
    expect(within(llm).getByText("cache_creation_input_tokens")).toBeInTheDocument();

    const audio = rowOf("AUDIO_SECOND");
    expect(within(audio).getAllByRole("listitem")).toHaveLength(1);
    expect(audio).toHaveTextContent("SECOND");
    expect(audio).toHaveTextContent("DECIMAL");
    expect(within(audio).getByText("quantity")).toBeInTheDocument();
  });

  it("asks only for retired types when that filter is chosen", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValueOnce(page(seeds())).mockResolvedValue(page([]));

    renderPage();
    await waitForRow("TTS_CHARACTER");
    await user.click(radioLabel("Retired"));

    await waitFor(() =>
      expect(api.listMeterTypes).toHaveBeenLastCalledWith(
        { status: "RETIRED" },
        1,
        100,
        expect.anything(),
      ),
    );
    expect(await screen.findByText("No meter types have this status.")).toBeInTheDocument();
  });
});

describe("MeterTypesPage create", SLOW, () => {
  it("says on the form that only QUANTITY can be created and multi-field shapes need code", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New meter type" }));

    expect(screen.getByText("New meter types always use the QUANTITY shape.")).toBeInTheDocument();
    expect(screen.getByText(/needs a code change and cannot be created here/)).toBeInTheDocument();
    // 只有这五项可填：没有形态、没有取数字段。
    const form = topDialog();
    expect(within(form).getAllByRole("textbox")).toHaveLength(4);
    expect(within(form).getAllByRole("radio")).toHaveLength(2);
  });

  it("creates only after the confirmation with exactly the five fields, then refreshes", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([]));
    api.createMeterType.mockResolvedValue(quantityType("VIDEO_SECOND", "SECOND", "DECIMAL"));

    renderPage();
    await fillCreateForm(user);

    expect(await screen.findByText("Create this meter type?")).toBeInTheDocument();
    expect(within(topDialog()).getByText("QUANTITY")).toBeInTheDocument();
    expect(api.createMeterType).not.toHaveBeenCalled();

    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(await screen.findByText("Meter type created.")).toBeInTheDocument();
    expect(api.createMeterType).toHaveBeenCalledTimes(1);
    expect(api.createMeterType).toHaveBeenCalledWith({
      code: "VIDEO_SECOND",
      display_name: "Video seconds",
      unit: "SECOND",
      quantity_kind: "DECIMAL",
      component_code: "VIDEO_SECOND",
    });
    await waitFor(() => expect(api.listMeterTypes).toHaveBeenCalledTimes(2));
  });

  it("refuses lowercase codes and units without changing their case", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([]));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New meter type" }));
    await paste(user, "Code", "video_second");
    await paste(user, "Display name", "Video seconds");
    await paste(user, "Unit", "second");
    await paste(user, "Component code", "VIDEO_SECOND");
    await user.click(buttonWithText("Review", topDialog()));

    expect(
      await screen.findByText(
        "Use 2–32 uppercase letters, digits or underscores, starting with a letter.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Use 1–16 uppercase letters, digits or underscores, starting with a letter."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Code")).toHaveValue("video_second");
    expect(api.createMeterType).not.toHaveBeenCalled();
  });

  it("shows the text for a taken component code with the request id", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([]));
    api.createMeterType.mockRejectedValue(
      new ApiError(
        "USAGE_METER_COMPONENT_CODE_TAKEN",
        "The component code is already in use.",
        "req-409",
      ),
    );

    renderPage();
    await fillCreateForm(user);
    await user.click(buttonWithText("Confirm and create", topDialog()));

    expect(
      await screen.findByText("This component code is already used by a meter type."),
    ).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
    expect(api.listMeterTypes).toHaveBeenCalledTimes(1);
  });

  it("sends once when the confirm button is double-clicked", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([]));
    api.createMeterType.mockReturnValue(new Promise(() => undefined));

    renderPage();
    await fillCreateForm(user);
    await user.dblClick(buttonWithText("Confirm and create", topDialog()));

    await waitFor(() => expect(buttonWithText("Confirm and create", topDialog())).toBeDisabled());
    expect(api.createMeterType).toHaveBeenCalledTimes(1);
  });
});

describe("MeterTypesPage rename and retire", SLOW, () => {
  it("renames a type after the confirmation and refreshes", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([quantityType("OCR_PAGE", "PAGE", "INTEGER")]));
    api.updateMeterType.mockResolvedValue(quantityType("OCR_PAGE", "PAGE", "INTEGER"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Rename" }));
    await user.clear(screen.getByLabelText("Display name"));
    await user.paste("OCR pages");
    await user.click(buttonWithText("Review", topDialog()));
    await user.click(await screen.findByText("Confirm and save"));

    expect(await screen.findByText("Display name saved.")).toBeInTheDocument();
    expect(api.updateMeterType).toHaveBeenCalledWith(ID, { display_name: "OCR pages" });
    await waitFor(() => expect(api.listMeterTypes).toHaveBeenCalledTimes(2));
  });

  it("retires a type only after the confirmation and says billing is unaffected", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([quantityType("OCR_PAGE", "PAGE", "INTEGER")]));
    api.updateMeterType.mockResolvedValue(quantityType("OCR_PAGE", "PAGE", "INTEGER"));

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));

    expect(await screen.findByText("Retire OCR_PAGE?")).toBeInTheDocument();
    expect(screen.getByText(/still accepted, matched and billed/)).toBeInTheDocument();
    expect(api.updateMeterType).not.toHaveBeenCalled();
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("Retired.")).toBeInTheDocument();
    expect(api.updateMeterType).toHaveBeenCalledWith(ID, { status: "RETIRED" });
    await waitFor(() => expect(api.listMeterTypes).toHaveBeenCalledTimes(2));
  });

  it("shows the error when the type no longer exists", async () => {
    const user = userEvent.setup();
    api.listMeterTypes.mockResolvedValue(page([quantityType("OCR_PAGE", "PAGE", "INTEGER")]));
    api.updateMeterType.mockRejectedValue(
      new ApiError("USAGE_METER_TYPE_NOT_FOUND", "The meter type does not exist.", "req-404"),
    );

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Retire" }));
    await user.click(buttonWithText("Confirm and retire", topDialog()));

    expect(await screen.findByText("This meter type does not exist.")).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
  });
});
