"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { BarChart3, Plus, CheckCircle2, XCircle, Lock, Send, Loader2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface SheetSummary { id: string; sheet_type: string; status: string; is_locked: boolean; }
interface GradeEntry { id: string; student_id: string; student_name: string; student_roll: string; internal_marks: number | null; external_marks: number | null; total_marks: number | null; grade_letter: string | null; grade_points: number | null; is_absent: boolean; }
interface Offering { id: string; course_number: string; course_title: string; section: string | null; }
interface SheetDetail { id: string; course_title: string; sheet_type: string; status: string; is_locked: boolean; entries: GradeEntry[]; approvals: { stage: number; role_required: string; status: string; approver_name: string | null; signed_at: string | null; remarks: string | null }[]; }

const STATUS_COLOR: Record<string, string> = { draft: "bg-gray-100 text-gray-700", submitted: "bg-amber-100 text-amber-700", under_review: "bg-blue-100 text-blue-700", approved: "bg-green-100 text-green-700", published: "bg-teal-100 text-teal-700" };
const GRADE_SCALE = [{ grade: "O", pts: 10, range: "90-100" }, { grade: "A+", pts: 9, range: "80-89" }, { grade: "A", pts: 8, range: "70-79" }, { grade: "B+", pts: 7, range: "60-69" }, { grade: "B", pts: 6, range: "55-59" }, { grade: "C", pts: 5, range: "50-54" }, { grade: "P", pts: 4, range: "45-49" }, { grade: "F", pts: 0, range: "0-44" }];

