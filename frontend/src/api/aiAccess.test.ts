import { afterEach, describe, expect, it, vi } from "vitest";

import { client } from "./client";
import { issueAiClientKey, listAiClientKeys, revokeAiClientKey } from "./aiAccess";

const ID = "00000000-0000-0000-0000-000000000000";
const CLIENT_ID = `aic_${"0".repeat(32)}`;
const BASE = `/api/v1/admin/customers/${ID}/projects/${ID}/ai-access`;
const key = { client_id: CLIENT_ID, status: "ACTIVE", created_at: "2026-10-16T00:00:00", revoked_at: null };
const envelope = (data: unknown) => ({ data: { success: true, data, error: null, request_id: "req-1" } });

afterEach(() => vi.restoreAllMocks());

describe("AI access API", () => {
  it("lists all keys at the project access path", async () => {
    const get = vi.spyOn(client, "get").mockResolvedValue(envelope([key]));
    await expect(listAiClientKeys(ID, ID)).resolves.toEqual([key]);
    expect(get).toHaveBeenCalledWith(BASE, {});
  });

  it("issues a key without a request body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ ...key, token: "not-a-real-token" }));
    await issueAiClientKey(ID, ID);
    expect(post).toHaveBeenCalledWith(`${BASE}/client-keys`);
  });

  it("revokes the selected client id without a request body", async () => {
    const post = vi.spyOn(client, "post").mockResolvedValue(envelope({ ...key, status: "REVOKED" }));
    await revokeAiClientKey(ID, ID, CLIENT_ID);
    expect(post).toHaveBeenCalledWith(`${BASE}/client-keys/${CLIENT_ID}/revoke`);
  });
});
