"use client";
import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { X, Download, Upload, Loader2 } from "lucide-react";

// Bulk Faculty/User Excel Upload task (this revision) — shared by BOTH
// upload locations (HOD Faculties -> Bulk Upload, Super Admin User
// Management -> Bulk Upload), which use the exact same Excel template and
// error-response shape, per the confirmed requirement not to duplicate this
// UI in two unrelated places (mirrors the backend's own
// `_build_user_bulk_template_workbook` sharing, app/api/v1/endpoints/auth.py).
// Deliberately a NEW, separate component from orientation/page.tsx's own
// inline bulk-upload UI — that feature's error shape (`{row, errors[]}`) is
// different from this one's flat, row+column-addressable findings
// (`{row, column, value, error}`, Section 23's explicit requirement), so
// sharing a single generic component across both would force one of them
// into the wrong shape; orientation.py's own UI is left untouched.
interface BulkUploadFinding { row: number; column: string; value: string; error: string; }
interface BulkUploadResult {
  success: boolean; imported_count: number; filename?: string;
  errors?: BulkUploadFinding[]; emails_sent?: number; emails_total?: number;
}

export function UserBulkUploadModal({
  uploadUrl, templateUrl, templateFilename, onClose, onSuccess,
  title = "Bulk Upload Users",
  description = (
    <>Upload an <span className="font-semibold">.xlsx</span> or <span className="font-semibold">.csv</span> file with columns:
      First name, Middle name (optional), Last name, AVFU email, Designation, Role, College, Department, Gender (optional), Mobile (optional).
      College and Department are matched by their exact existing code. Use <span className="font-semibold">Download Template</span> below to get the exact format.</>
  ),
}: {
  uploadUrl: string;
  templateUrl: string;
  templateFilename: string;
  onClose: () => void;
  onSuccess: (importedCount: number, emailsSent: number, emailsTotal: number) => void;
  // Bulk Course Upload task (this revision) — this modal's file-upload/
  // template-download/error-table UI is entirely generic (row/column/value/
  // error findings, same shared shape), so it's reused as-is for Courses
  // rather than duplicating the component; only the header copy differs,
  // which these two optional props override. Defaults preserve the exact
  // existing Faculty/User text byte-for-byte for the two existing callers.
  title?: string;
  description?: React.ReactNode;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<BulkUploadResult | null>(null);
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

  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("No file selected.");
      const fd = new FormData();
      fd.append("file", file);
      // The shared `api` instance defaults to Content-Type: application/json
      // (services/api.ts) — axios only computes the correct multipart
      // boundary header when no Content-Type is already present, so it must
      // be explicitly unset here (same fix already used by orientation's
      // own bulk-upload mutation) or the request reaches the backend with
      // no parseable `file` field at all.
      return api.post<BulkUploadResult>(uploadUrl, fd, { headers: { "Content-Type": undefined } });
    },
    onSuccess: (res) => {
      if (res.data.success) {
        onSuccess(res.data.imported_count, res.data.emails_sent ?? 0, res.data.emails_total ?? 0);
      } else {
        setResult(res.data);
      }
    },
    onError: (e: unknown) => {
      // Validation failures come back as a normal error response (400/409)
      // carrying the SAME structured { success, imported_count, errors }
      // shape as a successful call — surface it in the same result panel
      // rather than only a generic toast, per Section 24's explicit
      // "do not show a misleading success message" requirement.
      const data = (e as { response?: { data?: BulkUploadResult } })?.response?.data;
      if (data && typeof data.success === "boolean") {
        setResult(data);
      } else {
        toast.error("Upload failed. No users were created.");
      }
    },
  });

  function close() {
    setFile(null);
    setResult(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    onClose();
  }

  return (
    <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl p-6 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-bold text-gray-900">{title}</h3>
          <button onClick={close}><X size={20} className="text-gray-400 hover:text-gray-700" /></button>
        </div>
        <p className="text-sm text-gray-600 mb-3">{description}</p>
        <button onClick={() => downloadTemplate.mutate()} disabled={downloadTemplate.isPending}
          className="flex items-center gap-2 px-3 py-2 border border-gray-300 text-gray-700 rounded-xl text-sm font-semibold hover:bg-gray-50 disabled:opacity-50 mb-3">
          <Download size={14} /> Download Template
        </button>
        <input ref={fileInputRef} type="file" accept=".xlsx,.csv"
          onChange={(e) => { setFile(e.target.files?.[0] ?? null); setResult(null); }}
          className="w-full text-sm border border-gray-300 rounded-xl px-3 py-2 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:bg-gray-100 file:text-gray-700 file:font-semibold" />
        {file && <p className="text-xs text-gray-500 mt-1">Selected: {file.name}</p>}

        {result && !result.success && (
          <div className="mt-4 p-3 bg-red-50 border border-red-200 rounded-xl">
            <p className="text-sm font-bold text-red-700 mb-2">
              Bulk upload failed. No users were created because the file contains validation errors.
            </p>
            {/* Row/Column/Value/Error table (Section 23/24's explicit
                requirement) — scrollable for large files, every finding
                shown (validation never stops at the first error per row). */}
            <div className="max-h-64 overflow-auto border border-red-200 rounded-lg">
              <table className="w-full text-xs">
                <thead className="bg-red-100 sticky top-0">
                  <tr>{["Row", "Column", "Value", "Error"].map((h) => (
                    <th key={h} className="text-left px-2 py-1.5 font-semibold text-red-800">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {(result.errors ?? []).map((f, i) => (
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

        <div className="flex gap-3 mt-5">
          <button onClick={close} className="flex-1 py-2.5 border border-gray-300 text-gray-700 rounded-xl text-base font-bold hover:bg-gray-50">
            Cancel
          </button>
          <button onClick={() => upload.mutate()} disabled={!file || upload.isPending}
            className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-50 flex items-center justify-center gap-2">
            {upload.isPending ? (<><Loader2 size={16} className="animate-spin" /> Uploading…</>) : (<><Upload size={16} /> Upload</>)}
          </button>
        </div>
      </div>
    </div>
  );
}
