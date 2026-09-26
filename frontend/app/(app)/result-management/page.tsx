"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Eye, FileText, Loader2 } from "lucide-react";
import { api } from "@/services/api";
import { ResultDetailModal, ResultStatusBadge, type ResultListRow } from "@/components/ui/result-parts";

// Student Result Management: the student's own PUBLISHED semester results. View Details opens
// the result details, semester marksheet (course + grade, GPA, CGPA) and the Grade Card.

export default function ResultManagementPage() {
  const [openId, setOpenId] = useState<string | null>(null);
  const { data: rows = [], isLoading, isError } = useQuery<ResultListRow[]>({
    queryKey: ["ams-my-results"],
    queryFn: async () => (await api.get("/results/mine")).data,
  });
  const th = "text-left px-4 py-3 font-semibold text-gray-700";
  return (
    <div className="p-6 w-full">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FileText size={24} className="text-[#0D6E6E]" />Result Management</h1>
        <p className="text-gray-700 text-base mt-1">Your published semester results, marksheets and grade cards</p>
      </div>
      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[70vh]">
        {isLoading ? <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
          : isError ? <div className="text-center py-14 text-red-600">Could not load your results.</div>
          : rows.length === 0 ? <div className="text-center py-14 text-gray-600"><FileText size={40} className="mx-auto mb-3 opacity-30" /><p>No results have been published for you yet.</p></div>
          : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10"><tr>
                {["SL No", "Degree", "Semester", "Result", "Academic Year", "Action"].map((h) => <th key={h} className={th}>{h}</th>)}
              </tr></thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={r.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                    <td className="px-4 py-3">{r.degree ?? "—"}</td>
                    <td className="px-4 py-3">{r.semester ?? "—"}</td>
                    <td className="px-4 py-3"><ResultStatusBadge status={r.result_status} label={r.result_status_label} /></td>
                    <td className="px-4 py-3">{r.academic_year ?? "—"}</td>
                    <td className="px-4 py-3"><button onClick={() => setOpenId(r.id)} className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-semibold text-[#0D6E6E] border border-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]"><Eye size={13} /> View Details</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>
      {openId && <ResultDetailModal resultId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}
