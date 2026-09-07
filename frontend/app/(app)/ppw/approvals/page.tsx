"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ClipboardCheck, Loader2, CheckCircle2, RotateCcw, X } from "lucide-react";

interface PendingApproval {
  ppw_id: string; student_name: string | null; student_roll: string | null; student_email: string | null;
  research_title: string | null; status: string; stage_type: string; submitted_at: string;
  department_name: string | null; program_name: string | null;
}

interface CommitteeRow {
  category: string; faculty_name: string | null; designation: string | null;
  department_name: string | null; signature_status: string; signed_at: string | null;
  is_current_stage: boolean; remark: string | null;
}
interface HodApproval {
  status: string; approver_name: string | null; department_name: string | null;
  signed_at: string | null; is_current_stage: boolean; remark: string | null;
}
interface PpwDetail {
  id: string; status: string;
  field_of_investigation: string | null; minor_field: string | null;
  supporting_field: string | null; research_title: string | null;
  header: { student_name: string; student_roll: string | null; program_name: string | null; department_name: string | null };
  committee: { rows: CommitteeRow[] };
  hod_approval: HodApproval;
}

const STAGE_LABELS: Record<string, string> = {
  major_advisor: "Major Advisor", committee_member: "Advisory Committee Member", hod: "HOD",
};

function SignatureBadge({ status }: { status: string }) {
  if (status === "approved") return <span className="inline-flex items-center gap-1 text-green-700 text-sm font-semibold"><CheckCircle2 size={15} /> Approved</span>;
  if (status === "reverted") return <span className="inline-flex items-center gap-1 text-red-600 text-sm font-semibold"><RotateCcw size={15} /> Reverted</span>;
  if (status === "pending") return <span className="text-amber-600 text-sm font-semibold">Pending</span>;
  if (status === "not_applicable") return <span className="text-gray-400 text-sm">Not applicable</span>;
  return <span className="text-gray-400 text-sm">—</span>;
}

