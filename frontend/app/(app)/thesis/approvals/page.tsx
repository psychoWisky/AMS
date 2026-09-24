"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardCheck, Loader2, CheckCircle2, RotateCcw, X, Upload } from "lucide-react";
import {
  ExternalReportTable, RevertNotice, SignatureTable, StageTimeline, StudentInfoCard, ThesisStatusBadge,
  STUDENT_DOCUMENT_LABELS, apiErrorMessage, formatDateTime, type ThesisDetail,
} from "@/components/ui/thesis-parts";

// Approver inbox for Major Advisor (Faculty) / HOD / Librarian / Incharge Academic Cell / DPGS —
// follows the PPW/Synopsis/External Examiner approvals-page pattern exactly. There is no student
// variant. DPGS appears here twice in the workflow (send-for-evaluation, then final approval of
// the external evaluation) — both surface through the same inbox, distinguished by `acting_as`.

interface PendingRow {
  thesis_id: string; student_name: string; student_roll: string | null; degree_name: string | null;
  department_name: string | null; title: string | null; status: string; status_label: string;
  acting_as: string; requires_otp: boolean; cycle_number: number | null; submitted_at: string | null;
}

export default function ThesisApprovalsPage() {
  const qc = useQueryClient();
  const role = useRole();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [showRevert, setShowRevert] = useState(false);
  const [remark, setRemark] = useState("");
  const [libPercent, setLibPercent] = useState("");

  const { data: pending = [], isLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-thesis-pending"],
    queryFn: async () => (await api.get("/thesis/pending-approvals")).data,
  });
  const { data: detail } = useQuery<ThesisDetail>({
    queryKey: ["ams-thesis-detail-approver", selectedId],
    queryFn: async () => (await api.get(`/thesis/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  function close() { setSelectedId(null); setOtp(""); setOtpSent(false); setShowRevert(false); setRemark(""); setLibPercent(""); }
  function done() {
    qc.invalidateQueries({ queryKey: ["ams-thesis-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-thesis-detail-approver", selectedId] });
    setOtp(""); setOtpSent(false); setShowRevert(false);
  }

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/thesis/${selectedId}/approval/otp`),
    onSuccess: (res) => { setOtpSent(true); toast.success(res.data?.dev_otp ? `OTP sent. Dev OTP: ${res.data.dev_otp}` : "OTP sent to your email."); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not send the OTP.")),
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/thesis/${selectedId}/approval/approve`, detail?.my_pending_stage?.requires_otp ? { otp } : {}),
    onSuccess: () => { toast.success("Approved."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Approval failed.")),
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/thesis/${selectedId}/approval/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Reverted to the student."); done(); setSelectedId(null); },
    onError: (e) => toast.error(apiErrorMessage(e, "Revert failed.")),
  });
  const generateCertificateI = useMutation({
    mutationFn: () => api.post(`/thesis/${selectedId}/certificate-i/generate`),
    onSuccess: () => { toast.success("Certificate I generated and signed."); qc.invalidateQueries({ queryKey: ["ams-thesis-detail-approver", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not generate Certificate I.")),
  });
  const setLibraryPlagiarism = useMutation({
    mutationFn: () => api.patch(`/thesis/${selectedId}/library-plagiarism`, { plagiarism_library_percent: Number(libPercent) }),
    onSuccess: () => { toast.success("Library plagiarism percentage saved."); qc.invalidateQueries({ queryKey: ["ams-thesis-detail-approver", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the percentage.")),
  });
  const uploadLibraryReport = useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post(`/thesis/${selectedId}/library-plagiarism/report`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Library plagiarism report uploaded."); qc.invalidateQueries({ queryKey: ["ams-thesis-detail-approver", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Only .docx files are accepted.")),
  });

  const stage = detail?.my_pending_stage ?? null;
  const isLibrarian = role === "librarian" && stage?.stage_type === "librarian";
  const isMajorAdvisorStage = stage?.stage_type === "major_advisor";
  const certificateIReady = !!detail?.documents.certificate_i_pg27;

  function download(documentId: string) {
    window.open(`${api.defaults.baseURL}/thesis/${selectedId}/documents/${documentId}/download`, "_blank");
  }

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Thesis Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Initial Thesis applications awaiting your action in your current role.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your action right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Student", "Roll No.", "Degree", "Department", "Your Stage", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.thesis_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.degree_name ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.department_name ?? "—"}</td>
                  <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{row.acting_as}</span></td>
                  <td className="px-4 py-2.5 text-gray-500">{formatDateTime(row.submitted_at)}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.thesis_id); setOtp(""); setOtpSent(false); setShowRevert(false); }}
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
              <h2 className="text-xl font-bold text-gray-900">{detail.student.name}&apos;s Initial Thesis</h2>
              <div className="flex items-center gap-3">
                <ThesisStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close" className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
              </div>
            </div>

            <StudentInfoCard student={detail.student} />
            {detail.revert_info && <RevertNotice info={detail.revert_info} />}

            <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-1">
              <p className="text-sm font-semibold text-gray-700">Thesis Title</p>
              <p className="text-gray-900">{detail.title || "—"}</p>
              <div className="flex items-center gap-2 pt-2">
                {detail.documents.thesis_file && <button onClick={() => download(detail.documents.thesis_file!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Thesis File</button>}
                {detail.documents.plagiarism_student_report && <button onClick={() => download(detail.documents.plagiarism_student_report!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Student Plagiarism Report</button>}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-2">
              <p className="text-sm font-semibold text-gray-700">Plagiarism by Student: {detail.plagiarism_student_percent ?? "—"}%</p>
              <p className="text-sm font-semibold text-gray-700">Plagiarism by Library: {detail.plagiarism_library_percent ?? "—"}%</p>
              {detail.documents.plagiarism_library_report && (
                <button onClick={() => download(detail.documents.plagiarism_library_report!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Library Plagiarism Report</button>
              )}
              {isLibrarian && (
                <div className="border-t border-gray-100 pt-3 space-y-2">
                  <p className="text-xs font-semibold text-gray-600">Librarian actions (separate from Approve):</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <input type="number" min={0} max={100} placeholder="Library plagiarism %" value={libPercent} onChange={(e) => setLibPercent(e.target.value)}
                      className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm w-48" />
                    <button onClick={() => setLibraryPlagiarism.mutate()} disabled={!libPercent || setLibraryPlagiarism.isPending}
                      className="px-3 py-1.5 bg-gray-700 text-white rounded-lg text-xs font-semibold hover:bg-gray-800 disabled:opacity-50">Save %</button>
                    <label className="flex items-center gap-1 px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-semibold cursor-pointer hover:bg-gray-50">
                      <Upload size={12} /> Upload Report
                      <input type="file" accept=".docx" hidden onChange={(e) => e.target.files?.[0] && uploadLibraryReport.mutate(e.target.files[0])} />
                    </label>
                  </div>
                </div>
              )}
            </div>

            {STUDENT_DOCUMENT_LABELS && (
              <div className="bg-white rounded-2xl border border-gray-200 p-5">
                <p className="text-sm font-semibold text-gray-700 mb-2">Other Student Documents</p>
                <div className="flex flex-wrap gap-2">
                  {Object.keys(STUDENT_DOCUMENT_LABELS).filter((t) => t !== "thesis_file" && t !== "plagiarism_student_report" && detail.documents[t]).map((t) => (
                    <button key={t} onClick={() => download(detail.documents[t]!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">{STUDENT_DOCUMENT_LABELS[t]}</button>
                  ))}
                </div>
              </div>
            )}

            {detail.signature_table && <SignatureTable rows={detail.signature_table} />}
            {detail.stages && <StageTimeline stages={detail.stages} />}
            {detail.external_report && <ExternalReportTable rows={detail.external_report} />}

            {isMajorAdvisorStage && (
              <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-2">
                <p className="text-sm font-semibold text-gray-700">Certificate I (Form No. PG 27)</p>
                <p className="text-xs text-gray-500">
                  {certificateIReady ? "Generated and signed. You may proceed to approve this Initial Thesis." : "You must generate Certificate I for this submission before approving."}
                </p>
                <div className="flex items-center gap-2">
                  {detail.documents.certificate_i_pg27 && (
                    <button onClick={() => download(detail.documents.certificate_i_pg27!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Certificate I</button>
                  )}
                  {!certificateIReady && (
                    <button onClick={() => generateCertificateI.mutate()} disabled={generateCertificateI.isPending}
                      className="px-3 py-1.5 bg-gray-700 text-white rounded-lg text-xs font-semibold hover:bg-gray-800 disabled:opacity-50">
                      {generateCertificateI.isPending ? "Generating…" : "Generate Certificate I"}
                    </button>
                  )}
                </div>
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
                    {stage.stage_type !== "dpgs_final" && (
                      <button onClick={() => setShowRevert(true)}
                        className="flex items-center gap-1.5 px-4 py-2 border border-red-300 text-red-700 rounded-xl text-sm font-semibold hover:bg-red-50">
                        <RotateCcw size={15} /> Revert
                      </button>
                    )}
                  </div>
                ) : (
                  <div className="space-y-3" data-testid="thesis-revert-panel">
                    <h3 className="text-lg font-bold text-gray-900">Revert to Student</h3>
                    <p className="text-sm text-gray-600">The thesis returns to the student, who will see your remark and can correct and resubmit.</p>
                    <div>
                      <label className="block text-sm font-semibold text-gray-700 mb-1" htmlFor="thesis-revert-remark">Remark *</label>
                      <textarea id="thesis-revert-remark" value={remark} onChange={(e) => setRemark(e.target.value)} rows={4}
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
              <p className="border-t border-gray-100 pt-4 text-sm text-gray-500">You have no action pending on this thesis.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
