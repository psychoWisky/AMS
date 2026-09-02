"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole } from "@/stores/auth.store";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { ClipboardList, Loader2, CheckCircle2, XCircle, Eye, X } from "lucide-react";

interface Calendar { id: string; name: string; academic_year: string; }
interface Semester { id: string; calendar_id: string; name: string; }
interface Offering { id: string; course_number: string; course_title: string; semester_id: string; }
interface EnrollmentItem {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  offering_id: string; course_number: string; course_title: string; registration_id: string | null;
  status: string; status_label: string; remarks: string | null; enrolled_at: string;
}
interface Registration {
  id: string; student_id: string; student_name: string | null; student_roll: string | null;
  program_name: string | null; semester_id: string; stage: string; status_label: string;
  revert_remark: string | null; submitted_at: string; items: EnrollmentItem[];
}

const STAGE_STYLE: Record<string, string> = {
  teacher_pending: "bg-amber-100 text-amber-700",
  major_advisor_pending: "bg-blue-100 text-blue-700",
  hod_pending: "bg-purple-100 text-purple-700",
  hod_approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
};
const ITEM_STATUS_STYLE: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700",
  approved: "bg-green-100 text-green-700",
  reverted: "bg-red-100 text-red-700",
  withdrawn: "bg-gray-100 text-gray-600",
};

function RemarkPrompt({ title, onSubmit, onCancel }: { title: string; onSubmit: (remark: string) => void; onCancel: () => void }) {
  const [text, setText] = useState("");
  return (
    <div className="fixed inset-0 bg-black/40 z-[70] flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm p-6">
        <h3 className="text-lg font-bold mb-2">{title}</h3>
        <p className="text-sm text-gray-600 mb-3">A reason is required and will be shown to the student.</p>
        <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} placeholder="Reason…"
          className="w-full border border-gray-300 rounded-xl px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] resize-none" />
        <div className="flex gap-3 mt-4">
          <button onClick={onCancel} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-sm font-medium">Cancel</button>
          <button onClick={() => { if (!text.trim()) { toast.error("A remark is required."); return; } onSubmit(text.trim()); }}
            className="flex-1 py-2.5 bg-red-600 text-white rounded-xl text-sm font-bold hover:bg-red-700">Submit</button>
        </div>
      </div>
    </div>
  );
}

