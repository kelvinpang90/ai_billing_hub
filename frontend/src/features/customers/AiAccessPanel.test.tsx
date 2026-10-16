import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { Project } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import { AiAccessPanel } from "./AiAccessPanel";

const ID = "00000000-0000-0000-0000-000000000000";
const CLIENT_ID = `aic_${"0".repeat(32)}`;
const api = vi.hoisted(() => ({ listAiClientKeys: vi.fn(), issueAiClientKey: vi.fn(), revokeAiClientKey: vi.fn() }));
vi.mock("../../api/aiAccess", async () => ({ ...(await vi.importActual<typeof import("../../api/aiAccess")>("../../api/aiAccess")), ...api }));

const project: Project = { id: ID, name: "AI", description: null, created_at: "2026-10-16T00:00:00", updated_at: "2026-10-16T00:00:00" };
const key = { client_id: CLIENT_ID, status: "ACTIVE" as const, created_at: "2026-10-16T00:00:00", revoked_at: null };

function renderPanel() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AiAccessPanel customerId={ID} project={project} /></QueryClientProvider>);
}

beforeEach(() => vi.clearAllMocks());

describe("AiAccessPanel", () => {
  it("draws loading and failure states with the request reference", async () => {
    api.listAiClientKeys.mockReturnValueOnce(new Promise(() => undefined));
    const first = renderPanel();
    expect(screen.getByText("Loading AI client keys…")).toBeInTheDocument();
    first.unmount();
    api.listAiClientKeys.mockRejectedValue(new ApiError("X", "Backend failed.", "req-500"));
    renderPanel();
    expect(await screen.findByText("The AI client keys could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-500")).toBeInTheDocument();
  });

  it("shows an issued token once and removes it after closing", async () => {
    const user = userEvent.setup();
    api.listAiClientKeys.mockResolvedValue([key]);
    api.issueAiClientKey.mockResolvedValue({ ...key, token: "not-a-real-token" });
    renderPanel();
    await screen.findByText(CLIENT_ID);
    await user.click(screen.getByRole("button", { name: "Issue AI client key" }));
    expect(await screen.findByText("not-a-real-token")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "I have copied the token" }));
    expect(screen.queryByText("not-a-real-token")).not.toBeInTheDocument();
  });

  it("confirms revocation and shows a specialized 409 with request id", async () => {
    const user = userEvent.setup();
    api.listAiClientKeys.mockResolvedValue([key]);
    api.revokeAiClientKey.mockRejectedValue(new ApiError("AI_CLIENT_KEY_REVOKED", "Already revoked.", "req-409"));
    renderPanel();
    await screen.findByText(CLIENT_ID);
    await user.click(screen.getByRole("button", { name: "Revoke" }));
    expect(screen.getByText("Revoke this AI client key?")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Confirm and revoke" }));
    expect(await screen.findByText("This AI client key was already revoked. The list has been refreshed.")).toBeInTheDocument();
    expect(screen.getByText("req-409")).toBeInTheDocument();
  });
});
