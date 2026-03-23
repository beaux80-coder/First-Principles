import { useNavigate } from 'react-router-dom'

export default function LoginPage() {
  const navigate = useNavigate()

  // In development, bypass auth and go to dashboard directly
  const handleDevLogin = () => {
    navigate('/dashboard')
  }

  return (
    <div className="max-w-md mx-auto px-4 py-16">
      <div className="bg-white rounded-xl shadow-sm border border-gray-200 p-8 text-center">
        <h1 className="text-2xl font-bold text-gray-900 mb-4">Sign in to First Principles</h1>
        <p className="text-gray-600 mb-8">
          Access your employer dashboard, shadow mode, and care execution.
        </p>

        <button
          onClick={handleDevLogin}
          className="w-full bg-primary-600 text-white py-3 rounded-lg font-semibold hover:bg-primary-700 transition-colors mb-4"
        >
          Sign in with Auth0
        </button>

        <p className="text-xs text-gray-400">
          Auth0 integration configured via environment variables.
          In development mode, this bypasses authentication.
        </p>
      </div>
    </div>
  )
}
