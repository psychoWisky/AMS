"use client";
import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { X, Download, Upload, Loader2, AlertTriangle, Ban } from "lucide-react";

// Course-code duplicate-detection fix (this revision) — a DEDICATED modal
// for course bulk upload, deliberately NOT built on top of the shared
// `UserBulkUploadModal` (Faculty/User bulk upload), which has no concept of
// a warning/confirmation step and is left completely untouched. Course
// bulk upload now needs a genuinely different two-phase flow: the backend
// (`POST /courses/bulk-upload`) can respond with HTTP 409 + `warnings` +
// `confirmation_token` when it finds same-code-different-title courses —
// see docs/BUSINESS_LOGIC.md's course-duplicate-detection section. This
// component is the ONLY place that flow is implemented; the backend's
// validation (hard-duplicate rejection, warning detection, confirmation
// verification, final re-validation) is authoritative — this UI only
// presents whatever the backend already decided, never re-implements or
// second-guesses the rules itself.
interface BulkUploadFinding { row: number; column: string; value: string; error: string; }
interface DuplicateWarning {
  row: number;
  department_name: string;
  course_number: string;
  incoming_title: string;
  existing_title: string;
  existing_row: number | null;
  message: string;
}
interface BulkUploadResult {
  success: boolean;
  imported_count: number;
  filename?: string;
  errors?: BulkUploadFinding[];
  requires_confirmation?: boolean;
  warnings?: DuplicateWarning[];
  confirmation_token?: string;
}

