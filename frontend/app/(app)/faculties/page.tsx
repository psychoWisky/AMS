"use client";
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/services/api";
import { toast } from "sonner";
import { UserCog, Plus, Search, Loader2, Mail } from "lucide-react";

interface Faculty {
  id: string; email: string; full_name: string; title: string | null;
  designation: string | null; mobile: string | null;
}
interface DesignationOpt { id: string; name: string; is_active: boolean; }

const TITLES = ["Dr.", "Mr", "Mrs", "Miss"];

const EMPTY_FORM = {
  title: "Dr.", first_name: "", middle_name: "", last_name: "",
  date_of_birth: "", gender: "", email: "", mobile: "",
  designation: "", address: "",
};

export default function FacultiesPage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);

  const { data: faculty = [], isLoading } = useQuery<Faculty[]>({
    queryKey: ["ams-hod-faculty"],
    queryFn: async () => (await api.get("/auth/users", { params: { role: "faculty" } })).data,
  });

  // Designation-management task — Super Admin-managed master data replaces the
  // previously hardcoded 3-value list; only active designations are offered here.
  // The HOD Add Faculty form is selection-only — no create/edit/delete controls.
  const { data: designations = [], isLoading: designationsLoading, isError: designationsError } = useQuery<DesignationOpt[]>({
    queryKey: ["ams-active-designations"],
    queryFn: async () => (await api.get("/admin/designations", { params: { active: true } })).data,
    enabled: showCreate,
  });

  const createFaculty = useMutation({
    mutationFn: () => api.post("/auth/faculty", form),
    onSuccess: (res) => {
      toast.success(res.data.email_sent ? "Faculty account created — credentials emailed." : "Faculty account created. Email could not be sent — share credentials manually.");
      qc.invalidateQueries({ queryKey: ["ams-hod-faculty"] });
      setShowCreate(false); setForm(EMPTY_FORM);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to create faculty."),
  });

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
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2"><UserCog size={24} className="text-[#0D6E6E]" />Faculties</h1>
          <p className="text-gray-700 text-base mt-1">Faculty members in your department</p>
        </div>
        <button onClick={() => setShowCreate(true)}
          className="flex items-center gap-2 px-4 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-semibold text-base hover:bg-[#178F8F]">
          <Plus size={16} /> Add Faculty
        </button>
      </div>

      <div className="relative max-w-xs mb-4">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-600" />
        <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search faculty…"
          className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
      </div>

      <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
        {isLoading ? (
          <div className="flex items-center justify-center py-16 text-gray-600"><Loader2 className="animate-spin mr-2" />Loading…</div>
        ) : filtered.length === 0 ? (
          <div className="text-center py-16 text-gray-600"><UserCog size={40} className="mx-auto mb-3 opacity-30" /><p>No faculty found in your department yet.</p></div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-gray-50 border-b border-gray-200">
              <tr>{["Sl No", "Name", "Email", "Designation", "Mobile"].map((h) => (
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
    </div>
  );
}
