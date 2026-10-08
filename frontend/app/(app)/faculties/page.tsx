"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { UserCog, Plus, Search, Loader2, Mail, Upload, Pencil } from "lucide-react";
import { UserBulkUploadModal } from "@/components/ui/user-bulk-upload-modal";

interface Faculty {
  id: string; email: string; full_name: string; title: string | null;
  designation: string | null; mobile: string | null;
  first_name?: string | null; middle_name?: string | null; last_name?: string | null;
  employee_id?: string | null; date_of_birth?: string | null; gender?: string | null;
  blood_group?: string | null; father_name?: string | null; address?: string | null;
  college_id?: string | null;
}
interface DesignationOpt { id: string; name: string; is_active: boolean; }
interface CollegeOpt { id: string; name: string; }

const TITLES = ["Dr.", "Mr", "Mrs", "Miss"];
const GENDERS = ["Male", "Female", "Other"];
const BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"];

const EMPTY_FORM = {
  title: "Dr.", first_name: "", middle_name: "", last_name: "",
  date_of_birth: "", gender: "", email: "", mobile: "",
  designation: "", address: "",
};

// HOD: Edit Faculty (own department only) — same field set Super Admin
// edits via the Users page, minus role/department_id/is_active (an HOD can
// never reassign a faculty's role or department, or deactivate them) and
// minus abc_id (student-only, student-viewable-only). Posts to
// PATCH /auth/faculty/{id}, a separate endpoint from Super Admin's own
// PATCH /auth/users/{id} — the backend independently enforces the
// own-department-only, Faculty-only scope regardless of this form's state.
const EMPTY_EDIT_FORM = {
  email: "", first_name: "", middle_name: "", last_name: "", designation: "", mobile: "",
  title: "", employee_id: "", date_of_birth: "", gender: "", blood_group: "", father_name: "",
  address: "", college_id: "",
};

