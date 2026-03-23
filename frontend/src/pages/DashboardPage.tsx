export default function DashboardPage() {
  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      <h1 className="text-2xl font-bold text-gray-900 mb-6">Employer Dashboard</h1>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <DashboardCard title="Status" value="Active" subtitle="Phase 0 — Foundation" />
        <DashboardCard title="Employees" value="—" subtitle="Connect payroll to sync" />
        <DashboardCard title="Data Pipeline" value="—" subtitle="Ingesting public pricing data" />
      </div>

      <div className="bg-white rounded-xl border border-gray-200 p-8 text-center text-gray-500">
        <p className="text-lg mb-2">Dashboard features coming in Phase 4+</p>
        <p className="text-sm">
          Shadow mode comparison, claims processing, care execution metrics,
          and full cost transparency will appear here.
        </p>
      </div>
    </div>
  )
}

function DashboardCard({ title, value, subtitle }: { title: string; value: string; subtitle: string }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-6">
      <p className="text-sm text-gray-500 mb-1">{title}</p>
      <p className="text-2xl font-bold text-gray-900">{value}</p>
      <p className="text-sm text-gray-500 mt-1">{subtitle}</p>
    </div>
  )
}