export default function GradingPage() {
  const role = useRole();
  const user = useUser();
  const qc = useQueryClient();
  const [selectedOffering, setSelectedOffering] = useState("");
  const [selectedSheet, setSelectedSheet] = useState<SheetDetail | null>(null);
  const [editedMarks, setEditedMarks] = useState<Record<string, { internal: string; external: string; absent: boolean }>>({});
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  const { data: offerings = [] } = useQuery<Offering[]>({
    queryKey: ["ams-offerings-all"],
    queryFn: async () => (await api.get("/courses/offerings/all")).data,
  });

  const { data: sheets = [] } = useQuery<SheetSummary[]>({
    queryKey: ["ams-sheets", selectedOffering],
    queryFn: async () => (await api.get(`/grading/offering/${selectedOffering}/sheets`)).data,
    enabled: !!selectedOffering,
  });

  const loadSheet = async (sheetId: string) => {
    const res = await api.get(`/grading/sheets/${sheetId}`);
    const data = res.data;
    setSelectedSheet(data);
    const init: typeof editedMarks = {};
    data.entries.forEach((e: GradeEntry) => {
      init[e.student_id] = { internal: String(e.internal_marks ?? ""), external: String(e.external_marks ?? ""), absent: e.is_absent };
    });
    setEditedMarks(init);
  };

  const createSheet = useMutation({
    mutationFn: () => api.post(`/grading/sheets?offering_id=${selectedOffering}&sheet_type=final`),
    onSuccess: () => { toast.success("Grade sheet created."); qc.invalidateQueries({ queryKey: ["ams-sheets", selectedOffering] }); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const saveGrades = useMutation({
    mutationFn: () => {
      const entries = selectedSheet?.entries.map((e) => {
        const m = editedMarks[e.student_id];
        return { student_id: e.student_id, enrollment_id: null, internal_marks: m?.absent ? null : parseFloat(m?.internal) || null, external_marks: m?.absent ? null : parseFloat(m?.external) || null, is_absent: m?.absent ?? false };
      }) ?? [];
      return api.put(`/grading/sheets/${selectedSheet?.id}/entries`, { entries });
    },
    onSuccess: async () => { toast.success("Grades saved."); if (selectedSheet) await loadSheet(selectedSheet.id); },
  });

  const submitSheet = useMutation({
    mutationFn: () => api.patch(`/grading/sheets/${selectedSheet?.id}/submit`),
    onSuccess: async () => { toast.success("Sheet submitted for approval."); if (selectedSheet) await loadSheet(selectedSheet.id); },
  });

  const requestOtp = useMutation({
    mutationFn: () => api.get(`/grading/sheets/${selectedSheet?.id}/approval/otp`),
    onSuccess: () => { setOtpSent(true); toast.success("OTP sent to your email."); },
  });

  const approveStage = useMutation({
    mutationFn: () => api.post(`/grading/sheets/${selectedSheet?.id}/approve`, { pin: "0000", otp }),
    onSuccess: async () => { toast.success("Stage approved!"); setOtp(""); setOtpSent(false); if (selectedSheet) await loadSheet(selectedSheet.id); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "OTP invalid."),
  });

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><BarChart3 size={24} className="text-[#0D6E6E]" />Grading & Results</h1>
          <p className="text-gray-700 text-base mt-1">Grade sheets, tabulation, GPA/CGPA, and multi-stage approval</p>
        </div>
      </div>

      {/* Grading scale quick ref */}
      <div className="bg-white rounded-2xl border border-gray-200 p-4 mb-5">
        <p className="text-base font-semibold text-gray-700 mb-2">10-Point Grading Scale</p>
        <div className="flex flex-wrap gap-2">
          {GRADE_SCALE.map((g) => (
            <div key={g.grade} className="flex items-center gap-1.5 px-3 py-1 bg-gray-50 rounded-lg text-sm">
              <span className="font-bold text-[#0D6E6E]">{g.grade}</span>
              <span className="text-gray-700">{g.pts}pts ({g.range}%)</span>
            </div>
          ))}
        </div>
      </div>

      {/* Selection */}
      <div className="flex gap-3 mb-5">
        <select value={selectedOffering} onChange={(e) => { setSelectedOffering(e.target.value); setSelectedSheet(null); }}
          className="flex-1 max-w-md border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">Select offering…</option>
          {offerings.map((o) => <option key={o.id} value={o.id}>{o.course_number} — {o.course_title} {o.section ? `(${o.section})` : ""}</option>)}
        </select>
        {selectedOffering && !selectedSheet && (
          <button onClick={() => setConfirm({
            action: () => createSheet.mutate(),
            title: "Create Grade Sheet",
            message: "Create a new final grade sheet for this offering? Students with approved enrollments will be added automatically.",
            confirmLabel: "Yes, Create",
            confirmClassName: "bg-[#0D6E6E] hover:bg-[#178F8F] text-white",
          })} disabled={createSheet.isPending}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-60">
            <Plus size={14} /> Create Grade Sheet
          </button>
        )}
      </div>

      {/* Sheets list */}
      {selectedOffering && !selectedSheet && (
        <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-5">
          <p className="font-semibold text-gray-700 mb-3">Grade Sheets</p>
          {sheets.length === 0 ? <p className="text-sm text-gray-600">No sheets created yet. Create one above.</p> : (
            <div className="space-y-2">
              {sheets.map((s) => (
                <div key={s.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl cursor-pointer hover:bg-[#E6F4F4] transition-colors" onClick={() => loadSheet(s.id)}>
                  <div className="flex-1">
                    <p className="text-sm font-semibold capitalize">{s.sheet_type} Grade Sheet</p>
                  </div>
                  <span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[s.status] ?? ""}`}>{s.status}</span>
                  {s.is_locked && <Lock size={13} className="text-gray-600" />}
                  <span className="text-sm text-[#0D6E6E]">Open →</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Sheet detail & grade entry */}
      {selectedSheet && (
        <div className="space-y-5">
          <div className="flex items-center gap-3 bg-white rounded-2xl border border-gray-200 p-4">
            <button onClick={() => setSelectedSheet(null)} className="text-[#0D6E6E] hover:underline text-sm">← Back</button>
            <h2 className="font-bold text-gray-900 flex-1">{selectedSheet.course_title} — {selectedSheet.sheet_type} Sheet</h2>
            <span className={`px-3 py-1 rounded-full text-sm font-semibold ${STATUS_COLOR[selectedSheet.status] ?? ""}`}>{selectedSheet.status}</span>
            {selectedSheet.is_locked && <Lock size={14} className="text-gray-600" />}
          </div>

          {/* Grade entries table */}
          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
              <p className="font-semibold text-gray-700">Student Grades</p>
              {!selectedSheet.is_locked && (
                <div className="flex gap-2">
                  <button onClick={() => saveGrades.mutate()} disabled={saveGrades.isPending}
                    className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F] disabled:opacity-60">
                    {saveGrades.isPending ? <Loader2 size={14} className="animate-spin" /> : "Save Grades"}
                  </button>
                  {selectedSheet.status === "draft" && (
                    <button onClick={() => setConfirm({
                      action: () => submitSheet.mutate(),
                      title: "Submit for Approval",
                      message: "Are you sure you want to submit this grade sheet for the 5-stage approval process? Make sure all marks are final before submitting.",
                      confirmLabel: "Yes, Submit",
                      confirmClassName: "bg-amber-600 hover:bg-amber-700 text-white",
                    })} disabled={submitSheet.isPending}
                      className="px-4 py-2 bg-amber-600 text-white rounded-xl text-sm font-semibold hover:bg-amber-700 disabled:opacity-60 flex items-center gap-1.5">
                      <Send size={13} /> Submit for Approval
                    </button>
                  )}
                </div>
              )}
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["Roll No.", "Student Name", "Internal (40)", "External (60)", "Total", "Grade", "Points", "Absent"].map((h) => (
                    <th key={h} className="text-left px-4 py-3 font-semibold text-gray-600 text-sm">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {selectedSheet.entries.map((e, i) => {
                    const m = editedMarks[e.student_id] ?? { internal: "", external: "", absent: false };
                    return (
                      <tr key={e.student_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                        <td className="px-4 py-2.5 font-mono text-sm text-gray-600">{e.student_roll || "—"}</td>
                        <td className="px-4 py-2.5 font-medium">{e.student_name}</td>
                        <td className="px-4 py-2.5">
                          {selectedSheet.is_locked ? <span>{e.internal_marks ?? "—"}</span> : (
                            <input type="number" min={0} max={40} value={m.internal} disabled={m.absent}
                              onChange={(ev) => setEditedMarks((prev) => ({ ...prev, [e.student_id]: { ...m, internal: ev.target.value } }))}
                              className="w-20 border border-gray-200 rounded-lg px-2 py-1 text-base focus:outline-none focus:ring-1 focus:ring-[#0D6E6E] disabled:bg-gray-100" />
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          {selectedSheet.is_locked ? <span>{e.external_marks ?? "—"}</span> : (
                            <input type="number" min={0} max={100} value={m.external} disabled={m.absent}
                              onChange={(ev) => setEditedMarks((prev) => ({ ...prev, [e.student_id]: { ...m, external: ev.target.value } }))}
                              className="w-20 border border-gray-200 rounded-lg px-2 py-1 text-base focus:outline-none focus:ring-1 focus:ring-[#0D6E6E] disabled:bg-gray-100" />
                          )}
                        </td>
                        <td className="px-4 py-2.5 font-bold">{e.total_marks ?? "—"}</td>
                        <td className="px-4 py-2.5"><span className={`px-2 py-0.5 rounded text-base font-bold ${e.grade_letter === "F" ? "bg-red-100 text-red-700" : "bg-green-100 text-green-700"}`}>{e.grade_letter ?? "—"}</span></td>
                        <td className="px-4 py-2.5">{e.grade_points ?? "—"}</td>
                        <td className="px-4 py-2.5">
                          <input type="checkbox" checked={m.absent} disabled={selectedSheet.is_locked}
                            onChange={(ev) => setEditedMarks((prev) => ({ ...prev, [e.student_id]: { ...m, absent: ev.target.checked } }))} />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Approval pipeline */}
          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <p className="font-semibold text-gray-700 mb-4">5-Stage Approval Pipeline</p>
            <div className="space-y-2">
              {selectedSheet.approvals.map((a) => (
                <div key={a.stage} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center text-base font-bold text-white ${a.status === "approved" ? "bg-green-500" : a.status === "rejected" ? "bg-red-500" : "bg-gray-300"}`}>
                    {a.stage}
                  </div>
                  <div className="flex-1">
                    <p className="text-sm font-semibold capitalize">{a.role_required.replace("_", " ")}</p>
                    {a.approver_name && <p className="text-sm text-gray-700">{a.approver_name} · {a.signed_at ? new Date(a.signed_at).toLocaleString("en-IN") : ""}</p>}
                    {a.remarks && <p className="text-sm text-red-600">Remarks: {a.remarks}</p>}
                  </div>
                  <span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[a.status] ?? "bg-gray-100 text-gray-600"}`}>{a.status}</span>
                </div>
              ))}
            </div>

            {/* Approve button for current user's role */}
            {selectedSheet.status === "submitted" || selectedSheet.status === "under_review" ? (
              <div className="mt-4 p-4 bg-[#E6F4F4] rounded-xl">
                <p className="text-sm font-semibold text-[#0D6E6E] mb-3">Sign & Approve (Stage for your role)</p>
                {!otpSent ? (
                  <button onClick={() => requestOtp.mutate()} disabled={requestOtp.isPending}
                    className="px-4 py-2 bg-[#0D6E6E] text-white rounded-xl text-sm font-semibold hover:bg-[#178F8F]">
                    Send OTP to Email
                  </button>
                ) : (
                  <div className="flex gap-2">
                    <input value={otp} onChange={(e) => setOtp(e.target.value)} placeholder="Enter 6-digit OTP"
                      className="border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] w-40" />
                    <button onClick={() => approveStage.mutate()} disabled={approveStage.isPending || !otp}
                      className="px-4 py-2 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-60 flex items-center gap-1.5">
                      <CheckCircle2 size={14} /> Approve
                    </button>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>
      )}
      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
