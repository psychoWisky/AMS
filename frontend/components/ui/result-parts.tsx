"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText, Loader2, X } from "lucide-react";
import { api } from "@/services/api";
import { PdfPreviewModal, apiErrorMessage } from "@/components/ui/gradesheet-parts";

export interface ResultListRow {
  id: string; degree: string | null; semester: string | null; semester_id: string; academic_year: string | null;
  result_status: string; result_status_label: string; status: string; gpa: string | null; published_at: string | null;
}
export interface ResultDetail {
  id: string; status: string; version: number;
  student: { id: string; name: string | null; roll_no: string | null; college: string | null; department: string | null; degree: string | null };
  semester: string | null; academic_year: string | null; exam_label: string | null;
  result_status: string; result_status_label: string;
  promoted_class: string | null; admission_status: string | null; class_label: string | null; progression_note: string;
  courses: { offering_id: string; course_number: string; course_title: string; credit: string | null; credits: number | null; grade_letter: string; grade_points: number | null; credit_points: number | null }[];
  totals: { credits: number | null; grade_points: number | null; credit_points: number | null };
  gpa: string | null; cgpa: string | null; cgpa_applicable: boolean;
  compiled_at: string | null; published_at: string | null;
}

export const RESULT_STATUS_STYLE: Record<string, string> = {
  pass: "bg-green-100 text-green-700", pass_with_backlogs: "bg-amber-100 text-amber-700",
};

export function ResultStatusBadge({ status, label }: { status: string; label: string }) {
  return <span className={`inline-flex px-2.5 py-1 rounded-full text-sm font-semibold ${RESULT_STATUS_STYLE[status] ?? "bg-gray-100 text-gray-700"}`}>{label}</span>;
}

function Field({ label, value }: { label: string; value: string | null | undefined }) {
  return <div><p className="text-sm text-gray-600">{label}</p><p className="font-semibold text-gray-900">{value || "—"}</p></div>;
}

// Student Result and Admission Details + Semester Marksheet + Grade Card (PDF preview with
// Close / Download). Promoted Class / Admission Status / Class are shown as "not yet available"
// — AVFU has not confirmed any progression rule, so AMS never invents them.
export function ResultDetailModal({ resultId, onClose }: { resultId: string; onClose: () => void }) {
  const [showCard, setShowCard] = useState(false);
  const { data: r, isLoading, isError, error } = useQuery<ResultDetail>({
    queryKey: ["ams-result-detail", resultId],
    queryFn: async () => (await api.get(`/results/${resultId}`)).data,
    retry: false,
  });
  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-3xl max-h-[92vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h3 className="text-xl font-bold text-gray-900">Student Result and Admission Details</h3>
          <button onClick={onClose} aria-label="Close" className="text-gray-500 hover:text-gray-800"><X size={20} /></button>
        </div>
        {isLoading ? <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
          : isError || !r ? <div className="py-14 text-center text-red-600 px-6">{apiErrorMessage(error, "This result could not be loaded.")}</div>
          : (
            <div className="px-6 py-5 space-y-6">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-x-6 gap-y-4">
                <Field label="Name" value={r.student.name} /><Field label="Roll No" value={r.student.roll_no} /><Field label="Degree" value={r.student.degree} />
                <Field label="Semester" value={r.semester} /><Field label="Academic Year" value={r.academic_year} />
                <div><p className="text-sm text-gray-600">Result Status</p><ResultStatusBadge status={r.result_status} label={r.result_status_label} /></div>
                <Field label="Promoted Class" value={r.promoted_class ?? "Not yet available"} /><Field label="Admission Status" value={r.admission_status ?? "Not yet available"} />
              </div>
              <p className="text-xs text-gray-500 -mt-3">{r.progression_note}</p>

              <div>
                <h4 className="text-base font-bold text-gray-900 mb-3">Semester Marksheet</h4>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-x-6 gap-y-3 mb-4">
                  <Field label="Name" value={r.student.name} /><Field label="Roll No" value={r.student.roll_no} /><Field label="College" value={r.student.college} />
                  <Field label="Department" value={r.student.department} /><Field label="Degree" value={r.student.degree} /><Field label="Class" value={r.class_label ?? "—"} />
                  <Field label="Remark" value={r.result_status_label} /><Field label="Session" value={r.academic_year} />
                </div>
                <table className="w-full text-sm border border-gray-200">
                  <thead className="bg-gray-50"><tr><th className="text-left px-4 py-2.5 font-semibold text-gray-700">Course No.</th><th className="text-left px-4 py-2.5 font-semibold text-gray-700">Grade</th></tr></thead>
                  <tbody>
                    {r.courses.map((c) => (
                      <tr key={c.offering_id} className="border-t border-gray-100"><td className="px-4 py-2.5 font-mono font-semibold text-[#0D6E6E]" title={c.course_title}>{c.course_number}</td><td className="px-4 py-2.5 font-bold">{c.grade_letter}</td></tr>
                    ))}
                  </tbody>
                </table>
                <div className="flex flex-wrap gap-6 mt-4">
                  <p className="text-base">GPA: <span className="font-bold text-[#0D6E6E]">{r.gpa ?? "—"}</span></p>
                  <p className="text-base">CGPA: <span className="font-bold text-[#0D6E6E]">{r.cgpa_applicable ? r.cgpa : "—"}</span>{!r.cgpa_applicable && <span className="text-xs text-gray-500 ml-2">(no CGPA for the first semester)</span>}</p>
                </div>
              </div>
            </div>
          )}
        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-100">
          <button onClick={onClose} className="px-5 py-2.5 border border-gray-200 rounded-xl font-semibold text-gray-700 hover:bg-gray-50">Close</button>
          {r && <button onClick={() => setShowCard(true)} className="inline-flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#0a5858]"><FileText size={16} /> Grade Card</button>}
        </div>
        {showCard && r && <PdfPreviewModal title="Statement of Marks/Grades" path={`/results/${resultId}/grade-card`} filename={`GradeCard-${r.student.roll_no ?? "student"}-${r.semester ?? "semester"}.pdf`} onClose={() => setShowCard(false)} />}
      </div>
    </div>
  );
}
