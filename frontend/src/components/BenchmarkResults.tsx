import type { BenchmarkResponse } from '../api/client'

function formatCurrency(n: number): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n)
}

function formatPct(n: number): string {
  return `${n.toFixed(1)}%`
}

function benefitLabel(key: string): string {
  const labels: Record<string, string> = {
    health: 'Health',
    dental: 'Dental',
    vision: 'Vision',
    mental_health: 'Mental Health',
    life_insurance: 'Life Insurance',
    short_term_disability: 'Short-Term Disability',
    long_term_disability: 'Long-Term Disability',
  }
  return labels[key] || key
}

interface Props {
  result: BenchmarkResponse
  onReset: () => void
}

export default function BenchmarkResults({ result, onReset }: Props) {
  const r = result as any
  const { current_cost, system_cost, comparison, experience_comparison, transparency, data_quality } = r

  const handleShare = () => {
    navigator.clipboard.writeText(window.location.href)
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-12">
      {/* Hero savings banner */}
      <div className="bg-primary-700 text-white rounded-2xl p-8 mb-8 text-center">
        <p className="text-primary-200 text-sm uppercase tracking-wide mb-2">Estimated annual savings</p>
        <p className="text-5xl font-bold mb-2">{formatCurrency(comparison.annual_savings)}</p>
        <p className="text-primary-200 text-lg">
          {formatPct(comparison.savings_pct)} reduction &middot; {comparison.cost_ratio}x cost ratio
        </p>
        {comparison.admin_elimination > 0 && (
          <p className="text-primary-300 text-sm mt-2">
            Plus {formatCurrency(comparison.admin_elimination)}/year in eliminated administration costs
          </p>
        )}
      </div>

      {/* Cost comparison side-by-side */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        <div className="bg-white rounded-xl border border-gray-200 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-1">Your current estimated cost</h2>
          <p className="text-3xl font-bold text-gray-900 mb-4">{formatCurrency(current_cost.total_annual)}<span className="text-base font-normal text-gray-500">/year</span></p>
          <p className="text-sm text-gray-500 mb-4">{formatCurrency(current_cost.pepm)} per employee per month</p>
          <div className="space-y-2">
            {Object.entries(current_cost.breakdown).map(([key, value]) => (
              <div key={key} className="flex justify-between text-sm">
                <span className="text-gray-600">{key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</span>
                <span className="font-medium text-gray-900">{formatCurrency(value as number)}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="bg-white rounded-xl border-2 border-primary-500 p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-1">Under the system</h2>
          <p className="text-3xl font-bold text-primary-700 mb-4">{formatCurrency(system_cost.total_annual)}<span className="text-base font-normal text-gray-500">/year</span></p>
          <p className="text-sm text-gray-500 mb-4">{formatCurrency(system_cost.pepm)} per employee per month</p>
          <div className="space-y-2">
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">Care delivery (pass-through)</span>
              <span className="font-medium text-gray-900">{formatCurrency(system_cost.breakdown.pass_through.care_delivery)}</span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-gray-600">Stop-loss</span>
              <span className="font-medium text-gray-900">{formatCurrency(system_cost.breakdown.pass_through.stop_loss)}</span>
            </div>
            {['carrier_overhead', 'broker_commissions', 'waste', 'benefits_administration', 'state_premium_tax'].map(key => (
              <div key={key} className="flex justify-between text-sm">
                <span className="text-gray-600">{key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}</span>
                <span className="font-medium text-green-600">$0 — eliminated</span>
              </div>
            ))}
            <div className="border-t pt-2 mt-2 flex justify-between text-sm">
              <span className="text-gray-600">Value-share fee ({(system_cost.breakdown.value_share_pct * 100)}% of savings)</span>
              <span className="font-medium text-gray-900">{formatCurrency(system_cost.breakdown.value_share_fee)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Benefit type breakdown */}
      {r.benefit_type_breakdown && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">All benefit types — current vs system</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 pr-4 text-gray-500 font-medium">Benefit Type</th>
                  <th className="text-right py-2 px-2 text-gray-500 font-medium">Current</th>
                  <th className="text-right py-2 px-2 text-primary-700 font-medium">System</th>
                  <th className="text-right py-2 px-2 text-green-600 font-medium">Savings</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(r.benefit_type_breakdown).map(([key, bt]: [string, any]) => (
                  <tr key={key} className="border-b">
                    <td className="py-3 pr-4 text-gray-700 font-medium">{benefitLabel(key)}</td>
                    <td className="py-3 px-2 text-right text-gray-900">{formatCurrency(bt.current_annual)}</td>
                    <td className="py-3 px-2 text-right text-primary-700 font-semibold">{formatCurrency(bt.system_annual)}</td>
                    <td className="py-3 px-2 text-right text-green-600 font-semibold">{formatPct(bt.savings_pct)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Price examples */}
      {r.price_examples && r.price_examples.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-2">Real price comparisons for common services</h2>
          <p className="text-sm text-gray-500 mb-4">Hospital charges vs insurer negotiated rates vs Medicare — system always pays the lowest verified price</p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 pr-4 text-gray-500 font-medium">Service</th>
                  <th className="text-right py-2 px-2 text-gray-500 font-medium">Hospital</th>
                  <th className="text-right py-2 px-2 text-gray-500 font-medium">Insurer negotiated</th>
                  <th className="text-right py-2 px-2 text-gray-500 font-medium">Medicare</th>
                  <th className="text-right py-2 px-2 text-primary-700 font-medium">System pays</th>
                </tr>
              </thead>
              <tbody>
                {r.price_examples.map((ex: any) => (
                  <tr key={ex.code} className="border-b">
                    <td className="py-3 pr-4 text-gray-700">{ex.description}</td>
                    <td className="py-3 px-2 text-right text-gray-900">{ex.hospital_price ? formatCurrency(ex.hospital_price) : '—'}</td>
                    <td className="py-3 px-2 text-right text-gray-900">{ex.insurer_negotiated ? formatCurrency(ex.insurer_negotiated) : '—'}</td>
                    <td className="py-3 px-2 text-right text-gray-900">{ex.medicare_price ? formatCurrency(ex.medicare_price) : '—'}</td>
                    <td className="py-3 px-2 text-right text-primary-700 font-semibold">{ex.system_would_pay ? formatCurrency(ex.system_would_pay) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Local hospital quality */}
      {r.local_quality && r.local_quality.top_rated_hospitals && r.local_quality.top_rated_hospitals.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-2">Top-rated hospitals in your state</h2>
          <p className="text-sm text-gray-500 mb-4">{r.local_quality.hospitals_with_ratings_in_state} hospitals rated by CMS in your state</p>
          <div className="space-y-2">
            {r.local_quality.top_rated_hospitals.map((h: any, i: number) => (
              <div key={i} className="flex justify-between text-sm py-2 border-b border-gray-100">
                <span className="text-gray-700">{h.name}</span>
                <span className="text-yellow-600 font-semibold">{'★'.repeat(h.rating)}{'☆'.repeat(5 - h.rating)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Pharmacy insights */}
      {r.pharmacy_insights && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 mb-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-2">Pharmacy: no PBM, no spread pricing</h2>
          <p className="text-sm text-gray-600 mb-4">{r.pharmacy_insights.pbm_elimination}</p>
          <p className="text-sm text-gray-500">{r.pharmacy_insights.total_drug_prices_in_database.toLocaleString()} drug prices analyzed from CMS NADAC data</p>
        </div>
      )}

      {/* Experience comparison */}
      <div className="bg-white rounded-xl border border-gray-200 p-6 mb-8">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Employee experience comparison</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="text-left py-2 pr-4 text-gray-500 font-medium"></th>
                <th className="text-left py-2 px-4 text-gray-500 font-medium">Current plan</th>
                <th className="text-left py-2 px-4 text-primary-700 font-medium">Under the system</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Phone calls per care episode</td>
                <td className="py-3 px-4 text-gray-900">{String(experience_comparison.current.avg_phone_calls_per_episode)}</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">{String(experience_comparison.system.avg_phone_calls_per_episode)}</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Employee schedules appointments</td>
                <td className="py-3 px-4 text-gray-900">Yes</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">No — system handles it</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Employee cost-sharing</td>
                <td className="py-3 px-4 text-gray-900">Yes — avg {formatCurrency(experience_comparison.current.avg_employee_oop_per_year as number)}/yr</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">$0 — zero cost-sharing</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Dental plan</td>
                <td className="py-3 px-4 text-gray-900">Separate plan, separate network</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">Integrated — same interface, same coverage</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Vision plan</td>
                <td className="py-3 px-4 text-gray-900">Separate plan, separate network</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">Integrated — same interface, same coverage</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Mental health access</td>
                <td className="py-3 px-4 text-gray-900">{String(experience_comparison.current.mental_health_parity_gaps)}</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">{String(experience_comparison.system.mental_health_parity_gaps)}</td>
              </tr>
              <tr className="border-b">
                <td className="py-3 pr-4 text-gray-700">Benefits questions</td>
                <td className="py-3 px-4 text-gray-900">{String(experience_comparison.current.benefits_questions_answered_by)}</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">{String(experience_comparison.system.benefits_questions_answered_by)}</td>
              </tr>
              <tr>
                <td className="py-3 pr-4 text-gray-700">Enrollment process</td>
                <td className="py-3 px-4 text-gray-900">{String(experience_comparison.current.enrollment_process)}</td>
                <td className="py-3 px-4 text-primary-700 font-semibold">{String(experience_comparison.system.enrollment_process)}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {/* Cross-type intelligence */}
      {r.cross_type_insights && (
        <div className="bg-primary-50 rounded-xl border border-primary-200 p-6 mb-8">
          <h2 className="text-lg font-semibold text-primary-800 mb-2">Cross-benefit intelligence</h2>
          <p className="text-sm text-primary-700 mb-3">{r.cross_type_insights.cross_type_advantage}</p>
          <p className="text-xs text-primary-600">{r.cross_type_insights.benefit_types_analyzed} benefit types analyzed &middot; {r.cross_type_insights.patterns_detected} cross-type patterns detected</p>
        </div>
      )}

      {/* Transparency */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        <div className="bg-gray-100 rounded-xl p-6">
          <h3 className="font-semibold text-gray-700 mb-2">Current transparency</h3>
          <p className="text-gray-600 text-sm">{transparency.current}</p>
        </div>
        <div className="bg-primary-50 rounded-xl p-6 border border-primary-200">
          <h3 className="font-semibold text-primary-800 mb-2">System transparency</h3>
          <p className="text-primary-700 text-sm">{transparency.system}</p>
        </div>
      </div>

      {/* Data quality note */}
      <div className="bg-gray-50 rounded-xl p-4 mb-8 text-sm text-gray-600">
        <strong>Data quality:</strong> {String(data_quality.confidence_note)} ({String(data_quality.price_data_points_in_state).toLocaleString()} price data points in your state &middot; {String(data_quality.national_pharmacy_records).toLocaleString()} national pharmacy records &middot; {String(data_quality.insurer_negotiated_rates).toLocaleString()} insurer negotiated rates)
      </div>

      {/* CTAs */}
      <div className="flex flex-col sm:flex-row gap-4 justify-center">
        <button
          onClick={handleShare}
          className="px-6 py-3 bg-white border border-gray-300 rounded-lg font-medium text-gray-700 hover:bg-gray-50 transition-colors"
        >
          Copy shareable link
        </button>
        <button
          disabled
          className="px-6 py-3 bg-gray-200 rounded-lg font-medium text-gray-400 cursor-not-allowed"
          title="Shadow mode coming soon"
        >
          See your actual claims — Coming soon
        </button>
        <button
          onClick={onReset}
          className="px-6 py-3 text-primary-600 font-medium hover:underline"
        >
          Run another benchmark
        </button>
      </div>
    </div>
  )
}
