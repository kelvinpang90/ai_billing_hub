import { apiGet } from "./client";

export interface InternalUsageEvent {
  id: string;
  occurred_at: string;
  model: string;
  status: string;
  estimated_provider_cost_myr: string | null;
  reference_customer_price: string | null;
  billable_cost: string | null;
}

interface UsagePage {
  items: InternalUsageEvent[];
  total: number;
}

export function recentInternalUsage(customerId: string, signal?: AbortSignal): Promise<UsagePage> {
  const query = new URLSearchParams({ customer_id: customerId, page: "1", page_size: "20" });
  return apiGet<UsagePage>(`/api/v1/admin/usage-events?${query.toString()}`, signal);
}
