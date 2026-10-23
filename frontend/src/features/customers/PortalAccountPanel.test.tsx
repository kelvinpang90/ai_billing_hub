/**
 * 门户账号面板：各状态下出现哪些字段与按钮，各个写操作发出去的请求、成功后重读，以及各个 409 的文案。
 *
 * 按钮矩阵是这里最要紧的一条：多给一个按钮，管理员点下去得到的是一句 409；少给一个，就没有入口。
 * 规则全在后端，面板只按 docs/api.md 的状态给入口。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import "../../i18n";
import type { AccountStatus } from "../../api/adminCustomers";
import { ApiError } from "../../api/client";
import type { PortalAccount } from "../../api/portalAccounts";
import { PortalAccountPanel } from "./PortalAccountPanel";

const CUSTOMER_ID = "00000000-0000-0000-0000-000000000000";

const api = vi.hoisted(() => ({
  getPortalAccount: vi.fn(),
  createPortalAccount: vi.fn(),
  resendInvitation: vi.fn(),
  disablePortalAccount: vi.fn(),
  enablePortalAccount: vi.fn(),
  resetPortalTwoFactor: vi.fn(),
}));

vi.mock("../../api/portalAccounts", async () => {
  const actual =
    await vi.importActual<typeof import("../../api/portalAccounts")>("../../api/portalAccounts");
  return { ...actual, ...api };
});

const BUTTONS = {
  resend: "Resend invitation",
  disable: "Disable portal access",
  enable: "Enable portal access",
  reset2fa: "Reset 2FA",
  create: "Create portal account",
};

function account(overrides: Partial<PortalAccount> = {}): PortalAccount {
  return {
    email: "portal@example.com",
    status: "INVITED",
    invited_at: "2026-10-23T08:00:00",
    invitation_expires_at: "2026-10-26T08:00:00",
    activated_at: null,
    last_login_at: null,
    two_factor_enabled: false,
    ...overrides,
  };
}

function renderPanel(accountStatus: AccountStatus = "ENABLED") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <PortalAccountPanel
        customer={{ id: CUSTOMER_ID, email: "ops@example.com", account_status: accountStatus }}
      />
    </QueryClientProvider>,
  );
}

/** 面板里带边框的 Descriptions：label 在 <th>，值在紧跟的 <td>。 */
function valueOf(label: string): HTMLElement {
  const cell = screen.getByText(label).closest("th")?.nextElementSibling;
  if (!(cell instanceof HTMLElement)) {
    throw new Error(`${label} has no value cell`);
  }
  return cell;
}

function shownButtons(): string[] {
  return Object.values(BUTTONS).filter(
    (name) => screen.queryByRole("button", { name }) !== null,
  );
}

