"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { ClipboardList, CheckCircle2, XCircle, Loader2, RefreshCw } from "lucide-react";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";

interface MyEnrollment { id: string; offering_id: string; course_number: string; course_title: string; credit_structure: string; section: string | null; status: string; enrolled_at: string; remarks: string | null; }
interface Offering { id: string; course_number: string; course_title: string; credit_structure: string; section: string | null; max_enrollment: number; enrolled_count: number; status: string; faculty_names: string[]; }
interface EnrollmentRow { id: string; student_id: string; student_name: string; student_roll: string; status: string; enrolled_at: string; remarks: string | null; }

const STATUS_COLOR: Record<string, string> = { pending: "bg-amber-100 text-amber-700", approved: "bg-green-100 text-green-700", rejected: "bg-red-100 text-red-700", withdrawn: "bg-gray-100 text-gray-600" };

export default function EnrollmentPage() {
  const role = useRole();
  const qc = useQueryClient();
  const isStudent = role === "student";
  const isFacultyOrAdmin = !isStudent;
  const [selectedOffering, setSelectedOffering] = useState("");
  const [statusFilter, setStatusFilter] = useState("pending");
  const [confirm, setConfirm] = useState<{ action: () => void; title: string; message: string; confirmLabel: string; confirmClassName?: string } | null>(null);

  const { data: myEnrollments = [], isLoading: myLoading } = useQuery<MyEnrollment[]>({
    queryKey: ["my-enrollments"],
    queryFn: async () => (await api.get("/enrollment/my")).data,
    enabled: isStudent,
  });

  const { data: offerings = [] } = useQuery<Offering[]>({
    queryKey: ["ams-offerings-published"],
    queryFn: async () => {
      const data = await api.get("/courses/offerings/all");
      return data.data.filter((o: Offering) => o.status === "published");
    },
    enabled: isStudent,
  });

  const { data: offeringsAll = [] } = useQuery<Offering[]>({
    queryKey: ["ams-offerings-all"],
    queryFn: async () => (await api.get("/courses/offerings/all")).data,
    enabled: isFacultyOrAdmin,
  });

  const { data: enrollments = [] } = useQuery<EnrollmentRow[]>({
    queryKey: ["offering-enrollments", selectedOffering, statusFilter],
    queryFn: async () => (await api.get(`/enrollment/offering/${selectedOffering}?status=${statusFilter}`)).data,
    enabled: isFacultyOrAdmin && !!selectedOffering,
  });

  const enroll = useMutation({
    mutationFn: (offering_id: string) => api.post("/enrollment", { offering_id }),
    onSuccess: () => { toast.success("Enrollment request submitted!"); qc.invalidateQueries({ queryKey: ["my-enrollments"] }); },
    onError: (e: unknown) => toast.error((e as {response?:{data?:{detail?:string}}})?.response?.data?.detail ?? "Failed."),
  });

  const process = useMutation({
    mutationFn: ({ id, status, remarks }: { id: string; status: string; remarks?: string }) =>
      api.patch(`/enrollment/${id}?status=${status}${remarks ? `&remarks=${encodeURIComponent(remarks)}` : ""}`),
    onSuccess: () => { toast.success("Updated."); qc.invalidateQueries({ queryKey: ["offering-enrollments", selectedOffering, statusFilter] }); },
  });

  const bulkApprove = useMutation({
    mutationFn: () => api.post(`/enrollment/offering/${selectedOffering}/bulk-approve`, {
      enrollment_ids: enrollments.map((e) => e.id),
      status: "approved",
    }),
    onSuccess: () => { toast.success("Bulk approved!"); qc.invalidateQueries({ queryKey: ["offering-enrollments", selectedOffering, statusFilter] }); },
  });

  // Student view
  if (isStudent) {
    return (
      <div className="p-6 max-w-5xl mx-auto">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardList size={24} className="text-[#0D6E6E]" />My Enrollment</h1>
        <p className="text-gray-700 text-sm mb-6">View your enrolled courses and apply for new ones</p>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Available courses */}
          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <h2 className="font-bold text-gray-800 mb-4">Available Courses</h2>
            {offerings.length === 0 ? (
              <p className="text-sm text-gray-600 text-center py-8">
                No eligible courses found. If this seems wrong, your academic program may not be configured — contact administration.
              </p>
            ) : (
              <div className="space-y-2">
                {offerings.map((o) => {
                  const already = myEnrollments.some((e) => e.offering_id === o.id);
                  return (
                    <div key={o.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl">
                      <div className="flex-1">
                        <p className="text-base font-bold text-[#0D6E6E]">{o.course_number} <span className="text-gray-800 font-semibold">{o.course_title}</span></p>
                        <p className="text-sm text-gray-700">{o.credit_structure} credits · {o.enrolled_count}/{o.max_enrollment} enrolled</p>
                      </div>
                      <button disabled={already || enroll.isPending}
                        onClick={() => !already && setConfirm({
                          action: () => enroll.mutate(o.id),
                          title: "Confirm Enrollment",
                          message: `Are you sure you want to enroll in ${o.course_number} — ${o.course_title}?`,
                          confirmLabel: "Yes, Enroll",
                          confirmClassName: "bg-[#0D6E6E] hover:bg-[#178F8F] text-white",
                        })}
                        className="px-3 py-1.5 text-sm font-semibold rounded-lg bg-[#0D6E6E] text-white hover:bg-[#178F8F] disabled:opacity-50">
                        {already ? "Enrolled" : "Enroll"}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* My enrollments */}
          <div className="bg-white rounded-2xl border border-gray-200 p-5">
            <h2 className="font-bold text-gray-800 mb-4">My Enrollments</h2>
            {myLoading ? <div className="flex justify-center py-8"><Loader2 className="animate-spin text-gray-600" /></div> : myEnrollments.length === 0 ? (
              <p className="text-sm text-gray-600 text-center py-8">No enrollments yet.</p>
            ) : (
              <div className="space-y-2">
                {myEnrollments.map((e) => (
                  <div key={e.id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl">
                    <div className="flex-1">
                      <p className="text-base font-bold text-gray-800">{e.course_number} {e.course_title}</p>
                      <p className="text-sm text-gray-700">{e.credit_structure} credits</p>
                      {e.remarks && <p className="text-sm text-amber-600 mt-1">{e.remarks}</p>}
                    </div>
                    <span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[e.status] ?? "bg-gray-100"}`}>{e.status}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
    );
  }


  // Faculty/Admin view
  return (
    <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><ClipboardList size={24} className="text-[#0D6E6E]" />Enrollment Management</h1>
      <p className="text-gray-700 text-sm mb-6">Review and approve student enrollment requests</p>

      <div className="flex gap-3 mb-5">
        <select value={selectedOffering} onChange={(e) => setSelectedOffering(e.target.value)}
          className="flex-1 border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          <option value="">Select a course offering…</option>
          {offeringsAll.map((o) => <option key={o.id} value={o.id}>{o.course_number} — {o.course_title} {o.section ? `(${o.section})` : ""}</option>)}
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
          className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
          {["pending","approved","rejected","withdrawn"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        {selectedOffering && statusFilter === "pending" && (
          <button onClick={() => setConfirm({
            action: () => bulkApprove.mutate(),
            title: "Bulk Approve",
            message: `Are you sure you want to approve all ${enrollments.length} pending enrollment request(s)?`,
            confirmLabel: "Yes, Approve All",
            confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
          })} disabled={bulkApprove.isPending || enrollments.length === 0}
            className="px-4 py-2.5 bg-green-600 text-white rounded-xl text-sm font-semibold hover:bg-green-700 disabled:opacity-50">
            {bulkApprove.isPending ? "Approving…" : `Approve All (${enrollments.length})`}
          </button>
        )}
      </div>

      {selectedOffering ? (
        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          {enrollments.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><ClipboardList size={40} className="mx-auto mb-3 opacity-30" /><p>No {statusFilter} enrollments.</p></div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["Student", "Roll No.", "Enrolled", "Status", "Remarks", "Actions"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {enrollments.map((e, i) => (
                  <tr key={e.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 font-medium">{e.student_name}</td>
                    <td className="px-4 py-3 font-mono text-sm">{e.student_roll || "—"}</td>
                    <td className="px-4 py-3 text-gray-700 text-sm">{new Date(e.enrolled_at).toLocaleDateString("en-IN")}</td>
                    <td className="px-4 py-3"><span className={`px-2 py-0.5 rounded-full text-sm font-semibold ${STATUS_COLOR[e.status] ?? ""}`}>{e.status}</span></td>
                    <td className="px-4 py-3 text-gray-700 text-sm max-w-[150px] truncate">{e.remarks ?? "—"}</td>
                    <td className="px-4 py-3">
                      {e.status === "pending" && (
                        <div className="flex gap-2">
                          <button onClick={() => setConfirm({
                            action: () => process.mutate({ id: e.id, status: "approved" }),
                            title: "Approve Enrollment",
                            message: `Are you sure you want to approve ${e.student_name}'s enrollment request?`,
                            confirmLabel: "Yes, Approve",
                            confirmClassName: "bg-green-600 hover:bg-green-700 text-white",
                          })} className="p-1.5 text-green-600 hover:bg-green-50 rounded-lg"><CheckCircle2 size={16} /></button>
                          <button onClick={() => setConfirm({
                            action: () => process.mutate({ id: e.id, status: "rejected", remarks: "Rejected by faculty." }),
                            title: "Reject Enrollment",
                            message: `Are you sure you want to reject ${e.student_name}'s enrollment request?`,
                            confirmLabel: "Yes, Reject",
                            confirmClassName: "bg-red-600 hover:bg-red-700 text-white",
                          })} className="p-1.5 text-red-500 hover:bg-red-50 rounded-lg"><XCircle size={16} /></button>
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className="text-center py-16 text-gray-600 bg-white rounded-2xl border border-gray-200">
          <ClipboardList size={40} className="mx-auto mb-3 opacity-30" />
          <p>Select a course offering to manage enrollments.</p>
        </div>
      )}
      {confirm && <ConfirmDialog title={confirm.title} message={confirm.message} confirmLabel={confirm.confirmLabel} confirmClassName={confirm.confirmClassName} onConfirm={() => { confirm.action(); setConfirm(null); }} onCancel={() => setConfirm(null)} />}
    </div>
  );
}
