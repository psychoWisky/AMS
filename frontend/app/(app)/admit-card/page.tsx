"use client";
import { useState, useRef } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { api } from "@/services/api";
import { useRole, useUser } from "@/stores/auth.store";
import { toast } from "sonner";
import { IdCard, Send, Download, Loader2, CheckCircle2, Shield, BookOpen, Calendar, Search } from "lucide-react";

interface Semester { id: string; name: string; calendar_id: string; }
interface Calendar { id: string; name: string; academic_year: string; }
interface AdmitCardData {
  admit_card_id: string;
  student: { id: string; full_name: string; roll_number: string; uid: string; email: string; };
  exam: { semester_name: string; academic_year: string; exam_start: string | null; exam_end: string | null; };
  courses: { course_number: string; course_title: string; credit_structure: string; section: string | null; }[];
  generated_at: string;
  downloaded_at: string | null;
  is_downloaded: boolean;
}

export default function AdmitCardPage() {
  const role = useRole();
  const user = useUser();
  const isStudent = role === "student";
  const isAdmin = !isStudent;
  const printRef = useRef<HTMLDivElement>(null);

  // Student state
  const [semesterId, setSemesterId] = useState("");
  const [uid, setUid] = useState("");
  const [otp, setOtp] = useState("");
  const [otpSent, setOtpSent] = useState(false);
  const [admitCard, setAdmitCard] = useState<AdmitCardData | null>(null);

  // Admin state
  const [adminSemFilter, setAdminSemFilter] = useState("");
  const [search, setSearch] = useState("");

  // Shared: semesters + calendars
  const { data: calendars = [] } = useQuery<Calendar[]>({
    queryKey: ["ams-calendars"],
    queryFn: async () => (await api.get("/academic/calendars")).data,
  });
  const { data: semesters = [] } = useQuery<Semester[]>({
    queryKey: ["ams-all-semesters"],
    queryFn: async () => {
      const all = await Promise.all(calendars.map((c) => api.get(`/academic/calendars/${c.id}/semesters`)));
      return all.flatMap((r) => r.data);
    },
    enabled: calendars.length > 0,
  });

  // Admin: all admit cards
  const { data: allCards = [], isLoading: adminLoading } = useQuery<AdmitCardData[]>({
    queryKey: ["ams-admit-cards", adminSemFilter],
    queryFn: async () => (await api.get(`/admit-card/all${adminSemFilter ? `?semester_id=${adminSemFilter}` : ""}`)).data,
    enabled: isAdmin,
  });

  // Student: my admit cards history
  const { data: myCards = [] } = useQuery<AdmitCardData[]>({
    queryKey: ["ams-my-admit-cards"],
    queryFn: async () => (await api.get("/admit-card/my")).data,
    enabled: isStudent,
  });

  const requestOtp = useMutation({
    mutationFn: () => api.post("/admit-card/request-otp", { semester_id: semesterId, uid }),
    onSuccess: (res) => {
      setOtpSent(true);
      const devOtp = res.data.dev_otp;
      toast.success(`OTP sent to your email!${devOtp ? ` [DEV: ${devOtp}]` : ""}`);
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to send OTP."),
  });

  const generateCard = useMutation({
    mutationFn: () => api.post("/admit-card/generate", { semester_id: semesterId, uid, otp }),
    onSuccess: (res) => {
      setAdmitCard(res.data);
      toast.success("Admit card generated! Please print or save it now — you can only download it once.");
    },
    onError: (e: unknown) => toast.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Failed to generate admit card."),
  });

  function handlePrint() {
    const el = printRef.current;
    if (!el) return;
    const win = window.open("", "_blank", "width=900,height=700");
    if (!win) return;
    win.document.write(`
      <html><head><title>Admit Card — ${admitCard?.student.full_name}</title>
      <style>
        body { font-family: Arial, sans-serif; padding: 32px; color: #111; }
        h1 { font-size: 22px; text-align: center; margin-bottom: 4px; }
        .subtitle { text-align: center; color: #555; font-size: 14px; margin-bottom: 24px; }
        .header-bar { background: #0D6E6E; color: white; padding: 12px 20px; border-radius: 8px; margin-bottom: 20px; display:flex; justify-content:space-between; align-items:center; }
        .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }
        .info-box { border: 1px solid #ddd; border-radius: 6px; padding: 10px 14px; }
        .label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 2px; }
        .value { font-size: 15px; font-weight: bold; color: #111; }
        table { width: 100%; border-collapse: collapse; margin-top: 8px; }
        th { background: #f0f7f7; padding: 8px 12px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
        td { padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 14px; }
        .sig-row { display: flex; justify-content: space-between; margin-top: 40px; }
        .sig-box { text-align: center; border-top: 1px solid #999; padding-top: 6px; width: 160px; font-size: 12px; color: #555; }
        .footer { margin-top: 16px; font-size: 11px; color: #888; text-align: center; border-top: 1px solid #eee; padding-top: 10px; }
        .badge { background: #0D6E6E; color: white; border-radius: 4px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
        @media print { body { padding: 0; } }
      </style></head><body>
      ${el.innerHTML}
      </body></html>`);
    win.document.close();
    setTimeout(() => win.print(), 500);
  }

  const semesterLabel = (id: string) => {
    const s = semesters.find((s) => s.id === id);
    if (!s) return id;
    const cal = calendars.find((c) => c.id === s.calendar_id);
    return `${s.name}${cal ? ` (${cal.academic_year})` : ""}`;
  };

  const filteredCards = allCards.filter((c) => {
    const q = search.toLowerCase();
    return !search || c.student.full_name.toLowerCase().includes(q) || c.student.roll_number.toLowerCase().includes(q) || c.student.uid.toLowerCase().includes(q);
  });

  // ── ADMIN VIEW ──────────────────────────────────────────────────────────────
  if (isAdmin) {
    return (
      <div className="p-6 max-w-6xl mx-auto">
        <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><IdCard size={24} className="text-[#0D6E6E]" />Admit Card Management</h1>
        <p className="text-gray-700 text-base mt-1 mb-6">View and print all student admit cards</p>

        <div className="flex gap-3 mb-5 flex-wrap">
          <div className="relative flex-1 min-w-[200px]">
            <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by name, roll, UID…"
              className="w-full pl-9 pr-4 py-2.5 border border-gray-200 rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
          </div>
          <select value={adminSemFilter} onChange={(e) => setAdminSemFilter(e.target.value)}
            className="border border-gray-200 rounded-xl px-3 py-2.5 text-base focus:outline-none">
            <option value="">All Semesters</option>
            {semesters.map((s) => <option key={s.id} value={s.id}>{semesterLabel(s.id)}</option>)}
          </select>
        </div>

        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden">
          {adminLoading ? (
            <div className="flex justify-center py-16"><Loader2 className="animate-spin text-gray-600" /></div>
          ) : filteredCards.length === 0 ? (
            <div className="text-center py-16 text-gray-600"><IdCard size={40} className="mx-auto mb-3 opacity-30" /><p>No admit cards generated yet.</p></div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-gray-50 border-b border-gray-200">
                <tr>{["Student", "Roll No.", "UID", "Semester", "Generated", "Downloaded", "Action"].map((h) => (
                  <th key={h} className="text-left px-4 py-3 font-semibold text-gray-700">{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {filteredCards.map((c, i) => (
                  <tr key={c.admit_card_id} className={i % 2 === 0 ? "bg-white" : "bg-gray-50/50"}>
                    <td className="px-4 py-3 font-medium text-gray-900">{c.student.full_name}</td>
                    <td className="px-4 py-3 font-mono text-sm">{c.student.roll_number}</td>
                    <td className="px-4 py-3 font-mono text-sm text-[#0D6E6E]">{c.student.uid}</td>
                    <td className="px-4 py-3 text-gray-700">{c.exam.semester_name}</td>
                    <td className="px-4 py-3 text-gray-600 text-sm">{new Date(c.generated_at).toLocaleString("en-IN")}</td>
                    <td className="px-4 py-3">
                      {c.is_downloaded ? (
                        <span className="px-2 py-0.5 bg-green-100 text-green-700 rounded-full text-sm font-semibold">Downloaded</span>
                      ) : (
                        <span className="px-2 py-0.5 bg-amber-100 text-amber-700 rounded-full text-sm font-semibold">Pending</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <button onClick={() => window.open(`/admit-card/print/${c.admit_card_id}`, "_blank")}
                        className="flex items-center gap-1 px-3 py-1.5 text-sm font-semibold border border-[#0D6E6E] text-[#0D6E6E] rounded-lg hover:bg-[#E6F4F4]">
                        <Download size={13} /> View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    );
  }

  // ── STUDENT VIEW ────────────────────────────────────────────────────────────
  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h1 className="text-3xl font-bold text-gray-900 flex items-center gap-2 mb-2"><IdCard size={24} className="text-[#0D6E6E]" />Admit Card</h1>
      <p className="text-gray-700 text-base mb-6">Generate your exam admit card. You can download it <strong>only once</strong> per semester.</p>

      {/* Past admit cards */}
      {myCards.length > 0 && !admitCard && (
        <div className="bg-white rounded-2xl border border-gray-200 p-5 mb-6">
          <h2 className="font-bold text-gray-800 mb-3">Previously Generated</h2>
          <div className="space-y-2">
            {myCards.map((c) => (
              <div key={c.admit_card_id} className="flex items-center gap-3 p-3 bg-gray-50 rounded-xl">
                <BookOpen size={16} className="text-[#0D6E6E]" />
                <div className="flex-1">
                  <p className="font-semibold text-gray-800">{c.exam.semester_name} <span className="text-gray-500 font-normal text-sm">({c.exam.academic_year})</span></p>
                  <p className="text-sm text-gray-600">Generated: {new Date(c.generated_at).toLocaleString("en-IN")}</p>
                </div>
                <span className="px-2 py-0.5 bg-green-100 text-green-700 rounded-full text-sm font-semibold flex items-center gap-1">
                  <CheckCircle2 size={12} /> Downloaded
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Generation form */}
      {!admitCard && (
        <div className="bg-white rounded-2xl border border-gray-200 p-6">
          <h2 className="font-bold text-gray-800 mb-5 flex items-center gap-2"><Shield size={16} className="text-[#0D6E6E]" />Generate Admit Card</h2>

          <div className="space-y-4">
            <div>
              <label className="block text-base font-semibold text-gray-700 mb-2">Select Semester *</label>
              <select value={semesterId} onChange={(e) => { setSemesterId(e.target.value); setOtpSent(false); setOtp(""); }}
                className="w-full border border-gray-300 rounded-xl px-4 py-3 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]">
                <option value="">Select semester…</option>
                {semesters.map((s) => <option key={s.id} value={s.id}>{semesterLabel(s.id)}</option>)}
              </select>
            </div>

            <div>
              <label className="block text-base font-semibold text-gray-700 mb-2">Your University ID (UID / Roll Number) *</label>
              <input value={uid} onChange={(e) => { setUid(e.target.value); setOtpSent(false); }}
                placeholder="Enter your UID or Roll Number"
                className="w-full border border-gray-300 rounded-xl px-4 py-3 text-base focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
              <p className="text-sm text-gray-600 mt-1">Must match your registered student roll number.</p>
            </div>

            {!otpSent ? (
              <button onClick={() => {
                if (!semesterId) { toast.error("Please select a semester."); return; }
                if (!uid.trim()) { toast.error("Please enter your UID."); return; }
                requestOtp.mutate();
              }} disabled={requestOtp.isPending}
                className="w-full py-3 bg-[#0D6E6E] text-white rounded-xl font-bold text-base hover:bg-[#178F8F] disabled:opacity-60 flex items-center justify-center gap-2">
                {requestOtp.isPending ? <Loader2 size={18} className="animate-spin" /> : <Send size={18} />}
                {requestOtp.isPending ? "Sending OTP…" : "Send OTP to Email"}
              </button>
            ) : (
              <div className="space-y-3">
                <div className="p-3 bg-green-50 border border-green-200 rounded-xl text-green-800 text-sm flex items-center gap-2">
                  <CheckCircle2 size={15} /> OTP sent to your registered email address.
                </div>
                <div>
                  <label className="block text-base font-semibold text-gray-700 mb-2">Enter OTP *</label>
                  <input value={otp} onChange={(e) => setOtp(e.target.value)} placeholder="6-digit OTP"
                    maxLength={6} className="w-full border border-gray-300 rounded-xl px-4 py-3 text-base text-center tracking-widest font-mono focus:outline-none focus:ring-2 focus:ring-[#0D6E6E]" />
                </div>
                <div className="flex gap-3">
                  <button onClick={() => { setOtpSent(false); setOtp(""); }}
                    className="flex-1 py-3 border border-gray-200 rounded-xl text-base font-semibold text-gray-700 hover:bg-gray-50">
                    Resend OTP
                  </button>
                  <button onClick={() => {
                    if (!otp.trim() || otp.length < 6) { toast.error("Please enter the 6-digit OTP."); return; }
                    generateCard.mutate();
                  }} disabled={generateCard.isPending}
                    className="flex-1 py-3 bg-[#0D6E6E] text-white rounded-xl font-bold text-base hover:bg-[#178F8F] disabled:opacity-60 flex items-center justify-center gap-2">
                    {generateCard.isPending ? <Loader2 size={18} className="animate-spin" /> : <IdCard size={18} />}
                    {generateCard.isPending ? "Generating…" : "Generate Admit Card"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Generated Admit Card */}
      {admitCard && (
        <div className="space-y-4">
          <div className="flex items-center justify-between bg-green-50 border border-green-200 rounded-2xl px-5 py-4">
            <div className="flex items-center gap-2 text-green-800 font-semibold"><CheckCircle2 size={18} /> Admit card generated. Print or save it now — this is a one-time download.</div>
            <button onClick={handlePrint}
              className="flex items-center gap-2 px-5 py-2.5 bg-[#0D6E6E] text-white rounded-xl font-bold text-base hover:bg-[#178F8F]">
              <Download size={16} /> Print / Save as PDF
            </button>
          </div>

          {/* Printable card */}
          <div ref={printRef} className="bg-white rounded-2xl border-2 border-[#0D6E6E] overflow-hidden">
            {/* Header */}
            <div className="bg-[#0D6E6E] text-white px-8 py-5 flex items-center justify-between">
              <div>
                <h1 className="text-2xl font-bold">AVFU — Admit Card</h1>
                <p className="text-base opacity-90">Atal Bihari Vajpayee Farming University</p>
              </div>
              <div className="text-right text-sm opacity-80">
                <p>Academic Year: <strong>{admitCard.exam.academic_year}</strong></p>
                <p>Ref: {admitCard.admit_card_id.slice(0, 8).toUpperCase()}</p>
              </div>
            </div>

            <div className="p-8">
              {/* Student info grid */}
              <div className="grid grid-cols-2 gap-4 mb-6">
                {[
                  { label: "Student Name", value: admitCard.student.full_name },
                  { label: "Roll Number", value: admitCard.student.roll_number },
                  { label: "University ID (UID)", value: admitCard.student.uid },
                  { label: "Email", value: admitCard.student.email },
                  { label: "Semester", value: admitCard.exam.semester_name },
                  { label: "Exam Period", value: admitCard.exam.exam_start ? `${new Date(admitCard.exam.exam_start).toLocaleDateString("en-IN")} — ${new Date(admitCard.exam.exam_end!).toLocaleDateString("en-IN")}` : "To be announced" },
                ].map(({ label, value }) => (
                  <div key={label} className="border border-gray-200 rounded-xl p-3">
                    <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">{label}</p>
                    <p className="text-base font-bold text-gray-900">{value}</p>
                  </div>
                ))}
              </div>

              {/* Courses table */}
              <h3 className="font-bold text-gray-800 mb-3 flex items-center gap-2"><Calendar size={16} className="text-[#0D6E6E]" />Enrolled Courses</h3>
              <table className="w-full border border-gray-200 rounded-xl overflow-hidden text-sm mb-6">
                <thead className="bg-[#E6F4F4]">
                  <tr>
                    <th className="text-left px-4 py-2.5 font-semibold text-gray-700">Course No.</th>
                    <th className="text-left px-4 py-2.5 font-semibold text-gray-700">Course Title</th>
                    <th className="text-left px-4 py-2.5 font-semibold text-gray-700">Credits</th>
                    <th className="text-left px-4 py-2.5 font-semibold text-gray-700">Section</th>
                  </tr>
                </thead>
                <tbody>
                  {admitCard.courses.map((c, i) => (
                    <tr key={i} className={i % 2 === 0 ? "" : "bg-gray-50"}>
                      <td className="px-4 py-2.5 font-mono font-bold text-[#0D6E6E]">{c.course_number}</td>
                      <td className="px-4 py-2.5 text-gray-800">{c.course_title}</td>
                      <td className="px-4 py-2.5 text-gray-700">{c.credit_structure}</td>
                      <td className="px-4 py-2.5 text-gray-700">{c.section ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {/* Signatures */}
              <div className="flex justify-between items-end mt-8 pt-4 border-t border-gray-200">
                <div className="text-center">
                  <div className="h-12 mb-2" />
                  <div className="border-t border-gray-500 pt-1 w-40 text-sm text-gray-600">Student Signature</div>
                </div>
                <div className="text-center text-sm text-gray-500">
                  <p>Generated: {new Date(admitCard.generated_at).toLocaleString("en-IN")}</p>
                  <p className="text-xs mt-0.5">This is a system-generated document</p>
                </div>
                <div className="text-center">
                  <div className="h-12 mb-2" />
                  <div className="border-t border-gray-500 pt-1 w-40 text-sm text-gray-600">Controller of Examinations</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
