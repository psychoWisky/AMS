"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardList, Loader2, CheckCircle2, XCircle, Eye, X } from "lucide-react";
import { apiErrorMessage } from "@/components/ui/thesis-parts";

// Final Thesis's two independent generated-document workflows — Form PG-25(A) (student-
// generated, then Major-Advisor / Committee / HOD / Incharge / DPGS approve) and the Viva Voce
// Certificate (Major-Advisor-generated via Satisfactory/Unsatisfactory, then the same
// Committee -> HOD -> Incharge -> DPGS tail). Incharge Academic Cell approves here but is
// NEVER a named signatory on either generated PDF. A single page with a Kind switch, since
// every role's Committee/HOD/Incharge/DPGS view is structurally identical for both kinds.

type Kind = "pg25a" | "viva";

interface MaPendingRow { sl_no: number; certificate_id: string; thesis_id: string; student_roll: string | null; student_name: string; status: string; status_label: string }
interface VivaMaRow {
  sl_no: number; thesis_id: string; student_roll: string | null; student_name: string;
  event_at_ist: string | null; status: string; status_label: string;
  can_satisfactory: boolean; can_unsatisfactory: boolean; can_submit: boolean; can_regenerate: boolean;
}
interface InboxRow { sl_no: number; certificate_id: string; kind?: Kind; thesis_id: string; student_roll: string | null; student_name?: string; event_at_ist?: string | null; status: string; status_label: string }
interface CertDetail {
  id: string; kind: Kind; thesis_id: string; status: string; status_label: string;
  attempt_number: number; version_number: number; event_at_ist: string | null;
  student_name: string; student_roll: string | null;
  ma_name: string | null; ma_acted_at: string | null;
  hod_approved_by: string | null; hod_approved_at: string | null;
  dpgs_approved_by: string | null; approved_at: string | null;
  revert_remark: string | null; reverted_at: string | null;
  signatures: { role_label: string; name: string | null; status: string; signed_at: string | null; is_me: boolean }[];
}

