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
    if (!admitCard) return;
    const win = window.open("", "_blank", "width=900,height=750");
    if (!win) return;
    const card = admitCard;
    const examPeriod = card.exam.exam_start
      ? `${new Date(card.exam.exam_start).toLocaleDateString("en-IN", { day: "2-digit", month: "long", year: "numeric" })} to ${new Date(card.exam.exam_end!).toLocaleDateString("en-IN", { day: "2-digit", month: "long", year: "numeric" })}`
      : "To be announced";
    const generatedOn = new Date(card.generated_at).toLocaleDateString("en-IN", { day: "2-digit", month: "long", year: "numeric" });

    const courseRows = card.courses.map((c, i) => `
      <tr style="background:${i % 2 === 0 ? "#fff" : "#f7fafa"}">
        <td style="padding:9px 14px;font-family:monospace;font-weight:700;color:#0D6E6E;border-bottom:1px solid #e5e7eb">${c.course_number}</td>
        <td style="padding:9px 14px;border-bottom:1px solid #e5e7eb">${c.course_title}</td>
        <td style="padding:9px 14px;text-align:center;border-bottom:1px solid #e5e7eb">${c.credit_structure}</td>
        <td style="padding:9px 14px;text-align:center;border-bottom:1px solid #e5e7eb">${c.section ?? "—"}</td>
      </tr>`).join("");

    win.document.write(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>Admit Card — ${card.student.full_name}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: "Segoe UI", Arial, sans-serif; background: #f0f4f4; display: flex; justify-content: center; padding: 30px 20px; color: #1a1a1a; }
    .page { width: 780px; background: #fff; border: 2px solid #0D6E6E; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 24px rgba(0,0,0,0.12); }
    /* Header */
    .header { background: linear-gradient(135deg, #0a5555 0%, #0D6E6E 60%, #0f8080 100%); color: #fff; padding: 24px 32px; display: flex; align-items: center; gap: 20px; }
    .emblem { width: 72px; height: 72px; border-radius: 50%; background: rgba(255,255,255,0.15); border: 2px solid rgba(255,255,255,0.4); display: flex; align-items: center; justify-content: center; font-size: 28px; flex-shrink: 0; }
    .header-text { flex: 1; }
    .univ-name { font-size: 18px; font-weight: 700; letter-spacing: 0.3px; }
    .univ-sub { font-size: 12px; opacity: 0.8; margin-top: 2px; }
    .doc-title { text-align: right; }
    .doc-title h2 { font-size: 20px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase; border: 2px solid rgba(255,255,255,0.5); padding: 6px 16px; border-radius: 6px; }
    .doc-title p { font-size: 11px; opacity: 0.75; margin-top: 4px; }
    /* Notice bar */
    .notice { background: #fff3cd; border-bottom: 1px solid #ffc107; padding: 8px 32px; font-size: 12px; color: #7a5800; font-weight: 600; text-align: center; letter-spacing: 0.2px; }
    /* Body */
    .body { padding: 24px 32px; }
    /* Ref row */
    .ref-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 2px dashed #b2d8d8; }
    .ref-label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }
    .ref-value { font-weight: 700; font-family: monospace; color: #0D6E6E; font-size: 14px; }
    /* Info grid */
    .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 20px; }
    .info-box { border: 1px solid #d1e8e8; border-radius: 8px; padding: 10px 14px; background: #f7fafa; }
    .info-box.full { grid-column: 1 / -1; }
    .info-label { font-size: 10px; font-weight: 700; color: #0D6E6E; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 3px; }
    .info-value { font-size: 14px; font-weight: 700; color: #111; }
    /* Section heading */
    .section-heading { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; color: #0D6E6E; border-left: 3px solid #0D6E6E; padding-left: 10px; margin-bottom: 10px; }
    /* Course table */
    table { width: 100%; border-collapse: collapse; border: 1px solid #d1e8e8; border-radius: 8px; overflow: hidden; margin-bottom: 24px; font-size: 13px; }
    thead { background: #0D6E6E; color: #fff; }
    thead th { padding: 10px 14px; text-align: left; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; }
    thead th:last-child, thead th:nth-last-child(2) { text-align: center; }
    /* Instructions */
    .instructions { background: #f0f7f7; border: 1px solid #b2d8d8; border-radius: 8px; padding: 14px 18px; margin-bottom: 24px; }
    .instructions h4 { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #0D6E6E; margin-bottom: 8px; }
    .instructions ol { padding-left: 18px; }
    .instructions li { font-size: 11.5px; color: #444; margin-bottom: 4px; line-height: 1.5; }
    /* Signatures */
    .sig-row { display: flex; justify-content: space-between; align-items: flex-end; margin-top: 8px; }
    .sig-box { text-align: center; width: 180px; }
    .sig-line { border-top: 1.5px solid #555; padding-top: 6px; margin-top: 40px; font-size: 11.5px; color: #444; font-weight: 600; }
    .sig-sub { font-size: 10px; color: #888; margin-top: 2px; }
    /* Footer */
    .footer { background: #0D6E6E; color: rgba(255,255,255,0.8); text-align: center; padding: 8px 20px; font-size: 10.5px; }
    @media print {
      body { background: #fff; padding: 0; }
      .page { box-shadow: none; border-radius: 0; width: 100%; border: none; }
    }
  </style>
</head>
<body>
<div class="page">
  <div class="header">
    <div class="emblem">🎓</div>
    <div class="header-text">
      <div class="univ-name">Assam Veterinary and Fishery University</div>
      <div class="univ-sub">AVFU — Academic Management System</div>
      <div class="univ-sub" style="margin-top:4px">Khanapara, Guwahati, Assam — 781022</div>
    </div>
    <div class="doc-title">
      <h2>Admit Card</h2>
      <p>Examination ${card.exam.academic_year}</p>
    </div>
  </div>

  <div class="notice">⚠ This admit card is valid only with a valid photo identity proof. Carry it to every examination hall.</div>

  <div class="body">
    <div class="ref-row">
      <div><div class="ref-label">Admit Card No.</div><div class="ref-value">${card.admit_card_id.slice(0, 8).toUpperCase()}-${card.admit_card_id.slice(9, 13).toUpperCase()}</div></div>
      <div style="text-align:right"><div class="ref-label">Generated On</div><div class="ref-value" style="color:#555;font-family:sans-serif;font-size:13px">${generatedOn}</div></div>
    </div>

    <div class="info-grid">
      <div class="info-box">
        <div class="info-label">Student Name</div>
        <div class="info-value">${card.student.full_name}</div>
      </div>
      <div class="info-box">
        <div class="info-label">Roll Number</div>
        <div class="info-value" style="font-family:monospace">${card.student.roll_number}</div>
      </div>
      <div class="info-box">
        <div class="info-label">University ID (UID)</div>
        <div class="info-value" style="font-family:monospace;color:#0D6E6E">${card.student.uid}</div>
      </div>
      <div class="info-box">
        <div class="info-label">Email</div>
        <div class="info-value" style="font-size:13px">${card.student.email}</div>
      </div>
      <div class="info-box">
        <div class="info-label">Semester</div>
        <div class="info-value">${card.exam.semester_name}</div>
      </div>
      <div class="info-box">
        <div class="info-label">Examination Period</div>
        <div class="info-value" style="font-size:13px">${examPeriod}</div>
      </div>
    </div>

    <div class="section-heading">Enrolled Courses</div>
    <table>
      <thead>
        <tr>
          <th>Course No.</th>
          <th>Course Title</th>
          <th style="text-align:center">Credits</th>
          <th style="text-align:center">Section</th>
        </tr>
      </thead>
      <tbody>${courseRows}</tbody>
    </table>

    <div class="instructions">
      <h4>Important Instructions</h4>
      <ol>
        <li>This admit card must be presented at the examination centre before each paper.</li>
        <li>Candidates must carry a valid government-issued photo identity proof along with this admit card.</li>
        <li>Use of mobile phones, electronic devices, or unfair means will lead to cancellation of candidature.</li>
        <li>Candidates must be seated 15 minutes before the commencement of each examination.</li>
        <li>This is a system-generated document and does not require a physical signature to be valid.</li>
      </ol>
    </div>

    <div class="sig-row">
      <div class="sig-box">
        <div class="sig-line">Student Signature</div>
        <div class="sig-sub">${card.student.full_name}</div>
      </div>
      <div style="text-align:center;font-size:11px;color:#888">
        <div style="font-size:22px;margin-bottom:4px">✓</div>
        <div style="color:#0D6E6E;font-weight:700;font-size:12px">Verified &amp; Approved</div>
        <div>Academic Section, AVFU</div>
      </div>
      <div class="sig-box">
        <div class="sig-line">Controller of Examinations</div>
        <div class="sig-sub">Assam Veterinary and Fishery University</div>
      </div>
    </div>
  </div>

  <div class="footer">
    Assam Veterinary and Fishery University (AVFU) &nbsp;|&nbsp; Khanapara, Guwahati, Assam &nbsp;|&nbsp; This admit card is computer-generated
  </div>
</div>
<script>window.onload = function() { window.print(); }<\/script>
</body>
</html>`);
    win.document.close();
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

        <div className="bg-white rounded-2xl border border-gray-200 overflow-hidden overflow-x-auto">
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
                <p className="text-base opacity-90">Assam Veterinary and Fishery University</p>
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
