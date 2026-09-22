"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ClipboardCheck, Loader2, CheckCircle2, XCircle, X } from "lucide-react";
import {
  StudentInfoCard, apiErrorMessage, formatDate, type MigrationDetail,
} from "@/components/ui/migration-parts";

// The Registrar's Migration approval inbox — reachable only by the single REGISTRAR role.
// Follows the same list+detail-modal pattern already used by Thesis/External Examiner
// Selection approvals, simplified for a single Approve/Reject decision (no OTP, no stage
// timeline, no signature table — there is exactly one approval level here).

interface PendingRow {
  id: string; student_name: string; student_roll: string | null; degree: string | null; college: string | null;
  status: string; status_label: string; submitted_at: string | null;
}

export default function MigrationApprovalsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [remark, setRemark] = useState("");
  const [showReject, setShowReject] = useState(false);

  const { data: pending = [], isLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-migration-pending"],
    queryFn: async () => (await api.get("/migration/pending-approvals")).data,
  });
  const { data: detail } = useQuery<MigrationDetail>({
    queryKey: ["ams-migration-detail-registrar", selectedId],
    queryFn: async () => (await api.get(`/migration/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  function close() { setSelectedId(null); setRemark(""); setShowReject(false); }
  function done() {
    qc.invalidateQueries({ queryKey: ["ams-migration-pending"] });
    setSelectedId(null); setRemark(""); setShowReject(false);
  }

  const approve = useMutation({
    mutationFn: () => api.post(`/migration/${selectedId}/approval/approve`, remark.trim() ? { remark: remark.trim() } : {}),
    onSuccess: () => { toast.success("Migration application approved."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Approval failed.")),
  });
  const reject = useMutation({
    mutationFn: () => api.post(`/migration/${selectedId}/approval/reject`, remark.trim() ? { remark: remark.trim() } : {}),
    onSuccess: () => { toast.success("Migration application rejected."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Rejection failed.")),
  });

  function downloadReceipt() { window.open(`${api.defaults.baseURL}/migration/${selectedId}/receipt`, "_blank"); }

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Migration Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Migration applications submitted for your review.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your review right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Name", "Roll No.", "Degree", "College", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.degree ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.college ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-500">{formatDate(row.submitted_at)}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.id); setRemark(""); setShowReject(false); }}
                      className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Review</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={close}>
          <div className="bg-white rounded-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-gray-900">{detail.student_name}&apos;s Migration Application</h2>
              <button onClick={close} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>

            <StudentInfoCard m={detail} />

            <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <p><span className="font-semibold text-gray-700">Registration No.:</span> {detail.registration_no || "—"}</p>
              <p><span className="font-semibold text-gray-700">Date of Fee Payment:</span> {formatDate(detail.fee_payment_date)}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Last Examination with Roll No.:</span> {detail.last_exam_name_and_roll || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">School/College Passed From:</span> {detail.passed_from_institution || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Migration Reason:</span> {detail.migration_reason || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Address:</span> {detail.address || "—"}</p>
            </div>

            <div className="bg-white rounded-2xl border border-gray-200 p-5 flex items-center justify-between">
              <p className="text-sm font-semibold text-gray-700">Payment Receipt</p>
              {detail.receipt ? (
                <button onClick={downloadReceipt} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Receipt</button>
              ) : <p className="text-xs text-gray-400">Not uploaded</p>}
            </div>

            {!showReject ? (
              <div className="flex flex-wrap items-center gap-3 border-t border-gray-100 pt-4">
                <button onClick={() => approve.mutate()} disabled={approve.isPending}
                  className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                  <CheckCircle2 size={15} /> {approve.isPending ? "Approving…" : "Approve"}
                </button>
                <button onClick={() => setShowReject(true)}
                  className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 rounded-xl text-sm font-semibold hover:bg-red-50">
                  <XCircle size={15} /> Reject
                </button>
              </div>
            ) : (
              <div className="space-y-3 border-t border-gray-100 pt-4">
                <h3 className="text-lg font-bold text-gray-900">Reject Application</h3>
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1" htmlFor="migration-reject-remark">Remark (optional)</label>
                  <textarea id="migration-reject-remark" value={remark} onChange={(e) => setRemark(e.target.value)} rows={3}
                    placeholder="Optional — explain why the application is being rejected"
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
                </div>
                <div className="flex gap-3">
                  <button onClick={() => { setShowReject(false); setRemark(""); }} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
                  <button onClick={() => reject.mutate()} disabled={reject.isPending}
                    className="px-4 py-2 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-50">
                    {reject.isPending ? "Rejecting…" : "Confirm Reject"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
