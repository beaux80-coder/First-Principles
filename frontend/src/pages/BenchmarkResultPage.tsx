import { useEffect, useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { getBenchmark, type BenchmarkResponse } from '../api/client'
import BenchmarkResults from '../components/BenchmarkResults'

export default function BenchmarkResultPage() {
  const { queryId } = useParams<{ queryId: string }>()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [result, setResult] = useState<BenchmarkResponse | null>(null)

  useEffect(() => {
    if (!queryId) return
    getBenchmark(queryId)
      .then((data: unknown) => {
        const d = data as { results?: BenchmarkResponse; query_id?: string }
        if (d.results) {
          setResult({
            ...d.results,
            query_id: queryId,
            share_url: `/benchmark/${queryId}`,
          })
        }
      })
      .catch(err => setError(err instanceof Error ? err.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [queryId])

  if (loading) {
    return (
      <div className="flex justify-center items-center min-h-[50vh]">
        <p className="text-gray-500">Loading benchmark...</p>
      </div>
    )
  }

  if (error || !result) {
    return (
      <div className="max-w-xl mx-auto px-4 py-16 text-center">
        <p className="text-red-600 mb-4">{error || 'Benchmark not found'}</p>
        <button onClick={() => navigate('/')} className="text-primary-600 hover:underline">
          Run a new benchmark
        </button>
      </div>
    )
  }

  return <BenchmarkResults result={result} onReset={() => navigate('/')} />
}
