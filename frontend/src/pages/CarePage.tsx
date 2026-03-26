/**
 * Employee Care Execution Page (Function 9)
 *
 * Constitution: "Employee describes the problem and shows up.
 * System handles everything else."
 *
 * This page provides:
 * 1. Simple issue description (plain language, minimum cognitive load)
 * 2. Real-time care path display (what the system is doing)
 * 3. Provider details and appointment info
 * 4. Active episode tracking
 * 5. Zero cost display ($0 copay, $0 deductible, $0 OOP)
 *
 * Accessible on all standard devices. WCAG compliant.
 */

import { useState } from 'react'

const BENEFIT_TYPES = [
  { value: 'health', label: 'Health', icon: '🏥' },
  { value: 'dental', label: 'Dental', icon: '🦷' },
  { value: 'vision', label: 'Vision', icon: '👁' },
  { value: 'mental_health', label: 'Mental Health', icon: '🧠' },
  { value: 'life', label: 'Life Insurance', icon: '📋' },
  { value: 'std', label: 'Short-Term Disability', icon: '🩹' },
  { value: 'ltd', label: 'Long-Term Disability', icon: '♿' },
]

interface CareStep {
  step: number
  action: string
  description: string
  auto_handled: boolean
  assigned_provider_name?: string
}

interface CareResult {
  episode_id: string
  episode_status: string
  condition: string
  benefit_type: string
  care_path: CareStep[]
  provider_recommendation: {
    selected_provider?: {
      provider_name: string
      quality_score: number
      state: string
    }
    note?: string
  }
  cost_to_employee: {
    copay: number
    deductible: number
    out_of_pocket: number
    explanation: string
  }
  scheduling: {
    status: string
    estimated_first_appointment: string
  }
}

