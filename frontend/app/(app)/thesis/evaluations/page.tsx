"use client";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, blobErrorMessage, viewFileInNewTab } from "@/services/api";
import { toast } from "sonner";
import { ClipboardCheck, Loader2, Upload, FileText } from "lucide-react";
import { apiErrorMessage, formatDateTime, type ThesisDetail } from "@/components/ui/thesis-parts";

// External Examiner's own evaluation inbox — resolved entirely from the examiner's real
// ExternalExaminerAssignment rows (never a student/thesis id the client could tamper with).
// An examiner sees ONLY theses they are actually assigned to, and only once DPGS has sent
// the thesis for evaluation.

interface EvaluationRow {
  thesis_id: string; evaluation_id: string; student_name: string; title: string | null;
  status: string; submitted_at: string | null;
}

export default function ThesisEvaluationsPage() {
  const qc = useQueryClient();
  const { data: rows = [], isLoading } = useQuery<EvaluationRow[]>({
    queryKey: ["ams-thesis-my-evaluations"],
    queryFn: async () => (await api.get("/thesis/my-evaluations")).data,
  });

  const upload = useMutation({
    mutationFn: ({ thesisId, evaluationId, file }: { thesisId: string; evaluationId: string; file: File }) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post(`/thesis/${thesisId}/evaluations/${evaluationId}/report`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Evaluation report submitted."); qc.invalidateQueries({ queryKey: ["ams-thesis-my-evaluations"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Only .docx files are accepted.")),
  });

  async function viewThesisFile(thesisId: string) {
    try {
      const { data } = await api.get<ThesisDetail>(`/thesis/${thesisId}`);
      const doc = data.documents.thesis_file;
      if (!doc) { toast.error("No thesis file is available."); return; }
      await viewFileInNewTab(`/thesis/${thesisId}/documents/${doc.id}/download`);
    } catch (e) {
      toast.error(await blobErrorMessage(e, "Could not open the thesis file."));
    }
  }

  return (
    <div className="p-6 w-full max-w-4xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />My Evaluations</h1>
        <p className="text-gray-700 text-base mt-1">Theses assigned to you for external evaluation.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : rows.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">No thesis has been assigned to you for evaluation yet.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 divide-y divide-gray-100">
          {rows.map((r) => (
            <div key={r.evaluation_id} className="p-5 flex items-center justify-between gap-4">
              <div>
                <p className="font-semibold text-gray-800">{r.title || "—"}</p>
                <p className="text-sm text-gray-500">Student: {r.student_name}</p>
                <p className="text-xs text-gray-400 mt-1">
                  {r.status === "pending" ? "Awaiting your evaluation report" : r.status === "submitted" ? `Submitted ${formatDateTime(r.submitted_at)} — awaiting DPGS approval` : "Evaluation approved"}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button onClick={() => viewThesisFile(r.thesis_id)} className="flex items-center gap-1.5 px-3 py-2 border border-gray-200 rounded-xl text-sm font-semibold hover:bg-gray-50">
                  <FileText size={15} /> View Thesis File
                </button>
                {r.status === "pending" && (
                  <label className="flex items-center gap-1.5 px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold cursor-pointer hover:bg-[#178F8F]">
                    <Upload size={15} /> Upload Report
                    <input type="file" accept=".docx" hidden
                      onChange={(e) => e.target.files?.[0] && upload.mutate({ thesisId: r.thesis_id, evaluationId: r.evaluation_id, file: e.target.files[0] })} />
                  </label>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