export default function PpwApprovalsPage() {
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { data: pending = [], isLoading } = useQuery<PendingApproval[]>({
    queryKey: ["ams-ppw-pending-approvals"],
    queryFn: async () => (await api.get("/ppw/pending-approvals")).data,
  });

  const { data: detail } = useQuery<PpwDetail>({
    queryKey: ["ams-ppw-detail", selectedId],
    queryFn: async () => (await api.get(`/ppw/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [showRevert, setShowRevert] = useState(false);
  const [revertRemark, setRevertRemark] = useState("");

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/ppw/${selectedId}/approval/otp`),
    onSuccess: (res) => {
      setOtpSent(true);
      toast.success(res.data?.dev_otp ? `OTP sent. Dev OTP: ${res.data.dev_otp}` : "OTP sent to your email.");
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to send OTP."),
  });

  const approve = useMutation({
    mutationFn: () => api.post(`/ppw/${selectedId}/approval/approve`, { otp }),
    onSuccess: () => {
      toast.success("Approved.");
      setOtp(""); setOtpSent(false);
      qc.invalidateQueries({ queryKey: ["ams-ppw-pending-approvals"] });
      qc.invalidateQueries({ queryKey: ["ams-ppw-detail", selectedId] });
      setSelectedId(null);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Approval failed."),
  });

  const revert = useMutation({
    mutationFn: () => api.post(`/ppw/${selectedId}/approval/revert`, { remark: revertRemark }),
    onSuccess: () => {
      toast.success("PPW reverted to the student.");
      setShowRevert(false); setRevertRemark("");
      qc.invalidateQueries({ queryKey: ["ams-ppw-pending-approvals"] });
      setSelectedId(null);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Revert failed."),
  });

  const selectedRow = pending.find((p) => p.ppw_id === selectedId);

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />My PPW Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Programme of Work submissions awaiting your approval.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your action right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Student", "Roll No.", "Research Title", "Department / Programme", "Your Stage", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.ppw_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.research_title ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.department_name ?? "—"} / {row.program_name ?? "—"}</td>
                  <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{STAGE_LABELS[row.stage_type] ?? row.stage_type}</span></td>
                  <td className="px-4 py-2.5 text-gray-500">{new Date(row.submitted_at).toLocaleDateString()}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.ppw_id); setOtp(""); setOtpSent(false); setShowRevert(false); }}
                      className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Review</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selectedId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setSelectedId(null)}>
          <div className="bg-white rounded-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-bold text-gray-900">{detail.header.student_name}'s PPW</h2>
              <button onClick={() => setSelectedId(null)} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
            </div>

            <div className="space-y-1 text-sm text-gray-700 mb-4">
              <p><span className="font-semibold">Roll No.:</span> {detail.header.student_roll ?? "—"}</p>
              <p><span className="font-semibold">Programme / Department:</span> {detail.header.program_name ?? "—"} / {detail.header.department_name ?? "—"}</p>
              <p><span className="font-semibold">Field of Investigation:</span> {detail.field_of_investigation || "—"}</p>
              <p><span className="font-semibold">Minor Field:</span> {detail.minor_field || "—"}</p>
              <p><span className="font-semibold">Supporting Field:</span> {detail.supporting_field || "—"}</p>
              <p><span className="font-semibold">Research Title:</span> {detail.research_title || "—"}</p>
            </div>

            <div className="border-t border-gray-100 pt-4">
              <p className="text-sm font-semibold text-gray-700 mb-2">Advisory Committee</p>
              <div className="space-y-1.5 mb-3">
                {detail.committee.rows.filter((r) => r.signature_status !== "not_applicable").map((row) => (
                  <div key={row.category} className="flex items-center justify-between px-3 py-1.5 bg-gray-50 rounded-lg text-sm">
                    <span>{row.category} — {row.faculty_name ?? "Not assigned"}</span>
                    <SignatureBadge status={row.signature_status} />
                  </div>
                ))}
              </div>
              <div className="flex items-center justify-between px-3 py-1.5 bg-gray-50 rounded-lg text-sm">
                <span>HOD{detail.hod_approval.approver_name ? ` — ${detail.hod_approval.approver_name}` : ""}</span>
                <SignatureBadge status={detail.hod_approval.status} />
              </div>
            </div>

            {selectedRow && (
              <div className="border-t border-gray-100 mt-4 pt-4">
                {!showRevert ? (
                  <>
                    <p className="text-sm font-semibold text-[#0D6E6E] mb-3">Sign &amp; Approve — {STAGE_LABELS[selectedRow.stage_type] ?? selectedRow.stage_type}</p>
                    {!otpSent ? (
                      <div className="flex gap-2">
                        <button onClick={() => requestOtp.mutate()} disabled={requestOtp.isPending}
                          className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-60">
                          {requestOtp.isPending ? "Sending…" : "Send OTP to Email"}
                        </button>
                        <button onClick={() => setShowRevert(true)} className="px-4 py-2 border border-red-300 text-red-600 rounded-xl text-sm font-semibold hover:bg-red-50">Revert</button>
                      </div>
                    ) : (
                      <div className="flex gap-2 items-center flex-wrap">
                        <input value={otp} onChange={(e) => setOtp(e.target.value)} placeholder="Enter 6-digit OTP"
                          className="border border-gray-300 rounded-lg px-3 py-2 text-sm w-40" />
                        <button onClick={() => approve.mutate()} disabled={approve.isPending || !otp}
                          className="px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-60 flex items-center gap-1.5">
                          <CheckCircle2 size={14} /> Approve
                        </button>
                        <button onClick={() => setShowRevert(true)} className="px-4 py-2 border border-red-300 text-red-600 rounded-xl text-sm font-semibold hover:bg-red-50">Revert</button>
                      </div>
                    )}
                  </>
                ) : (
                  <>
                    <p className="text-sm font-semibold text-red-700 mb-2">Revert with remark (required)</p>
                    <textarea value={revertRemark} onChange={(e) => setRevertRemark(e.target.value)} rows={3}
                      placeholder="Explain what needs to be corrected…"
                      className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm mb-2" />
                    <div className="flex gap-2">
                      <button onClick={() => revert.mutate()} disabled={revert.isPending || !revertRemark.trim()}
                        className="px-4 py-2 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-60">
                        {revert.isPending ? "Reverting…" : "Confirm Revert"}
                      </button>
                      <button onClick={() => setShowRevert(false)} className="px-4 py-2 border border-gray-300 text-gray-600 rounded-xl text-sm font-semibold hover:bg-gray-50">Cancel</button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
