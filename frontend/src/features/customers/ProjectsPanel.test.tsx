/**
 * 客户详情里的项目区块：三种状态、分页、建项目的校验与提交。
 *
 * ⚠️ 建项目**不幂等**（docs/api.md）：双击就是两个项目。与建客户一样用双击测「提交中
 * 不能再提交」—— 只断言按钮变灰，抓不到两次 onFinish 都赶在变灰之前的竞态。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Page, Project } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { ProjectsPanel, toCreateProjectBody } from "./ProjectsPanel";

// antd 的 TextArea 不管有没有 autoSize 都会挂 ResizeObserver，jsdom 里没有：补一个空实现。
class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = NoopResizeObserver;
}

const CUSTOMER_ID ="00000000-0000-4000-8000-000000000000";
const PROJECT_ID = "00000000-0000-4000-8000-000000000001";

const api = vi.hoisted(() => ({
  listProjects: vi.fn(),
  createProject: vi.fn(),
}));

vi.mock("../../api/adminCustomers", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/adminCustomers")>("../../api/adminCustomers");
  return { ...actual, ...api };
});

// 凭据面板自己的用例在 IntegrationAccessPanel.test.tsx；这里只看它挂没挂上、拿到的是哪个项目。
vi.mock("./IntegrationAccessPanel", () => ({
  IntegrationAccessPanel: ({ customerId, project }: { customerId: string; project: Project }) =>
    `credentials:${customerId}:${project.id}`,
}));

function project(overrides: Partial<Project> = {}): Project {
  return {
    id: PROJECT_ID,
    name: "Chatbot",
    description: null,
    created_at: "2026-09-20T08:31:00",
    updated_at: "2026-09-20T08:31:00",
    ...overrides,
  };
}

function page(items: Project[], total: number, number = 1): Page<Project> {
  return { items, page: number, page_size: 20, total };
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

function renderPanel(renderProjectDetails?: (project: Project) => string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProjectsPanel customerId={CUSTOMER_ID} renderProjectDetails={renderProjectDetails} />
    </QueryClientProvider>,
  );
}

async function openCreateForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "New project" }));
}

function submitButton() {
  // 提交中按钮里多一个 loading 图标（aria-label="loading"），名字不再是精确的这一句。
  return screen.getByRole("button", { name: /Create project/ });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ProjectsPanel", () => {
  it("shows a loading state while the first page is on its way", () => {
    api.listProjects.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(screen.getByText("Loading projects…")).toBeInTheDocument();
  });

  it("shows the backend message and the request id when the list fails", async () => {
    api.listProjects.mockRejectedValue(
      new ApiError("INTERNAL_ERROR", "An unexpected error occurred.", "req-500"),
    );

    renderPanel();

    expect(await screen.findByText("The projects could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText(/An unexpected error occurred\./)).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says so when the customer has no projects, and still offers to create one", async () => {
    api.listProjects.mockResolvedValue(page([], 0));

    renderPanel();

    expect(await screen.findByText("This customer has no projects yet.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New project" })).toBeInTheDocument();
  });

  it("lists projects in the backend's order with name, description and creation time", async () => {
    api.listProjects.mockResolvedValue(
      page(
        [
          project({ name: "Oldest", description: "Support bot" }),
          project({
            id: "00000000-0000-4000-8000-000000000002",
            name: "Newer",
            created_at: "2026-09-20T20:00:00",
          }),
        ],
        2,
      ),
    );

    renderPanel();

    await screen.findByText("Oldest");
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("Oldest");
    expect(rows[0]).toHaveTextContent("Support bot");
    expect(rows[0]).toHaveTextContent("2026-09-20 16:31:00");
    expect(rows[1]).toHaveTextContent("Newer");
    // 没有描述显示一道横线；创建时间按吉隆坡时间，UTC 20:00 已经是第二天。
    expect(rows[1]).toHaveTextContent("—");
    expect(rows[1]).toHaveTextContent("2026-09-21 04:00:00");
    expect(api.listProjects).toHaveBeenCalledWith(CUSTOMER_ID, 1, 20, expect.anything());
  });

  it("asks the backend for the page the user moves to", async () => {
    const user = userEvent.setup();
    api.listProjects.mockImplementation((_customerId: string, requested: number) =>
      Promise.resolve(
        page([project({ name: `Project on page ${String(requested)}` })], 45, requested),
      ),
    );

    renderPanel();

    expect(await screen.findByText("45 projects")).toBeInTheDocument();
    await user.click(screen.getByTitle("2"));

    expect(await screen.findByText("Project on page 2")).toBeInTheDocument();
    expect(api.listProjects).toHaveBeenLastCalledWith(CUSTOMER_ID, 2, 20, expect.anything());
  });

  it("explains an empty page past the end instead of claiming there are no projects", async () => {
    api.listProjects.mockResolvedValue(page([], 45, 9));

    renderPanel();

    expect(await screen.findByText("There are no projects on this page.")).toBeInTheDocument();
    expect(screen.queryByText("This customer has no projects yet.")).not.toBeInTheDocument();
  });

  it("does not submit without a project name", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    renderPanel();

    await openCreateForm(user);
    await user.click(submitButton());

    expect(await screen.findByText("Enter the project name.")).toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("treats a project name of only spaces as missing", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    renderPanel();

    await openCreateForm(user);
    await user.type(screen.getByLabelText("Project name"), "   ");
    await user.click(submitButton());

    expect(await screen.findByText("Enter the project name.")).toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("rejects a name or description longer than the backend accepts", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    renderPanel();

    await openCreateForm(user);
    await user.click(screen.getByLabelText("Project name"));
    await user.paste("n".repeat(256));
    await user.click(screen.getByLabelText("Description"));
    await user.paste("d".repeat(1001));
    await user.click(submitButton());

    expect(
      await screen.findByText("The project name can be at most 255 characters."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("The description can be at most 1000 characters."),
    ).toBeInTheDocument();
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("accepts the longest values the backend accepts once the outer spaces are trimmed", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    api.createProject.mockResolvedValue(project());
    renderPanel();

    await openCreateForm(user);
    await user.click(screen.getByLabelText("Project name"));
    await user.paste(` ${"n".repeat(255)} `);
    await user.click(screen.getByLabelText("Description"));
    await user.paste(` ${"d".repeat(1000)} `);
    await user.click(submitButton());

    await waitFor(() => {
      expect(api.createProject).toHaveBeenCalledWith(CUSTOMER_ID, {
        name: "n".repeat(255),
        description: "d".repeat(1000),
      });
    });
  });

  it("sends a trimmed name without an empty description and refreshes the list", async () => {
    const user = userEvent.setup();
    api.listProjects
      .mockResolvedValueOnce(page([], 0))
      .mockResolvedValue(page([project({ name: "Chatbot" })], 1));
    api.createProject.mockResolvedValue(project({ name: "Chatbot" }));
    renderPanel();

    await openCreateForm(user);
    await user.type(screen.getByLabelText("Project name"), "  Chatbot  ");
    await user.type(screen.getByLabelText("Description"), "   ");
    await user.click(submitButton());

    expect(await screen.findByText("Project created.")).toBeInTheDocument();
    expect(api.createProject).toHaveBeenCalledTimes(1);
    expect(api.createProject).toHaveBeenCalledWith(CUSTOMER_ID, { name: "Chatbot" });
    // 列表重新读过一次，新项目是从后端读回来的，不是前端拼进去的。
    expect(await screen.findByText("Chatbot")).toBeInTheDocument();
    expect(api.listProjects).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: /Create project/ })).not.toBeInTheDocument();
  });

  it("disables the submit button while the request is in flight and sends it once", async () => {
    const user = userEvent.setup();
    const pending = deferred<Project>();
    api.listProjects.mockResolvedValue(page([], 0));
    api.createProject.mockReturnValue(pending.promise);
    renderPanel();

    await openCreateForm(user);
    await user.type(screen.getByLabelText("Project name"), "Chatbot");
    await user.dblClick(submitButton());

    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    expect(api.createProject).toHaveBeenCalledTimes(1);

    pending.resolve(project());

    expect(await screen.findByText("Project created.")).toBeInTheDocument();
    expect(api.createProject).toHaveBeenCalledTimes(1);
  });

  it("shows the backend message and request id when the customer is gone", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    api.createProject.mockRejectedValue(
      new ApiError("CUSTOMER_NOT_FOUND", "The customer does not exist.", "req-404"),
    );
    renderPanel();

    await openCreateForm(user);
    await user.type(screen.getByLabelText("Project name"), "Chatbot");
    await user.click(submitButton());

    expect(await screen.findByText(/The customer does not exist\./)).toBeInTheDocument();
    expect(screen.getByText("req-404")).toBeInTheDocument();
    expect(screen.getByText("The project could not be created.")).toBeInTheDocument();
    // 表单留着，可以改了再交。
    expect(screen.getByLabelText("Project name")).toHaveValue("Chatbot");
    expect(submitButton()).toBeEnabled();
  });

  it("shows the backend message when the backend rejects the body", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([], 0));
    api.createProject.mockRejectedValue(
      new ApiError("VALIDATION_ERROR", "Invalid field: body.name", "req-422"),
    );
    renderPanel();

    await openCreateForm(user);
    await user.type(screen.getByLabelText("Project name"), "Chatbot");
    await user.click(submitButton());

    expect(await screen.findByText(/Invalid field: body\.name/)).toBeInTheDocument();
    expect(screen.getByText("req-422")).toBeInTheDocument();
    expect(screen.queryByText("Project created.")).not.toBeInTheDocument();
  });

  it("mounts the project's integration credentials into each row by default", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([project()], 1));

    renderPanel();

    await screen.findByText("Chatbot");
    // 没展开之前不挂：凭据接口只为展开的项目请求。
    expect(screen.queryByText(`credentials:${CUSTOMER_ID}:${PROJECT_ID}`)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand row" }));

    expect(
      await screen.findByText(`credentials:${CUSTOMER_ID}:${PROJECT_ID}`),
    ).toBeInTheDocument();
  });

  it("shows what the caller renders into the mount point instead, when given", async () => {
    const user = userEvent.setup();
    api.listProjects.mockResolvedValue(page([project()], 1));

    renderPanel((mounted) => `mounted:${mounted.id}`);

    await screen.findByText("Chatbot");
    await user.click(screen.getByRole("button", { name: "Expand row" }));

    expect(await screen.findByText(`mounted:${PROJECT_ID}`)).toBeInTheDocument();
  });
});

describe("toCreateProjectBody", () => {
  it("trims both fields and leaves an empty description out", () => {
    expect(toCreateProjectBody({ name: " Chatbot ", description: "  " })).toEqual({
      name: "Chatbot",
    });
    expect(toCreateProjectBody({ name: "Chatbot" })).toEqual({ name: "Chatbot" });
    expect(toCreateProjectBody({ name: "Chatbot", description: " Support bot " })).toEqual({
      name: "Chatbot",
      description: "Support bot",
    });
  });
});
