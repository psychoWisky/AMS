"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { ClipboardCheck, Search, Eye, X, CheckCircle, XCircle, Clock, AlertCircle, ExternalLink } from "lucide-react";

interface AppSummary {
  id: string; application_number: string; status: string;
  first_name: string; last_name: string; personal_email: string;
  mobile: string; program_id: string | null; academic_year: string;
  category: string; submitted_at: string;
}
interface AppDetail extends AppSummary {
  middle_name: string | null; date_of_birth: string; gender: string;
  nationality: string; religion: string | null; mother_tongue: string | null;
  aadhar_number: string; alt_mobile: string | null;
  current_address: string; current_city: string; current_state: string; current_pincode: string;
  permanent_address: string; permanent_city: string; permanent_state: string; permanent_pincode: string;
  father_name: string; father_occupation: string | null; father_mobile: string | null; father_income: string | null;
  mother_name: string; mother_occupation: string | null; mother_mobile: string | null;
  tenth_board: string; tenth_school: string; tenth_year: number; tenth_percentage: number; tenth_roll: string | null;
  twelfth_board: string; twelfth_school: string; twelfth_year: number; twelfth_percentage: number;
  twelfth_roll: string | null; twelfth_stream: string; entrance_exam: string | null; entrance_score: number | null;
  documents: Record<string, string | null>;
  remarks: string | null; reviewed_at: string | null;
}

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-amber-100 text-amber-700",
  under_review: "bg-blue-100 text-blue-700",
  accepted: "bg-green-100 text-green-700",
  rejected: "bg-red-100 text-red-700",
  waitlisted: "bg-purple-100 text-purple-700",
};
const STATUS_LABELS: Record<string, string> = {
  pending: "Pending", under_review: "Under Review",
  accepted: "Accepted", rejected: "Rejected", waitlisted: "Waitlisted",
};
const STATUS_ICONS: Record<string, React.ReactNode> = {
  pending: <Clock size={12} />, under_review: <AlertCircle size={12} />,
  accepted: <CheckCircle size={12} />, rejected: <XCircle size={12} />, waitlisted: <Clock size={12} />,
};

function InfoRow({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div className="py-2 grid grid-cols-5 gap-2 border-b border-gray-100 last:border-0">
      <dt className="col-span-2 text-xs font-semibold text-gray-500 uppercase tracking-wide">{label}</dt>
      <dd className="col-span-3 text-sm text-gray-800">{value ?? "—"}</dd>
    </div>
  );
}
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-5">
      <h4 className="text-xs font-bold uppercase tracking-wider text-teal-700 bg-teal-50 px-3 py-1.5 rounded-lg mb-2">{title}</h4>
      <dl>{children}</dl>
    </div>
  );
}