/** 等面板落定：已开通时等状态一项出现，未开通时等那句说明出现。 */
async function settled(provisioned: boolean): Promise<void> {
  if (provisioned) {
    await screen.findByText("Portal status");
  } else {
    await screen.findByText("This customer has no portal account yet.");
  }
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("PortalAccountPanel", () => {
  it("says it is loading while the portal account is being read", async () => {
    api.getPortalAccount.mockReturnValue(new Promise(() => undefined));

    renderPanel();

    expect(await screen.findByText("Loading the portal account…")).toBeInTheDocument();
    expect(api.getPortalAccount).toHaveBeenCalledWith(CUSTOMER_ID, expect.anything());
  });

  it("shows the failure with its request id and reads again on retry", async () => {
    const user = userEvent.setup();
    api.getPortalAccount
      .mockRejectedValueOnce(
        new ApiError("DATABASE_NOT_CONFIGURED", "The database is not configured.", "req-503"),
      )
      .mockResolvedValue(null);

    renderPanel();

    expect(await screen.findByText("The portal account could not be loaded.")).toBeInTheDocument();
    expect(screen.getByText("req-503")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("This customer has no portal account yet.")).toBeInTheDocument();
    expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
  });

  it("offers to create the account with the contact email when there is none", async () => {
    api.getPortalAccount.mockResolvedValue(null);

    renderPanel();

    await settled(false);
    expect(screen.getByRole("region", { name: "Customer portal account" })).toBeInTheDocument();
    expect(screen.getByLabelText("Login email")).toHaveValue("ops@example.com");
    expect(shownButtons()).toEqual([BUTTONS.create]);
  });

  it("shows every field of an invited account", async () => {
    api.getPortalAccount.mockResolvedValue(account());

    renderPanel();

    await settled(true);
    expect(valueOf("Login email")).toHaveTextContent("portal@example.com");
    expect(valueOf("Portal status")).toHaveTextContent("Invited");
    expect(valueOf("Invited at")).toHaveTextContent("2026-10-23 16:00:00");
    expect(valueOf("Invitation expires at")).toHaveTextContent("2026-10-26 16:00:00");
    expect(valueOf("Activated at")).toHaveTextContent("—");
    expect(valueOf("Last login")).toHaveTextContent("—");
    expect(valueOf("Two-factor authentication")).toHaveTextContent("Off");
    // 已开通就没有开通表单。
    expect(screen.queryByLabelText("Login email")).not.toBeInTheDocument();
  });

  it("says the invitation needs resending when an invited account has no valid one", async () => {
    api.getPortalAccount.mockResolvedValue(account({ invitation_expires_at: null }));

    renderPanel();

    await settled(true);
    expect(valueOf("Invitation expires at")).toHaveTextContent(
      "No valid invitation (expired or voided). Resend the invitation.",
    );
  });

  it("shows the activation, last login and 2FA of an active account", async () => {
    api.getPortalAccount.mockResolvedValue(
      account({
        status: "ACTIVE",
        invitation_expires_at: null,
        activated_at: "2026-10-24T01:00:00",
        last_login_at: "2026-10-25T02:30:00",
        two_factor_enabled: true,
      }),
    );

    renderPanel();

    await settled(true);
    expect(valueOf("Portal status")).toHaveTextContent("Active");
    expect(valueOf("Invitation expires at")).toHaveTextContent("—");
    expect(valueOf("Activated at")).toHaveTextContent("2026-10-24 09:00:00");
    expect(valueOf("Last login")).toHaveTextContent("2026-10-25 10:30:00");
    expect(valueOf("Two-factor authentication")).toHaveTextContent("On");
  });

  it.each([
    { status: "INVITED", twoFactor: false, buttons: [BUTTONS.resend, BUTTONS.disable] },
    { status: "ACTIVE", twoFactor: false, buttons: [BUTTONS.disable] },
    { status: "ACTIVE", twoFactor: true, buttons: [BUTTONS.disable, BUTTONS.reset2fa] },
    { status: "DISABLED", twoFactor: false, buttons: [BUTTONS.enable] },
    { status: "DISABLED", twoFactor: true, buttons: [BUTTONS.enable, BUTTONS.reset2fa] },
  ] as const)(
    "offers exactly $buttons for $status (2FA $twoFactor)",
    async ({ status, twoFactor, buttons }) => {
      api.getPortalAccount.mockResolvedValue(account({ status, two_factor_enabled: twoFactor }));

      renderPanel();

      await settled(true);
      expect(valueOf("Portal status")).toHaveTextContent(
        { INVITED: "Invited", ACTIVE: "Active", DISABLED: "Disabled" }[status],
      );
      expect(shownButtons()).toEqual(buttons);
    },
  );

  it.each([
    { name: "no account", value: null, buttons: [] },
    { name: "INVITED", value: account(), buttons: [BUTTONS.disable] },
    { name: "ACTIVE", value: account({ status: "ACTIVE" }), buttons: [BUTTONS.disable] },
    { name: "DISABLED", value: account({ status: "DISABLED" }), buttons: [] },
    {
      name: "DISABLED with 2FA",
      value: account({ status: "DISABLED", two_factor_enabled: true }),
      buttons: [BUTTONS.reset2fa],
    },
  ])(
    "does not offer create, resend or enable for a closed customer ($name)",
    async ({ value, buttons }) => {
      api.getPortalAccount.mockResolvedValue(value);

      renderPanel("CLOSED");

      await settled(value !== null);
      expect(
        screen.getByText(
          "The customer account is closed. Portal access can still be disabled and 2FA reset, but it cannot be created, re-invited or enabled.",
        ),
      ).toBeInTheDocument();
      expect(shownButtons()).toEqual(buttons);
      expect(screen.queryByLabelText("Login email")).not.toBeInTheDocument();
    },
  );

  it("creates the account with the contact email as it is and refreshes the panel", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValueOnce(null).mockResolvedValue(account({ email: "ops@example.com" }));
    api.createPortalAccount.mockResolvedValue(account({ email: "ops@example.com" }));
    renderPanel();

    await settled(false);
    await user.click(screen.getByRole("button", { name: BUTTONS.create }));

    await waitFor(() => {
      expect(api.createPortalAccount).toHaveBeenCalledWith(CUSTOMER_ID, "ops@example.com");
    });
    expect(
      await screen.findByText("Portal account created. The invitation has been sent."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("Portal status")).toBeInTheDocument();
    expect(api.createPortalAccount).toHaveBeenCalledTimes(1);
  });

  it("creates the account with an edited login email", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValueOnce(null).mockResolvedValue(account());
    api.createPortalAccount.mockResolvedValue(account());
    renderPanel();

    await settled(false);
    const email = screen.getByLabelText("Login email");
    await user.clear(email);
    await user.click(email);
    await user.paste("portal@example.com");
    await user.click(screen.getByRole("button", { name: BUTTONS.create }));

    await waitFor(() => {
      expect(api.createPortalAccount).toHaveBeenCalledWith(CUSTOMER_ID, "portal@example.com");
    });
    await waitFor(() => {
      expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
    });
  });

  it("resends with no email when the login email is left as it is", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account());
    api.resendInvitation.mockResolvedValue(account());
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.resend }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByLabelText("Login email")).toHaveValue("portal@example.com");
    await user.click(within(dialog).getByRole("button", { name: "Send new invitation" }));

    await waitFor(() => {
      expect(api.resendInvitation).toHaveBeenCalledWith(CUSTOMER_ID);
    });
    expect(
      await screen.findByText("A new invitation has been sent. Earlier links no longer work."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("resends to a corrected login email", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account());
    api.resendInvitation.mockResolvedValue(account({ email: "corrected@example.com" }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.resend }));
    const dialog = await screen.findByRole("dialog");
    const email = within(dialog).getByLabelText("Login email");
    await user.clear(email);
    await user.click(email);
    await user.paste("corrected@example.com");
    await user.click(within(dialog).getByRole("button", { name: "Send new invitation" }));

    await waitFor(() => {
      expect(api.resendInvitation).toHaveBeenCalledWith(CUSTOMER_ID, "corrected@example.com");
    });
    await waitFor(() => {
      expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
    });
  });

  it("disables only after confirmation and refreshes the panel", async () => {
    const user = userEvent.setup();
    api.getPortalAccount
      .mockResolvedValueOnce(account({ status: "ACTIVE" }))
      .mockResolvedValue(account({ status: "DISABLED" }));
    api.disablePortalAccount.mockResolvedValue(account({ status: "DISABLED" }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.disable }));
    const dialog = await screen.findByRole("dialog");
    expect(api.disablePortalAccount).not.toHaveBeenCalled();
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm and disable portal access" }),
    );

    await waitFor(() => {
      expect(api.disablePortalAccount).toHaveBeenCalledWith(CUSTOMER_ID);
    });
    expect(await screen.findByText("Portal access disabled.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: BUTTONS.enable })).toBeInTheDocument();
    expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
  });

  it("sends nothing when the disable confirmation is cancelled", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account({ status: "ACTIVE" }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.disable }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(api.disablePortalAccount).not.toHaveBeenCalled();
  });

  it("enables only after confirmation and refreshes the panel", async () => {
    const user = userEvent.setup();
    api.getPortalAccount
      .mockResolvedValueOnce(account({ status: "DISABLED" }))
      .mockResolvedValue(account({ status: "ACTIVE" }));
    api.enablePortalAccount.mockResolvedValue(account({ status: "ACTIVE" }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.enable }));
    const dialog = await screen.findByRole("dialog");
    expect(api.enablePortalAccount).not.toHaveBeenCalled();
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm and enable portal access" }),
    );

    await waitFor(() => {
      expect(api.enablePortalAccount).toHaveBeenCalledWith(CUSTOMER_ID);
    });
    expect(
      await screen.findByText(
        "Portal access enabled. Resend the invitation if the customer still needs one.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: BUTTONS.disable })).toBeInTheDocument();
    expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
  });

  it("asks for offline verification and a reason before resetting 2FA", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account({ status: "ACTIVE", two_factor_enabled: true }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.reset2fa }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText("Verify offline first that you are talking to this customer."),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Confirm and reset 2FA" }));

    expect(await within(dialog).findByText("Enter a reason.")).toBeInTheDocument();
    expect(api.resetPortalTwoFactor).not.toHaveBeenCalled();
  });

  it("does not accept a reason of only spaces or longer than 255 characters", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account({ status: "ACTIVE", two_factor_enabled: true }));
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.reset2fa }));
    const dialog = await screen.findByRole("dialog");
    const reason = within(dialog).getByLabelText("Reason");
    await user.click(reason);
    await user.paste("   ");
    await user.click(within(dialog).getByRole("button", { name: "Confirm and reset 2FA" }));
    expect(await within(dialog).findByText("Enter a reason.")).toBeInTheDocument();

    await user.clear(reason);
    await user.click(reason);
    await user.paste("a".repeat(256));
    await user.click(within(dialog).getByRole("button", { name: "Confirm and reset 2FA" }));
    expect(
      await within(dialog).findByText("The reason can be at most 255 characters."),
    ).toBeInTheDocument();
    expect(api.resetPortalTwoFactor).not.toHaveBeenCalled();
  });

  it("resets 2FA with a 255-character reason and refreshes the panel", async () => {
    const user = userEvent.setup();
    const longest = "a".repeat(255);
    api.getPortalAccount
      .mockResolvedValueOnce(account({ status: "ACTIVE", two_factor_enabled: true }))
      .mockResolvedValue(account({ status: "ACTIVE", two_factor_enabled: false }));
    api.resetPortalTwoFactor.mockResolvedValue(
      account({ status: "ACTIVE", two_factor_enabled: false }),
    );
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.reset2fa }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Reason"));
    await user.paste(` ${longest} `);
    await user.click(within(dialog).getByRole("button", { name: "Confirm and reset 2FA" }));

    await waitFor(() => {
      expect(api.resetPortalTwoFactor).toHaveBeenCalledWith(CUSTOMER_ID, longest);
    });
    expect(
      await screen.findByText(
        "Two-factor authentication has been reset. The customer can sign in with their password and turn it on again.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: BUTTONS.reset2fa })).not.toBeInTheDocument();
    });
    expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
  });

  type Operation = "create" | "resend" | "disable" | "enable" | "reset2fa";

  const CONFIRM: Record<Exclude<Operation, "create">, string> = {
    resend: "Send new invitation",
    disable: "Confirm and disable portal access",
    enable: "Confirm and enable portal access",
    reset2fa: "Confirm and reset 2FA",
  };

  const MOCKS = {
    create: api.createPortalAccount,
    resend: api.resendInvitation,
    disable: api.disablePortalAccount,
    enable: api.enablePortalAccount,
    reset2fa: api.resetPortalTwoFactor,
  };

  it.each([
    {
      operation: "create",
      start: null,
      code: "PORTAL_ACCOUNT_EXISTS",
      text: "This customer already has a portal account. The panel has been refreshed.",
    },
    {
      operation: "create",
      start: null,
      code: "EMAIL_IN_USE",
      text: "This email address is already used by another user. Choose a different one.",
    },
    {
      operation: "create",
      start: null,
      code: "ACCOUNT_CLOSED",
      text: "The customer account is closed: portal access cannot be created, re-invited or enabled.",
    },
    {
      operation: "resend",
      start: account(),
      code: "PORTAL_ACCOUNT_NOT_INVITED",
      text: "The invitation can only be resent while the account is invited. The panel has been refreshed.",
    },
    {
      operation: "resend",
      start: account(),
      code: "EMAIL_IN_USE",
      text: "This email address is already used by another user. Choose a different one.",
    },
    {
      operation: "resend",
      start: account(),
      code: "ACCOUNT_CLOSED",
      text: "The customer account is closed: portal access cannot be created, re-invited or enabled.",
    },
    {
      operation: "disable",
      start: account({ status: "ACTIVE" }),
      code: "PORTAL_ACCOUNT_ALREADY_DISABLED",
      text: "Portal access is already disabled. The panel has been refreshed.",
    },
    {
      operation: "enable",
      start: account({ status: "DISABLED" }),
      code: "PORTAL_ACCOUNT_NOT_DISABLED",
      text: "Portal access is not disabled, so there is nothing to enable. The panel has been refreshed.",
    },
    {
      operation: "enable",
      start: account({ status: "DISABLED" }),
      code: "ACCOUNT_CLOSED",
      text: "The customer account is closed: portal access cannot be created, re-invited or enabled.",
    },
    {
      operation: "reset2fa",
      start: account({ status: "ACTIVE", two_factor_enabled: true }),
      code: "PORTAL_2FA_NOT_ENABLED",
      text: "This portal account has no two-factor authentication to reset. The panel has been refreshed.",
    },
  ] as const)(
    "explains 409 $code from $operation with its request id and refreshes the panel",
    async ({ operation, start, code, text }) => {
      const user = userEvent.setup();
      // 409 之后重读：开通撞上 PORTAL_ACCOUNT_EXISTS 时，重读得到的就是已有的账号。
      api.getPortalAccount
        .mockResolvedValueOnce(start)
        .mockResolvedValue(code === "PORTAL_ACCOUNT_EXISTS" ? account() : start);
      MOCKS[operation].mockRejectedValue(new ApiError(code, "Conflict.", "req-409"));
      renderPanel();

      await settled(start !== null);
      if (operation === "create") {
        await user.click(screen.getByRole("button", { name: BUTTONS.create }));
      } else {
        await user.click(screen.getByRole("button", { name: BUTTONS[operation] }));
        const dialog = await screen.findByRole("dialog");
        if (operation === "reset2fa") {
          await user.click(within(dialog).getByLabelText("Reason"));
          await user.paste("Lost phone; identity confirmed by call-back.");
        }
        await user.click(within(dialog).getByRole("button", { name: CONFIRM[operation] }));
      }

      expect(await screen.findByText(text)).toBeInTheDocument();
      expect(screen.getByText("req-409")).toBeInTheDocument();
      await waitFor(() => {
        expect(api.getPortalAccount).toHaveBeenCalledTimes(2);
      });
      // 重读之后提示仍在。
      expect(screen.getByText(text)).toBeInTheDocument();
    },
  );

  it("shows the backend message and request id for an error without its own text", async () => {
    const user = userEvent.setup();
    api.getPortalAccount.mockResolvedValue(account({ status: "ACTIVE" }));
    api.disablePortalAccount.mockRejectedValue(
      new ApiError("DATABASE_NOT_CONFIGURED", "The database is not configured.", "req-503"),
    );
    renderPanel();

    await settled(true);
    await user.click(screen.getByRole("button", { name: BUTTONS.disable }));
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm and disable portal access" }),
    );

    expect(await within(dialog).findByText("Portal access was not disabled.")).toBeInTheDocument();
    expect(within(dialog).getByText(/The database is not configured\./)).toBeInTheDocument();
    expect(within(dialog).getByText("req-503")).toBeInTheDocument();
    // 不是 409：不重读。
    expect(api.getPortalAccount).toHaveBeenCalledTimes(1);
  });
});
