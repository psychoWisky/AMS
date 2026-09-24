"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardList, Loader2, CheckCircle2, XCircle, Eye, X, RefreshCw, Send } from "lucide-react";
import { apiErrorMessage } from "@/components/ui/thesis-parts";

// PG25 (Thesis Seminar Certificate, Form No. PG 25) — CORRECTED workflow: the student's Major
// Advisor drives generation (Satisfactory/Unsatisfactory, Submit, Regenerate after a revert);
// the Advisory Committee approves; the student's own-department HOD gives the final approval.
// A faculty member may see BOTH the "My Advisees" (Major Advisor capacity) and Home/Pending
// (Advisory Committee capacity) sections, since the same person can hold both roles for
// different students. HOD sees a department-scoped final-approval inbox.

interface MaRow {
  sl_no: number; thesis_id: string; student_roll: string | null; student_name: string;
  seminar_at: string | null; seminar_at_ist: string | null;
  attempt_number: number | null; version_number: number | null;
  status: string; status_label: string; ma_signed: boolean;
  can_satisfactory: boolean; can_unsatisfactory: boolean; can_submit: boolean; can_regenerate: boolean;
}
interface HodRow {
  sl_no: number; certificate_id: string; thesis_id: string; student_roll: string | null;
  seminar_at: string | null; seminar_at_ist: string | null; status: string; status_label: string;
}
interface HomeRow { sl_no: number; certificate_id: string; thesis_id: string; student_name: string; student_roll: string | null; status: string; status_label: string }
interface PendingRow {
  sl_no: number; certificate_id: string; thesis_id: string; student_roll: string | null; student_name: string;
  seminar_at: string | null; seminar_at_ist: string | null; status: string; status_label: string;
}
interface CertDetail {
  id: string; thesis_id: string; status: string; status_label: string;
  attempt_number: number; version_number: number;
  student_name: string; student_roll: string | null;
  seminar_at: string | null; seminar_at_ist: string | null;
  ma_name: string | null; ma_signed_at: string | null;
  hod_approved_by: string | null; approved_at: string | null;
  revert_remark: string | null; reverted_at: string | null;
  signatures: { role_label: string; name: string | null; status: string; signed_at: string | null; is_me: boolean }[];
}