export default function AdmissionsPage() {
  const qc = useQueryClient();
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<AppDetail | null>(null);
  const [newStatus, setNewStatus] = useState("");
  const [remarks, setRemarks] = useState("");

  const { data: apps = [], isLoading } = useQuery<AppSummary[]>({
    queryKey: ["ams-admissions", statusFilter],
    queryFn: async () => (await api.get(`/admission/applications${statusFilter ? `?status=${statusFilter}` : ""}`)).data,
  });

  const updateStatus = useMutation({
    mutationFn: ({ id, status, remarks }: { id: string; status: string; remarks: string }) =>
      api.patch(`/admission/applications/${id}/status`, { status, remarks }),
    onSuccess: (_, { status }) => {
      toast.success(`Application marked as ${STATUS_LABELS[status] ?? status}`);
      qc.invalidateQueries({ queryKey: ["ams-admissions"] });
      if (selected) setSelected((s) => s ? { ...s, status, remarks } : null);
      setNewStatus(""); setRemarks("");
    },
    onError: () => toast.error("Failed to update status."),
  });

  async function openDetail(id: string) {
    try {
      const res = await api.get(`/admission/applications/${id}`);
      setSelected(res.data);
      setNewStatus(res.data.status);
      setRemarks(res.data.remarks ?? "");
    } catch { toast.error("Failed to load application."); }
  }

  const filtered = apps.filter((a) => {
    const q = search.toLowerCase();
    return !q || `${a.first_name} ${a.last_name}`.toLowerCase().includes(q)
      || a.application_number.toLowerCase().includes(q)
      || a.personal_email.toLowerCase().includes(q)
      || a.mobile.includes(q);
  });

  const counts = apps.reduce((acc, a) => { acc[a.status] = (acc[a.status] ?? 0) + 1; return acc; }, {} as Record<string, number>);

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2">
            <ClipboardCheck size={24} className="text-teal-700" /> Admission Applications
          </h1>
          <p className="text-gray-500 text-sm mt-1">Review and process student admission requests</p>
        </div>
        <a href="/apply" target="_blank" rel="noopener noreferrer"
          className="flex items-center gap-2 text-sm font-semibold text-teal-700 border border-teal-300 rounded-xl px-4 py-2 hover:bg-teal-50">
          <ExternalLink size={14} /> Public Apply Page
        </a>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-5 gap-3 mb-6">
        {[
          { key: "", label: "Total", color: "bg-gray-100 text-gray-700" },
          { key: "pending", label: "Pending", color: "bg-amber-100 text-amber-700" },
          { key: "under_review", label: "Under Review", color: "bg-blue-100 text-blue-700" },
          { key: "accepted", label: "Accepted", color: "bg-green-100 text-green-700" },
          { key: "rejected", label: "Rejected", color: "bg-red-100 text-red-700" },
        ].map(({ key, label, color }) => (
          <button key={key} onClick={() => setStatusFilter(key)}
            className={`rounded-xl p-4 text-left transition-all border-2 ${statusFilter === key ? "border-teal-600 shadow-sm" : "border-transparent"} ${color}`}>
            <p className="text-2xl font-bold">{key === "" ? apps.length : (counts[key] ?? 0)}</p>
            <p className="text-sm font-semibold mt-0.5">{label}</p>
          </button>
        ))}
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
        <input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name, application no., email, or mobile…"
          className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-teal-600" />
      </div>

      {/* Table */}
      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="flex justify-center py-20 text-gray-400">Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-20 text-gray-400">
            <ClipboardCheck size={40} className="mx-auto mb-3 opacity-30" />
            <p className="font-medium">No applications found</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                {["App. No.", "Applicant", "Email / Mobile", "Year & Category", "Submitted", "Status", "Action"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.map((a, i) => (
                <tr key={a.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 font-mono text-xs font-bold text-teal-700">{a.application_number}</td>
                  <td className="px-4 py-3 font-semibold text-gray-900">{a.first_name} {a.last_name}</td>
                  <td className="px-4 py-3">
                    <p className="text-gray-700">{a.personal_email}</p>
                    <p className="text-gray-400 text-xs">{a.mobile}</p>
                  </td>
                  <td className="px-4 py-3">
                    <p className="text-gray-700">{a.academic_year}</p>
                    <p className="text-gray-400 text-xs">{a.category}</p>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs whitespace-nowrap">
                    {new Date(a.submitted_at).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" })}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-flex items-center gap-1 px-2.5 py-1 rounded-full text-xs font-semibold ${STATUS_STYLES[a.status] ?? "bg-gray-100 text-gray-600"}`}>
                      {STATUS_ICONS[a.status]} {STATUS_LABELS[a.status] ?? a.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <button onClick={() => openDetail(a.id)}
                      className="flex items-center gap-1 px-3 py-1.5 text-xs font-semibold border border-teal-300 text-teal-700 rounded-lg hover:bg-teal-50">
                      <Eye size={12} /> Review
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Detail Drawer */}
      {selected && (
        <div className="fixed inset-0 z-50 flex">
          <div className="flex-1 bg-black/40 backdrop-blur-sm" onClick={() => setSelected(null)} />
          <div className="w-[600px] bg-white shadow-2xl overflow-y-auto flex flex-col">
            {/* Drawer header */}
            <div className="bg-teal-700 px-6 py-5 flex items-start justify-between flex-shrink-0">
              <div>
                <p className="text-teal-200 text-xs font-mono">{selected.application_number}</p>
                <h2 className="text-white font-bold text-xl mt-0.5">{selected.first_name} {selected.middle_name ? `${selected.middle_name} ` : ""}{selected.last_name}</h2>
                <span className={`inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-xs font-semibold mt-2 ${STATUS_STYLES[selected.status]}`}>
                  {STATUS_ICONS[selected.status]} {STATUS_LABELS[selected.status]}
                </span>
              </div>
              <button onClick={() => setSelected(null)} className="text-teal-200 hover:text-white mt-1"><X size={20} /></button>
            </div>

            <div className="flex-1 p-6 space-y-1">
              <Section title="Program Details">
                <InfoRow label="Academic Year" value={selected.academic_year} />
                <InfoRow label="Category" value={selected.category} />
              </Section>

              <Section title="Personal Information">
                <InfoRow label="Date of Birth" value={selected.date_of_birth} />
                <InfoRow label="Gender" value={selected.gender} />
                <InfoRow label="Nationality" value={selected.nationality} />
                <InfoRow label="Religion" value={selected.religion} />
                <InfoRow label="Mother Tongue" value={selected.mother_tongue} />
                <InfoRow label="Aadhar Number" value={selected.aadhar_number} />
                <InfoRow label="Email" value={selected.personal_email} />
                <InfoRow label="Mobile" value={selected.mobile} />
                <InfoRow label="Alt. Mobile" value={selected.alt_mobile} />
              </Section>

              <Section title="Current Address">
                <InfoRow label="Address" value={selected.current_address} />
                <InfoRow label="City" value={selected.current_city} />
                <InfoRow label="State" value={selected.current_state} />
                <InfoRow label="Pincode" value={selected.current_pincode} />
              </Section>

              <Section title="Permanent Address">
                <InfoRow label="Address" value={selected.permanent_address} />
                <InfoRow label="City" value={selected.permanent_city} />
                <InfoRow label="State" value={selected.permanent_state} />
                <InfoRow label="Pincode" value={selected.permanent_pincode} />
              </Section>

              <Section title="Guardian / Family">
                <InfoRow label="Father's Name" value={selected.father_name} />
                <InfoRow label="Father's Occupation" value={selected.father_occupation} />
                <InfoRow label="Father's Mobile" value={selected.father_mobile} />
                <InfoRow label="Annual Income" value={selected.father_income} />
                <InfoRow label="Mother's Name" value={selected.mother_name} />
                <InfoRow label="Mother's Occupation" value={selected.mother_occupation} />
                <InfoRow label="Mother's Mobile" value={selected.mother_mobile} />
              </Section>

              <Section title="Academic — 10th">
                <InfoRow label="Board" value={selected.tenth_board} />
                <InfoRow label="School" value={selected.tenth_school} />
                <InfoRow label="Year" value={selected.tenth_year} />
                <InfoRow label="Percentage" value={selected.tenth_percentage ? `${selected.tenth_percentage}%` : null} />
                <InfoRow label="Roll No." value={selected.tenth_roll} />
              </Section>

              <Section title="Academic — 12th">
                <InfoRow label="Board" value={selected.twelfth_board} />
                <InfoRow label="School / College" value={selected.twelfth_school} />
                <InfoRow label="Year" value={selected.twelfth_year} />
                <InfoRow label="Percentage" value={selected.twelfth_percentage ? `${selected.twelfth_percentage}%` : null} />
                <InfoRow label="Stream" value={selected.twelfth_stream} />
                <InfoRow label="Roll No." value={selected.twelfth_roll} />
                <InfoRow label="Entrance Exam" value={selected.entrance_exam} />
                <InfoRow label="Entrance Score" value={selected.entrance_score} />
              </Section>

              <Section title="Uploaded Documents">
                <div className="grid grid-cols-2 gap-2 mt-1">
                  {Object.entries({
                    photo: "Photograph",
                    signature: "Signature",
                    tenth_marksheet: "10th Marksheet",
                    twelfth_marksheet: "12th Marksheet",
                    aadhar: "Aadhar Card",
                    category_cert: "Category Certificate",
                    transfer_cert: "Transfer Certificate",
                  }).map(([key, label]) => {
                    const url = selected.documents[key];
                    return (
                      <div key={key} className="flex items-center gap-2 p-2 bg-gray-50 rounded-lg">
                        <span className={`w-2 h-2 rounded-full flex-shrink-0 ${url ? "bg-green-500" : "bg-gray-300"}`} />
                        <span className="text-xs text-gray-600 flex-1">{label}</span>
                        {url && (
                          <a href={`http://localhost:8001${url}`} target="_blank" rel="noopener noreferrer"
                            className="text-xs text-teal-700 font-semibold hover:underline flex-shrink-0">View</a>
                        )}
                      </div>
                    );
                  })}
                </div>
              </Section>

              {/* Status Update */}
              <div className="bg-gray-50 rounded-xl p-4 border border-gray-200 mt-4">
                <h4 className="text-xs font-bold uppercase tracking-wider text-gray-500 mb-3">Update Status</h4>
                <select value={newStatus} onChange={(e) => setNewStatus(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-teal-600">
                  <option value="pending">Pending</option>
                  <option value="under_review">Under Review</option>
                  <option value="accepted">Accepted</option>
                  <option value="rejected">Rejected</option>
                  <option value="waitlisted">Waitlisted</option>
                </select>
                <textarea value={remarks} onChange={(e) => setRemarks(e.target.value)}
                  placeholder="Remarks / reason (optional but recommended when rejecting or waitlisting)"
                  rows={3} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm mb-3 focus:outline-none focus:ring-2 focus:ring-teal-600 resize-none" />
                <button
                  onClick={() => updateStatus.mutate({ id: selected.id, status: newStatus, remarks })}
                  disabled={updateStatus.isPending || newStatus === selected.status}
                  className="w-full py-2.5 bg-teal-700 text-white rounded-xl text-sm font-bold hover:bg-teal-800 disabled:opacity-50 transition-colors">
                  {updateStatus.isPending ? "Saving…" : "Save Status"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