export default function FacultiesPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [showBulkUpload, setShowBulkUpload] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editingFaculty, setEditingFaculty] = useState<Faculty | null>(null);
  const [editForm, setEditForm] = useState(EMPTY_EDIT_FORM);

  const { data: faculty = [], isLoading } = useQuery<Faculty[]>({
    queryKey: ["ams-hod-faculty"],
    queryFn: async () => (await api.get("/auth/users", { params: { role: "faculty" } })).data,
  });

  const { data: colleges = [] } = useQuery<CollegeOpt[]>({
    queryKey: ["ams-colleges-active"],
    queryFn: async () => (await api.get("/admin/colleges")).data,
    enabled: !!editingFaculty,
  });

  // Designation-management task — Super Admin-managed master data replaces the
  // previously hardcoded 3-value list; only active designations are offered here.
  // The HOD Add Faculty form is selection-only — no create/edit/delete controls.
  const { data: designations = [], isLoading: designationsLoading, isError: designationsError } = useQuery<DesignationOpt[]>({
    queryKey: ["ams-active-designations"],
    queryFn: async () => (await api.get("/admin/designations", { params: { active: true } })).data,
    enabled: showCreate || !!editingFaculty,
  });

  const createFaculty = useMutation({
    mutationFn: () => api.post("/auth/faculty", form),
    onSuccess: () => {
      // Bulk Upload SMTP timeout fix — credential email is now durably
      // queued (never sent synchronously in the request), so success here
      // always means "queued", not "sent"; `email_sent` no longer exists.
      toast.success("Faculty account created. Credential email queued for delivery.");
      qc.invalidateQueries({ queryKey: ["ams-hod-faculty"] });
      setShowCreate(false); setForm(EMPTY_FORM);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create faculty."),
  });

  const updateFaculty = useMutation({
    mutationFn: () => api.patch(`/auth/faculty/${editingFaculty?.id}`, {
      email: editForm.email,
      first_name: editForm.first_name,
      middle_name: editForm.middle_name || null,
      last_name: editForm.last_name,
      designation: editForm.designation || null,
      mobile: editForm.mobile || null,
      title: editForm.title || null,
      employee_id: editForm.employee_id || null,
      date_of_birth: editForm.date_of_birth || null,
      gender: editForm.gender || null,
      blood_group: editForm.blood_group || null,
      father_name: editForm.father_name || null,
      address: editForm.address || null,
      college_id: editForm.college_id || null,
    }),
    onSuccess: () => {
      toast.success("Faculty details updated.");
      qc.invalidateQueries({ queryKey: ["ams-hod-faculty"] });
      closeEdit();
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to update faculty."),
  });

  const openEdit = (f: Faculty) => {
    setEditForm({
      email: f.email, first_name: f.first_name ?? "", middle_name: f.middle_name ?? "", last_name: f.last_name ?? "",
      designation: f.designation ?? "", mobile: f.mobile ?? "", title: f.title ?? "", employee_id: f.employee_id ?? "",
      date_of_birth: f.date_of_birth ?? "", gender: f.gender ?? "", blood_group: f.blood_group ?? "",
      father_name: f.father_name ?? "", address: f.address ?? "", college_id: f.college_id ?? "",
    });
    setEditingFaculty(f);
  };
  const closeEdit = () => { setEditingFaculty(null); setEditForm(EMPTY_EDIT_FORM); };

  const filtered = faculty.filter((f) =>
    !search || f.full_name.toLowerCase().includes(search.toLowerCase()) || f.email.toLowerCase().includes(search.toLowerCase())
  );

  function submit() {
    if (!form.first_name || !form.last_name || !form.email || !form.date_of_birth || !form.gender || !form.mobile || !form.address || !form.designation) {
      toast.error("Please fill in all required fields.");
      return;
    }
    createFaculty.mutate();
  }

  return (
    <div className="p-6 w-full">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><UserCog size={24} className="text-[#0D6E6E]" />Faculties</h1>
          <p className="text-gray-700 text-base mt-1">Faculty members in your department</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Bulk Faculty/User Excel Upload task (this revision) — HOD-scoped;
              the backend independently enforces this endpoint to HOD only
              and forces role=FACULTY/department=this HOD's own department
              regardless of the Excel file's contents. */}
          <button onClick={() => setShowBulkUpload(true)}
            className="flex items-center gap-2 px-4 py-2.5 border border-[#0D6E6E] text-[#0D6E6E] rounded-xl font-semibold text-base hover:bg-[#E6F4F4]">
            <Upload size={16} /> Bulk Upload
          </button>
          <button onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
            <Plus size={16} /> Add Faculty
          </button>
        </div>
      </div>

      <div className="relative max-w-xs mb-4">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search faculty…"
          className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-auto max-h-[65vh]">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><UserCog size={40} className="mx-auto mb-3 opacity-30" /><p>No faculty found in your department yet.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200 sticky top-0 z-10">
              <tr>{["Sl No", "Name", "Email", "Designation", "Mobile", "Action"].map((h) => (
                <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
              ))}</tr>
            </thead>
            <tbody>
              {filtered.map((f, i) => (
                <tr key={f.id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                  <td className="px-4 py-3 text-gray-600">{i + 1}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{f.title ? `${f.title} ` : ""}{f.full_name}</td>
                  <td className="px-4 py-3 text-gray-700">{f.email}</td>
                  <td className="px-4 py-3 text-gray-700">{f.designation ?? "—"}</td>
                  <td className="px-4 py-3 text-gray-700">{f.mobile ?? "—"}</td>
                  <td className="px-4 py-3">
                    <button onClick={() => openEdit(f)} title="Edit faculty" className="p-1.5 text-gray-600 hover:bg-gray-100 rounded-lg"><Pencil size={16} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showCreate && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Add Faculty</h3>
            <p className="text-sm text-gray-600 mb-4">
              The faculty account will be created in your department. An email with login details will be sent to the AVFU email address.
            </p>
            <div className="space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Sur Name *</label>
                  <select value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-2 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    {TITLES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className="block text-base font-semibold text-gray-700 mb-1">First Name *</label>
                  <input value={form.first_name} onChange={(e) => setForm((f) => ({ ...f, first_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Middle Name</label>
                  <input value={form.middle_name} onChange={(e) => setForm((f) => ({ ...f, middle_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Last Name *</label>
                  <input value={form.last_name} onChange={(e) => setForm((f) => ({ ...f, last_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">DOB *</label>
                  <input type="date" value={form.date_of_birth} onChange={(e) => setForm((f) => ({ ...f, date_of_birth: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Gender *</label>
                  <select value={form.gender} onChange={(e) => setForm((f) => ({ ...f, gender: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">Select…</option>
                    <option value="male">Male</option>
                    <option value="female">Female</option>
                    <option value="other">Other</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Email (AVFU) *</label>
                <input type="email" placeholder="name@avfu.ac.in" value={form.email} onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Mobile No. *</label>
                  <input value={form.mobile} onChange={(e) => setForm((f) => ({ ...f, mobile: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Designation *</label>
                  <select value={form.designation} onChange={(e) => setForm((f) => ({ ...f, designation: e.target.value }))}
                    disabled={designationsLoading}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50">
                    <option value="">{designationsLoading ? "Loading…" : "Select…"}</option>
                    {designations.map((d) => <option key={d.id} value={d.name}>{d.name}</option>)}
                  </select>
                  {designationsError && <p className="text-xs text-red-600 mt-1">Could not load designations. Please close and reopen this form.</p>}
                  {!designationsLoading && !designationsError && designations.length === 0 && (
                    <p className="text-xs text-amber-700 mt-1">No active designations available — contact a Super Admin.</p>
                  )}
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Address *</label>
                <textarea value={form.address} onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))} rows={2}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <p className="text-sm text-gray-600 flex items-center gap-1.5 bg-[#E6F4F4] text-[#0D6E6E] rounded-xl px-3 py-2">
                <Mail size={14} /> Initial password will be the AVFU email itself; the faculty must change it after first login.
              </p>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={() => { setShowCreate(false); setForm(EMPTY_FORM); }} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={submit} disabled={createFaculty.isPending}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {createFaculty.isPending ? "Creating…" : "Create Faculty Account"}
              </button>
            </div>
          </div>
        </div>
      )}

      {editingFaculty && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg p-6 max-h-[90vh] overflow-y-auto">
            <h3 className="text-xl font-bold mb-1">Edit Faculty</h3>
            <p className="text-sm text-gray-600 mb-4">{editingFaculty.full_name}</p>
            <div className="space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Title</label>
                  <select value={editForm.title} onChange={(e) => setEditForm((f) => ({ ...f, title: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-2 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">—</option>
                    {TITLES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                </div>
                <div className="col-span-2">
                  <label className="block text-base font-semibold text-gray-700 mb-1">First Name *</label>
                  <input value={editForm.first_name} onChange={(e) => setEditForm((f) => ({ ...f, first_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Middle Name</label>
                  <input value={editForm.middle_name} onChange={(e) => setEditForm((f) => ({ ...f, middle_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Last Name *</label>
                  <input value={editForm.last_name} onChange={(e) => setEditForm((f) => ({ ...f, last_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Email (AVFU) *</label>
                <input type="email" value={editForm.email} onChange={(e) => setEditForm((f) => ({ ...f, email: e.target.value }))}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Mobile No.</label>
                  <input value={editForm.mobile} onChange={(e) => setEditForm((f) => ({ ...f, mobile: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Designation</label>
                  <select value={editForm.designation} onChange={(e) => setEditForm((f) => ({ ...f, designation: e.target.value }))}
                    disabled={designationsLoading}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E] disabled:bg-gray-50">
                    <option value="">{designationsLoading ? "Loading…" : "Select…"}</option>
                    {designations.map((d) => <option key={d.id} value={d.name}>{d.name}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Employee ID</label>
                  <input value={editForm.employee_id} onChange={(e) => setEditForm((f) => ({ ...f, employee_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Date of Birth</label>
                  <input type="date" value={editForm.date_of_birth} onChange={(e) => setEditForm((f) => ({ ...f, date_of_birth: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Gender</label>
                  <select value={editForm.gender} onChange={(e) => setEditForm((f) => ({ ...f, gender: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">—</option>
                    {GENDERS.map((g) => <option key={g} value={g}>{g}</option>)}
                  </select>
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Blood Group</label>
                  <select value={editForm.blood_group} onChange={(e) => setEditForm((f) => ({ ...f, blood_group: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">—</option>
                    {BLOOD_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">Father&apos;s Name</label>
                  <input value={editForm.father_name} onChange={(e) => setEditForm((f) => ({ ...f, father_name: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-1">College/Outstation</label>
                  <select value={editForm.college_id} onChange={(e) => setEditForm((f) => ({ ...f, college_id: e.target.value }))}
                    className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                    <option value="">—</option>
                    {colleges.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-base font-semibold text-gray-700 mb-1">Address</label>
                <textarea value={editForm.address} onChange={(e) => setEditForm((f) => ({ ...f, address: e.target.value }))} rows={2}
                  className="w-full border border-gray-300 rounded-xl px-3 py-2 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              </div>
            </div>
            <div className="flex gap-3 mt-5">
              <button onClick={closeEdit} className="flex-1 py-2.5 border border-gray-200 rounded-xl text-base font-medium hover:bg-gray-50">Cancel</button>
              <button onClick={() => updateFaculty.mutate()} disabled={updateFaculty.isPending || !editForm.first_name || !editForm.last_name || !editForm.email}
                className="flex-1 py-2.5 bg-[#0D6E6E] text-white rounded-xl text-base font-bold hover:bg-[#178F8F] disabled:opacity-60">
                {updateFaculty.isPending ? "Saving…" : "Save Changes"}
              </button>
            </div>
          </div>
        </div>
      )}

      {showBulkUpload && (
        <UserBulkUploadModal
          uploadUrl="/auth/faculty/bulk-upload"
          templateUrl="/auth/faculty/bulk-upload/template"
          templateFilename="ams_users_bulk_upload_template.xlsx"
          onClose={() => setShowBulkUpload(false)}
          onSuccess={(count, queued) => {
            toast.success(
              `${count} faculty account${count === 1 ? "" : "s"} created successfully.` +
              (queued > 0 ? ` ${queued} credential email(s) queued for delivery.` : ""),
            );
            qc.invalidateQueries({ queryKey: ["ams-hod-faculty"] });
            setShowBulkUpload(false);
          }}
        />
      )}
    </div>
  );
}
