"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, blobErrorMessage, viewFileInNewTab } from "@/services/api";
import { toast } from "sonner";
import { GitBranch, Loader2, Plus, Upload, Eye, X } from "lucide-react";
import {
  DecisionNotice, MigrationStatusBadge, StudentInfoCard, apiErrorMessage, formatDate, type MigrationDetail,
} from "@/components/ui/migration-parts";

// The student's own Migration Management page. A student may have at most one ACTIVE
// (draft/submitted) application at a time (backend-enforced by a database partial unique
// index) but any number of historical (approved/rejected) ones — a new application is
// always allowed after a prior one is decided.

interface MigrationRow {
  id: string; student_name: string; student_roll: string | null; degree: string | null; college: string | null;
  status: string; status_label: string;
}

const EMPTY_FORM = {
  registration_no: "", last_exam_name_and_roll: "", passed_from_institution: "",
  fee_payment_date: "", migration_reason: "", address: "",
};
type Form = typeof EMPTY_FORM;

export default function MigrationManagementPage() {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [form, setForm] = useState<Form>(EMPTY_FORM);
  const [hydratedFor, setHydratedFor] = useState<string | null>(null);

  const { data: mine = [], isLoading } = useQuery<MigrationRow[]>({
    queryKey: ["ams-migration-mine"],
    queryFn: async () => (await api.get("/migration/mine")).data,
  });
  const { data: detail, refetch } = useQuery<MigrationDetail>({
    queryKey: ["ams-migration-detail", openId],
    queryFn: async () => (await api.get(`/migration/${openId}`)).data,
    enabled: !!openId,
  });

  // Hydrate the editable form from the server exactly once per opened application (adjusting
  // state during render, not in a useEffect — avoids an extra render pass and the
  // react-hooks/set-state-in-effect lint rule this codebase otherwise flags).
  if (detail && hydratedFor !== detail.id) {
    setHydratedFor(detail.id);
    setForm({
      registration_no: detail.registration_no ?? "", last_exam_name_and_roll: detail.last_exam_name_and_roll ?? "",
      passed_from_institution: detail.passed_from_institution ?? "", fee_payment_date: detail.fee_payment_date ?? "",
      migration_reason: detail.migration_reason ?? "", address: detail.address ?? "",
    });
  }

  const create = useMutation({
    mutationFn: () => api.post("/migration", {}),
    onSuccess: (res) => {
      toast.success("Migration application draft created.");
      qc.invalidateQueries({ queryKey: ["ams-migration-mine"] });
      setOpenId(res.data.id);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not create a Migration application.")),
  });

  const save = useMutation({
    mutationFn: () => api.patch(`/migration/${openId}`, form),
    onSuccess: () => { toast.success("Changes saved."); refetch(); qc.invalidateQueries({ queryKey: ["ams-migration-mine"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save changes.")),
  });

  const uploadReceipt = useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post(`/migration/${openId}/receipt`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Payment receipt uploaded."); refetch(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Only PDF files are accepted.")),
  });

  const submit = useMutation({
    mutationFn: () => api.post(`/migration/${openId}/submit`),
    onSuccess: () => { toast.success("Migration application submitted."); refetch(); qc.invalidateQueries({ queryKey: ["ams-migration-mine"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit your Migration application.")),
  });

  function close() { setOpenId(null); setForm(EMPTY_FORM); setHydratedFor(null); }
  async function downloadReceipt() {
    try {
      await viewFileInNewTab(`/migration/${openId}/receipt`);
    } catch (e) {
      toast.error(await blobErrorMessage(e, "Could not open the receipt."));
    }
  }

  const canSubmit = !!detail?.can_edit &&
    Object.values(form).every((v) => v.trim()) && !!detail?.receipt;

  return (
    <div className="p-6 w-full max-w-4xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><GitBranch size={24} className="text-[#0D6E6E]" />Migration Management</h1>
          <p className="text-gray-700 text-base mt-1">Create and track your Migration application.</p>
        </div>
        <button onClick={() => create.mutate()} disabled={create.isPending}
          className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F] disabled:opacity-50">
          {create.isPending ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Create Migration
        </button>
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
        {isLoading ? (
          <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
        ) : mine.length === 0 ? (
          <div className="text-center py-16 text-gray-500">You have not created a Migration application yet.</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["SL No.", "Name", "Roll No.", "Degree", "College", "Status", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {mine.map((r, i) => (
                <tr key={r.id} className="border-b border-gray-50 last:border-0">
                  <td className="px-4 py-2.5">{i + 1}</td>
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{r.student_name}</td>
                  <td className="px-4 py-2.5 text-gray-600">{r.student_roll ?? "—"}</td>
                  <td className="px-4 py-2.5">{r.degree ?? "—"}</td>
                  <td className="px-4 py-2.5">{r.college ?? "—"}</td>
                  <td className="px-4 py-2.5"><MigrationStatusBadge status={r.status} label={r.status_label} /></td>
                  <td className="px-4 py-2.5"><button onClick={() => setOpenId(r.id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]"><Eye size={13} /> View</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {openId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={close}>
          <div className="bg-white rounded-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-5" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-gray-900">Migration Application</h2>
              <div className="flex items-center gap-3">
                <MigrationStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
              </div>
            </div>

            <DecisionNotice m={detail} />
            <StudentInfoCard m={detail} />

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Registration No.</label>
                {detail.can_edit ? (
                  <input value={form.registration_no} onChange={(e) => setForm((f) => ({ ...f, registration_no: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2">{detail.registration_no || "—"}</p>}
              </div>
              <div>
                <label className="block text-sm font-semibold text-gray-700 mb-1">Date of Payment of Migration Fee</label>
                {detail.can_edit ? (
                  <input type="date" value={form.fee_payment_date} onChange={(e) => setForm((f) => ({ ...f, fee_payment_date: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2">{formatDate(detail.fee_payment_date)}</p>}
              </div>
              <div className="md:col-span-2">
                <label className="block text-sm font-semibold text-gray-700 mb-1">Name of Last Examination with Roll No.</label>
                {detail.can_edit ? (
                  <input value={form.last_exam_name_and_roll} onChange={(e) => setForm((f) => ({ ...f, last_exam_name_and_roll: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2">{detail.last_exam_name_and_roll || "—"}</p>}
              </div>
              <div className="md:col-span-2">
                <label className="block text-sm font-semibold text-gray-700 mb-1">School/College Passed From</label>
                {detail.can_edit ? (
                  <input value={form.passed_from_institution} onChange={(e) => setForm((f) => ({ ...f, passed_from_institution: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2">{detail.passed_from_institution || "—"}</p>}
              </div>
              <div className="md:col-span-2">
                <label className="block text-sm font-semibold text-gray-700 mb-1">Migration Reason</label>
                {detail.can_edit ? (
                  <textarea rows={3} value={form.migration_reason} onChange={(e) => setForm((f) => ({ ...f, migration_reason: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2 whitespace-pre-wrap">{detail.migration_reason || "—"}</p>}
              </div>
              <div className="md:col-span-2">
                <label className="block text-sm font-semibold text-gray-700 mb-1">Address</label>
                {detail.can_edit ? (
                  <textarea rows={2} value={form.address} onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                ) : <p className="text-gray-800 text-sm py-2 whitespace-pre-wrap">{detail.address || "—"}</p>}
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-gray-200 p-5 flex items-center justify-between">
              <div>
                <p className="text-sm font-semibold text-gray-700 mb-1">Payment Receipt (PDF)</p>
                {detail.receipt ? <p className="text-xs text-gray-500">{detail.receipt.original_filename}</p> : <p className="text-xs text-gray-400">Not uploaded yet</p>}
              </div>
              <div className="flex gap-2">
                {detail.receipt && <button onClick={downloadReceipt} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Receipt</button>}
                {detail.can_edit && (
                  <label className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold cursor-pointer hover:bg-[#178F8F]">
                    <Upload size={12} /> {detail.receipt ? "Replace" : "Upload"}
                    <input type="file" accept=".pdf,application/pdf" hidden onChange={(e) => e.target.files?.[0] && uploadReceipt.mutate(e.target.files[0])} />
                  </label>
                )}
              </div>
            </div>

            {detail.can_edit && (
              <div className="flex gap-3 justify-end border-t border-gray-100 pt-4">
                <button onClick={close} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Close</button>
                <button onClick={() => save.mutate()} disabled={save.isPending}
                  className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                  {save.isPending ? "Saving…" : "Save Changes"}
                </button>
                <button onClick={() => { if (confirm("Submit this Migration application for Registrar review? You will not be able to edit it afterward.")) submit.mutate(); }}
                  disabled={!canSubmit || submit.isPending}
                  className="px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                  {submit.isPending ? "Submitting…" : "Submit"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
