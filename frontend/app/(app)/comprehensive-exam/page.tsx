"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, blobErrorMessage, viewFileInNewTab } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { ClipboardCheck, Loader2, Plus, X, CheckCircle2, RotateCcw, Eye, Upload, UserCheck } from "lucide-react";
import {
  ApplicationDetail, ApplicationListItem, ComprehensiveExamStatusBadge, apiErrorMessage, formatDate, formatDateTime,
  PanelDetail, PanelPendingRow, PANEL_STAGE_LABELS, ExternalReportDetail, EXTERNAL_REPORT_STAGE_LABELS,
} from "@/components/ui/comprehensive-exam-parts";
import { ProposalTable, type ExaminerProposal } from "@/components/ui/external-examiner-parts";

// Comprehensive Examination — one page serves the student (own application) and every
// approver role (Major Advisor/HOD/Incharge Academic Cell/DPGS/Vice Chancellor), exactly like
// Advisory Committee's research/page.tsx. Backend endpoints are role-scoped independently —
// this page never filters for security, only for the right UI.

const APPLICATION_TYPE_OPTIONS = ["APPLICATION FOR HOLDING COMPREHENSIVE EXAMINATION"];

export default function ComprehensiveExamPage() {
  const role = useRole();
  return role === "student" ? <StudentView /> : <ApproverView />;
}

// ── Student view ──────────────────────────────────────────────────────────────