function RegistrationDetailModal({ registrationId, onClose }: { registrationId: string; onClose: () => void }) {
  const { data: reg } = useQuery<Registration>({
    queryKey: ["ams-registration-detail", registrationId],
    queryFn: async () => (await api.get(`/enrollment/registrations/${registrationId}`)).data,
  });
  return (
    <div className="fixed inset-0 bg-black/40 z-[60] flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[85vh] overflow-y-auto">
        <div className="flex items-start justify-between mb-4">
          <div>
            <h3 className="text-lg font-bold text-gray-900">{reg?.student_name ?? "Loading…"}</h3>
            <p className="text-sm text-gray-600">{reg?.student_roll} — {reg?.program_name ?? "—"}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-700"><X size={20} /></button>
        </div>
        {!reg ? (
          <div className="flex justify-center py-10"><Loader2 className="animate-spin text-gray-600" /></div>
        ) : (
          <>
            <span className={`inline-flex px-2.5 py-1 rounded-full text-xs font-semibold mb-3 ${STAGE_STYLE[reg.stage] ?? "bg-gray-100"}`}>{reg.status_label}</span>
            <div className="space-y-2">
              {reg.items.map((it) => (
                <div key={it.id} className="flex items-center justify-between p-2.5 bg-gray-50 rounded-lg text-sm">
                  <div>
                    <span className="font-mono font-bold text-[#0D6E6E]">{it.course_number}</span> <span>{it.course_title}</span>
                    {it.remarks && <p className="text-xs text-red-600 mt-0.5">{it.remarks}</p>}
                  </div>
                  <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${ITEM_STATUS_STYLE[it.status] ?? "bg-gray-100"}`}>{it.status_label}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function CourseRequestPage() {
  const role = useRole();
  const qc = useQueryClient();
  const isFacultyLike = role === "faculty" || role === "research_supervisor" || role === "hod" || role === "super_admin" || role === "academic_admin";
  const isHodLike = role === "hod" || role === "super_admin" || role === "academic_admin";

  const [calendarId, setCalendarId] = useState("");
  const [semesterId, setSemesterId] = useState("");
  const [offeringId, setOfferingId] = useState("");
  const [detailId, setDetailId] = useState<string | null>(null);
  const [remarkFor, setRemarkFor] = useState<{ kind: "item" | "ma" | "hod"; id: string } | null>(null);
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string } | null>(null);

  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-semesters-for-request", calendarId],
    queryFn: async () => (await api.get(`/academic/calendars/${calendarId}/semesters`)).data,
    enabled: !!calendarId,
  });

  // Course dropdown restricted server-side to this faculty member's own assigned offerings.
  const { data: myOfferings = [] } = useQuery<Offering[]>({
    queryKey: ["ams-my-offerings", calendarId, semesterId],
    queryFn: async () => (await api.get("/courses/offerings/all", { params: { mine: true, calendar_id: calendarId || undefined, semester_id: semesterId || undefined } })).data,
    enabled: role === "faculty" || role === "research_supervisor",
  });

  const { data: students = [], isLoading: studentsLoading } = useQuery<EnrollmentItem[]>({
    queryKey: ["ams-offering-students", offeringId],
    queryFn: async () => (await api.get(`/enrollment/offering/${offeringId}`)).data,
    enabled: !!offeringId,
  });

  const { data: maQueue = [] } = useQuery<Registration[]>({
    queryKey: ["ams-ma-queue"],
    queryFn: async () => (await api.get("/enrollment/registrations", { params: { stage: "major_advisor_pending" } })).data,
    enabled: role === "faculty" || role === "research_supervisor",
  });

  const { data: hodQueue = [] } = useQuery<Registration[]>({
    queryKey: ["ams-hod-queue"],
    queryFn: async () => (await api.get("/enrollment/registrations", { params: { stage: "hod_pending" } })).data,
    enabled: isHodLike,
  });

  function invalidateAll() {
    qc.invalidateQueries({ queryKey: ["ams-offering-students"] });
    qc.invalidateQueries({ queryKey: ["ams-ma-queue"] });
    qc.invalidateQueries({ queryKey: ["ams-hod-queue"] });
  }

  const processItem = useMutation({
    mutationFn: ({ id, status, remarks }: { id: string; status: string; remarks?: string }) =>
      api.patch(`/enrollment/${id}`, null, { params: { status, remarks } }),
    onSuccess: () => { toast.success("Updated."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const maDecision = useMutation({
    mutationFn: ({ id, approved, remark }: { id: string; approved: boolean; remark?: string }) =>
      api.patch(`/enrollment/registrations/${id}/major-advisor-approval`, { approved, remark }),
    onSuccess: () => { toast.success("Recorded."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  const hodDecision = useMutation({
    mutationFn: ({ id, approved, remark }: { id: string; approved: boolean; remark?: string }) =>
      api.patch(`/enrollment/registrations/${id}/hod-approval`, { approved, remark }),
    onSuccess: () => { toast.success("Recorded."); invalidateAll(); },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed."),
  });

  if (!isFacultyLike) {
    return <div className="p-6 max-w-3xl mx-auto text-gray-600">Course Request is available to Faculty, HOD, and Admin roles.</div>;
  }

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-8">
      {(role === "faculty" || role === "research_supervisor") && (
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Course Request</h1>
          <p className="text-gray-700 text-sm mb-4">Review and approve/revert student course selections for courses assigned to you.</p>

          <div className="flex gap-3 mb-4">
            <select value={calendarId} onChange={(e) => { setCalendarId(e.target.value); setSemesterId(""); setOfferingId(""); }}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
              <option value="">Academic Year…</option>
              {calendars.map((c) => <option key={c.id} value={c.id}>{c.academic_year}</option>)}
            </select>
            <select value={semesterId} onChange={(e) => { setSemesterId(e.target.value); setOfferingId(""); }} disabled={!calendarId}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none disabled:opacity-50">
              <option value="">Semester…</option>
              {semesters.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <select value={offeringId} onChange={(e) => setOfferingId(e.target.value)}
              className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none min-w-[220px]">
              <option value="">Course…</option>
              {myOfferings.map((o) => <option key={o.id} value={o.id}>{o.course_number} — {o.course_title}</option>)}
            </select>
          </div>

          <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
            {!offeringId ? (
              <div className="text-center py-12 text-gray-500">Select a course to see student registrations.</div>
            ) : studentsLoading ? (
              <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
            ) : students.length === 0 ? (
              <div className="text-center py-16 text-gray-600"><ClipboardList size={40} className="mx-auto mb-3 opacity-30" /><p>No student registrations for this course yet.</p></div>
            ) : (
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b border-gray-200">
                  <tr>{["Sl No", "Student Name", "Roll No", "Status", "Action"].map((h) => (
                    <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                  ))}</tr>
                </thead>
                <tbody>
                  {students.map((s, i) => (
                    <tr key={s.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                      <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                      <td className="px-4 py-3 font-medium text-gray-900">{s.student_name ?? "—"}</td>
                      <td className="px-4 py-3 font-mono text-sm">{s.student_roll ?? "—"}</td>
                      <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${ITEM_STATUS_STYLE[s.status] ?? "bg-gray-100"}`}>{s.status_label}</span></td>
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          {s.registration_id && (
                            <button onClick={() => setDetailId(s.registration_id)} className="flex items-center gap-1 text-xs font-semibold text-[#0D6E6E] hover:underline"><Eye size={12} /> View Details</button>
                          )}
                          {s.status === "pending" && (
                            <>
                              <button onClick={() => setConfirm({
                                action: () => processItem.mutate({ id: s.id, status: "approved" }),
                                title: "Approve", message: `Approve ${s.student_name}'s selection of ${s.course_number}?`,
                              })} className="p-1.5 text-green-600 hover:bg-green-50 rounded-lg"><CheckCircle2 size={16} /></button>
                              <button onClick={() => setRemarkFor({ kind: "item", id: s.id })} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg"><XCircle size={16} /></button>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      {(role === "faculty" || role === "research_supervisor") && maQueue.length > 0 && (
        <div>
          <h2 className="text-xl font-bold text-gray-900 mb-1">Registrations Awaiting Your Approval as Major Advisor</h2>
          <p className="text-gray-700 text-sm mb-4">All selected courses have cleared Course Teacher approval.</p>
          <div className="space-y-2">
            {maQueue.map((r) => (
              <div key={r.id} className="bg-white rounded-2xl border border-gray-200 p-4 flex items-center justify-between">
                <div>
                  <p className="font-semibold text-gray-900">{r.student_name} <span className="text-gray-500 font-mono text-sm">({r.student_roll})</span></p>
                  <p className="text-sm text-gray-600">{r.items.map((it) => it.course_number).join(", ")}</p>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => setDetailId(r.id)} className="text-sm font-semibold text-[#0D6E6E] hover:underline mr-2">View</button>
                  <button onClick={() => setConfirm({ action: () => maDecision.mutate({ id: r.id, approved: true }), title: "Approve Registration", message: `Approve ${r.student_name}'s registration as Major Advisor?` })}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-sm font-semibold rounded-lg hover:bg-green-700"><CheckCircle2 size={14} /> Approve</button>
                  <button onClick={() => setRemarkFor({ kind: "ma", id: r.id })}
                    className="flex items-center gap-1.5 px-3 py-1.5 border border-red-300 text-red-700 text-sm font-semibold rounded-lg hover:bg-red-50"><XCircle size={14} /> Revert</button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {isHodLike && (
        <div>
          <h2 className="text-xl font-bold text-gray-900 mb-1">Registrations Awaiting HOD Approval</h2>
          <p className="text-gray-700 text-sm mb-4">Approved by the student&apos;s Major Advisor — final review for this department.</p>
          {hodQueue.length === 0 ? (
            <div className="bg-white rounded-2xl border border-gray-200 text-center py-10 text-gray-500">No registrations awaiting HOD approval.</div>
          ) : (
            <div className="space-y-2">
              {hodQueue.map((r) => (
                <div key={r.id} className="bg-white rounded-2xl border border-gray-200 p-4 flex items-center justify-between">
                  <div>
                    <p className="font-semibold text-gray-900">{r.student_name} <span className="text-gray-500 font-mono text-sm">({r.student_roll})</span></p>
                    <p className="text-sm text-gray-600">{r.program_name} — {r.items.map((it) => it.course_number).join(", ")}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <button onClick={() => setDetailId(r.id)} className="text-sm font-semibold text-[#0D6E6E] hover:underline mr-2">View</button>
                    <button onClick={() => setConfirm({ action: () => hodDecision.mutate({ id: r.id, approved: true }), title: "Approve Registration", message: `Approve ${r.student_name}'s registration as HOD? This completes the registration for this demo phase.` })}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white text-sm font-semibold rounded-lg hover:bg-green-700"><CheckCircle2 size={14} /> Approve</button>
                    <button onClick={() => setRemarkFor({ kind: "hod", id: r.id })}
                      className="flex items-center gap-1.5 px-3 py-1.5 border border-red-300 text-red-700 text-sm font-semibold rounded-lg hover:bg-red-50"><XCircle size={14} /> Revert</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {detailId && <RegistrationDetailModal registrationId={detailId} onClose={() => setDetailId(null)} />}

      {remarkFor && (
        <RemarkPrompt
          title="Revert — Reason Required"
          onCancel={() => setRemarkFor(null)}
          onSubmit={(remark) => {
            if (remarkFor.kind === "item") processItem.mutate({ id: remarkFor.id, status: "reverted", remarks: remark });
            if (remarkFor.kind === "ma") maDecision.mutate({ id: remarkFor.id, approved: false, remark });
            if (remarkFor.kind === "hod") hodDecision.mutate({ id: remarkFor.id, approved: false, remark });
            setRemarkFor(null);
          }}
        />
      )}

      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel="Yes, Approve" confirmClassName="bg-green-600 hover:bg-green-700 text-white"
        onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
