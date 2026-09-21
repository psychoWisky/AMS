"use client";
import { useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ScrollText, Loader2, Upload, Eye, Download, Send, Lock, Plus } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  ApprovalTable, PdfPreviewModal, RevertNotice, StudentInfoCard, SynopsisHistory, SynopsisStatusBadge,
  apiErrorMessage, blobErrorMessage, downloadPdf, formatDateTime, type SynopsisDetail,
} from "@/components/ui/synopsis-parts";

// Student's First Synopsis. Every value about the student (name, roll, programme, disciplines, college) comes from
// the backend and is read-only here. The backend enforces ownership, the PDF-only upload, one First Synopsis per
// student, and the workflow — this page only presents it.

const MAX_MB = 10;

export default function SynopsisPage() {
  const qc = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [draftTitle, setDraftTitle] = useState<string | null>(null);   // null = no unsaved edit
  const [confirmSubmit, setConfirmSubmit] = useState(false);
  const [preview, setPreview] = useState<"file" | "document" | null>(null);

  const { data, isLoading, isError } = useQuery<SynopsisDetail | null>({
    queryKey: ["ams-synopsis-me"],
    queryFn: async () => {
      try {
        return (await api.get<SynopsisDetail>("/synopsis/me")).data;
      } catch (e) {
        if ((e as { response?: { status?: number } })?.response?.status === 404) return null;
        throw e;
      }
    },
    retry: false,
  });

  const title = draftTitle ?? data?.title ?? "";

  const refresh = () => qc.invalidateQueries({ queryKey: ["ams-synopsis-me"] });

  const create = useMutation({
    mutationFn: () => api.post("/synopsis", {}),
    onSuccess: () => { toast.success("Synopsis created."); refresh(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not create the Synopsis.")),
  });
  const saveTitle = useMutation({
    mutationFn: () => api.patch(`/synopsis/${data?.id}`, { title }),
    onSuccess: () => { toast.success("Title saved."); setDraftTitle(null); refresh(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Could not save the title.")),
  });
  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      // the shared client defaults to JSON; clearing it lets the browser set the multipart boundary (same as the bulk-upload modals)
      return api.post(`/synopsis/${data?.id}/file`, form, { headers: { "Content-Type": undefined } });
    },
    onSuccess: () => { toast.success("PDF uploaded."); refresh(); },
    onError: (e) => toast.error(apiErrorMessage(e, "Upload failed.")),
  });
  const submit = useMutation({
    mutationFn: () => api.patch(`/synopsis/${data?.id}/submit`),
    onSuccess: () => { toast.success("Synopsis submitted for approval."); setConfirmSubmit(false); refresh(); },
    onError: (e) => { setConfirmSubmit(false); toast.error(apiErrorMessage(e, "Could not submit the Synopsis.")); },
  });
  const download = useMutation({
    mutationFn: (kind: "file" | "document") => downloadPdf(`/synopsis/${data?.id}/${kind}`, kind === "file" ? "synopsis-upload.pdf" : "synopsis.pdf"),
    onError: async (e) => toast.error(await blobErrorMessage(e, "Download failed.")),
  });

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) { toast.error("Only PDF files can be uploaded."); return; }
    if (file.size > MAX_MB * 1024 * 1024) { toast.error(`The file is larger than ${MAX_MB} MB.`); return; }
    upload.mutate(file);
  }

  if (isLoading) return <div className="flex justify-center py-24"><Loader2 className="animate-spin text-gray-600" /></div>;
  if (isError) return <div className="p-6"><p className="text-red-700">Could not load your Synopsis. Please try again.</p></div>;

  const header = (
    <div>
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><ScrollText size={24} className="text-[#0D6E6E]" />Synopsis</h1>
      <p className="text-gray-700 text-base mt-1">Synopsis of Thesis/Dissertation Problem — First Synopsis</p>
    </div>
  );

  if (!data) {
    return (
      <div className="p-6 max-w-3xl space-y-6">
        {header}
        <div className="bg-white rounded-2xl border border-gray-200 p-8 text-center">
          <p className="text-gray-800 mb-1 font-semibold">You have not created your First Synopsis yet.</p>
          <p className="text-gray-600 text-sm mb-5">A student can have only one First Synopsis. Your name, roll number, programme, disciplines and college are filled in automatically from your AMS record.</p>
          <button onClick={() => create.mutate()} disabled={create.isPending}
            className="inline-flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F] disabled:opacity-60">
            {create.isPending ? <Loader2 size={16} className="animate-spin" /> : <Plus size={16} />} Create First Synopsis
          </button>
        </div>
      </div>
    );
  }

  const titleDirty = title.trim() !== (data.title ?? "");
  const canSubmit = data.can_edit && !!data.title?.trim() && !!data.file && !titleDirty;
  const submitHint = !data.title?.trim() ? "Enter and save the Title of the Research Problem."
    : titleDirty ? "Save your title changes before submitting."
    : !data.file ? "Upload your Synopsis PDF." : null;
  const resubmitting = data.status === "reverted";

  return (
    <div className="p-6 max-w-5xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        {header}
        <SynopsisStatusBadge status={data.status} label={data.status_label} />
      </div>

      {data.revert_info && <RevertNotice info={data.revert_info} />}

      {data.status === "approved" && (
        <div className="rounded-2xl border border-green-300 bg-green-50 p-4 flex items-start gap-3">
          <Lock size={18} className="text-green-700 mt-0.5" />
          <div className="text-sm text-green-900">
            <p className="font-bold">Approved on {formatDateTime(data.approved_at)}</p>
            <p>Your Synopsis has received final approval and can no longer be edited. The approved document is frozen.</p>
          </div>
        </div>
      )}

      <section className="space-y-2">
        <h2 className="text-lg font-bold text-gray-900">Your details</h2>
        <p className="text-sm text-gray-600">Filled in automatically from your AMS record — they cannot be edited here.</p>
        <StudentInfoCard student={data.student} title={data.title} />
      </section>

      <section className="bg-white rounded-2xl border border-gray-200 p-5 space-y-3">
        <h2 className="text-lg font-bold text-gray-900">Title of the Research Problem</h2>
        <textarea value={title} onChange={(e) => setDraftTitle(e.target.value)} rows={3} disabled={!data.can_edit}
          placeholder="Enter the title of your research problem"
          className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50" />
        {data.can_edit && (
          <button onClick={() => saveTitle.mutate()} disabled={!titleDirty || saveTitle.isPending}
            className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-50">
            {saveTitle.isPending ? "Saving…" : "Save Title"}
          </button>
        )}
      </section>

      <section className="bg-white rounded-2xl border border-gray-200 p-5 space-y-3">
        <h2 className="text-lg font-bold text-gray-900">Synopsis PDF</h2>
        {data.file ? (
          <div className="flex flex-wrap items-center justify-between gap-3 bg-gray-50 rounded-xl px-4 py-3">
            <div className="text-sm">
              <p className="font-semibold text-gray-900">{data.file.original_filename}</p>
              <p className="text-gray-600">{data.file.page_count} page{data.file.page_count === 1 ? "" : "s"} · {(data.file.size_bytes / 1024 / 1024).toFixed(2)} MB · uploaded {formatDateTime(data.file.uploaded_at)}</p>
            </div>
            <div className="flex gap-2">
              <button onClick={() => setPreview("file")} className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 rounded-lg text-sm font-semibold hover:bg-white"><Eye size={14} /> Preview</button>
              <button onClick={() => download.mutate("file")} className="flex items-center gap-1.5 px-3 py-1.5 border border-gray-300 rounded-lg text-sm font-semibold hover:bg-white"><Download size={14} /> Download</button>
            </div>
          </div>
        ) : <p className="text-sm text-gray-600">No PDF uploaded yet.</p>}
        {data.can_edit && (
          <div>
            <input ref={fileInput} type="file" accept=".pdf,application/pdf" onChange={onPick} className="hidden" data-testid="synopsis-file-input" />
            <button onClick={() => fileInput.current?.click()} disabled={upload.isPending}
              className="flex items-center gap-2 px-4 py-2 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-sm font-semibold hover:bg-[#E6F4F4] disabled:opacity-60">
              {upload.isPending ? <Loader2 size={15} className="animate-spin" /> : <Upload size={15} />} {data.file ? "Replace PDF" : "Upload PDF"}
            </button>
            <p className="text-xs text-gray-500 mt-1">PDF only, up to {MAX_MB} MB.</p>
          </div>
        )}
      </section>

      {data.can_edit && (
        <section className="bg-white rounded-2xl border border-gray-200 p-5">
          <button onClick={() => setConfirmSubmit(true)} disabled={!canSubmit || submit.isPending}
            className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold hover:bg-[#178F8F] disabled:opacity-50">
            <Send size={16} /> {resubmitting ? "Resubmit Synopsis" : "Submit Synopsis"}
          </button>
          {submitHint && <p className="text-sm text-amber-700 mt-2">{submitHint}</p>}
          <p className="text-xs text-gray-500 mt-2">Submitting sends the Synopsis to your Major Advisor, then every Advisory Committee member, the HOD, the Incharge Academic Cell and the DPGS.</p>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="text-lg font-bold text-gray-900">Approval progress</h2>
        <ApprovalTable committee={data.committee} approvals={data.approvals} />
      </section>

      {data.file && (
        <section className="bg-white rounded-2xl border border-gray-200 p-5 space-y-3">
          <h2 className="text-lg font-bold text-gray-900">Generated Synopsis document</h2>
          <p className="text-sm text-gray-600">{data.status === "approved" ? "The approved, frozen document." : "The AVFU Synopsis document — signatures appear here as your approvers sign."}</p>
          <div className="flex gap-2">
            <button onClick={() => setPreview("document")} className="flex items-center gap-1.5 px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F]"><Eye size={15} /> View Document</button>
            <button onClick={() => download.mutate("document")} disabled={download.isPending}
              className="flex items-center gap-1.5 px-4 py-2 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl text-sm font-semibold hover:bg-[#E6F4F4] disabled:opacity-60">
              {download.isPending ? <Loader2 size={15} className="animate-spin" /> : <Download size={15} />} Download PDF
            </button>
          </div>
        </section>
      )}

      {data.history.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-lg font-bold text-gray-900">Approval history</h2>
          <SynopsisHistory history={data.history} />
        </section>
      )}

      {confirmSubmit && (
        <ConfirmDialog title={resubmitting ? "Resubmit Synopsis" : "Submit Synopsis"}
          message="Your Synopsis will be sent for approval starting with your Major Advisor. You cannot edit it while it is under approval."
          confirmLabel="Yes, Submit" confirmClassName="bg-[#0D6E6E] hover:bg-[#178F8F] text-white"
          onConfirm={() => submit.mutate()} onCancel={() => setConfirmSubmit(false)} />
      )}
      {preview === "file" && <PdfPreviewModal title="Uploaded Synopsis PDF" path={`/synopsis/${data.id}/file`} params={{ inline: "true" }} filename="synopsis-upload.pdf" onClose={() => setPreview(null)} />}
      {preview === "document" && <PdfPreviewModal title="Synopsis Document" path={`/synopsis/${data.id}/document`} params={{ inline: "true" }} filename="synopsis.pdf" onClose={() => setPreview(null)} />}
    </div>
  );
}