function StudentView() {
  const qc = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [selectedType, setSelectedType] = useState(APPLICATION_TYPE_OPTIONS[0]);
  const [openId, setOpenId] = useState<string | null>(null);

  const { data: applications = [], isLoading } = useQuery<ApplicationListItem[]>({
    queryKey: ["ams-comp-exam-applications"],
    queryFn: async () => (await api.get("/comprehensive-exam/applications")).data,
  });

  const create = useMutation({
    mutationFn: () => api.post("/comprehensive-exam/applications", {}),
    onSuccess: () => {
      toast.success("Application submitted.");
      qc.invalidateQueries({ queryKey: ["ams-comp-exam-applications"] });
      setShowCreate(false);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to submit application.")),
  });

  return (
    <div className="p-6 w-full space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Comprehensive Examination</h1>
          <p className="text-gray-600 text-base mt-1">Application, Viva, and Viva Report status.</p>
        </div>
        {applications.length === 0 && (
          <button onClick={() => setShowCreate(true)} className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Create Application
          </button>
        )}
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : applications.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">No Comprehensive Examination application yet.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Sl No.", "Application", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
            <tbody>
              {applications.map((a, i) => (
                <tr key={a.id} className="border-b border-gray-50 last:border-0">
                  <td className="px-4 py-2.5">{i + 1}</td>
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{a.application_label}</td>
                  <td className="px-4 py-2.5"><ComprehensiveExamStatusBadge status={a.status} /></td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => setOpenId(a.id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">
                      <Eye size={13} /> View Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showCreate && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setShowCreate(false)}>
          <div className="bg-white rounded-2xl max-w-lg w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">Create Comprehensive Exam Application</h3>
              <button onClick={() => setShowCreate(false)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <div>
              <label className="block text-sm font-semibold text-gray-700 mb-1">Select Application</label>
              <select value={selectedType} onChange={(e) => setSelectedType(e.target.value)}
                className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                {APPLICATION_TYPE_OPTIONS.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <p className="text-xs text-gray-500 mt-2">Eligibility (Major ≥ 20 credits, Minor ≥ 8 credits, excluding research) is verified automatically by the server.</p>
            </div>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowCreate(false)} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Close</button>
              <button onClick={() => create.mutate()} disabled={create.isPending} className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                {create.isPending ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {openId && <DetailModal applicationId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}

// ── Approver view (Major Advisor / HOD / Incharge / DPGS / Vice Chancellor) ───────────────

const _PANEL_ROLES = ["hod", "incharge_academic_cell", "dpgs", "vice_chancellor"];

function ApproverView() {
  const role = useRole();
  const [openId, setOpenId] = useState<string | null>(null);
  const { data: applications = [], isLoading } = useQuery<ApplicationListItem[]>({
    queryKey: ["ams-comp-exam-applications"],
    queryFn: async () => (await api.get("/comprehensive-exam/applications")).data,
  });
  const { data: panels = [], isLoading: panelsLoading } = useQuery<PanelPendingRow[]>({
    queryKey: ["ams-comp-exam-panel-pending"],
    queryFn: async () => (await api.get("/comprehensive-exam/external-panel/pending-approvals")).data,
    enabled: _PANEL_ROLES.includes(role ?? ""),
  });

  return (
    <div className="p-6 w-full space-y-8">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardCheck size={24} className="text-[#0D6E6E]" />Comprehensive Examination</h1>
        <p className="text-gray-600 text-base mt-1">Applications relevant to your role.</p>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
      ) : applications.length === 0 ? (
        <div className="bg-white rounded-2xl border border-gray-200 p-12 text-center text-gray-500">Nothing pending your review right now.</div>
      ) : (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Student", "Roll No.", "Degree", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
            <tbody>
              {applications.map((a) => (
                <tr key={a.id} className="border-b border-gray-50 last:border-0">
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{a.student_name ?? "—"}</td>
                  <td className="px-4 py-2.5 text-gray-600">{a.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{a.degree_level}</td>
                  <td className="px-4 py-2.5"><ComprehensiveExamStatusBadge status={a.status} /></td>
                  <td className="px-4 py-2.5">
                    <button onClick={() => setOpenId(a.id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">
                      <Eye size={13} /> View Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* PhD External Examiner Panel dashboard — only students eligible/in-progress per the
          backend's own authoritative rules (GET /external-panel/pending-approvals); the
          frontend never re-derives eligibility itself. */}
      {_PANEL_ROLES.includes(role ?? "") && (
        <div className="space-y-3">
          <h2 className="text-xl font-bold text-gray-900 flex items-center gap-2"><UserCheck size={20} className="text-[#0D6E6E]" />PhD External Examiner Panels</h2>
          {panelsLoading ? (
            <div className="flex justify-center py-10"><Loader2 className="animate-spin text-gray-600" /></div>
          ) : panels.length === 0 ? (
            <div className="bg-white rounded-2xl border border-gray-200 p-10 text-center text-gray-500">No External Panels are currently available.</div>
          ) : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Student", "Roll No.", "Department", "Programme", "Current Stage", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
                <tbody>
                  {panels.map((p) => (
                    <tr key={p.cycle_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{p.student_name ?? "—"}</td>
                      <td className="px-4 py-2.5 text-gray-600">{p.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5">{p.department_name ?? "—"}</td>
                      <td className="px-4 py-2.5">{p.degree_level}</td>
                      <td className="px-4 py-2.5"><ComprehensiveExamStatusBadge status={p.current_stage} label={PANEL_STAGE_LABELS[p.current_stage]} /></td>
                      <td className="px-4 py-2.5">
                        <button onClick={() => setOpenId(p.application_id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">
                          <Eye size={13} /> View Details
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {openId && <DetailModal applicationId={openId} onClose={() => setOpenId(null)} />}
    </div>
  );
}

// ── Shared Detail modal (application + viva + internal report + external panel/report) ────

function DetailModal({ applicationId, onClose }: { applicationId: string; onClose: () => void }) {
  const role = useRole();
  const user = useUser();
  const qc = useQueryClient();
  const [remark, setRemark] = useState("");
  const [showRevertFor, setShowRevertFor] = useState<null | { kind: "application" | "report"; id: string }>(null);
  const [vivaDate, setVivaDate] = useState("");

  const { data: detail, isLoading } = useQuery<ApplicationDetail>({
    queryKey: ["ams-comp-exam-detail", applicationId],
    queryFn: async () => (await api.get(`/comprehensive-exam/applications/${applicationId}`)).data,
  });

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["ams-comp-exam-applications"] });
    qc.invalidateQueries({ queryKey: ["ams-comp-exam-detail", applicationId] });
  }

  const approveApp = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/applications/${applicationId}/approve`),
    onSuccess: () => { toast.success("Approved."); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to approve.")),
  });
  const revertApp = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/applications/${applicationId}/revert`, { remark }),
    onSuccess: () => { toast.success("Reverted."); setShowRevertFor(null); setRemark(""); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to revert.")),
  });
  const scheduleViva = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/applications/${applicationId}/viva`, { viva_date: new Date(vivaDate).toISOString() }),
    onSuccess: () => { toast.success("Viva scheduled."); setVivaDate(""); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to schedule Viva.")),
  });
  const recordResult = useMutation({
    mutationFn: (result: "satisfactory" | "unsatisfactory") => {
      const latestViva = detail?.vivas[detail.vivas.length - 1];
      return api.post(`/comprehensive-exam/vivas/${latestViva?.id}/result`, { result });
    },
    onSuccess: () => { toast.success("Result recorded."); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to record result.")),
  });
  const submitReport = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/viva-reports/${detail?.latest_viva_report?.id}/submit`),
    onSuccess: () => { toast.success("Submitted."); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to submit.")),
  });
  const signReport = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/viva-reports/${detail?.latest_viva_report?.id}/committee-sign`),
    onSuccess: () => { toast.success("Signed."); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to sign.")),
  });
  const approveReport = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/viva-reports/${detail?.latest_viva_report?.id}/approve`),
    onSuccess: () => { toast.success("Approved."); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to approve.")),
  });
  const revertReport = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/viva-reports/${detail?.latest_viva_report?.id}/revert`, { remark }),
    onSuccess: () => { toast.success("Reverted."); setShowRevertFor(null); setRemark(""); invalidate(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to revert.")),
  });

  async function viewDoc(path: string, label: string) {
    try {
      await viewFileInNewTab(path);
    } catch (e) {
      toast.error(await blobErrorMessage(e, `Could not open the ${label}.`));
    }
  }

  const isMA = role === "faculty" && detail?.ma_id === user?.id;
  const canActOnApplication = ["hod", "incharge_academic_cell", "dpgs", "super_admin"].includes(role ?? "") || isMA;
  const latestViva = detail?.vivas[detail.vivas.length - 1];

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={onClose}>
      <div className="bg-white rounded-2xl max-w-3xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-5" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold text-gray-900">APPLICATION FOR HOLDING COMPREHENSIVE EXAMINATION</h2>
          <button onClick={onClose} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
        </div>

        {isLoading || !detail ? (
          <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
        ) : (
          <div className="space-y-6">
            {/* Application */}
            <div className="bg-gray-50 rounded-xl p-4 space-y-2">
              <div className="flex items-center justify-between">
                <p className="font-semibold text-gray-800">Application — {detail.degree_level}</p>
                <ComprehensiveExamStatusBadge status={detail.status} />
              </div>
              <p className="text-sm text-gray-600">Major: {detail.major_credits_completed}/{detail.major_credits_required} credits · Minor: {detail.minor_credits_completed}/{detail.minor_credits_required} credits</p>
              {detail.status === "reverted" && detail.revert_remark && <p className="text-sm text-red-600">Revert remark: {detail.revert_remark}</p>}
              <div className="flex flex-wrap gap-2 pt-2">
                <button onClick={() => viewDoc(`/comprehensive-exam/applications/${detail.id}/document`, "application")} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View / Print</button>
                {["ma_pending", "hod_pending", "incharge_pending", "dpgs_pending"].includes(detail.status) && canActOnApplication && (
                  <>
                    <button onClick={() => approveApp.mutate()} disabled={approveApp.isPending} className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50"><CheckCircle2 size={13} /> Approve</button>
                    <button onClick={() => setShowRevertFor({ kind: "application", id: detail.id })} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><RotateCcw size={13} /> Revert</button>
                  </>
                )}
              </div>
            </div>

            {/* Viva */}
            {detail.status === "approved" && (
              <div className="bg-gray-50 rounded-xl p-4 space-y-2">
                <p className="font-semibold text-gray-800">Viva</p>
                {detail.vivas.map((v) => (
                  <p key={v.id} className="text-sm text-gray-600">Attempt {v.attempt_number}: {formatDate(v.viva_date)} — {v.result}</p>
                ))}
                {role === "hod" && (!latestViva || latestViva.result === "unsatisfactory") && (
                  <div className="flex items-center gap-2 pt-2">
                    <input type="datetime-local" value={vivaDate} onChange={(e) => setVivaDate(e.target.value)} className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm" />
                    <button onClick={() => scheduleViva.mutate()} disabled={!vivaDate || scheduleViva.isPending} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50">Schedule Viva</button>
                  </div>
                )}
                {isMA && latestViva && latestViva.result === "pending" && (
                  <div className="flex gap-2 pt-2">
                    <button onClick={() => recordResult.mutate("satisfactory")} disabled={recordResult.isPending} className="px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">Satisfactory</button>
                    <button onClick={() => recordResult.mutate("unsatisfactory")} disabled={recordResult.isPending} className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-semibold hover:bg-red-700 disabled:opacity-50">Unsatisfactory</button>
                  </div>
                )}
              </div>
            )}

            {/* Internal Viva Report */}
            {detail.latest_viva_report && (
              <div className="bg-gray-50 rounded-xl p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <p className="font-semibold text-gray-800">Internal Viva Report</p>
                  <ComprehensiveExamStatusBadge status={detail.latest_viva_report.status} />
                </div>
                <div className="flex flex-wrap gap-2">
                  <button onClick={() => viewDoc(`/comprehensive-exam/viva-reports/${detail.latest_viva_report!.id}/document`, "Viva Report")} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View / Print</button>
                  {isMA && detail.latest_viva_report.status === "generated" && (
                    <button onClick={() => submitReport.mutate()} disabled={submitReport.isPending} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50">Submit</button>
                  )}
                  {role === "faculty" && !isMA && detail.latest_viva_report.status === "committee_pending" && detail.latest_viva_report.signatures.some((s) => s.faculty_id === user?.id && s.status === "pending") && (
                    <button onClick={() => signReport.mutate()} disabled={signReport.isPending} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50">Sign</button>
                  )}
                  {["hod_pending", "incharge_pending", "dpgs_pending"].includes(detail.latest_viva_report.status) && ["hod", "incharge_academic_cell", "dpgs", "super_admin"].includes(role ?? "") && (
                    <>
                      <button onClick={() => approveReport.mutate()} disabled={approveReport.isPending} className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50"><CheckCircle2 size={13} /> Approve</button>
                      <button onClick={() => setShowRevertFor({ kind: "report", id: detail.latest_viva_report!.id })} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><RotateCcw size={13} /> Revert</button>
                    </>
                  )}
                </div>
              </div>
            )}

            {/* PhD External Panel + External Viva Report */}
            {detail.degree_level === "PhD" && detail.latest_viva_report?.status === "approved" && (
              <ExternalPanelSection
                applicationId={detail.id} isMA={isMA} role={role} userId={user?.id}
                externalPanel={detail.external_panel} externalReport={detail.external_viva_report}
                onChanged={invalidate} viewDoc={viewDoc}
              />
            )}
          </div>
        )}

        {showRevertFor && (
          <div className="border-t border-gray-100 pt-4 space-y-3">
            <h3 className="text-base font-bold text-gray-900">Revert</h3>
            <textarea value={remark} onChange={(e) => setRemark(e.target.value)} rows={3} placeholder="A remark is required"
              className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
            <div className="flex gap-3">
              <button onClick={() => { setShowRevertFor(null); setRemark(""); }} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button
                onClick={() => {
                  if (!remark.trim()) { toast.error("A remark is required."); return; }
                  if (showRevertFor.kind === "application") revertApp.mutate();
                  else if (showRevertFor.kind === "report") revertReport.mutate();
                }}
                disabled={revertApp.isPending || revertReport.isPending}
                className="px-4 py-2 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-50">
                Confirm Revert
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── PhD External Panel + External Viva Report sub-section ─────────────────────────────────

function ExternalPanelSection({
  applicationId, isMA, role, userId, externalPanel, externalReport, onChanged, viewDoc,
}: {
  applicationId: string; isMA: boolean; role: string | null | undefined; userId: string | undefined;
  externalPanel: ApplicationDetail["external_panel"]; externalReport: ApplicationDetail["external_viva_report"];
  onChanged: () => void; viewDoc: (path: string, label: string) => void;
}) {
  const qc = useQueryClient();
  const [showPropose, setShowPropose] = useState(false);
  const [proposals, setProposals] = useState(Array.from({ length: 5 }, () => ({ name: "", specialization: "", designation: "", email: "", phone: "", institution: "" })));
  const [showRevertPanel, setShowRevertPanel] = useState(false);
  const [panelRemark, setPanelRemark] = useState("");
  const [vcPick, setVcPick] = useState<string | null>(null);

  const cycleId = externalPanel?.cycle_id ?? null;
  const panelKey = ["ams-comp-exam-panel", cycleId];
  const { data: panel } = useQuery<PanelDetail>({
    queryKey: panelKey,
    queryFn: async () => (await api.get(`/comprehensive-exam/external-panel/${cycleId}`)).data,
    enabled: !!cycleId,
  });

  function invalidatePanel() {
    qc.invalidateQueries({ queryKey: panelKey });
    qc.invalidateQueries({ queryKey: ["ams-comp-exam-panel-pending"] });
    onChanged();
  }

  const propose = useMutation({
    mutationFn: () => api.post("/comprehensive-exam/external-panel", { application_id: applicationId, proposals }),
    onSuccess: () => { toast.success("External Panel proposed."); setShowPropose(false); invalidatePanel(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to propose External Panel.")),
  });
  const approvePanel = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-panel/${cycleId}/approve`),
    onSuccess: () => { toast.success("Approved."); invalidatePanel(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to approve.")),
  });
  const revertPanel = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-panel/${cycleId}/revert`, { remark: panelRemark }),
    onSuccess: () => { toast.success("External Panel reverted."); setShowRevertPanel(false); setPanelRemark(""); invalidatePanel(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to revert.")),
  });
  const vcSelect = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-panel/${cycleId}/vc-selection`, { proposal_id: vcPick }),
    onSuccess: () => { toast.success("External Examiner selected and notified."); setVcPick(null); invalidatePanel(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to select the External Examiner.")),
  });

  function updateProposal(idx: number, field: string, value: string) {
    setProposals((prev) => prev.map((p, i) => (i === idx ? { ...p, [field]: value } : p)));
  }

  const canApproveHere =
    (panel?.current_stage === "hod" && role === "hod") ||
    (panel?.current_stage === "incharge_academic_cell" && role === "incharge_academic_cell") ||
    (panel?.current_stage === "dpgs" && role === "dpgs");
  const canRevertHere = canApproveHere || (panel?.current_stage === "vc" && role === "vice_chancellor");
  const isVcTurn = panel?.current_stage === "vc" && role === "vice_chancellor";
  const selectedExaminer = panel?.proposals.find((p) => p.id === panel.selected_proposal_id) ?? null;
  const asExaminerProposals: ExaminerProposal[] = (panel?.proposals ?? []).map((p) => ({ ...p, is_reused_examiner: false, edited: false }));

  return (
    <div className="bg-gray-50 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <p className="font-semibold text-gray-800">PhD External Examiner Panel</p>
        {panel && (
          <ComprehensiveExamStatusBadge
            status={panel.status === "reverted" ? "reverted" : (panel.current_stage ?? "approved")}
            label={panel.status === "reverted" ? "Reverted" : PANEL_STAGE_LABELS[panel.current_stage ?? "approved"]}
          />
        )}
      </div>

      {!externalPanel && isMA && (
        <button onClick={() => setShowPropose(true)} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Propose 5 External Examiners</button>
      )}
      {externalPanel && !cycleId && (
        <p className="text-sm text-gray-500">External Panel status: {externalPanel.selection_status}.</p>
      )}

      {panel && (
        <div className="space-y-3">
          {panel.status === "reverted" && panel.revert_remark && (
            <p className="text-sm text-red-600">Revert remark: {panel.revert_remark}</p>
          )}
          {isMA && panel.status === "reverted" && (
            <button onClick={() => setShowPropose(true)} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Propose a New Panel</button>
          )}

          <ProposalTable proposals={asExaminerProposals} selectable={isVcTurn} selected={vcPick ? new Set([vcPick]) : new Set()} onToggle={(id) => setVcPick(id)} maxSelectable={panel.required_selection_count} />

          {isVcTurn && (
            <div className="flex items-center gap-3">
              <p className="text-xs text-gray-500">{vcPick ? 1 : 0} of {panel.required_selection_count} selected.</p>
              <button onClick={() => vcSelect.mutate()} disabled={!vcPick || vcSelect.isPending} className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                <CheckCircle2 size={13} /> {vcSelect.isPending ? "Confirming…" : "Confirm Selection"}
              </button>
            </div>
          )}

          {selectedExaminer && (
            <div className="bg-green-50 border border-green-200 rounded-xl p-3 space-y-1">
              <p className="text-sm font-semibold text-green-800">External Examiner Selected</p>
              <p className="text-sm text-gray-700">{selectedExaminer.name} — {selectedExaminer.designation}, {selectedExaminer.institution}</p>
              <p className="text-xs text-gray-500">{selectedExaminer.email} · {selectedExaminer.phone}</p>
              <p className="text-xs text-gray-500">{panel.email_sent_at ? `Notification email sent ${formatDate(panel.email_sent_at)}.` : "Notification email queued."}</p>
            </div>
          )}

          {canApproveHere && (
            <div className="flex gap-2">
              <button onClick={() => approvePanel.mutate()} disabled={approvePanel.isPending} className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50"><CheckCircle2 size={13} /> Approve</button>
              <button onClick={() => setShowRevertPanel(true)} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><RotateCcw size={13} /> Revert</button>
            </div>
          )}
          {!canApproveHere && canRevertHere && (
            <button onClick={() => setShowRevertPanel(true)} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><RotateCcw size={13} /> Revert</button>
          )}

          {showRevertPanel && (
            <div className="border-t border-gray-200 pt-3 space-y-2">
              <textarea value={panelRemark} onChange={(e) => setPanelRemark(e.target.value)} rows={2} placeholder="A remark is required"
                className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
              <div className="flex gap-2">
                <button onClick={() => { setShowRevertPanel(false); setPanelRemark(""); }} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
                <button
                  onClick={() => { if (!panelRemark.trim()) { toast.error("A remark is required."); return; } revertPanel.mutate(); }}
                  disabled={revertPanel.isPending}
                  className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-semibold hover:bg-red-700 disabled:opacity-50">
                  Confirm Revert
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {selectedExaminer && (
        <ExternalReportWorkflow
          applicationId={applicationId} isMA={isMA} role={role} userId={userId}
          report={externalReport} onChanged={onChanged} viewDoc={viewDoc}
        />
      )}

      {showPropose && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-[60] p-4" onClick={() => setShowPropose(false)}>
          <div className="bg-white rounded-2xl max-w-2xl w-full max-h-[85vh] overflow-y-auto p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">Propose 5 External Examiners</h3>
              <button onClick={() => setShowPropose(false)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            {proposals.map((p, idx) => (
              <div key={idx} className="border border-gray-200 rounded-xl p-3 grid grid-cols-2 gap-2">
                <p className="col-span-2 text-xs font-semibold text-gray-500">Examiner {idx + 1}</p>
                {(["name", "specialization", "designation", "email", "phone", "institution"] as const).map((field) => (
                  <input key={field} placeholder={field[0].toUpperCase() + field.slice(1)} value={p[field]}
                    onChange={(e) => updateProposal(idx, field, e.target.value)}
                    className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm" />
                ))}
              </div>
            ))}
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowPropose(false)} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={() => propose.mutate()} disabled={propose.isPending} className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                {propose.isPending ? "Submitting…" : "Submit Panel"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── External Viva Report (physically-signed PDF): upload -> submit -> Committee -> HOD ─────
// -> Incharge -> DPGS -> Approved. Every stage is a plain Approve/Revert workflow record — the
// physical signatures already exist inside the uploaded PDF; AMS never captures an electronic
// signature, OTP, or any signing payload at any of these stages.

function ExternalReportWorkflow({
  applicationId, isMA, role, userId, report, onChanged, viewDoc,
}: {
  applicationId: string; isMA: boolean; role: string | null | undefined; userId: string | undefined;
  report: ExternalReportDetail | null; onChanged: () => void; viewDoc: (path: string, label: string) => void;
}) {
  const [showRevert, setShowRevert] = useState(false);
  const [remark, setRemark] = useState("");

  const submit = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-viva-reports/${report?.id}/submit`),
    onSuccess: () => { toast.success("Submitted for approval."); onChanged(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to submit.")),
  });
  const committeeApprove = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-viva-reports/${report?.id}/committee-sign`),
    onSuccess: () => { toast.success("Approved."); onChanged(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to approve.")),
  });
  const approve = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-viva-reports/${report?.id}/approve`),
    onSuccess: () => { toast.success("Approved."); onChanged(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to approve.")),
  });
  const revert = useMutation({
    mutationFn: () => api.post(`/comprehensive-exam/external-viva-reports/${report?.id}/revert`, { remark }),
    onSuccess: () => { toast.success("Reverted."); setShowRevert(false); setRemark(""); onChanged(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to revert.")),
  });

  const mySignature = report?.signatures.find((s) => s.faculty_id === userId);
  const canApproveCommittee = report?.status === "committee_pending" && mySignature?.status === "pending";
  const canRevertCommittee = report?.status === "committee_pending" && !!mySignature;
  const canApproveStage =
    (report?.status === "hod_pending" && role === "hod") ||
    (report?.status === "incharge_pending" && role === "incharge_academic_cell") ||
    (report?.status === "dpgs_pending" && role === "dpgs");
  const canRevertStage = canApproveStage;

  // Approval history — a plain audit trail of Approve/Revert events, never an electronic
  // signature record (no signature payload/OTP exists anywhere in this model).
  const history: { label: string; at: string | null }[] = report ? [
    { label: "Uploaded by Major Advisor", at: report.uploaded_at },
    ...(report.submitted_at ? [{ label: "Submitted by Major Advisor", at: report.submitted_at }] : []),
    ...report.signatures.filter((s) => s.status === "signed").map((s) => ({ label: `Advisory Committee (${s.role_snapshot.replace(/_/g, " ")}) approved`, at: s.signed_at })),
    ...(report.hod_approved ? [{ label: "HOD approved", at: report.hod_approved_at }] : []),
    ...(report.incharge_approved ? [{ label: "Incharge Academic Cell approved", at: report.incharge_approved_at }] : []),
    ...(report.dpgs_approved ? [{ label: "DPGS approved — Final", at: report.dpgs_approved_at }] : []),
    ...(report.status === "reverted" ? [{ label: `Reverted${report.revert_remark ? ` — Reason: ${report.revert_remark}` : ""}`, at: report.reverted_at }] : []),
  ] : [];

  return (
    <div className="border-t border-gray-200 pt-3 space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm font-semibold text-gray-700">External Viva Report{report ? ` (v${report.version_number})` : ""}</p>
        {report && <ComprehensiveExamStatusBadge status={report.status} label={EXTERNAL_REPORT_STAGE_LABELS[report.status]} />}
      </div>

      {isMA && (
        <button onClick={() => viewDoc(`/comprehensive-exam/applications/${applicationId}/external-viva-report/blank-document`, "blank External Viva Report")} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">
          Generate / Print Report for Signatures
        </button>
      )}

      {report && (
        <div className="space-y-2">
          <button onClick={() => viewDoc(`/comprehensive-exam/external-viva-reports/${report.id}/document`, "External Viva Report")} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">
            View / Print Uploaded Signed Report
          </button>

          {report.status === "reverted" && report.revert_remark && (
            <p className="text-sm text-red-600">Revert remark: {report.revert_remark}</p>
          )}

          {isMA && report.status === "uploaded" && (
            <button onClick={() => submit.mutate()} disabled={submit.isPending} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50">
              {submit.isPending ? "Submitting…" : "Submit for Approval"}
            </button>
          )}

          {(canApproveCommittee || canApproveStage) && (
            <div className="flex gap-2">
              <button
                onClick={() => (canApproveCommittee ? committeeApprove.mutate() : approve.mutate())}
                disabled={committeeApprove.isPending || approve.isPending}
                className="flex items-center gap-1 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">
                <CheckCircle2 size={13} /> Approve
              </button>
              <button onClick={() => setShowRevert(true)} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">
                <RotateCcw size={13} /> Revert
              </button>
            </div>
          )}
          {!canApproveCommittee && !canApproveStage && (canRevertCommittee || canRevertStage) && (
            <button onClick={() => setShowRevert(true)} className="flex items-center gap-1 px-3 py-1.5 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">
              <RotateCcw size={13} /> Revert
            </button>
          )}

          {showRevert && (
            <div className="space-y-2">
              <textarea value={remark} onChange={(e) => setRemark(e.target.value)} rows={2} placeholder="Reason for Revert (required)"
                className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
              <div className="flex gap-2">
                <button onClick={() => { setShowRevert(false); setRemark(""); }} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
                <button
                  onClick={() => { if (!remark.trim()) { toast.error("A remark is required."); return; } revert.mutate(); }}
                  disabled={revert.isPending}
                  className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-xs font-semibold hover:bg-red-700 disabled:opacity-50">
                  Confirm Revert
                </button>
              </div>
            </div>
          )}

          {history.length > 0 && (
            <div className="pt-2 space-y-1">
              <p className="text-xs font-semibold text-gray-500 uppercase">Approval History</p>
              {history.map((h, i) => (
                <p key={i} className="text-xs text-gray-600">{h.label} — {formatDateTime(h.at)}</p>
              ))}
            </div>
          )}
        </div>
      )}

      {isMA && (!report || report.status === "reverted") && (
        <UploadSignedReport applicationId={applicationId} onUploaded={onChanged} />
      )}
    </div>
  );
}

function UploadSignedReport({ applicationId, onUploaded }: { applicationId: string; onUploaded: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: () => {
      const fd = new FormData();
      fd.append("file", file as File);
      return api.post(`/comprehensive-exam/applications/${applicationId}/external-viva-report/upload`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Signed report uploaded."); setFile(null); onUploaded(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Failed to upload signed report.")),
  });
  return (
    <div className="flex items-center gap-2">
      <input type="file" accept=".pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)} className="text-xs" />
      <button onClick={() => upload.mutate()} disabled={!file || upload.isPending} className="flex items-center gap-1 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50">
        <Upload size={13} /> {upload.isPending ? "Uploading…" : "Upload Signed Report"}
      </button>
    </div>
  );
}