export function CourseBulkUploadModal({
  uploadUrl, templateUrl, templateFilename, onClose, onSuccess,
  title = "Bulk Upload Courses",
  description,
}: {
  uploadUrl: string;
  templateUrl: string;
  templateFilename: string;
  onClose: () => void;
  onSuccess: (importedCount: number) => void;
  title?: string;
  description?: React.ReactNode;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [errorResult, setErrorResult] = useState<BulkUploadResult | null>(null);
  const [pendingWarnings, setPendingWarnings] = useState<DuplicateWarning[] | null>(null);
  const [confirmationToken, setConfirmationToken] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const downloadTemplate = useMutation({
    mutationFn: async () => {
      const res = await api.get(templateUrl, { responseType: "blob" });
      const blobUrl = window.URL.createObjectURL(res.data);
      const link = document.createElement("a");
      link.href = blobUrl;
      link.download = templateFilename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(blobUrl);
    },
    onError: () => toast.error("Failed to download template."),
  });

  function resetForNewFile() {
    setErrorResult(null);
    setPendingWarnings(null);
    setConfirmationToken(null);
    setConfirmed(false);
  }

  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("No file selected.");
      const fd = new FormData();
      fd.append("file", file);
      // Course duplicate-warning confirmation fix — the SAME file is
      // re-submitted for the confirm step (never just a bare "confirmed"
      // flag): the backend independently re-hashes whatever file arrives
      // and rejects a mismatch, so it must actually be the file the
      // warnings were shown for, not merely a claim that it is.
      if (pendingWarnings && confirmationToken) {
        fd.append("confirm_warnings", "true");
        fd.append("confirmation_token", confirmationToken);
      }
      return api.post<BulkUploadResult>(uploadUrl, fd, { headers: { "Content-Type": undefined } });
    },
    onSuccess: (res) => {
      if (res.data.success) {
        onSuccess(res.data.imported_count);
      } else {
        setErrorResult(res.data);
      }
    },
    onError: (e: unknown) => {
      const err = e as { response?: { status?: number; data?: BulkUploadResult } };
      const data = err?.response?.data;
      if (err?.response?.status === 409 && data?.requires_confirmation) {
        // Warning path — backend found same-code-different-title courses
        // and created NOTHING. Show them and require explicit confirmation
        // before the next upload attempt can proceed.
        setPendingWarnings(data.warnings ?? []);
        setConfirmationToken(data.confirmation_token ?? null);
        setConfirmed(false);
        setErrorResult(null);
        return;
      }
      if (data && typeof data.success === "boolean") {
        setErrorResult(data);
        return;
      }
      toast.error("Upload failed. No courses were created.");
    },
  });

  function close() {
    setFile(null);
    resetForNewFile();
    if (fileInputRef.current) fileInputRef.current.value = "";
    onClose();
  }

  const needsConfirmation = pendingWarnings !== null && pendingWarnings.length > 0;
  const uploadDisabled = !file || upload.isPending || (needsConfirmation && !confirmed);

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold text-gray-900">{title}</h3>
          <button onClick={close}><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
        </div>
        <p className="text-sm text-gray-600 mb-3">
          {description ?? (
            <>Upload an <span className="font-semibold">.xlsx</span> or <span className="font-semibold">.csv</span> file with columns:
              Course Number, Course Title, Programme (UG/PG/PhD), Course Type, Credit Type, Theory Credit, Practical Credit, Status, Department.
              Course codes no longer need to be unique — the same code may exist more than once, including within the same department,
              as long as the course titles differ. Use <span className="font-semibold">Download Template</span> below to get the exact format.</>
          )}
        </p>
        <button onClick={() => downloadTemplate.mutate()} disabled={downloadTemplate.isPending}
          className="flex items-center gap-2 px-3 py-2 border border-gray-300 text-gray-700 rounded-xl text-sm font-semibold hover:bg-gray-50 disabled:opacity-50 mb-3">
          <Download size={14} /> Download Template
        </button>
        <input ref={fileInputRef} type="file" accept=".xlsx,.csv"
          onChange={(e) => { setFile(e.target.files?.[0] ?? null); resetForNewFile(); }}
          className="w-full text-sm border border-gray-300 rounded-xl px-3 py-2 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:bg-gray-100 file:text-gray-700 file:font-semibold" />
        {file && <p className="text-xs text-gray-500 mt-1">Selected: {file.name}</p>}

        {/* Hard duplicate / ordinary validation errors — cannot be overridden. */}
        {errorResult && !errorResult.success && (
          <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-xl">
            <p className="text-sm font-bold text-red-700 mb-2 flex items-center gap-1.5">
              <Ban size={15} /> Duplicate course(s) detected — this file cannot be uploaded as-is.
            </p>
            <p className="text-xs text-red-700 mb-2">
              One or more rows are an exact duplicate (same department, same course code, and the same title) of an
              existing course or another row in this file. These rows cannot be uploaded. There is no "Continue anyway"
              option for an exact duplicate — please correct or remove the affected row(s) and re-upload.
            </p>
            <div className="max-h-64 overflow-auto border border-red-200 rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-red-100 sticky top-0">
                  <tr>{["Row", "Column", "Value", "Error"].map((h) => (
                    <th key={h} className="text-left px-2 py-1.5 font-semibold text-red-800">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {(errorResult.errors ?? []).map((f, i) => (
                    <tr key={i} className={i % 2 === 0 ? "bg-white" : "bg-red-50/50"}>
                      <td className="px-2 py-1.5 font-mono">{f.row}</td>
                      <td className="px-2 py-1.5 font-semibold text-gray-800 whitespace-nowrap">{f.column}</td>
                      <td className="px-2 py-1.5 font-mono text-gray-600 max-w-[160px] truncate" title={f.value}>{f.value || "—"}</td>
                      <td className="px-2 py-1.5 text-red-700">{f.error}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Same code, different title — reviewable, requires explicit confirmation. */}
        {needsConfirmation && (
          <div className="mt-4 p-3 bg-amber-50 border border-amber-200 rounded-xl">
            <p className="text-sm font-bold text-amber-800 mb-2 flex items-center gap-1.5">
              <AlertTriangle size={15} /> Course code already exists — please review before continuing
            </p>
            <p className="text-xs text-amber-800 mb-2">
              The row(s) below share a department and course code with an existing course (or another row in this
              file), but the title is different. This can be a valid, intentionally separate course (e.g. the same
              code offered in different semesters) — please verify before continuing.
            </p>
            <div className="max-h-64 overflow-auto border border-amber-200 rounded-lg divide-y divide-amber-200">
              {pendingWarnings!.map((w, i) => (
                <div key={i} className="p-2.5 text-xs bg-white">
                  <p className="font-semibold text-gray-800 mb-1">
                    Row {w.row} — {w.department_name} / <span className="font-mono">{w.course_number}</span>
                  </p>
                  <p className="text-gray-600">
                    Existing: <span className="font-mono">{w.department_name} / {w.course_number}</span> — “{w.existing_title}”
                    {w.existing_row ? ` (also row ${w.existing_row} in this file)` : ""}
                  </p>
                  <p className="text-gray-600">
                    New: <span className="font-mono">{w.department_name} / {w.course_number}</span> — “{w.incoming_title}” (row {w.row})
                  </p>
                </div>
              ))}
            </div>
            <label className="flex items-start gap-2 mt-3 text-xs text-amber-900 cursor-pointer">
              <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} className="mt-0.5" />
              I have reviewed the potential duplicate courses above and confirm that they are intentional.
            </label>
          </div>
        )}

        <div className="flex gap-3 mt-5">
          <button onClick={close} className="flex-1 py-2.5 border border-gray-300 text-gray-700 rounded-xl text-base font-bold hover:bg-gray-50">
            Cancel
          </button>
          <button onClick={() => upload.mutate()} disabled={uploadDisabled}
            className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50 flex items-center justify-center gap-2">
            {upload.isPending
              ? (<><Loader2 size={16} className="animate-spin" /> Uploading…</>)
              : (<><Upload size={16} /> {needsConfirmation ? "Confirm & Upload" : "Upload"}</>)}
          </button>
        </div>
      </div>
    </div>
  );
}
