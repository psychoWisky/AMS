"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { useRole } from "@/stores/auth.store";
import { ClipboardList, Loader2, CheckCircle2, XCircle, Eye, X } from "lucide-react";
import { apiErrorMessage } from "@/components/ui/thesis-parts";

// PG25 (Thesis Seminar Certificate, Form No. PG 25) — HOD and Advisory Committee (Faculty)
// screens. Distinct from /thesis/approvals: PG25 happens BEFORE the Initial Thesis is even
// submitted, and is not part of the linear Major Advisor -> HOD -> ... approval chain.
// The "Sign" (HOD explicit) and "Revert" buttons are intentionally deferred no-ops — the
// backend enforces authorization but performs no state change; see the implementation report.

interface HodRow {
  sl_no: number; thesis_id: string; student_roll: string | null; student_name: string;
  seminar_at: string | null; seminar_at_ist: string | null; status: string; status_label: string;
  hod_signed: boolean; can_act: boolean;
}
interface HomeRow { sl_no: number; certificate_id: string; thesis_id: string; student_name: string; student_roll: string | null; status: string; status_label: string }
interface PendingRow {
  sl_no: number; certificate_id: string; thesis_id: string; student_roll: string | null; student_name: string;
  seminar_at: string | null; seminar_at_ist: string | null; status: string; status_label: string;
}
interface CertDetail {
  id: string; thesis_id: string; status: string; student_name: string; student_roll: string | null;
  seminar_at: string | null; seminar_at_ist: string | null; approved_at: string | null;
  signatures: { role_label: string; name: string | null; status: string; signed_at: string | null; is_me: boolean }[];
}

export default function Pg25Page() {
  const role = useRole();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"home" | "pending">("pending");
  const [viewCertId, setViewCertId] = useState<string | null>(null);

  const isHod = role === "hod";

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
    qc.invalidateQueries({ queryKey: ["ams-pg25-hod"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-home"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-pending"] });
    qc.invalidateQueries({ queryKey: ["ams-pg25-detail"] });
  }

  const satisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/satisfactory`),
    onSuccess: () => { toast.success("Recorded as satisfactory. PG 25 generated and routed to the Advisory Committee."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record the seminar outcome.")),
  });
  const unsatisfactory = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/unsatisfactory`),
    onSuccess: (res) => toast.info(res.data?.message ?? "Recorded."),
    onError: (e) => toast.error(apiErrorMessage(e, "Could not record.")),
  });
  const hodSign = useMutation({
    mutationFn: (thesisId: string) => api.post(`/thesis/${thesisId}/pg25/hod-sign`),
    onSuccess: (res) => toast.info(res.data?.message ?? "No additional action is defined for this button yet."),
    onError: (e) => toast.error(apiErrorMessage(e, "Could not process.")),
  });
  const sign = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/pg25/${certificateId}/sign`),
    onSuccess: () => { toast.success("Signed."); refreshAll(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not sign.")),
  });
  const revert = useMutation({
    mutationFn: (certificateId: string) => api.post(`/thesis/pg25/${certificateId}/revert`),
    onSuccess: (res) => toast.info(res.data?.message ?? "No business effect is defined for Revert yet."),
    onError: (e) => toast.error(apiErrorMessage(e, "Could not process.")),
  });

  return (
    <div className="p-6 w-full space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Thesis Seminar Certificate (PG 25)</h1>
        <p className="text-gray-600 text-base mt-1">
          {isHod ? "Record the seminar outcome for students in your department." : "Sign the Thesis Seminar Certificate for students on your Advisory Committee."}
        </p>
      </div>

      {isHod && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Department Students</h2>
          {hodLoading ? <Loader2 className="animate-spin text-gray-500" /> : (
            <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["SL No", "Roll No.", "Name", "Seminar Date & Time", "Status", "Signature", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr>
                </thead>
                <tbody>
                  {hodRows.map((r) => (
                    <tr key={r.thesis_id} className="border-b border-gray-50 last:border-0">
                      <td className="px-4 py-2.5">{r.sl_no}</td>
                      <td className="px-4 py-2.5">{r.student_roll ?? "—"}</td>
                      <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                      <td className="px-4 py-2.5 text-gray-600">{r.seminar_at_ist ?? "—"}</td>
                      <td className="px-4 py-2.5"><span className="px-2 py-0.5 rounded bg-amber-100 text-amber-700 text-xs font-semibold">{r.status_label}</span></td>
                      <td className="px-4 py-2.5">{r.hod_signed ? <span className="text-green-700 text-xs font-semibold">✓ Signed</span> : <span className="text-gray-400 text-xs">—</span>}</td>
                      <td className="px-4 py-2.5">
                        {r.can_act ? (
                          <div className="flex gap-1.5">
                            <button onClick={() => satisfactory.mutate(r.thesis_id)} disabled={satisfactory.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50"><CheckCircle2 size={12} /> Satisfactory</button>
                            <button onClick={() => unsatisfactory.mutate(r.thesis_id)} disabled={unsatisfactory.isPending}
                              className="flex items-center gap-1 px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50"><XCircle size={12} /> Unsatisfactory</button>
                          </div>
                        ) : (
                          <button onClick={() => hodSign.mutate(r.thesis_id)} className="px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">Sign</button>
                        )}
                      </td>
                    </tr>
                  ))}
                  {hodRows.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-gray-500">No students found.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <section className="space-y-3">
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
                      <td className="px-4 py-2.5 text-gray-400 text-xs">Pending your signature</td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1.5">
                          <button onClick={() => setViewCertId(r.certificate_id)} className="px-2.5 py-1 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View</button>
                          <button onClick={() => sign.mutate(r.certificate_id)} disabled={sign.isPending} className="px-2.5 py-1 bg-green-600 text-white rounded-lg text-xs font-semibold hover:bg-green-700 disabled:opacity-50">Sign</button>
                          <button onClick={() => revert.mutate(r.certificate_id)} className="px-2.5 py-1 border border-red-300 text-red-700 rounded-lg text-xs font-semibold hover:bg-red-50">Revert</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {pendingRows.length === 0 && <tr><td colSpan={7} className="px-4 py-6 text-center text-gray-500">Nothing pending your signature.</td></tr>}
                </tbody>
              </table>
            </div>
          )
        )}
      </section>

      {viewCertId && certDetail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={() => setViewCertId(null)}>
          <div className="bg-white rounded-2xl max-w-lg w-full p-6 space-y-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h3 className="text-lg font-bold text-gray-900">{certDetail.student_name}&apos;s PG 25</h3>
              <button onClick={() => setViewCertId(null)} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
            </div>
            <p className="text-sm text-gray-600">Roll No.: {certDetail.student_roll ?? "—"}</p>
            <p className="text-sm text-gray-600">Seminar: {certDetail.seminar_at_ist ?? "—"}</p>
            <div className="divide-y divide-gray-100 border border-gray-200 rounded-xl">
              {certDetail.signatures.map((s, i) => (
                <div key={i} className="flex items-center justify-between px-4 py-2 text-sm">
                  <span>{s.role_label}{s.is_me && <span className="text-xs text-gray-400 ml-1">(you)</span>}</span>
                  <span className={s.status === "signed" ? "text-green-700 font-semibold text-xs" : "text-amber-600 font-semibold text-xs"}>{s.status === "signed" ? "✓ Signed" : "Pending"}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
