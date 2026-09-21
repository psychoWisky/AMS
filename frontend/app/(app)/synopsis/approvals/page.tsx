"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ClipboardCheck, Loader2, CheckCircle2, RotateCcw, X, Eye } from "lucide-react";
import {
  ApprovalTable, PdfPreviewModal, StudentInfoCard, SynopsisHistory, SynopsisStatusBadge,
  apiErrorMessage, formatDateTime, type SynopsisDetail,
} from "@/components/ui/synopsis-parts";

// "My Synopsis Approvals" — Synopses whose CURRENT stage the signed-in user (in their ACTIVE role) can act on.
// The list, the detail and every action are authorized by the backend from the session and the student's real
// Advisory Committee; ids in this page are only handles, never permissions.

interface PendingRow {
  synopsis_id: string; student_name: string; student_roll: string | null; program_name: string | null;
  department_name: string | null; title: string | null; status: string; status_label: string;
  acting_as: string; requires_otp: boolean; cycle_number: number | null; submitted_at: string | null;
}

export default function SynopsisApprovalsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [showRevert, setShowRevert] = useState(false);
  const [remark, setRemark] = useState("");
  const [preview, setPreview] = useState<"file" | "document" | null>(null);

  const { data: pending = [], isLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-synopsis-pending"],
    queryFn: async () => (await api.get("/synopsis/pending-approvals")).data,
  });
  const { data: detail } = useQuery<SynopsisDetail>({
    queryKey: ["ams-synopsis-detail", selectedId],
    queryFn: async () => (await api.get(`/synopsis/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  function close() { setSelectedId(null); setOtp(""); setOtpSent(false); setShowRevert(false); setRemark(""); setPreview(null); }
  function done() {
    qc.invalidateQueries({ queryKey: ["ams-synopsis-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-synopsis-detail", selectedId] });
    close();
  }

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/synopsis/${selectedId}/approval/otp`),
    onSuccess: (res) => { setOtpSent(true); toast.success(res.data?.dev_otp ? `OTP sent. Dev OTP: ${res.data.dev_otp}` : "OTP sent to your email."); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not send the OTP.")),
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/synopsis/${selectedId}/approval/approve`, detail?.my_pending_stage?.requires_otp ? { otp } : {}),
    onSuccess: () => { toast.success("Approved."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Approval failed.")),
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/synopsis/${selectedId}/approval/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Synopsis reverted to the student."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Revert failed.")),
  });

  const stage = detail?.my_pending_stage ?? null;

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />My Synopsis Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Synopsis submissions awaiting your approval in your current role.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your action right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Student", "Roll No.", "Title", "Department / Programme", "Your Stage", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.synopsis_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.title ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.department_name ?? "—"} / {row.program_name ?? "—"}</td>
                  <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{row.acting_as}</span></td>
                  <td className="px-4 py-2.5 text-gray-500">{formatDateTime(row.submitted_at)}{row.cycle_number && row.cycle_number > 1 ? ` (submission ${row.cycle_number})` : ""}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.synopsis_id); setOtp(""); setOtpSent(false); setShowRevert(false); }}
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
          <div className="bg-white rounded-2xl max-w-3xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-xl font-bold text-gray-900">{detail.student.student_name}&apos;s Synopsis</h2>
              <div className="flex items-center gap-3">
                <SynopsisStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close" className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
              </div>
            </div>

            <StudentInfoCard student={detail.student} title={detail.title} />

            <div className="flex flex-wrap gap-2">
              {detail.file && <button onClick={() => setPreview("file")} className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 rounded-lg text-sm font-semibold hover:bg-gray-50"><Eye size={14} /> View uploaded PDF ({detail.file.page_count} pp.)</button>}
              {detail.file && <button onClick={() => setPreview("document")} className="flex items-center gap-1.5 px-3 py-1.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-lg text-sm font-semibold hover:bg-[#E6F4F4]"><Eye size={14} /> View Synopsis document</button>}
            </div>

            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Approval progress</p>
              <ApprovalTable committee={detail.committee} approvals={detail.approvals} />
            </div>

            {detail.history.some((c) => c.status === "reverted") && (
              <div>
                <p className="text-sm font-semibold text-gray-700 mb-2">Earlier submissions</p>
                <SynopsisHistory history={detail.history.filter((c) => c.status !== "active")} />
              </div>
            )}

            {stage ? (
              <div className="border-t border-gray-100 pt-4">
                <p className="text-sm text-gray-700 mb-3">You are acting as <span className="font-semibold">{stage.role_label}</span>.</p>
                {!showRevert ? (
                  <div className="flex flex-wrap items-center gap-3">
                    {stage.requires_otp ? (
                      !otpSent ? (
                        <button onClick={() => requestOtp.mutate()} disabled={requestOtp.isPending}
                          className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-60">
                          {requestOtp.isPending ? "Sending…" : "Send OTP to Email"}
                        </button>
                      ) : (
                        <>
                          <input value={otp} onChange={(e) => setOtp(e.target.value)} placeholder="Enter 6-digit OTP" maxLength={6} inputMode="numeric"
                            className="border border-gray-300 rounded-xl px-3 py-2 text-sm w-44 focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                          <button onClick={() => approve.mutate()} disabled={otp.length < 6 || approve.isPending}
                            className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                            <CheckCircle2 size={15} /> {approve.isPending ? "Approving…" : "Approve & Sign"}
                          </button>
                        </>
                      )
                    ) : (
                      <button onClick={() => approve.mutate()} disabled={approve.isPending}
                        className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-60">
                        <CheckCircle2 size={15} /> {approve.isPending ? "Approving…" : "Approve"}
                      </button>
                    )}
                    <button onClick={() => setShowRevert(true)}
                      className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 rounded-xl text-sm font-semibold hover:bg-red-50">
                      <RotateCcw size={15} /> Revert
                    </button>
                  </div>
                ) : (
                  <div className="space-y-3" data-testid="revert-panel">
                    <h3 className="text-lg font-bold text-gray-900">Revert Synopsis</h3>
                    <p className="text-sm text-gray-600">The Synopsis returns to the student, who will see your remark and can correct and resubmit.</p>
                    <div>
                      <label className="block text-sm font-semibold text-gray-700 mb-1" htmlFor="revert-remark">Remark *</label>
                      <textarea id="revert-remark" value={remark} onChange={(e) => setRemark(e.target.value)} rows={4}
                        placeholder="Explain what the student needs to correct"
                        className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
                    </div>
                    <div className="flex gap-3">
                      <button onClick={() => { setShowRevert(false); setRemark(""); }} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
                      <button onClick={() => revert.mutate()} disabled={!remark.trim() || revert.isPending}
                        className="px-4 py-2 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-50">
                        {revert.isPending ? "Reverting…" : "Revert"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <p className="border-t border-gray-100 pt-4 text-sm text-gray-500">You have no action pending on this Synopsis.</p>
            )}
          </div>
        </div>
      )}

      {preview === "file" && detail && <PdfPreviewModal title="Uploaded Synopsis PDF" path={`/synopsis/${detail.id}/file`} params={{ inline: "true" }} filename="synopsis-upload.pdf" onClose={() => setPreview(null)} />}
      {preview === "document" && detail && <PdfPreviewModal title="Synopsis Document" path={`/synopsis/${detail.id}/document`} params={{ inline: "true" }} filename="synopsis.pdf" onClose={() => setPreview(null)} />}
    </div>
  );
}
