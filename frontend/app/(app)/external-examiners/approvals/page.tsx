"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardCheck, Loader2, CheckCircle2, RotateCcw, X, Pencil } from "lucide-react";
import {
  ProposalTable, RevertNotice, SelectionStatusBadge, StageTimeline, StudentInfoCard,
  apiErrorMessage, formatDateTime, type ExaminerProposal, type SelectionDetail,
} from "@/components/ui/external-examiner-parts";

// Approver inbox for HOD / Faculty (as Major Advisor) / Incharge Academic Cell / DPGS / Vice
// Chancellor — follows the PPW/Synopsis approvals-page pattern exactly. There is no student variant.

interface PendingRow {
  selection_id: string; student_name: string; student_roll: string | null; program_name: string | null;
  department_name: string | null; degree_level: string; status: string; status_label: string;
  acting_as: string; requires_otp: boolean; cycle_number: number | null; submitted_at: string | null;
}

const EDIT_FIELDS = ["name", "specialization", "designation", "email", "phone", "institution"] as const;

export default function ExternalExaminerApprovalsPage() {
  const qc = useQueryClient();
  const role = useRole();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [showRevert, setShowRevert] = useState(false);
  const [remark, setRemark] = useState("");
  const [editing, setEditing] = useState<ExaminerProposal | null>(null);
  const [editForm, setEditForm] = useState<Record<string, string>>({});
  const [vcPicks, setVcPicks] = useState<Set<string>>(new Set());

  const { data: pending = [], isLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-ee-pending"],
    queryFn: async () => (await api.get("/external-examiners/pending-approvals")).data,
  });
  const { data: detail } = useQuery<SelectionDetail>({
    queryKey: ["ams-ee-detail", selectedId],
    queryFn: async () => (await api.get(`/external-examiners/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  function close() { setSelectedId(null); setOtp(""); setOtpSent(false); setShowRevert(false); setRemark(""); setEditing(null); setVcPicks(new Set()); }
  function done() {
    qc.invalidateQueries({ queryKey: ["ams-ee-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-ee-detail", selectedId] });
    setOtp(""); setOtpSent(false); setShowRevert(false); setEditing(null);
  }

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/external-examiners/${selectedId}/approval/otp`),
    onSuccess: (res) => { setOtpSent(true); toast.success(res.data?.dev_otp ? `OTP sent. Dev OTP: ${res.data.dev_otp}` : "OTP sent to your email."); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not send the OTP.")),
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/external-examiners/${selectedId}/approval/approve`, detail?.my_pending_stage?.requires_otp ? { otp } : {}),
    onSuccess: () => { toast.success("Approved."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Approval failed.")),
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/external-examiners/${selectedId}/approval/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Reverted to the Major Advisor."); done(); setSelectedId(null); },
    onError: (e) => toast.error(apiErrorMessage(e, "Revert failed.")),
  });
  const saveEdit = useMutation({
    mutationFn: () => api.patch(`/external-examiners/${selectedId}/proposals/${editing?.id}`, editForm),
    onSuccess: () => { toast.success("Proposal updated."); setEditing(null); qc.invalidateQueries({ queryKey: ["ams-ee-detail", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the correction.")),
  });
  const vcSelect = useMutation({
    mutationFn: () => api.post(`/external-examiners/${selectedId}/vc-selection`, { proposal_ids: Array.from(vcPicks) }),
    onSuccess: () => { toast.success("Selection confirmed."); done(); setVcPicks(new Set()); },
    onError: (e) => toast.error(apiErrorMessage(e, "Selection failed.")),
  });

  function openEdit(p: ExaminerProposal) {
    setEditing(p);
    setEditForm({ name: p.name, specialization: p.specialization, designation: p.designation, email: p.email, phone: p.phone, institution: p.institution });
  }
  function toggleVcPick(id: string) {
    setVcPicks((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else if (!detail || next.size < detail.required_selection_count) next.add(id);
      return next;
    });
  }

  const stage = detail?.my_pending_stage ?? null;
  const isIncharge = role === "incharge_academic_cell";
  const isVc = role === "vice_chancellor" && stage?.stage_type === "vc";

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />External Examiner Approvals</h1>
        <p className="text-gray-600 text-base mt-1">External Examiner selections awaiting your action in your current role.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your action right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Student", "Roll No.", "Degree", "Department / Programme", "Your Stage", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.selection_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.degree_level}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.department_name ?? "—"} / {row.program_name ?? "—"}</td>
                  <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{row.acting_as}</span></td>
                  <td className="px-4 py-2.5 text-gray-500">{formatDateTime(row.submitted_at)}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.selection_id); setOtp(""); setOtpSent(false); setShowRevert(false); }}
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
          <div className="bg-white rounded-2xl max-w-4xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-xl font-bold text-gray-900">{detail.student.name}&apos;s External Examiner Selection</h2>
              <div className="flex items-center gap-3">
                <SelectionStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close" className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
              </div>
            </div>

            <StudentInfoCard student={detail.student} degreeLevel={detail.degree_level} />
            {detail.revert_info && <RevertNotice info={detail.revert_info} />}

            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">
                Proposed examiners{isVc ? ` — select exactly ${detail.required_selection_count}` : ""}
              </p>
              {isVc ? (
                <>
                  <ProposalTable proposals={detail.proposals} selectable selected={vcPicks} onToggle={toggleVcPick} maxSelectable={detail.required_selection_count} />
                  <p className="text-xs text-gray-500 mt-1">{vcPicks.size} of {detail.required_selection_count} selected.</p>
                </>
              ) : (
                <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="bg-gray-50 border-b border-gray-200">
                      <tr>{["Slot", "Name", "Specialization", "Designation", "Email", "Phone", "Institution", ...(isIncharge && detail.can_edit === false && stage?.stage_type === "incharge_academic_cell" ? ["Edit"] : isIncharge && stage?.stage_type === "incharge_academic_cell" ? ["Edit"] : [])].map((h) => (
                        <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
                      ))}</tr>
                    </thead>
                    <tbody>
                      {detail.proposals.map((p) => (
                        <tr key={p.id} className="border-b border-gray-50 last:border-0">
                          <td className="px-4 py-2.5">{p.slot_number}</td>
                          <td className="px-4 py-2.5 font-semibold text-gray-800">{p.name}{p.edited && <span className="ml-1.5 text-xs font-normal text-amber-700">(edited)</span>}</td>
                          <td className="px-4 py-2.5">{p.specialization}</td>
                          <td className="px-4 py-2.5">{p.designation}</td>
                          <td className="px-4 py-2.5">{p.email}</td>
                          <td className="px-4 py-2.5">{p.phone}</td>
                          <td className="px-4 py-2.5">{p.institution}</td>
                          {isIncharge && stage?.stage_type === "incharge_academic_cell" && (
                            <td className="px-4 py-2.5"><button onClick={() => openEdit(p)} title="Edit" className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><Pencil size={14} /></button></td>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Approval progress</p>
              <StageTimeline stages={detail.stages} />
            </div>

            {stage ? (
              <div className="border-t border-gray-100 pt-4">
                <p className="text-sm text-gray-700 mb-3">You are acting as <span className="font-semibold">{stage.role_label}</span>.</p>
                {isVc ? (
                  <div className="flex gap-3">
                    <button onClick={() => vcSelect.mutate()} disabled={vcPicks.size !== detail.required_selection_count || vcSelect.isPending}
                      className="flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                      <CheckCircle2 size={15} /> {vcSelect.isPending ? "Confirming…" : "Confirm Selection"}
                    </button>
                    <button onClick={() => setShowRevert(true)} className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 rounded-xl text-sm font-semibold hover:bg-red-50">
                      <RotateCcw size={15} /> Revert
                    </button>
                  </div>
                ) : !showRevert ? (
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
                  <div className="space-y-3" data-testid="ee-revert-panel">
                    <h3 className="text-lg font-bold text-gray-900">Revert to Major Advisor</h3>
                    <p className="text-sm text-gray-600">The selection returns to the Major Advisor, who will see your remark and can correct and resubmit.</p>
                    <div>
                      <label className="block text-sm font-semibold text-gray-700 mb-1" htmlFor="ee-revert-remark">Remark *</label>
                      <textarea id="ee-revert-remark" value={remark} onChange={(e) => setRemark(e.target.value)} rows={4}
                        placeholder="Explain what the Major Advisor needs to correct"
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
              <p className="border-t border-gray-100 pt-4 text-sm text-gray-500">You have no action pending on this selection.</p>
            )}
          </div>
        </div>
      )}

      {editing && (
        <div className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center p-4" onClick={() => setEditing(null)}>
          <div className="bg-white rounded-2xl max-w-lg w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">Edit Examiner (Slot {editing.slot_number})</h3>
              <button onClick={() => setEditing(null)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <div className="grid grid-cols-1 gap-3">
              {EDIT_FIELDS.map((field) => (
                <div key={field}>
                  <label className="block text-xs font-semibold text-gray-600 mb-1 capitalize">{field}</label>
                  <input value={editForm[field] ?? ""} onChange={(e) => setEditForm((f) => ({ ...f, [field]: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              ))}
            </div>
            <div className="flex gap-3">
              <button onClick={() => setEditing(null)} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={() => saveEdit.mutate()} disabled={saveEdit.isPending}
                className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                {saveEdit.isPending ? "Saving…" : "Save Correction"}
              </button>
            </div>
            <p className="text-xs text-gray-500">Saving does not approve this proposal — approve separately once you are satisfied.</p>
          </div>
        </div>
      )}
    </div>
  );
}
