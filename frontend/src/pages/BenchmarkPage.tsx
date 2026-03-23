import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createBenchmark, type BenchmarkResponse } from '../api/client'
import BenchmarkResults from '../components/BenchmarkResults'

const INDUSTRIES = [
  'Technology', 'Healthcare', 'Finance', 'Manufacturing', 'Retail',
  'Construction', 'Education', 'Professional Services', 'Hospitality',
  'Transportation', 'Real Estate', 'Nonprofit', 'Government', 'Other',
]

const STATES = [
  'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA',
  'KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ',
  'NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT',
  'VA','WA','WV','WI','WY','DC',
]

export default function BenchmarkPage() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<BenchmarkResponse | null>(null)

  const [form, setForm] = useState({
    employee_count: '',
    state: '',
    industry: '',
    annual_spend: '',
  })

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const res = await createBenchmark({
        employee_count: parseInt(form.employee_count),
        state: form.state,
        industry: form.industry,
        annual_spend: form.annual_spend ? parseFloat(form.annual_spend) : null,
      })
      setResult(res)
      // Update URL to shareable link without full navigation
      window.history.pushState({}, '', `/benchmark/${res.query_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong')
    } finally {
      setLoading(false)
    }
  }

  if (result) {
    return <BenchmarkResults result={result} onReset={() => { setResult(null); navigate('/') }} />
  }

  return (
    <div className="max-w-3xl mx-auto px-4 py-16">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold text-gray-900 mb-4">
          What are your benefits actually costing you?
        </h1>
        <p className="text-lg text-gray-600 max-w-2xl mx-auto">
          See a complete breakdown of where every dollar goes — and what your employees'
          experience could look like. Free. No login. Takes 30 seconds.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="bg-white rounded-xl shadow-sm border border-gray-200 p-8">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label htmlFor="employee_count" className="block text-sm font-medium text-gray-700 mb-1">
              Number of employees
            </label>
            <input
              id="employee_count"
              type="number"
              required
              min={1}
              max={100000}
              placeholder="e.g. 150"
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 focus:ring-2 focus:ring-primary-500 focus:border-primary-500 outline-none"
              value={form.employee_count}
              onChange={e => setForm(f => ({ ...f, employee_count: e.target.value }))}
            />
          </div>

          <div>
            <label htmlFor="state" className="block text-sm font-medium text-gray-700 mb-1">
              Primary state
            </label>
            <select
              id="state"
              required
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 focus:ring-2 focus:ring-primary-500 focus:border-primary-500 outline-none"
              value={form.state}
              onChange={e => setForm(f => ({ ...f, state: e.target.value }))}
            >
              <option value="">Select state</option>
              {STATES.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>

          <div>
            <label htmlFor="industry" className="block text-sm font-medium text-gray-700 mb-1">
              Industry
            </label>
            <select
              id="industry"
              required
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 focus:ring-2 focus:ring-primary-500 focus:border-primary-500 outline-none"
              value={form.industry}
              onChange={e => setForm(f => ({ ...f, industry: e.target.value }))}
            >
              <option value="">Select industry</option>
              {INDUSTRIES.map(i => <option key={i} value={i}>{i}</option>)}
            </select>
          </div>

          <div>
            <label htmlFor="annual_spend" className="block text-sm font-medium text-gray-700 mb-1">
              Approximate annual benefits spend ($)
              <span className="text-gray-400 font-normal"> — optional</span>
            </label>
            <input
              id="annual_spend"
              type="number"
              min={0}
              placeholder="e.g. 1500000"
              className="w-full border border-gray-300 rounded-lg px-4 py-2.5 focus:ring-2 focus:ring-primary-500 focus:border-primary-500 outline-none"
              value={form.annual_spend}
              onChange={e => setForm(f => ({ ...f, annual_spend: e.target.value }))}
            />
          </div>
        </div>

        {error && (
          <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          className="mt-8 w-full bg-primary-600 text-white py-3 rounded-lg font-semibold text-lg hover:bg-primary-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {loading ? 'Analyzing...' : 'See your benchmark'}
        </button>

        <p className="mt-4 text-center text-xs text-gray-400">
          No data is stored with your identity. Results are shareable via URL.
        </p>
      </form>
    </div>
  )
}
