import { useState } from 'react'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000/api/v1'

// Constitution F10: "The benchmark tool is designed to be independently useful
// as a broker advisory tool: comprehensive, accurate, free, zero-login."

export default function BrokerPage() {
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [form, setForm] = useState({
    employee_count: '',
    current_spend_pepm: '',
    state: '',
    industry: '',
  })

  const handleModelSavings = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    try {
      const resp = await fetch(`${API}/broker/model-savings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          employer_id: 'new',
          employee_count: parseInt(form.employee_count),
          current_spend_pepm: parseFloat(form.current_spend_pepm),
        }),
      })
      setResult(await resp.json())
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="max-w-4xl mx-auto px-4 py-16">
      <div className="text-center mb-12">
        <h1 className="text-3xl font-bold text-gray-900 mb-3">Broker Advisory Tools</h1>
        <p className="text-gray-600">
          Free analytical tools for benefits brokers and consultants.
          No login. No commitment. Model savings for any employer.
        </p>
      </div>

      <form onSubmit={handleModelSavings} className="bg-white rounded-xl shadow-sm border p-8 mb-8">
        <h2 className="text-lg font-semibold mb-4">Model Employer Savings</h2>
        <div className="grid grid-cols-2 gap-4 mb-6">
          <div>
            <label className="block text-sm text-gray-600 mb-1">Employees</label>
            <input type="number" required min={1} placeholder="100"
              className="w-full border rounded-lg px-3 py-2"
              value={form.employee_count}
              onChange={e => setForm(f => ({ ...f, employee_count: e.target.value }))} />
          </div>
          <div>
            <label className="block text-sm text-gray-600 mb-1">Current PEPM ($)</label>
            <input type="number" required min={1} placeholder="800"
              className="w-full border rounded-lg px-3 py-2"
              value={form.current_spend_pepm}
              onChange={e => setForm(f => ({ ...f, current_spend_pepm: e.target.value }))} />
          </div>
        </div>
        <button type="submit" disabled={loading}
          className="w-full bg-primary-600 text-white py-3 rounded-lg font-semibold hover:bg-primary-700 disabled:opacity-50">
          {loading ? 'Modeling...' : 'Model Savings'}
        </button>
      </form>

      {result && (
        <div className="bg-white rounded-xl shadow-sm border p-8">
          <h2 className="text-lg font-semibold mb-4">Projected Savings</h2>
          {result.savings_summary && (
            <div className="grid grid-cols-3 gap-4 mb-6">
              <div className="bg-green-50 rounded-lg p-4 text-center">
                <p className="text-sm text-green-600">Annual Savings</p>
                <p className="text-2xl font-bold text-green-700">
                  ${result.savings_summary.total_annual_savings?.toLocaleString() || '—'}
                </p>
              </div>
              <div className="bg-blue-50 rounded-lg p-4 text-center">
                <p className="text-sm text-blue-600">Savings %</p>
                <p className="text-2xl font-bold text-blue-700">
                  {result.savings_summary.savings_pct || '—'}%
                </p>
              </div>
              <div className="bg-purple-50 rounded-lg p-4 text-center">
                <p className="text-sm text-purple-600">Employee OOP</p>
                <p className="text-2xl font-bold text-purple-700">$0</p>
              </div>
            </div>
          )}

          <h3 className="text-sm font-medium text-gray-500 mb-2 uppercase tracking-wide">
            Constitutional Guarantees
          </h3>
          <ul className="space-y-1 text-sm text-gray-600">
            <li>✓ Zero employee out-of-pocket on all covered services</li>
            <li>✓ Every line item auditable by employer</li>
            <li>✓ Clinical decisions made by TEE — zero financial influence</li>
            <li>✓ Company revenue = 0 if savings = 0 (value-share only)</li>
            <li>✓ Broker fee from value-share revenue, fully disclosed</li>
            <li>✓ All 7 benefit types from day one</li>
          </ul>
        </div>
      )}
    </div>
  )
}