export default function Pg25Page() {
  const role = useRole();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"home" | "pending">("pending");
  const [viewCertId, setViewCertId] = useState<string | null>(null);
  const [revertTarget, setRevertTarget] = useState<string | null>(null);
  const [remark, setRemark] = useState("");

  const isHod = role === "hod";
  const canBeMa = role === "faculty" || role === "hod";

  const { data: maRows = [], isLoading: maLoading } = useQuery<MaRow[]>({
    queryKey: ["ams-pg25-ma"],
    queryFn: async () => (await api.get("/thesis/pg25/ma")).data,
    enabled: canBeMa,
  });
  const { data: hodRows = [], isLoading: hodLoading } = useQuery<HodRow[]>({
    queryKey: ["ams-pg25-hod"],
    queryFn: async () => (await api.get("/thesis/pg25/hod")).data,
    enabled: isHod,
  });
  const { data: homeRows = [], isLoading: homeLoading } = useQuery<HomeRow[]>({
    queryKey: ["ams-pg25-home"],
    queryFn: async () => (await api.get("/thesis/pg25/mine/home")).data,
  });
  const { data: pendingRows = [], isLoading: pendingLoading } = useQuery<PendingRow[]>({
    queryKey: ["ams-pg25-pending"],
    queryFn: async () => (await api.get("/thesis/pg25/mine/pending")).data,
  });
  const { data: certDetail } = useQuery<CertDetail>({
    queryKey: ["ams-pg25-detail", viewCertId],
    queryFn: async () => (await api.get(`/thesis/pg25/${viewCertId}`)).data,
    enabled: !!viewCertId,
  });

  function refreshAll() {
    qc.invalidateQueries({ queryKey: ["ams-pg25-ma"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-hod"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-home"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-detail"] });
  }

  const satisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/satisfactory`),
    onSuccess: () => { toast.success("Seminar recorded as satisfactory. Certificate generated — click Submit next."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record the seminar outcome.")),
  });
  const unsatisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/unsatisfactory`),
    onSuccess: () => { toast.success("Recorded as unsatisfactory. Conduct the seminar again, then record the new attempt."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record the outcome.")),
  });
  const submit = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/submit`),
    onSuccess: () => { toast.success("Signed and submitted."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit.")),
  });
  const regenerate = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/regenerate`),
    onSuccess: () => { toast.success("Certificate regenerated — click Submit to sign and re-route it."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not regenerate.")),
  });
  const sign = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/pg25/${certificateId}/sign`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });
  const hodApprove = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/pg25/${certificateId}/hod-approve`),
    onSuccess: () => { toast.success("Approved."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not approve.")),
  });
  const revert = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/pg25/${certificateId}/revert`, { remark: remark.trim() }),
    onSuccess: () => { toast.success("Reverted to the Major Advisor."); setRevertTarget(null); setRemark(""); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not revert.")),
  });

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Thesis Seminar Certificate (PG 25)</h1>
        <p className="text-gray-600 text-base mt-1">Major Advisor generates and signs, the Advisory Committee approves, and the HOD gives final approval.</p>
      </div>

      {canBeMa && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">My Advisees</h2>
          {maLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["SL No", "Roll No.", "Name", "Seminar Date & Time", "Status", "Signature", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {maRows.map((r) => (
                    <tr key={r.thesis_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5 text-gray-600">{r.seminar_at_ist ?? "—"}</td>
                      <td className="px-4 py-2.5">
                        <span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span>
                        {r.attempt_number && <span className="block text-[10px] text-gray-400 mt-0.5">Attempt {r.attempt_number} · v{r.version_number}</span>}
                      </td>
                      <td className="px-4 py-2.5">{r.ma_signed ? <span className="text-green-700 text-xs font-semibold">✓ Signed</span> : <span className="text-gray-400 text-xs">—</span>}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1.5">
                          {r.can_satisfactory && (
                            <button onClick={() => satisfactory.mutate(r.thesis_id)} disabled={satisfactory.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50"><CheckCircle2 size={12} /> Satisfactory</button>
                          )}
                          {r.can_unsatisfactory && (
                            <button onClick={() => unsatisfactory.mutate(r.thesis_id)} disabled={unsatisfactory.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><XCircle size={12} /> Unsatisfactory</button>
                          )}
                          {r.can_submit && (
                            <button onClick={() => submit.mutate(r.thesis_id)} disabled={submit.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F] disabled:opacity-50"><Send size={12} /> Submit</button>
                          )}
                          {r.can_regenerate && (
                            <button onClick={() => regenerate.mutate(r.thesis_id)} disabled={regenerate.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 bg-amber-600 text-white rounded-lg text-xs font-semibold hover:bg-amber-700 disabled:opacity-50"><RefreshCw size={12} /> Regenerate</button>
                          )}
                          {!r.can_satisfactory && !r.can_unsatisfactory && !r.can_submit && !r.can_regenerate && (
                            <span className="text-xs text-gray-400">Awaiting Committee/HOD</span>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                  {maRows.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-gray-500">No advisees found.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {isHod && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Pending Your Final Approval</h2>
          {hodLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["SL No", "Roll No.", "Seminar Date & Time", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {hodRows.map((r) => (
                    <tr key={r.certificate_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 text-gray-600">{r.seminar_at_ist ?? "—"}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1.5">
                          <button onClick={() => setViewCertId(r.certificate_id)} className="px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View</button>
                          <button onClick={() => hodApprove.mutate(r.certificate_id)} disabled={hodApprove.isPending}
                            className="px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">Approve</button>
                          <button onClick={() => setRevertTarget(r.certificate_id)} className="px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">Revert</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {hodRows.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">Nothing pending your final approval.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section className="space-y-3">
        <h2 className="text-lg font-bold text-gray-900">Advisory Committee</h2>
        <div className="flex gap-2">
          <button onClick={() => setTab("pending")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${tab === "pending" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Pending</button>
          <button onClick={() => setTab("home")} className={`px-4 py-2 rounded-xl text-sm font-semibold ${tab === "home" ? "bg-[#0D6E6E] text-white" : "bg-white border border-gray-200 text-gray-700"}`}>Home</button>
        </div>

        {tab === "home" && (
          homeLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["SL No", "Name", "Roll No.", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
                <tbody>
                  {homeRows.map((r) => (
                    <tr key={r.certificate_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-green-100 text-green-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5"><button onClick={() => setViewCertId(r.certificate_id)} className="flex items-center gap-1 px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50"><Eye size={12} /> View</button></td>
                    </tr>
                  ))}
                  {homeRows.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-500">No approved certificates yet.</td></tr>}
                </tbody>
              </table>
            </div>
          )
        )}

        {tab === "pending" && (
          pendingLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200"><tr>{["SL No", "Roll No.", "Name", "Seminar Date & Time", "Status", "Signature", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
                <tbody>
                  {pendingRows.map((r) => (
                    <tr key={r.certificate_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5 text-gray-600">{r.seminar_at_ist ?? "—"}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5 text-gray-400 text-xs">Pending your approval</td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1.5">
                          <button onClick={() => setViewCertId(r.certificate_id)} className="px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View</button>
                          <button onClick={() => sign.mutate(r.certificate_id)} disabled={sign.isPending} className="px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">Approve</button>
                          <button onClick={() => setRevertTarget(r.certificate_id)} className="px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">Revert</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {pendingRows.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-gray-500">Nothing pending your approval.</td></tr>}
                </tbody>
              </table>
            </div>
          )
        )}
      </section>

      {revertTarget && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => { setRevertTarget(null); setRemark(""); }}>
          <div className="bg-white rounded-2xl max-w-md w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <h3 className="text-lg font-bold text-gray-900">Revert to Major Advisor</h3>
            <p className="text-sm text-gray-600">The certificate returns to the Major Advisor, who must regenerate and resubmit it.</p>
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
              <h3 className="text-lg font-bold text-gray-900">{certDetail.student_name}&apos;s PG 25</h3>
              <button onClick={() => setViewCertId(null)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <p className="text-sm text-gray-600">Roll No.: {certDetail.student_roll ?? "—"} · Attempt {certDetail.attempt_number}, v{certDetail.version_number}</p>
            <p className="text-sm text-gray-600">Seminar: {certDetail.seminar_at_ist ?? "—"}</p>
            <p className="text-sm text-gray-600">Major Advisor: {certDetail.ma_name ?? "—"} {certDetail.ma_signed_at ? "(Signed)" : "(Not yet signed)"}</p>
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
              {certDetail.signatures.length === 0 && <div className="px-4 py-2 text-sm text-gray-400">No Advisory Committee signatures required for this certificate.</div>}
            </div>
            <p className="text-sm text-gray-600">HOD: {certDetail.hod_approved_by ? `Approved by ${certDetail.hod_approved_by}` : "Pending"}</p>
          </div>
        </div>
      )}
    </div>
  );
}
