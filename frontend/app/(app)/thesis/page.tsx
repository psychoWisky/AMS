"use client";
import { useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { FileText, Loader2, Plus, Upload, Eye, X } from "lucide-react";
import {
  ExternalReportTable, RevertNotice, SignatureTable, StageTimeline, StudentInfoCard, ThesisStatusBadge,
  STUDENT_DOCUMENT_LABELS, PRINTABLE_DOCUMENT_TYPES, apiErrorMessage, type ThesisDetail,
} from "@/components/ui/thesis-parts";

// The student's own Thesis Management page. There is at most ONE Initial Thesis per student
// (backend-enforced by a database partial unique index — this page never assumes more).
// Final Thesis is explicitly out of scope; nothing here creates one.

interface ThesisRow { id: string; title: string | null; thesis_type_label: string; status: string; status_label: string }

const STUDENT_DOC_TYPES = Object.keys(STUDENT_DOCUMENT_LABELS);

export default function ThesisManagementPage() {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<string | null>(null);
  const [percent, setPercent] = useState("");
  const [software, setSoftware] = useState("");
  const [abstractText, setAbstractText] = useState("");
  const [hydratedFor, setHydratedFor] = useState<string | null>(null);
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({});

  const { data: mine = [], isLoading } = useQuery<ThesisRow[]>({
    queryKey: ["ams-thesis-mine"],
    queryFn: async () => (await api.get("/thesis/mine")).data,
  });
  const { data: detail, refetch } = useQuery<ThesisDetail>({
    queryKey: ["ams-thesis-detail", openId],
    queryFn: async () => (await api.get(`/thesis/${openId}`)).data,
    enabled: !!openId,
  });

  // Hydrate the editable fields from the server exactly once per opened Thesis (not on every
  // refetch) — otherwise "Save Changes" would silently null out anything the student never
  // re-typed after a background refresh (e.g. after an unrelated document upload). Adjusting
  // state during render (React's documented pattern for "resetting state when a prop/key
  // changes") rather than in a useEffect, so this never causes an extra render pass.
  if (detail && hydratedFor !== detail.id) {
    setHydratedFor(detail.id);
    setPercent(detail.plagiarism_student_percent != null ? String(detail.plagiarism_student_percent) : "");
    setSoftware(detail.plagiarism_software_name ?? "");
    setAbstractText(detail.abstract ?? "");
  }

  const create = useMutation({
    mutationFn: () => api.post("/thesis", {}),
    onSuccess: (res) => {
      toast.success("Initial Thesis draft created.");
      qc.invalidateQueries({ queryKey: ["ams-thesis-mine"] });
      setOpenId(res.data.id);
    },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not create your Initial Thesis. Fill in your Research Title in your PPW first.")),
  });

  const saveDetails = useMutation({
    mutationFn: () => api.patch(`/thesis/${openId}`, {
      plagiarism_student_percent: percent === "" ? null : Number(percent),
      plagiarism_software_name: software || null,
      abstract: abstractText || null,
    }),
    onSuccess: () => { toast.success("Changes saved."); refetch(); qc.invalidateQueries({ queryKey: ["ams-thesis-mine"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save changes.")),
  });

  const uploadDoc = useMutation({
    mutationFn: ({ type, file }: { type: string; file: File }) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.post(`/thesis/${openId}/documents/${type}`, fd, { headers: { "Content-Type": "multipart/form-data" } });
    },
    onSuccess: () => { toast.success("Document uploaded."); refetch(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Only .docx files are accepted.")),
  });

  const submit = useMutation({
    mutationFn: () => api.post(`/thesis/${openId}/submit`),
    onSuccess: () => { toast.success("Initial Thesis submitted for approval."); refetch(); qc.invalidateQueries({ queryKey: ["ams-thesis-mine"] }); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not submit your Initial Thesis.")),
  });

  function openDetail(id: string) {
    setOpenId(id);
  }
  function close() {
    setOpenId(null); setPercent(""); setSoftware(""); setAbstractText(""); setHydratedFor(null);
  }
  function download(documentId: string) {
    window.open(`${api.defaults.baseURL}/thesis/${openId}/documents/${documentId}/download`, "_blank");
  }
  function pickFile(type: string) {
    fileInputs.current[type]?.click();
  }

  const hasThesis = mine.length > 0;

  return (
    <div className="p-6 w-full max-w-5xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><FileText size={24} className="text-[#0D6E6E]" />Thesis Management</h1>
          <p className="text-gray-700 text-base mt-1">Prepare and track your Initial Thesis application.</p>
        </div>
        {!hasThesis && (
          <button onClick={() => create.mutate()} disabled={create.isPending}
            className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F] disabled:opacity-50">
            {create.isPending ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Upload Thesis
          </button>
        )}
      </div>

      <section className="space-y-2">
        <h2 className="text-lg font-bold text-gray-900">Thesis Applications</h2>
        {isLoading ? <Loader2 className="animate-spin text-gray-500" /> : mine.length === 0 ? (
          <p className="text-sm text-gray-600">You have not started an Initial Thesis yet.</p>
        ) : (
          <div className="bg-white rounded-2xl border border-gray-200 overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200"><tr>{["Sl No.", "Title", "Thesis Type", "Status", "Action"].map((h) => <th key={h} className="text-left px-4 py-2.5 font-semibold text-gray-600">{h}</th>)}</tr></thead>
              <tbody>
                {mine.map((r, i) => (
                  <tr key={r.id} className="border-b border-gray-50 last:border-0">
                    <td className="px-4 py-2.5">{i + 1}</td>
                    <td className="px-4 py-2.5 font-semibold text-gray-800">{r.title || "—"}</td>
                    <td className="px-4 py-2.5">{r.thesis_type_label}</td>
                    <td className="px-4 py-2.5"><ThesisStatusBadge status={r.status} label={r.status_label} /></td>
                    <td className="px-4 py-2.5"><button onClick={() => openDetail(r.id)} className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]"><Eye size={13} /> View Details</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {openId && detail && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50 p-4" onClick={close}>
          <div className="bg-white rounded-2xl max-w-4xl w-full max-h-[90vh] overflow-y-auto p-6 space-y-6" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <h2 className="text-xl font-bold text-gray-900">Thesis Status</h2>
              <div className="flex items-center gap-3">
                <ThesisStatusBadge status={detail.status} label={detail.status_label} />
                <button onClick={close} aria-label="Close"><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
              </div>
            </div>

            {detail.revert_info && <RevertNotice info={detail.revert_info} />}

            {/* 1. Student & Thesis Details */}
            <section className="space-y-3">
              <h3 className="text-lg font-bold text-gray-900">1. Student & Thesis Details</h3>
              <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-1">
                <p className="text-sm font-semibold text-gray-700">Thesis Title</p>
                <p className="text-gray-900">{detail.title || "—"}</p>
              </div>

              <div className="bg-white rounded-2xl border border-gray-200 p-5 flex items-center justify-between">
                <div>
                  <p className="text-sm font-semibold text-gray-700 mb-1">Thesis File</p>
                  {detail.documents.thesis_file ? <p className="text-xs text-gray-500">{detail.documents.thesis_file.original_filename}</p> : <p className="text-xs text-gray-400">Not uploaded yet</p>}
                </div>
                <div className="flex gap-2">
                  {detail.documents.thesis_file && <button onClick={() => download(detail.documents.thesis_file!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Thesis File</button>}
                  {detail.can_edit && (
                    <>
                      <input ref={(el) => { fileInputs.current.thesis_file = el; }} type="file" accept=".docx" hidden
                        onChange={(e) => e.target.files?.[0] && uploadDoc.mutate({ type: "thesis_file", file: e.target.files[0] })} />
                      <button onClick={() => pickFile("thesis_file")} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">{detail.documents.thesis_file ? "Replace" : "Upload"}</button>
                    </>
                  )}
                </div>
              </div>

              <StudentInfoCard student={detail.student} />

              <div className="bg-white rounded-2xl border border-gray-200 p-5 space-y-2">
                <p className="text-sm font-semibold text-gray-700">Plagiarism by Student: {detail.plagiarism_student_percent ?? "—"}%</p>
                {detail.can_edit ? (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <input type="number" min={0} max={100} placeholder="Plagiarism %" value={percent} onChange={(e) => setPercent(e.target.value)}
                      className="border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                    <input type="text" placeholder="Plagiarism Software Name" value={software} onChange={(e) => setSoftware(e.target.value)}
                      className="border border-gray-300 rounded-lg px-3 py-2 text-sm" />
                  </div>
                ) : null}
                <div className="flex items-center gap-2">
                  {detail.documents.plagiarism_student_report && <button onClick={() => download(detail.documents.plagiarism_student_report!.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">View Report</button>}
                  {detail.can_edit && (
                    <>
                      <input ref={(el) => { fileInputs.current.plagiarism_student_report = el; }} type="file" accept=".docx" hidden
                        onChange={(e) => e.target.files?.[0] && uploadDoc.mutate({ type: "plagiarism_student_report", file: e.target.files[0] })} />
                      <button onClick={() => pickFile("plagiarism_student_report")} className="px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]">Upload</button>
                    </>
                  )}
                </div>
              </div>

              <div className="bg-white rounded-2xl border border-gray-200 p-5">
                <p className="text-sm font-semibold text-gray-700">Plagiarism by Library: {detail.plagiarism_library_percent ?? "—"}%</p>
                <p className="text-xs text-gray-500 mt-1">The library plagiarism report is confidential and is not shown to students.</p>
              </div>

              {detail.can_edit && (
                <div>
                  <label className="block text-sm font-semibold text-gray-700 mb-1">Thesis Abstract</label>
                  <textarea rows={5} value={abstractText} onChange={(e) => setAbstractText(e.target.value)}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              )}
              {!detail.can_edit && detail.abstract && (
                <div className="bg-white rounded-2xl border border-gray-200 p-5">
                  <p className="text-sm font-semibold text-gray-700 mb-1">Thesis Abstract</p>
                  <p className="text-gray-800 whitespace-pre-wrap text-sm">{detail.abstract}</p>
                </div>
              )}

              {detail.signature_table && <SignatureTable rows={detail.signature_table} />}
            </section>

            {/* 2. Student Documents */}
            <section className="space-y-3">
              <h3 className="text-lg font-bold text-gray-900">2. Student Documents</h3>
              <div className="bg-white rounded-2xl border border-gray-200 divide-y divide-gray-100">
                {STUDENT_DOC_TYPES.filter((t) => t !== "thesis_file" && t !== "plagiarism_student_report").map((type) => {
                  const doc = detail.documents[type];
                  const printable = PRINTABLE_DOCUMENT_TYPES.has(type);
                  return (
                    <div key={type} className="flex items-center justify-between px-5 py-3">
                      <div>
                        <p className="text-sm font-semibold text-gray-800">{STUDENT_DOCUMENT_LABELS[type]}</p>
                        {doc ? <p className="text-xs text-gray-500">{doc.original_filename}</p> : <p className="text-xs text-gray-400">Not uploaded</p>}
                      </div>
                      <div className="flex gap-2">
                        {doc && <button onClick={() => download(doc.id)} className="px-3 py-1.5 border border-gray-200 rounded-lg text-xs font-semibold hover:bg-gray-50">{printable ? "View / Print" : "View"}</button>}
                        {detail.can_edit && (
                          <>
                            <input ref={(el) => { fileInputs.current[type] = el; }} type="file" accept=".docx" hidden
                              onChange={(e) => e.target.files?.[0] && uploadDoc.mutate({ type, file: e.target.files[0] })} />
                            <button onClick={() => pickFile(type)} className="flex items-center gap-1 px-3 py-1.5 bg-[#0D6E6E] text-white rounded-lg text-xs font-semibold hover:bg-[#178F8F]"><Upload size={12} /> Upload</button>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </section>

            {/* 3. External Report */}
            <section className="space-y-3">
              <h3 className="text-lg font-bold text-gray-900">3. External Report</h3>
              {detail.external_report ? <ExternalReportTable rows={detail.external_report} /> : (
                <p className="text-sm text-gray-600">The external evaluation is not yet complete.</p>
              )}
            </section>

            {detail.stages && (
              <section className="space-y-2">
                <h3 className="text-lg font-bold text-gray-900">Approval Progress</h3>
                <StageTimeline stages={detail.stages} />
              </section>
            )}

            {detail.can_edit && (
              <div className="flex gap-3 justify-end border-t border-gray-100 pt-4">
                <button onClick={close} className="px-4 py-2 border border-gray-200 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-50">Close</button>
                <button onClick={() => saveDetails.mutate()} disabled={saveDetails.isPending}
                  className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
                  {saveDetails.isPending ? "Saving…" : "Save Changes"}
                </button>
                <button onClick={() => { if (confirm("Submit this Initial Thesis for approval? You will not be able to edit it until it is reverted.")) submit.mutate(); }}
                  disabled={submit.isPending}
                  className="px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
                  {submit.isPending ? "Submitting…" : "Submit Thesis"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
