import { useEffect, useState } from 'react'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1'

// Constitution F6B: "Every employer can see every line item. Every determination
// is auditable. Every price is verifiable. The dashboard is a window, not a wall."

interface DashboardData {
  employer_name: string
  employee_count: number
  spending: {
    total_paid: number
    total_claims: number
    employee_out_of_pocket: number
    by_benefit_type: Record<string, { total_paid: number; claim_count: number; pepm: number }>
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  savings: Record<string, any>
  clinical_rates: {
    total_claims: number
    approval_rate_pct: number
    auto_adjudication_rate_pct: number
  }
  care_execution: {
    total_episodes_managed: number
    appointments_scheduled: number
    employee_actions_per_episode: number
    zero_phone_calls_pct: number
  }
  employee_experience: {
    avg_employee_oop_per_year: number
    zero_cost_sharing: boolean
    benefit_types_covered: number
  }
  network_effect: {
    platform_employers: number
    platform_employees: number
    cost_improvement_from_data: string
  }
  transparency_attestation: Record<string, boolean>
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState('')
  const [employerId] = useState('11111111-1111-1111-1111-111111111111')

  useEffect(() => {
    fetch(`${API}/dashboard/${employerId}`)
      .then(r => r.json())
      .then(setData)
      .catch(e => setError(e.message))
  }, [employerId])

  if (error) return <div className="p-8 text-red-600">Error: {error}</div>
  if (!data) return <div className="p-8 text-gray-500">Loading dashboard...</div>

  const bt = data.spending.by_benefit_type || {}

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      <div className="flex justify-between items-center mb-8">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{data.employer_name}</h1>
          <p className="text-gray-500">{data.employee_count} employees</p>
        </div>
        <div className="text-right">
          <span className="inline-block bg-green-100 text-green-800 text-sm font-medium px-3 py-1 rounded-full">
            Live
          </span>
        </div>
      </div>

      {/* Key metrics row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <Card title="Total Paid" value={`$${data.spending.total_paid.toLocaleString()}`} />
        <Card title="Employee OOP" value={`$${data.spending.employee_out_of_pocket}`} accent="green" />
        <Card title="Claims" value={data.spending.total_claims.toString()} />
        <Card title="Auto-Adjudicated" value={`${data.clinical_rates.auto_adjudication_rate_pct}%`} />
      </div>

      {/* Spending by benefit type */}
      <Section title="Spending by Benefit Type">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {Object.entries(bt).map(([type, info]) => (
            <div key={type} className="bg-gray-50 rounded-lg p-4">
              <p className="text-xs text-gray-500 uppercase tracking-wide">{type.replace('_', ' ')}</p>
              <p className="text-lg font-semibold">${(info as Record<string, number>).total_paid?.toLocaleString()}</p>
              <p className="text-xs text-gray-400">{(info as Record<string, number>).claim_count} claims &middot; ${(info as Record<string, number>).pepm}/PEPM</p>
            </div>
          ))}
        </div>
      </Section>

      {/* Care Execution */}
      <Section title="Care Execution">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card title="Episodes Managed" value={data.care_execution.total_episodes_managed.toString()} />
          <Card title="Appointments Scheduled" value={data.care_execution.appointments_scheduled.toString()} />
          <Card title="Employee Actions/Episode" value={data.care_execution.employee_actions_per_episode.toString()} accent="green" />
          <Card title="Zero Phone Calls" value={`${data.care_execution.zero_phone_calls_pct}%`} accent="green" />
        </div>
      </Section>

      {/* Employee Experience */}
      <Section title="Employee Experience">
        <div className="grid grid-cols-3 gap-4">
          <Card title="Annual OOP" value={`$${data.employee_experience.avg_employee_oop_per_year}`} accent="green" />
          <Card title="Benefit Types" value={data.employee_experience.benefit_types_covered.toString()} />
          <Card title="Zero Cost-Sharing" value={data.employee_experience.zero_cost_sharing ? 'Yes' : 'No'} accent="green" />
        </div>
      </Section>

      {/* Network Effect */}
      <Section title="Platform Network Effect">
        <div className="grid grid-cols-3 gap-4">
          <Card title="Platform Employers" value={data.network_effect.platform_employers.toString()} />
          <Card title="Platform Employees" value={data.network_effect.platform_employees.toString()} />
          <div className="bg-blue-50 rounded-lg p-4 col-span-3 md:col-span-1">
            <p className="text-xs text-blue-600 font-medium">Data Advantage</p>
            <p className="text-sm text-blue-800 mt-1">{data.network_effect.cost_improvement_from_data}</p>
          </div>
        </div>
      </Section>

      {/* Transparency */}
      <Section title="Transparency Attestation">
        <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
          {Object.entries(data.transparency_attestation).map(([key, val]) => (
            <div key={key} className="flex items-center gap-2">
              <span className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${val ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                {val ? '✓' : '✗'}
              </span>
              <span className="text-sm text-gray-700">{key.replace(/_/g, ' ')}</span>
            </div>
          ))}
        </div>
      </Section>
    </div>
  )
}

function Card({ title, value, accent }: { title: string; value: string; accent?: string }) {
  const colors = accent === 'green' ? 'text-green-700' : 'text-gray-900'
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5">
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">{title}</p>
      <p className={`text-xl font-bold ${colors}`}>{value}</p>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-8">
      <h2 className="text-lg font-semibold text-gray-900 mb-4">{title}</h2>
      {children}
    </div>
  )
}
