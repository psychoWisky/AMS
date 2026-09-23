"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardCheck, Loader2, CheckCircle2, RotateCcw, X, Upload, FileText } from "lucide-react";
import {
  ApprovalHistoryTable, MajorAdvisorAssessment, RevertNotice, StageTimeline, StudentInfoCard, ProgressReportStatusBadge,
  apiErrorMessage, formatDateTime, type ProgressReportDetail,
} from "@/components/ui/progress-report-parts";

// Approver inbox for Major Advisor (Faculty) / Advisory Committee Member (Faculty) / HOD /
// Incharge Academic Cell / DPGS — follows the PPW/Synopsis/Thesis approvals-page pattern.
// Major Advisor gets two SEPARATE actions here: Upload Proceedings and Approve — uploading
// never approves the stage.

interface PendingRow {
  report_id: string; student_name: string; student_roll: string | null;
  session_year: string | null; session_semester: string | null; academic_year: string | null;
  status: string; status_label: string; acting_as: string; requires_otp: boolean;
  cycle_number: number | null; submitted_at: string | null;
}

export default function ProgressReportApprovalsPage() {
  const qc = useQueryClient();
  const role = useRole();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [showRevert, setShowRevert] = useState(false);
  const [remark, setRemark] = useState("");
  const [advisoryRemark, setAdvisoryRemark] = useState("");
  const [overallProgress, setOverallProgress] = useState("");
  const [studentConduct, setStudentConduct] = useState("");
  const [hydratedAdvisorFor, setHydratedAdvisorFor] = useState<string | null>(null);

  const { data: pending = [], isLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-progress-report-pending"],
    queryFn: async () => (await api.get("/progress-reports/pending-approvals")).data,
  });
  const { data: detail } = useQuery<ProgressReportDetail>({
    queryKey: ["ams-progress-report-detail-approver", selectedId],
    queryFn: async () => (await api.get(`/progress-reports/${selectedId}`)).data,
    enabled: !!selectedId,
  });

  function close() { setSelectedId(null); setOtp(""); setOtpSent(false); setShowRevert(false); setRemark(""); }
  function done() {
    qc.invalidateQueries({ queryKey: ["ams-progress-report-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-progress-report-detail-approver", selectedId] });
    setOtp(""); setOtpSent(false); setShowRevert(false);
  }

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/progress-reports/${selectedId}/approval/otp`),
    onSuccess: (res) => { setOtpSent(true); toast.success(res.data?.dev_otp ? `OTP sent. Dev OTP: ${res.data.dev_otp}` : "OTP sent to your email."); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not send the OTP.")),
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/progress-reports/${selectedId}/approval/approve`, detail?.my_pending_stage?.requires_otp ? { otp } : {}),
    onSuccess: () => { toast.success("Approved."); done(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Approval failed.")),
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/progress-reports/${selectedId}/approval/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Reverted."); done(); setSelectedId(null); },
    onError: (e) => toast.error(apiErrorMessage(e, "Revert failed.")),
  });
  const saveAdvisorFields = useMutation({
    mutationFn: () => api.patch(`/progress-reports/${selectedId}/advisor-fields`, {
      advisory_remark: advisoryRemark.trim() || null,
      overall_progress: overallProgress.trim() || null,
      student_conduct: studentConduct.trim() || null,
    }),
    onSuccess: () => { toast.success("Assessment saved."); qc.invalidateQueries({ queryKey: ["ams-progress-report-detail-approver", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the assessment.")),
  });
  const uploadProceedings = useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post(`/progress-reports/${selectedId}/proceedings`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Proceedings uploaded."); qc.invalidateQueries({ queryKey: ["ams-progress-report-detail-approver", selectedId] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Only PDF files are accepted.")),
  });

  function downloadProceedings() { window.open(`${api.defaults.baseURL}/progress-reports/${selectedId}/proceedings`, "_blank"); }

  const stage = detail?.my_pending_stage ?? null;
  const isMajorAdvisor = role === "faculty" && stage?.stage_type === "major_advisor";

  // Hydrate the Major Advisor's own editable fields once per opened report (not on every
  // refetch) — so re-opening after saving, or after a later-stage revert brings the report
  // back to this stage, shows whatever is already stored rather than a blank form.
  if (detail && isMajorAdvisor && hydratedAdvisorFor !== detail.id) {
    setHydratedAdvisorFor(detail.id);
    setAdvisoryRemark(detail.advisory_remark ?? "");
    setOverallProgress(detail.overall_progress ?? "");
    setStudentConduct(detail.student_conduct ?? "");
  }

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Progress Report Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Progress Reports awaiting your action in your current role.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : pending.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your action right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Student", "Roll No.", "Session", "Academic Year", "Your Stage", "Submitted", ""].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {pending.map((row) => (
                <tr key={row.report_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{row.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{row.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.session_year ?? "—"} / {row.session_semester ?? "—"}</td>
                  <td className="px-4 py-2.5">{row.academic_year ?? "—"}</td>
                  <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{row.acting_as}</span></td>
                  <td className="px-4 py-2.5 text-gray-500">{formatDateTime(row.submitted_at)}</td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => { setSelectedId(row.report_id); setOtp(""); setOtpSent(false); setShowRevert(false); }}
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
              <h2 className="text-xl font-bold text-gray-900">{detail.student.name}&apos;s Progress Report</h2>
              <div className="flex items-center gap-3">
                <ProgressReportStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close" className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
              </div>
            </div>

            <StudentInfoCard r={detail} />
            {detail.revert_info && <RevertNotice info={detail.revert_info} />}

            <div className="bg-white rounded-2xl border border-gray-200 p-5 grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <p><span className="font-semibold text-gray-700">Academic Year:</span> {detail.academic_year ?? "—"}</p>
              <p><span className="font-semibold text-gray-700">Semester:</span> {detail.semester_name ?? "—"}</p>
              <p><span className="font-semibold text-gray-700">Session:</span> {detail.session_year ?? "—"} / {detail.session_semester ?? "—"}</p>
              <p><span className="font-semibold text-gray-700">Period:</span> {detail.period_from ?? "—"} – {detail.period_to ?? "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Research Title:</span> {detail.research_title || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Research Progress:</span> {detail.research_progress || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Leave Availed:</span> {detail.leave_availed || "—"}</p>
              <p className="md:col-span-2"><span className="font-semibold text-gray-700">Fellowship/Stipend:</span> {detail.fellowship_stipend || "—"}</p>
              <p><span className="font-semibold text-gray-700">Expected to complete in time:</span> {detail.expected_completion ?? "—"}</p>
              {detail.expected_completion === "No" && (
                <p className="md:col-span-2"><span className="font-semibold text-gray-700">Reason:</span> {detail.completion_delay_reason || "—"}</p>
              )}
            </div>

            <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-3">
              <p className="text-sm font-semibold text-gray-700">Proceedings of the Advisory Committee Meeting</p>
              <div className="flex items-center gap-2">
                {detail.proceedings ? (
                  <button onClick={downloadProceedings} className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50"><FileText size={13} /> View Proceedings ({detail.proceedings.original_filename})</button>
                ) : <p className="text-xs text-gray-400">Not uploaded yet</p>}
                {isMajorAdvisor && (
                  <label className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold cursor-pointer hover:bg-[#178F8F]">
                    <Upload size={12} /> Upload Proceedings
                    <input type="file" accept=".pdf,application/pdf" hidden onChange={(e) => e.target.files?.[0] && uploadProceedings.mutate(e.target.files[0])} />
                  </label>
                )}
              </div>
              {isMajorAdvisor && (
                <div className="border-t border-gray-100 pt-3 space-y-3">
                  <p className="text-xs font-semibold text-gray-600">Major Advisor assessment (separate from Approve):</p>
                  <div>
                    <label className="block text-xs font-semibold text-gray-600 mb-1">Advisory Remark:</label>
                    <textarea rows={2} value={advisoryRemark} onChange={(e) => setAdvisoryRemark(e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-gray-600 mb-1">Overall progress of the student:</label>
                    <textarea rows={2} value={overallProgress} onChange={(e) => setOverallProgress(e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  </div>
                  <div>
                    <label className="block text-xs font-semibold text-gray-600 mb-1">Student Conduct:</label>
                    <textarea rows={2} value={studentConduct} onChange={(e) => setStudentConduct(e.target.value)}
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  </div>
                  <button onClick={() => saveAdvisorFields.mutate()} disabled={saveAdvisorFields.isPending}
                    className="px-3 py-1.5 bg-gray-700 text-white rounded-lg text-xs font-semibold hover:bg-gray-800 disabled:opacity-50">
                    {saveAdvisorFields.isPending ? "Saving…" : "Save Assessment"}
                  </button>
                </div>
              )}
            </div>

            {!isMajorAdvisor && detail.advisory_remark !== undefined && <MajorAdvisorAssessment r={detail} />}

            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Approval Progress</p>
              <StageTimeline stages={detail.stages} />
            </div>
            <div>
              <p className="text-sm font-semibold text-gray-700 mb-2">Approval History</p>
              <ApprovalHistoryTable history={detail.history} />
            </div>

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
                  <div className="space-y-3" data-testid="progress-report-revert-panel">
                    <h3 className="text-lg font-bold text-gray-900">Revert</h3>
                    <p className="text-sm text-gray-600">A remark is required. The report returns one step back in the approval chain.</p>
                    <div>
                      <label className="block text-sm font-semibold text-gray-700 mb-1" htmlFor="progress-report-revert-remark">Remark *</label>
                      <textarea id="progress-report-revert-remark" value={remark} onChange={(e) => setRemark(e.target.value)} rows={4}
                        placeholder="Explain what needs to be corrected"
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
              <p className="border-t border-gray-100 pt-4 text-sm text-gray-500">You have no action pending on this report.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
