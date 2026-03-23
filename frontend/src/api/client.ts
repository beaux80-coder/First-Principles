const API_BASE = import.meta.env.VITE_API_URL || ''

export async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!res.ok) {
    const error = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(error.detail || `API error: ${res.status}`)
  }
  return res.json()
}

export interface BenchmarkRequest {
  employee_count: number
  state: string
  industry: string
  annual_spend: number | null
}

export interface BenchmarkResponse {
  query_id: string
  inputs: Record<string, unknown>
  current_cost: {
    total_annual: number
    breakdown: Record<string, number>
    pepm: number
  }
  system_cost: {
    total_annual: number
    breakdown: {
      pass_through: Record<string, number>
      value_share_fee: number
      value_share_pct: number
    }
    pepm: number
  }
  comparison: {
    annual_savings: number
    savings_pct: number
    cost_ratio: number
  }
  experience_comparison: {
    current: Record<string, unknown>
    system: Record<string, unknown>
  }
  transparency: Record<string, string>
  data_quality: Record<string, unknown>
  share_url: string
}

export function createBenchmark(data: BenchmarkRequest): Promise<BenchmarkResponse> {
  return apiFetch('/api/v1/benchmark/', {
    method: 'POST',
    body: JSON.stringify(data),
  })
}

export function getBenchmark(queryId: string): Promise<BenchmarkResponse> {
  return apiFetch(`/api/v1/benchmark/${queryId}`)
}