export default function CarePage() {
  const [step, setStep] = useState<'describe' | 'loading' | 'result'>('describe')
  const [symptoms, setSymptoms] = useState('')
  const [benefitType, setBenefitType] = useState('health')
  const [result, setResult] = useState<CareResult | null>(null)
  const [error, setError] = useState('')
  const [episodes, setEpisodes] = useState<Record<string, unknown>[]>([])
  const [showOnboarding, setShowOnboarding] = useState(true)

  // TODO: Get from auth context
  const employeeId = '22222222-2222-2222-2222-222222222221'

  const handleSubmit = async () => {
    if (!symptoms.trim()) return
    setStep('loading')
    setError('')

    try {
      const resp = await fetch('/api/v1/care/navigate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          employee_id: employeeId,
          condition: symptoms,
          benefit_type: benefitType,
          symptoms: symptoms.split(',').map(s => s.trim()).filter(Boolean),
        }),
      })

      if (!resp.ok) {
        const err = await resp.json()
        throw new Error(err.detail || 'Something went wrong')
      }

      const data = await resp.json()
      setResult(data)
      setStep('result')
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Unknown error')
      setStep('describe')
    }
  }

  const loadEpisodes = async () => {
    try {
      const resp = await fetch(`/api/v1/care/status/${employeeId}`)
      if (resp.ok) {
        const data = await resp.json()
        setEpisodes(data.active_episodes || [])
      }
    } catch (_) { /* ignore fetch errors for episode loading */ }
  }

  return (
    <div className="max-w-2xl mx-auto px-4 py-8" role="main" aria-label="Care Navigation">

      {/* Cost guarantee banner */}
      <div
        className="bg-green-50 border border-green-200 rounded-lg p-4 mb-6 text-center"
        role="status"
        aria-label="Cost guarantee"
      >
        <p className="text-green-800 font-semibold text-lg">
          Your cost: $0 copay. $0 deductible. $0 out-of-pocket. Always.
        </p>
      </div>

      {/* First-time user onboarding / help section (F9 Q11) */}
      {showOnboarding && step === 'describe' && (
        <div
          className="bg-blue-50 border border-blue-200 rounded-lg p-5 mb-6 relative"
          role="region"
          aria-label="How it works"
        >
          <button
            onClick={() => setShowOnboarding(false)}
            className="absolute top-2 right-3 text-blue-400 hover:text-blue-600 text-sm"
            aria-label="Dismiss help section"
          >
            Got it
          </button>
          <h2 className="text-lg font-semibold text-blue-900 mb-3">
            Here's how it works
          </h2>
          <ul className="space-y-2 text-blue-800 text-sm">
            <li className="flex items-start gap-2">
              <span className="font-bold text-blue-600 mt-0.5" aria-hidden="true">1.</span>
              <span>
                <strong>Tell us what's wrong.</strong> We handle everything else &mdash;
                finding a provider, scheduling, verifying the price, and coordinating your care.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="font-bold text-blue-600 mt-0.5" aria-hidden="true">2.</span>
              <span>
                <strong>No phone calls, no searching, no scheduling.</strong> Just describe your
                issue in plain language and we take care of it.
              </span>
            </li>
            <li className="flex items-start gap-2">
              <span className="font-bold text-blue-600 mt-0.5" aria-hidden="true">3.</span>
              <span>
                <strong>Zero copays. Zero deductibles. Zero bills.</strong> Your employer covers
                everything &mdash; you will never receive a bill for covered care.
              </span>
            </li>
          </ul>
        </div>
      )}

      {step === 'describe' && (
        <div>
          <h1 className="text-2xl font-bold mb-2">What's going on?</h1>
          <p className="text-gray-600 mb-6">
            Tell us in your own words. We'll handle everything from here.
          </p>

          {/* Benefit type selector */}
          <fieldset className="mb-4">
            <legend className="text-sm font-medium text-gray-700 mb-2">
              What type of care?
            </legend>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2" role="radiogroup">
              {BENEFIT_TYPES.map(bt => (
                <button
                  key={bt.value}
                  onClick={() => setBenefitType(bt.value)}
                  className={`p-3 rounded-lg border text-sm text-center transition-colors ${
                    benefitType === bt.value
                      ? 'border-blue-500 bg-blue-50 text-blue-700'
                      : 'border-gray-200 hover:border-gray-300'
                  }`}
                  role="radio"
                  aria-checked={benefitType === bt.value}
                  aria-label={bt.label}
                >
                  <span className="text-xl block mb-1" aria-hidden="true">{bt.icon}</span>
                  {bt.label}
                </button>
              ))}
            </div>
          </fieldset>

          {/* Symptom input */}
          <label htmlFor="symptoms" className="block text-sm font-medium text-gray-700 mb-1">
            Describe what you're experiencing
          </label>
          <textarea
            id="symptoms"
            className="w-full border rounded-lg p-4 text-lg min-h-[120px] mb-4 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
            placeholder="Example: I've had a bad headache for 3 days and I feel nauseous..."
            value={symptoms}
            onChange={e => setSymptoms(e.target.value)}
            aria-describedby="symptoms-help"
          />
          <p id="symptoms-help" className="text-sm text-gray-500 mb-4">
            Use plain language. No medical terms needed. We'll figure it out.
          </p>

          {error && (
            <div className="bg-red-50 border border-red-200 rounded-lg p-3 mb-4" role="alert">
              <p className="text-red-700">{error}</p>
            </div>
          )}

          <button
            onClick={handleSubmit}
            disabled={!symptoms.trim()}
            className="w-full bg-blue-600 text-white py-4 rounded-lg text-lg font-semibold hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
            aria-label="Get care now"
          >
            Get Care Now
          </button>

          {/* Active episodes */}
          <button
            onClick={loadEpisodes}
            className="mt-4 text-blue-600 underline text-sm"
          >
            View my active care episodes
          </button>
          {episodes.length > 0 && (
            <div className="mt-4 space-y-2">
              {episodes.map((ep: Record<string, unknown>, i: number) => (
                <div key={i} className="border rounded-lg p-3 bg-gray-50">
                  <p className="font-medium">{ep.condition || 'Care episode'}</p>
                  <p className="text-sm text-gray-600">
                    Status: {ep.status} | Type: {ep.benefit_type}
                  </p>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {step === 'loading' && (
        <div className="text-center py-12" role="status" aria-label="Processing your request">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4" aria-hidden="true" />
          <p className="text-lg text-gray-600">
            Finding you the best care...
          </p>
          <p className="text-sm text-gray-400 mt-2">
            Selecting provider, scheduling appointment, verifying price
          </p>
        </div>
      )}

      {step === 'result' && result && (
        <div>
          <div className="bg-green-50 border border-green-200 rounded-lg p-6 mb-6" role="status">
            <h2 className="text-xl font-bold text-green-800 mb-2">
              Your care is set up
            </h2>
            <p className="text-green-700">
              You don't need to do anything else except show up.
            </p>
          </div>

          {/* Provider info */}
          {result.provider_recommendation?.selected_provider && (
            <div className="border rounded-lg p-4 mb-4">
              <h3 className="font-semibold mb-2">Your Provider</h3>
              <p className="text-lg">{result.provider_recommendation.selected_provider.provider_name}</p>
              <p className="text-sm text-gray-600">
                Quality Score: {result.provider_recommendation.selected_provider.quality_score}/100
              </p>
            </div>
          )}

          {/* Care path */}
          <div className="border rounded-lg p-4 mb-4">
            <h3 className="font-semibold mb-3">What we're handling for you</h3>
            <ol className="space-y-3" aria-label="Care steps">
              {result.care_path.map((s) => (
                <li key={s.step} className="flex items-start gap-3">
                  <span className="flex-shrink-0 w-6 h-6 rounded-full bg-blue-100 text-blue-700 text-sm flex items-center justify-center font-medium" aria-hidden="true">
                    {s.step}
                  </span>
                  <div>
                    <p className="font-medium">{s.description}</p>
                    {s.assigned_provider_name && (
                      <p className="text-sm text-gray-600">Provider: {s.assigned_provider_name}</p>
                    )}
                    <p className="text-xs text-green-600">
                      {s.auto_handled ? 'Handled automatically' : 'Requires your input'}
                    </p>
                  </div>
                </li>
              ))}
            </ol>
          </div>

          {/* Cost */}
          <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-4">
            <h3 className="font-semibold text-green-800 mb-1">Your Cost</h3>
            <p className="text-3xl font-bold text-green-700">$0.00</p>
            <p className="text-sm text-green-600">{result.cost_to_employee.explanation}</p>
          </div>

          {/* New issue button */}
          <button
            onClick={() => {
              setStep('describe')
              setSymptoms('')
              setResult(null)
            }}
            className="w-full border border-blue-600 text-blue-600 py-3 rounded-lg font-semibold hover:bg-blue-50 transition-colors"
          >
            Describe another issue
          </button>
        </div>
      )}
    </div>
  )
}