export default function FinalApprovalsPage() {
  const role = useRole();
  const qc = useQueryClient();
  const [kind, setKind] = useState<Kind>("pg25a");
  const [committeeTab, setCommitteeTab] = useState<"home" | "pending">("pending");
  const [viewCertId, setViewCertId] = useState<string | null>(null);
  const [revertTarget, setRevertTarget] = useState<string | null>(null);
  const [remark, setRemark] = useState("");

  const isHod = role === "hod";
  const isIncharge = role === "incharge_academic_cell";
  const isDpgs = role === "dpgs";
  const canBeMa = role === "faculty" || role === "hod";

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ["ams-finalcert"] });
  }

  // --- PG-25(A): Major Advisor approval inbox (student generates/submits; MA is a separate stage) ---
  const { data: maPending = [], isLoading: maPendingLoading } = useQuery<MaPendingRow[]>({
    queryKey: ["ams-finalcert", "ma-pending"],
    queryFn: async () => (await api.get("/thesis/finalcert/ma-pending")).data,
    enabled: canBeMa && kind === "pg25a",
  });
  const maApprove = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/ma-approve`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });

  // --- Viva: Major Advisor's own advisee list (Satisfactory/Unsatisfactory/Submit/Regenerate) ---
  // Academic Year / Semester are real, server-enforced filters backed by the student's actual
  // CourseRegistration rows (never a fabricated field) — same source Progress Report uses.
  const [vivaYear, setVivaYear] = useState("");
  const [vivaSemester, setVivaSemester] = useState("");
  const { data: calendars = [] } = useQuery<{ id: string; academic_year: string }[]>({
    queryKey: ["ams-calendars"], queryFn: async () => (await api.get("/academic/calendars")).data,
    enabled: canBeMa && kind === "viva",
  });
  const { data: vivaSemesters = [] } = useQuery<{ id: string; name: string }[]>({
    queryKey: ["ams-viva-semesters", vivaYear],
    queryFn: async () => (await api.get(`/academic/calendars/${vivaYear}/semesters`)).data,
    enabled: canBeMa && kind === "viva" && !!vivaYear,
  });
  const { data: vivaMaRows = [], isLoading: vivaMaLoading } = useQuery<VivaMaRow[]>({
    queryKey: ["ams-finalcert", "viva-ma", vivaYear, vivaSemester],
    queryFn: async () => (await api.get("/thesis/viva/ma", { params: { academic_year_id: vivaYear || undefined, semester_id: vivaSemester || undefined } })).data,
    enabled: canBeMa && kind === "viva",
  });
  const vivaSatisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/viva/satisfactory`),
    onSuccess: () => { toast.success("Viva recorded as satisfactory. Certificate generated — click Submit next."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record the viva outcome.")),
  });
  const vivaUnsatisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/viva/unsatisfactory`),
    onSuccess: () => { toast.success("Recorded as unsatisfactory. The student must undergo another viva."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record the outcome.")),
  });
  const vivaSubmit = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/viva/submit`),
    onSuccess: () => { toast.success("Signed and submitted."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit.")),
  });
  const vivaRegenerate = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/viva/regenerate`),
    onSuccess: () => { toast.success("Regenerated — click Submit to sign and re-route it."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not regenerate.")),
  });

  // --- Advisory Committee (both kinds) ---
  const { data: homeRows = [], isLoading: homeLoading } = useQuery<InboxRow[]>({
    queryKey: ["ams-finalcert", "home", kind],
    queryFn: async () => (await api.get(`/thesis/finalcert/mine/home?kind=${kind}`)).data,
    enabled: committeeTab === "home",
  });
  const { data: pendingRows = [], isLoading: pendingLoading } = useQuery<InboxRow[]>({
    queryKey: ["ams-finalcert", "pending", kind],
    queryFn: async () => (await api.get(`/thesis/finalcert/mine/pending?kind=${kind}`)).data,
    enabled: committeeTab === "pending",
  });
  const sign = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/sign`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });

  // --- HOD / Incharge / DPGS inboxes ---
  const { data: hodRows = [], isLoading: hodLoading } = useQuery<InboxRow[]>({
    queryKey: ["ams-finalcert", "hod", kind], queryFn: async () => (await api.get(`/thesis/finalcert/hod?kind=${kind}`)).data, enabled: isHod,
  });
  const { data: inchargeRows = [], isLoading: inchargeLoading } = useQuery<InboxRow[]>({
    queryKey: ["ams-finalcert", "incharge", kind], queryFn: async () => (await api.get(`/thesis/finalcert/incharge?kind=${kind}`)).data, enabled: isIncharge,
  });
  const { data: dpgsRows = [], isLoading: dpgsLoading } = useQuery<InboxRow[]>({
    queryKey: ["ams-finalcert", "dpgs", kind], queryFn: async () => (await api.get(`/thesis/finalcert/dpgs?kind=${kind}`)).data, enabled: isDpgs,
  });
  const hodApprove = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/hod-approve`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); }, onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });
  const inchargeApprove = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/incharge-approve`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); }, onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });
  const dpgsApprove = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/dpgs-approve`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); }, onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });
  const revert = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/finalcert/${certificateId}/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Reverted."); setRevertTarget(null); setRemark(""); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not revert.")),
  });

  const { data: certDetail } = useQuery<CertDetail>({
    queryKey: ["ams-finalcert-detail", viewCertId],
    queryFn: async () => (await api.get(`/thesis/finalcert/${viewCertId}`)).data,
    enabled: !!viewCertId,
  });

  const label = kind === "pg25a" ? "Form PG-25(A)" : "Viva Voce Certificate";

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Final Thesis Approvals</h1>
        <p className="text-gray-600 text-base mt-1">Form PG-25(A) and the Viva Voce Certificate — Major Advisor, Advisory Committee, HOD, Incharge Academic Cell, DPGS.</p>
      </div>

      <div className="flex gap-2">
        <button onClick={() => setKind("pg25a")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${kind === "pg25a" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Form PG-25(A)</button>
        <button onClick={() => setKind("viva")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${kind === "viva" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Viva Voce Certificate</button>
      </div>

      {canBeMa && kind === "pg25a" && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Awaiting My Approval (Major Advisor)</h2>
          {maPendingLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["SL No", "Roll No.", "Name", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
                <tbody>
                  {maPending.map((r) => (
                    <tr key={r.certificate_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1.5">
                          <button onClick={() => setViewCertId(r.certificate_id)} className="px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View</button>
                          <button onClick={() => maApprove.mutate(r.certificate_id)} disabled={maApprove.isPending} className="px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">Approve</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {maPending.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">Nothing pending your approval.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {canBeMa && kind === "viva" && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">My Advisees — Viva</h2>
          <div className="flex flex-wrap gap-3">
            <select value={vivaYear} onChange={(e) => { setVivaYear(e.target.value); setVivaSemester(""); }}
              className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm">
              <option value="">All Academic Years</option>
              {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
            </select>
            <select value={vivaSemester} onChange={(e) => setVivaSemester(e.target.value)} disabled={!vivaYear}
              className="border border-gray-300 rounded-lg px-3 py-1.5 text-sm disabled:opacity-50">
              <option value="">All Semesters</option>
              {vivaSemesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          {vivaMaLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["SL No", "Roll No.", "Name", "Viva Date & Time", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
                <tbody>
                  {vivaMaRows.map((r) => (
                    <tr key={r.thesis_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5 text-gray-600">{r.event_at_ist ?? "—"}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1.5">
                          {r.can_satisfactory && <button onClick={() => vivaSatisfactory.mutate(r.thesis_id)} className="flex items-center gap-1 px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700"><CheckCircle2 size={12} /> Satisfactory</button>}
                          {r.can_unsatisfactory && <button onClick={() => vivaUnsatisfactory.mutate(r.thesis_id)} className="flex items-center gap-1 px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><XCircle size={12} /> Unsatisfactory</button>}
                          {r.can_submit && <button onClick={() => vivaSubmit.mutate(r.thesis_id)} className="px-2.5 py-1 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Submit</button>}
                          {r.can_regenerate && <button onClick={() => vivaRegenerate.mutate(r.thesis_id)} className="px-2.5 py-1 bg-amber-600 text-white rounded-lg text-xs font-semibold hover:bg-amber-700">Regenerate</button>}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {vivaMaRows.length === 0 && <tr><td colSpan={6} className="px-4 py-6 text-center text-gray-500">No advisees found.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {isHod && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Pending Your Final Approval (HOD) — {label}</h2>
          {hodLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <InboxTable rows={hodRows} onView={setViewCertId} onApprove={(id) => hodApprove.mutate(id)} onRevert={setRevertTarget} />
          )}
        </section>
      )}
      {isIncharge && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Pending Incharge Academic Cell Approval — {label}</h2>
          {inchargeLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <InboxTable rows={inchargeRows} onView={setViewCertId} onApprove={(id) => inchargeApprove.mutate(id)} onRevert={setRevertTarget} />
          )}
        </section>
      )}
      {isDpgs && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Pending DPGS Approval — {label}</h2>
          {dpgsLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <InboxTable rows={dpgsRows} onView={setViewCertId} onApprove={(id) => dpgsApprove.mutate(id)} onRevert={setRevertTarget} />
          )}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-bold text-gray-900">Advisory Committee — {label}</h2>
        <div className="flex gap-2">
          <button onClick={() => setCommitteeTab("pending")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${committeeTab === "pending" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Pending</button>
          <button onClick={() => setCommitteeTab("home")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${committeeTab === "home" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Home</button>
        </div>
        {committeeTab === "home" ? (
          homeLoading ? <Loader2 className="animate-spin text-gray-500" /> : <InboxTable rows={homeRows} onView={setViewCertId} />
        ) : (
          pendingLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <InboxTable rows={pendingRows} onView={setViewCertId} onApprove={(id) => sign.mutate(id)} onRevert={setRevertTarget} />
          )
        )}
      </section>

      {revertTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => { setRevertTarget(null); setRemark(""); }}>
          <div className="bg-white rounded-2xl max-w-md w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-bold text-gray-900">Revert</h3>
            <p className="text-sm text-gray-600">The certificate returns to the {kind === "pg25a" ? "student" : "Major Advisor"} for regeneration.</p>
            <textarea value={remark} onChange={(e) => setRemark(e.target.value)} rows={4} placeholder="Explain what needs to be corrected"
              className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-400" />
            <div className="flex justify-end gap-3">
              <button onClick={() => { setRevertTarget(null); setRemark(""); }} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Cancel</button>
              <button onClick={() => revert.mutate(revertTarget)} disabled={!remark.trim() || revert.isPending}
                className="px-4 py-2 bg-red-600 text-white rounded-xl text-sm font-semibold hover:bg-red-700 disabled:opacity-50">{revert.isPending ? "Reverting…" : "Revert"}</button>
            </div>
          </div>
        </div>
      )}

      {viewCertId && certDetail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setViewCertId(null)}>
          <div className="bg-white rounded-2xl max-w-lg w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">{certDetail.student_name}&apos;s {certDetail.kind === "pg25a" ? "Form PG-25(A)" : "Viva Voce Certificate"}</h3>
              <button onClick={() => setViewCertId(null)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <p className="text-sm text-gray-600">Roll No.: {certDetail.student_roll ?? "—"} · Attempt {certDetail.attempt_number}, v{certDetail.version_number}</p>
            <p className="text-sm text-gray-600">Major Advisor: {certDetail.ma_name ?? "—"} {certDetail.ma_acted_at ? "(Signed)" : "(Not yet signed)"}</p>
            {certDetail.revert_remark && (
              <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                <span className="font-semibold">Revert remark:</span> {certDetail.revert_remark}
              </div>
            )}
            <div className="divide-y divide-gray-100 border border-gray-200 rounded-xl">
              {certDetail.signatures.map((s, i) => (
                <div key={i} className="flex items-center justify-between px-4 py-2 text-sm">
                  <span>{s.role_label}{s.is_me && <span className="text-xs text-gray-400 ml-1">(you)</span>}</span>
                  <span className={s.status === "signed" ? "text-green-700 font-semibold text-xs" : "text-amber-600 font-semibold text-xs"}>{s.status === "signed" ? "✓ Signed" : "Pending"}</span>
                </div>
              ))}
              {certDetail.signatures.length === 0 && <div className="px-4 py-2 text-sm text-gray-400">No Advisory Committee signatures required.</div>}
            </div>
            <p className="text-sm text-gray-600">HOD: {certDetail.hod_approved_by ? `Approved by ${certDetail.hod_approved_by}` : "Pending"}</p>
          </div>
        </div>
      )}
    </div>
  );
}

function InboxTable({ rows, onView, onApprove, onRevert }: { rows: InboxRow[]; onView: (id: string) => void; onApprove?: (id: string) => void; onRevert?: (id: string) => void }) {
  return (
    <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 border-b border-gray-200"><tr>{["SL No", "Roll No.", "Name", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.certificate_id} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-2.5">{r.sl_no}</td>
              <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
              <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name ?? "—"}</td>
              <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
              <td className="px-4 py-2.5">
                <div className="flex gap-1.5">
                  <button onClick={() => onView(r.certificate_id)} className="flex items-center gap-1 px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50"><Eye size={12} /> View</button>
                  {onApprove && <button onClick={() => onApprove(r.certificate_id)} className="px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700">Approve</button>}
                  {onRevert && <button onClick={() => onRevert(r.certificate_id)} className="px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">Revert</button>}
                </div>
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">Nothing to show.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
