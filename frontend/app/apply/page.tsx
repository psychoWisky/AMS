"use client";
import { useState, useEffect } from "react";
import axios from "axios";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001/api/v1";

const STEPS = [
  "Program & Category",
  "Personal Details",
  "Address & Guardian",
  "Academic Background",
  "Documents & Submit",
];

const STATE_CITIES: Record<string, string[]> = {
  "Andhra Pradesh": ["Visakhapatnam","Vijayawada","Guntur","Nellore","Kurnool","Rajamahendravaram","Tirupati","Kakinada","Kadapa","Anantapur"],
  "Arunachal Pradesh": ["Itanagar","Naharlagun","Pasighat","Tezpur","Bomdila","Ziro","Along","Tezu","Aalo","Roing"],
  "Assam": ["Guwahati","Silchar","Dibrugarh","Jorhat","Nagaon","Tinsukia","Tezpur","Bongaigaon","Dhubri","Diphu","Goalpara","Karimganj","North Lakhimpur","Sivasagar"],
  "Bihar": ["Patna","Gaya","Bhagalpur","Muzaffarpur","Purnia","Darbhanga","Bihar Sharif","Arrah","Begusarai","Katihar","Munger","Chhapra","Hajipur","Saharsa"],
  "Chhattisgarh": ["Raipur","Bhilai","Bilaspur","Korba","Durg","Rajnandgaon","Jagdalpur","Raigarh","Ambikapur","Dhamtari"],
  "Goa": ["Panaji","Margao","Vasco da Gama","Mapusa","Ponda","Bicholim","Curchorem","Sanquelim"],
  "Gujarat": ["Ahmedabad","Surat","Vadodara","Rajkot","Bhavnagar","Jamnagar","Junagadh","Gandhinagar","Anand","Navsari","Morbi","Nadiad","Surendranagar","Bharuch"],
  "Haryana": ["Faridabad","Gurugram","Panipat","Ambala","Yamunanagar","Rohtak","Hisar","Karnal","Sonipat","Panchkula","Bhiwani","Sirsa","Bahadurgarh"],
  "Himachal Pradesh": ["Shimla","Mandi","Solan","Dharamshala","Palampur","Baddi","Nahan","Kullu","Hamirpur","Una","Bilaspur","Chamba"],
  "Jharkhand": ["Ranchi","Jamshedpur","Dhanbad","Bokaro","Deoghar","Phusro","Hazaribagh","Giridih","Ramgarh","Medininagar","Chirkunda"],
  "Karnataka": ["Bengaluru","Mysuru","Hubballi","Mangaluru","Kalaburagi","Belagavi","Ballari","Vijayapura","Tumkur","Shivamogga","Raichur","Bidar","Davanagere","Hassan"],
  "Kerala": ["Thiruvananthapuram","Kochi","Kozhikode","Thrissur","Kollam","Palakkad","Alappuzha","Kannur","Kasaragod","Kottayam","Malappuram","Pathanamthitta"],
  "Madhya Pradesh": ["Bhopal","Indore","Jabalpur","Gwalior","Ujjain","Sagar","Dewas","Satna","Ratlam","Rewa","Murwara","Singrauli","Burhanpur","Khandwa"],
  "Maharashtra": ["Mumbai","Pune","Nagpur","Nashik","Thane","Aurangabad","Solapur","Amravati","Kolhapur","Navi Mumbai","Sangli","Malegaon","Jalgaon","Akola","Latur","Dhule"],
  "Manipur": ["Imphal","Thoubal","Bishnupur","Churachandpur","Ukhrul","Senapati","Tamenglong","Jiribam"],
  "Meghalaya": ["Shillong","Tura","Jowai","Nongpoh","Baghmara","Williamnagar","Resubelpara","Mairang"],
  "Mizoram": ["Aizawl","Lunglei","Champhai","Serchhip","Kolasib","Saiha","Lawngtlai","Mamit"],
  "Nagaland": ["Kohima","Dimapur","Mokokchung","Tuensang","Wokha","Zunheboto","Phek","Mon","Longleng"],
  "Odisha": ["Bhubaneswar","Cuttack","Rourkela","Brahmapur","Sambalpur","Puri","Balasore","Bhadrak","Baripada","Jharsuguda","Jeypore","Bargarh","Kendujhar","Koraput"],
  "Punjab": ["Ludhiana","Amritsar","Jalandhar","Patiala","Bathinda","Mohali","Hoshiarpur","Batala","Pathankot","Moga","Abohar","Malerkotla","Khanna","Phagwara"],
  "Rajasthan": ["Jaipur","Jodhpur","Kota","Bikaner","Ajmer","Udaipur","Bhilwara","Alwar","Sikar","Sri Ganganagar","Bharatpur","Pali","Nagaur","Barmer","Chittorgarh"],
  "Sikkim": ["Gangtok","Namchi","Jorethang","Mangan","Gyalshing","Ravangla","Singtam","Rangpo"],
  "Tamil Nadu": ["Chennai","Coimbatore","Madurai","Tiruchirappalli","Salem","Tirunelveli","Vellore","Erode","Tiruppur","Thoothukudi","Dindigul","Thanjavur","Ranipet","Sivakasi"],
  "Telangana": ["Hyderabad","Warangal","Nizamabad","Karimnagar","Ramagundam","Khammam","Mahbubnagar","Nalgonda","Siddipet","Adilabad"],
  "Tripura": ["Agartala","Dharmanagar","Udaipur","Kailashahar","Belonia","Khowai","Ambassa","Sonamura"],
  "Uttar Pradesh": ["Lucknow","Kanpur","Agra","Varanasi","Meerut","Allahabad","Ghaziabad","Noida","Bareilly","Aligarh","Moradabad","Saharanpur","Gorakhpur","Firozabad","Mathura","Muzaffarnagar","Jhansi","Faizabad"],
  "Uttarakhand": ["Dehradun","Haridwar","Roorkee","Haldwani","Rudrapur","Kashipur","Rishikesh","Kotdwar","Ramnagar","Pithoragarh"],
  "West Bengal": ["Kolkata","Asansol","Siliguri","Durgapur","Bardhaman","Malda","Baharampur","Habra","Kharagpur","Shantipur","Dankuni","Dhulian","Ranaghat","Haldia"],
  "Andaman and Nicobar Islands": ["Port Blair","Diglipur","Mayabunder","Rangat","Car Nicobar"],
  "Chandigarh": ["Chandigarh"],
  "Dadra and Nagar Haveli and Daman and Diu": ["Daman","Diu","Silvassa"],
  "Delhi": ["New Delhi","Delhi","Dwarka","Rohini","Pitampura","Janakpuri","Lajpat Nagar","Saket","Karol Bagh","Connaught Place","Shahdara","Preet Vihar"],
  "Jammu and Kashmir": ["Srinagar","Jammu","Anantnag","Sopore","Baramulla","Kathua","Udhampur","Punch","Rajouri","Leh"],
  "Ladakh": ["Leh","Kargil","Diskit","Padum","Khalsi"],
  "Lakshadweep": ["Kavaratti","Agatti","Minicoy","Amini","Androth"],
  "Puducherry": ["Puducherry","Karaikal","Mahe","Yanam"],
};

const INDIAN_STATES = Object.keys(STATE_CITIES).sort();

interface Program { id: string; name: string; code: string; level: string; department_name: string | null; }

type DocField = "doc_photo" | "doc_signature" | "doc_tenth_marksheet" | "doc_twelfth_marksheet" | "doc_aadhar" | "doc_category_cert" | "doc_transfer_cert";

interface FormData {
  // Step 1
  program_id: string; academic_year: string; category: string;
  // Step 2
  first_name: string; middle_name: string; last_name: string; date_of_birth: string;
  gender: string; nationality: string; religion: string; mother_tongue: string;
  aadhar_number: string; personal_email: string; mobile: string; alt_mobile: string;
  // Step 3 - current
  current_address: string; current_city: string; current_state: string; current_pincode: string;
  // Step 3 - permanent
  permanent_address: string; permanent_city: string; permanent_state: string; permanent_pincode: string;
  same_as_current: boolean;
  // Step 3 - guardian
  father_name: string; father_occupation: string; father_mobile: string; father_income: string;
  mother_name: string; mother_occupation: string; mother_mobile: string;
  // Step 4
  tenth_board: string; tenth_school: string; tenth_year: string; tenth_percentage: string; tenth_roll: string;
  twelfth_board: string; twelfth_school: string; twelfth_year: string; twelfth_percentage: string; twelfth_roll: string;
  twelfth_stream: string; entrance_exam: string; entrance_score: string;
}

const INIT: FormData = {
  program_id: "", academic_year: `${new Date().getFullYear()}-${(new Date().getFullYear() + 1).toString().slice(2)}`, category: "",
  first_name: "", middle_name: "", last_name: "", date_of_birth: "", gender: "", nationality: "Indian",
  religion: "", mother_tongue: "", aadhar_number: "", personal_email: "", mobile: "", alt_mobile: "",
  current_address: "", current_city: "", current_state: "", current_pincode: "",
  permanent_address: "", permanent_city: "", permanent_state: "", permanent_pincode: "",
  same_as_current: false,
  father_name: "", father_occupation: "", father_mobile: "", father_income: "",
  mother_name: "", mother_occupation: "", mother_mobile: "",
  tenth_board: "", tenth_school: "", tenth_year: "", tenth_percentage: "", tenth_roll: "",
  twelfth_board: "", twelfth_school: "", twelfth_year: "", twelfth_percentage: "", twelfth_roll: "",
  twelfth_stream: "", entrance_exam: "", entrance_score: "",
};

function Field({ label, required, error, children }: { label: string; required?: boolean; error?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-sm font-semibold text-gray-700 mb-1">
        {label}{required && <span className="text-red-500 ml-0.5">*</span>}
      </label>
      {children}
      {error && <p className="text-xs text-red-500 mt-1">{error}</p>}
    </div>
  );
}

const inp = "w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-teal-600 focus:border-transparent";
const sel = inp + " bg-white";

export default function ApplyPage() {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<FormData>(INIT);
  const [docs, setDocs] = useState<Partial<Record<DocField, File | null>>>({});
  const [programs, setPrograms] = useState<Program[]>([]);
  const [errors, setErrors] = useState<Partial<Record<string, string>>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<{ application_number: string } | null>(null);
  const [declared, setDeclared] = useState(false);
  const [currentCityOther, setCurrentCityOther] = useState(false);
  const [permanentCityOther, setPermanentCityOther] = useState(false);

  useEffect(() => {
    axios.get(`${API}/admission/programs`).then((r) => setPrograms(r.data)).catch(() => {});
  }, []);

  function set(key: keyof FormData, val: string | boolean) {
    setForm((f) => {
      const next = { ...f, [key]: val };
      if (key === "current_state") { next.current_city = ""; setCurrentCityOther(false); }
      if (key === "permanent_state") { next.permanent_city = ""; setPermanentCityOther(false); }
      if (key === "same_as_current" && val === true) {
        next.permanent_address = f.current_address;
        next.permanent_city = f.current_city;
        next.permanent_state = f.current_state;
        next.permanent_pincode = f.current_pincode;
        setPermanentCityOther(currentCityOther);
      }
      return next;
    });
    setErrors((e) => ({ ...e, [key]: undefined }));
  }

  function setDoc(key: DocField, file: File | null) {
    setDocs((d) => ({ ...d, [key]: file }));
  }

  function validate(): boolean {
    const e: Partial<Record<string, string>> = {};
    if (step === 0) {
      if (!form.academic_year) e.academic_year = "Required";
      if (!form.category) e.category = "Required";
    }
    if (step === 1) {
      if (!form.first_name.trim()) e.first_name = "Required";
      if (!form.last_name.trim()) e.last_name = "Required";
      if (!form.date_of_birth) e.date_of_birth = "Required";
      if (!form.gender) e.gender = "Required";
      if (!form.aadhar_number || !/^\d{12}$/.test(form.aadhar_number)) e.aadhar_number = "Must be 12 digits";
      if (!form.personal_email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.personal_email)) e.personal_email = "Valid email required";
      if (!form.mobile || !/^[6-9]\d{9}$/.test(form.mobile)) e.mobile = "Valid 10-digit mobile required";
    }
    if (step === 2) {
      if (!form.current_address.trim()) e.current_address = "Required";
      if (!form.current_city.trim()) e.current_city = "Required";
      if (!form.current_state) e.current_state = "Required";
      if (!form.current_pincode || !/^\d{6}$/.test(form.current_pincode)) e.current_pincode = "6-digit pincode required";
      if (!form.permanent_address.trim()) e.permanent_address = "Required";
      if (!form.permanent_city.trim()) e.permanent_city = "Required";
      if (!form.permanent_state) e.permanent_state = "Required";
      if (!form.permanent_pincode || !/^\d{6}$/.test(form.permanent_pincode)) e.permanent_pincode = "6-digit pincode required";
      if (!form.father_name.trim()) e.father_name = "Required";
      if (!form.mother_name.trim()) e.mother_name = "Required";
    }
    if (step === 3) {
      if (!form.tenth_board.trim()) e.tenth_board = "Required";
      if (!form.tenth_school.trim()) e.tenth_school = "Required";
      if (!form.tenth_year || isNaN(Number(form.tenth_year))) e.tenth_year = "Required";
      if (!form.tenth_percentage || isNaN(Number(form.tenth_percentage))) e.tenth_percentage = "Required";
      if (!form.twelfth_board.trim()) e.twelfth_board = "Required";
      if (!form.twelfth_school.trim()) e.twelfth_school = "Required";
      if (!form.twelfth_year || isNaN(Number(form.twelfth_year))) e.twelfth_year = "Required";
      if (!form.twelfth_percentage || isNaN(Number(form.twelfth_percentage))) e.twelfth_percentage = "Required";
      if (!form.twelfth_stream) e.twelfth_stream = "Required";
    }
    if (step === 4) {
      if (!docs.doc_photo) e.doc_photo = "Passport photograph is required";
      if (!docs.doc_signature) e.doc_signature = "Signature is required";
      if (!docs.doc_tenth_marksheet) e.doc_tenth_marksheet = "10th marksheet is required";
      if (!docs.doc_twelfth_marksheet) e.doc_twelfth_marksheet = "12th marksheet is required";
      if (!docs.doc_aadhar) e.doc_aadhar = "Aadhar card is required";
      if (!declared) e.declaration = "You must accept the declaration";
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  }

  function next() { if (validate()) setStep((s) => s + 1); }
  function back() { setStep((s) => s - 1); }

  async function submit() {
    if (!validate()) return;
    setSubmitting(true);
    try {
      const fd = new FormData();
      const textFields: (keyof Omit<FormData, "same_as_current">)[] = [
        "program_id","academic_year","category","first_name","middle_name","last_name",
        "date_of_birth","gender","nationality","religion","mother_tongue","aadhar_number",
        "personal_email","mobile","alt_mobile","current_address","current_city","current_state",
        "current_pincode","permanent_address","permanent_city","permanent_state","permanent_pincode",
        "father_name","father_occupation","father_mobile","father_income","mother_name",
        "mother_occupation","mother_mobile","tenth_board","tenth_school","tenth_year",
        "tenth_percentage","tenth_roll","twelfth_board","twelfth_school","twelfth_year",
        "twelfth_percentage","twelfth_roll","twelfth_stream","entrance_exam","entrance_score",
      ];
      for (const k of textFields) {
        const v = form[k] as string;
        if (v) fd.append(k, v);
      }
      const docKeys: DocField[] = ["doc_photo","doc_signature","doc_tenth_marksheet","doc_twelfth_marksheet","doc_aadhar","doc_category_cert","doc_transfer_cert"];
      for (const k of docKeys) {
        const f = docs[k];
        if (f) fd.append(k, f);
      }
      const res = await axios.post(`${API}/admission/apply`, fd);
      setSubmitted(res.data);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Submission failed. Please try again.";
      setErrors({ submit: msg });
    } finally {
      setSubmitting(false);
    }
  }

  if (submitted) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-teal-50 to-white flex items-center justify-center p-6">
        <div className="bg-white rounded-2xl shadow-xl p-10 max-w-lg w-full text-center">
          <div className="w-20 h-20 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-6">
            <svg className="w-10 h-10 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
          </div>
          <h2 className="text-2xl font-bold text-gray-900 mb-3">Application Submitted!</h2>
          <p className="text-gray-600 mb-6">Your admission application has been received successfully.</p>
          <div className="bg-teal-50 border border-teal-200 rounded-xl p-5 mb-6">
            <p className="text-sm text-teal-700 font-semibold mb-1">Your Application Number</p>
            <p className="text-2xl font-bold text-teal-800 font-mono">{submitted.application_number}</p>
          </div>
          <p className="text-sm text-gray-500">Please save this number for tracking your application status. You will be contacted on your registered email and mobile once the application is reviewed.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-teal-50 via-white to-blue-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 shadow-sm">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-teal-700 flex items-center justify-center text-white font-bold text-lg flex-shrink-0">AV</div>
          <div>
            <h1 className="text-base font-bold text-gray-900">Assam Veterinary and Fishery University</h1>
            <p className="text-xs text-gray-500">Online Admission Application Portal</p>
          </div>
        </div>
      </header>

      <div className="max-w-4xl mx-auto px-6 py-8">
        {/* Step progress */}
        <div className="mb-8">
          <div className="flex items-center justify-between">
            {STEPS.map((s, i) => (
              <div key={i} className="flex items-center flex-1">
                <div className="flex flex-col items-center">
                  <div className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold transition-colors ${i < step ? "bg-teal-600 text-white" : i === step ? "bg-teal-700 text-white ring-4 ring-teal-100" : "bg-gray-200 text-gray-500"}`}>
                    {i < step ? "✓" : i + 1}
                  </div>
                  <span className={`mt-1 text-xs font-medium text-center hidden sm:block ${i === step ? "text-teal-700" : "text-gray-400"}`}>{s}</span>
                </div>
                {i < STEPS.length - 1 && <div className={`flex-1 h-0.5 mx-2 ${i < step ? "bg-teal-600" : "bg-gray-200"}`} />}
              </div>
            ))}
          </div>
        </div>

        <div className="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden">
          <div className="bg-teal-700 px-8 py-5">
            <h2 className="text-white font-bold text-lg">Step {step + 1}: {STEPS[step]}</h2>
            <p className="text-teal-200 text-sm mt-0.5">Fields marked with * are required</p>
          </div>

          <div className="p-8">
            {/* STEP 0 — Program & Category */}
            {step === 0 && (
              <div className="space-y-5">
                <Field label="Program Applying For">
                  <select value={form.program_id} onChange={(e) => set("program_id", e.target.value)} className={sel}>
                    <option value="">-- Select Program --</option>
                    {programs.map((p) => (
                      <option key={p.id} value={p.id}>{p.name} ({p.code}) — {p.level}{p.department_name ? ` | ${p.department_name}` : ""}</option>
                    ))}
                  </select>
                </Field>
                <div className="grid grid-cols-2 gap-5">
                  <Field label="Academic Year" required error={errors.academic_year}>
                    <input value={form.academic_year} onChange={(e) => set("academic_year", e.target.value)} placeholder="e.g. 2026-27" className={inp} />
                  </Field>
                  <Field label="Category" required error={errors.category}>
                    <select value={form.category} onChange={(e) => set("category", e.target.value)} className={sel}>
                      <option value="">Select category</option>
                      {["General","OBC","SC","ST","EWS","PH (Physically Handicapped)"].map((c) => <option key={c}>{c}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 text-sm text-blue-800">
                  <strong>Note:</strong> After your admission is approved, you will receive your official university email ID and login credentials to access the Academic Management System (AMS).
                </div>
              </div>
            )}

            {/* STEP 1 — Personal Details */}
            {step === 1 && (
              <div className="space-y-5">
                <div className="grid grid-cols-3 gap-5">
                  <Field label="First Name" required error={errors.first_name}>
                    <input value={form.first_name} onChange={(e) => set("first_name", e.target.value)} className={inp} />
                  </Field>
                  <Field label="Middle Name">
                    <input value={form.middle_name} onChange={(e) => set("middle_name", e.target.value)} className={inp} />
                  </Field>
                  <Field label="Last Name" required error={errors.last_name}>
                    <input value={form.last_name} onChange={(e) => set("last_name", e.target.value)} className={inp} />
                  </Field>
                </div>
                <div className="grid grid-cols-2 gap-5">
                  <Field label="Date of Birth" required error={errors.date_of_birth}>
                    <input type="date" value={form.date_of_birth} onChange={(e) => set("date_of_birth", e.target.value)} max={new Date().toISOString().slice(0,10)} className={inp} />
                  </Field>
                  <Field label="Gender" required error={errors.gender}>
                    <select value={form.gender} onChange={(e) => set("gender", e.target.value)} className={sel}>
                      <option value="">Select</option>
                      {["Male","Female","Transgender","Prefer not to say"].map((g) => <option key={g}>{g}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="grid grid-cols-3 gap-5">
                  <Field label="Nationality" required>
                    <input value={form.nationality} onChange={(e) => set("nationality", e.target.value)} className={inp} />
                  </Field>
                  <Field label="Religion">
                    <input value={form.religion} onChange={(e) => set("religion", e.target.value)} placeholder="Optional" className={inp} />
                  </Field>
                  <Field label="Mother Tongue">
                    <input value={form.mother_tongue} onChange={(e) => set("mother_tongue", e.target.value)} placeholder="Optional" className={inp} />
                  </Field>
                </div>
                <div className="grid grid-cols-2 gap-5">
                  <Field label="Aadhar Number" required error={errors.aadhar_number}>
                    <input value={form.aadhar_number} onChange={(e) => set("aadhar_number", e.target.value.replace(/\D/g,"").slice(0,12))} placeholder="12-digit Aadhar number" maxLength={12} className={`${inp} font-mono tracking-widest`} />
                  </Field>
                  <Field label="Personal Email" required error={errors.personal_email}>
                    <input type="email" value={form.personal_email} onChange={(e) => set("personal_email", e.target.value)} placeholder="your@email.com" className={inp} />
                  </Field>
                </div>
                <div className="grid grid-cols-2 gap-5">
                  <Field label="Mobile Number" required error={errors.mobile}>
                    <input value={form.mobile} onChange={(e) => set("mobile", e.target.value.replace(/\D/g,"").slice(0,10))} placeholder="10-digit mobile" maxLength={10} className={inp} />
                  </Field>
                  <Field label="Alternate Mobile">
                    <input value={form.alt_mobile} onChange={(e) => set("alt_mobile", e.target.value.replace(/\D/g,"").slice(0,10))} placeholder="Optional" maxLength={10} className={inp} />
                  </Field>
                </div>
              </div>
            )}

            {/* STEP 2 — Address & Guardian */}
            {step === 2 && (
              <div className="space-y-6">
                <div>
                  <h3 className="font-bold text-gray-700 mb-4 pb-2 border-b text-sm uppercase tracking-wide">Current Address</h3>
                  <div className="space-y-4">
                    <Field label="Address Line" required error={errors.current_address}>
                      <textarea value={form.current_address} onChange={(e) => set("current_address", e.target.value)} rows={2} placeholder="House no., Street, Area, Landmark" className={inp} />
                    </Field>
                    <div className="grid grid-cols-3 gap-4">
                      <Field label="State" required error={errors.current_state}>
                        <select value={form.current_state} onChange={(e) => set("current_state", e.target.value)} className={sel}>
                          <option value="">Select State</option>
                          {INDIAN_STATES.map((s) => <option key={s}>{s}</option>)}
                        </select>
                      </Field>
                      <Field label="City" required error={errors.current_city}>
                        <select
                          value={currentCityOther ? "__other__" : form.current_city}
                          onChange={(e) => {
                            if (e.target.value === "__other__") { setCurrentCityOther(true); set("current_city", ""); }
                            else { setCurrentCityOther(false); set("current_city", e.target.value); }
                          }}
                          className={sel} disabled={!form.current_state}>
                          <option value="">{form.current_state ? "Select City" : "Select state first"}</option>
                          {(STATE_CITIES[form.current_state] ?? []).map((c) => <option key={c}>{c}</option>)}
                          <option value="__other__">Other (type manually)</option>
                        </select>
                        {currentCityOther && (
                          <input autoFocus value={form.current_city} onChange={(e) => set("current_city", e.target.value)}
                            placeholder="Enter your city name" className={`${inp} mt-2`} />
                        )}
                      </Field>
                      <Field label="Pincode" required error={errors.current_pincode}>
                        <input value={form.current_pincode} onChange={(e) => set("current_pincode", e.target.value.replace(/\D/g,"").slice(0,6))} maxLength={6} className={inp} />
                      </Field>
                    </div>
                  </div>
                </div>

                <div>
                  <div className="flex items-center justify-between mb-4 pb-2 border-b">
                    <h3 className="font-bold text-gray-700 text-sm uppercase tracking-wide">Permanent Address</h3>
                    <label className="flex items-center gap-2 text-sm text-teal-700 cursor-pointer">
                      <input type="checkbox" checked={form.same_as_current} onChange={(e) => set("same_as_current", e.target.checked)} className="rounded" />
                      Same as current address
                    </label>
                  </div>
                  <div className="space-y-4">
                    <Field label="Address Line" required error={errors.permanent_address}>
                      <textarea value={form.permanent_address} onChange={(e) => set("permanent_address", e.target.value)} rows={2} placeholder="House no., Street, Area, Landmark" className={inp} />
                    </Field>
                    <div className="grid grid-cols-3 gap-4">
                      <Field label="State" required error={errors.permanent_state}>
                        <select value={form.permanent_state} onChange={(e) => set("permanent_state", e.target.value)} className={sel}>
                          <option value="">Select State</option>
                          {INDIAN_STATES.map((s) => <option key={s}>{s}</option>)}
                        </select>
                      </Field>
                      <Field label="City" required error={errors.permanent_city}>
                        <select
                          value={permanentCityOther ? "__other__" : form.permanent_city}
                          onChange={(e) => {
                            if (e.target.value === "__other__") { setPermanentCityOther(true); set("permanent_city", ""); }
                            else { setPermanentCityOther(false); set("permanent_city", e.target.value); }
                          }}
                          className={sel} disabled={!form.permanent_state}>
                          <option value="">{form.permanent_state ? "Select City" : "Select state first"}</option>
                          {(STATE_CITIES[form.permanent_state] ?? []).map((c) => <option key={c}>{c}</option>)}
                          <option value="__other__">Other (type manually)</option>
                        </select>
                        {permanentCityOther && (
                          <input autoFocus value={form.permanent_city} onChange={(e) => set("permanent_city", e.target.value)}
                            placeholder="Enter your city name" className={`${inp} mt-2`} />
                        )}
                      </Field>
                      <Field label="Pincode" required error={errors.permanent_pincode}>
                        <input value={form.permanent_pincode} onChange={(e) => set("permanent_pincode", e.target.value.replace(/\D/g,"").slice(0,6))} maxLength={6} className={inp} />
                      </Field>
                    </div>
                  </div>
                </div>

                <div>
                  <h3 className="font-bold text-gray-700 mb-4 pb-2 border-b text-sm uppercase tracking-wide">Guardian / Family Details</h3>
                  <div className="space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                      <Field label="Father's Full Name" required error={errors.father_name}>
                        <input value={form.father_name} onChange={(e) => set("father_name", e.target.value)} className={inp} />
                      </Field>
                      <Field label="Father's Occupation">
                        <input value={form.father_occupation} onChange={(e) => set("father_occupation", e.target.value)} className={inp} />
                      </Field>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <Field label="Father's Mobile">
                        <input value={form.father_mobile} onChange={(e) => set("father_mobile", e.target.value.replace(/\D/g,"").slice(0,10))} maxLength={10} className={inp} />
                      </Field>
                      <Field label="Annual Family Income (₹)">
                        <select value={form.father_income} onChange={(e) => set("father_income", e.target.value)} className={sel}>
                          <option value="">Select range</option>
                          {["Below 1,00,000","1,00,000 – 2,50,000","2,50,000 – 5,00,000","5,00,000 – 10,00,000","Above 10,00,000"].map((r) => <option key={r}>{r}</option>)}
                        </select>
                      </Field>
                    </div>
                    <div className="grid grid-cols-2 gap-4">
                      <Field label="Mother's Full Name" required error={errors.mother_name}>
                        <input value={form.mother_name} onChange={(e) => set("mother_name", e.target.value)} className={inp} />
                      </Field>
                      <Field label="Mother's Occupation">
                        <input value={form.mother_occupation} onChange={(e) => set("mother_occupation", e.target.value)} className={inp} />
                      </Field>
                    </div>
                    <Field label="Mother's Mobile">
                      <input value={form.mother_mobile} onChange={(e) => set("mother_mobile", e.target.value.replace(/\D/g,"").slice(0,10))} maxLength={10} className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:ring-2 focus:ring-teal-600 max-w-xs" />
                    </Field>
                  </div>
                </div>
              </div>
            )}

            {/* STEP 3 — Academic Background */}
            {step === 3 && (
              <div className="space-y-6">
                <div>
                  <h3 className="font-bold text-gray-700 mb-4 pb-2 border-b text-sm uppercase tracking-wide">10th Standard (Matriculation)</h3>
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="Board" required error={errors.tenth_board}>
                      <input value={form.tenth_board} onChange={(e) => set("tenth_board", e.target.value)} placeholder="e.g. CBSE, SEBA, ICSE" className={inp} />
                    </Field>
                    <Field label="School Name" required error={errors.tenth_school}>
                      <input value={form.tenth_school} onChange={(e) => set("tenth_school", e.target.value)} className={inp} />
                    </Field>
                    <Field label="Year of Passing" required error={errors.tenth_year}>
                      <input type="number" value={form.tenth_year} onChange={(e) => set("tenth_year", e.target.value)} placeholder="YYYY" min={2000} max={new Date().getFullYear()} className={inp} />
                    </Field>
                    <Field label="Percentage / CGPA" required error={errors.tenth_percentage}>
                      <input type="number" value={form.tenth_percentage} onChange={(e) => set("tenth_percentage", e.target.value)} placeholder="e.g. 85.5" step={0.01} min={0} max={100} className={inp} />
                    </Field>
                    <Field label="Roll Number">
                      <input value={form.tenth_roll} onChange={(e) => set("tenth_roll", e.target.value)} placeholder="Optional" className={inp} />
                    </Field>
                  </div>
                </div>

                <div>
                  <h3 className="font-bold text-gray-700 mb-4 pb-2 border-b text-sm uppercase tracking-wide">12th Standard (Higher Secondary)</h3>
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="Board" required error={errors.twelfth_board}>
                      <input value={form.twelfth_board} onChange={(e) => set("twelfth_board", e.target.value)} placeholder="e.g. CBSE, AHSEC, ICSE" className={inp} />
                    </Field>
                    <Field label="School / College Name" required error={errors.twelfth_school}>
                      <input value={form.twelfth_school} onChange={(e) => set("twelfth_school", e.target.value)} className={inp} />
                    </Field>
                    <Field label="Year of Passing" required error={errors.twelfth_year}>
                      <input type="number" value={form.twelfth_year} onChange={(e) => set("twelfth_year", e.target.value)} placeholder="YYYY" min={2000} max={new Date().getFullYear()} className={inp} />
                    </Field>
                    <Field label="Percentage / CGPA" required error={errors.twelfth_percentage}>
                      <input type="number" value={form.twelfth_percentage} onChange={(e) => set("twelfth_percentage", e.target.value)} placeholder="e.g. 78.0" step={0.01} min={0} max={100} className={inp} />
                    </Field>
                    <Field label="Stream" required error={errors.twelfth_stream}>
                      <select value={form.twelfth_stream} onChange={(e) => set("twelfth_stream", e.target.value)} className={sel}>
                        <option value="">Select stream</option>
                        {["Science (PCB)","Science (PCM)","Commerce","Arts / Humanities","Vocational"].map((s) => <option key={s}>{s}</option>)}
                      </select>
                    </Field>
                    <Field label="Roll Number">
                      <input value={form.twelfth_roll} onChange={(e) => set("twelfth_roll", e.target.value)} placeholder="Optional" className={inp} />
                    </Field>
                  </div>
                </div>

                <div>
                  <h3 className="font-bold text-gray-700 mb-4 pb-2 border-b text-sm uppercase tracking-wide">Entrance Exam (if applicable)</h3>
                  <div className="grid grid-cols-2 gap-4">
                    <Field label="Exam Name">
                      <input value={form.entrance_exam} onChange={(e) => set("entrance_exam", e.target.value)} placeholder="e.g. NEET, JEE, CUET" className={inp} />
                    </Field>
                    <Field label="Score / Rank">
                      <input type="number" value={form.entrance_score} onChange={(e) => set("entrance_score", e.target.value)} placeholder="Optional" className={inp} />
                    </Field>
                  </div>
                </div>
              </div>
            )}

            {/* STEP 4 — Documents & Submit */}
            {step === 4 && (
              <div className="space-y-6">
                <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-800">
                  <strong>Document Requirements:</strong> Upload clear scanned copies or photographs. Accepted formats: PDF, JPG, PNG. Maximum file size: 10 MB each.
                </div>

                <div className="grid grid-cols-1 gap-4">
                  {([
                    { key: "doc_photo" as DocField, label: "Passport Size Photograph", required: true, hint: "Recent passport size photo, white background" },
                    { key: "doc_signature" as DocField, label: "Applicant's Signature", required: true, hint: "Scanned signature on white paper" },
                    { key: "doc_tenth_marksheet" as DocField, label: "10th Marksheet", required: true, hint: "Mark sheet / grade card" },
                    { key: "doc_twelfth_marksheet" as DocField, label: "12th Marksheet", required: true, hint: "Mark sheet / grade card" },
                    { key: "doc_aadhar" as DocField, label: "Aadhar Card (Both sides)", required: true, hint: "Clear copy of front and back" },
                    { key: "doc_category_cert" as DocField, label: "Category Certificate", required: false, hint: "Required for OBC/SC/ST/EWS/PH applicants" },
                    { key: "doc_transfer_cert" as DocField, label: "Transfer / Migration Certificate", required: false, hint: "From your previous institution (if applicable)" },
                  ] as const).map(({ key, label, required, hint }) => (
                    <div key={key} className={`flex items-center gap-4 p-4 border rounded-xl transition-colors ${errors[key] ? "border-red-300 bg-red-50" : "border-gray-200 hover:border-teal-300"}`}>
                      <div className="flex-1">
                        <p className="text-sm font-semibold text-gray-700">{label}{required && <span className="text-red-500 ml-1">*</span>}</p>
                        <p className="text-xs text-gray-400 mt-0.5">{hint}</p>
                        {errors[key] && <p className="text-xs text-red-500 mt-1 font-medium">{errors[key]}</p>}
                      </div>
                      <div className="flex-shrink-0">
                        {docs[key] ? (
                          <div className="flex items-center gap-2">
                            <span className="text-xs text-green-700 bg-green-50 border border-green-200 rounded-full px-3 py-1 font-medium">✓ {docs[key]!.name.slice(0, 20)}{docs[key]!.name.length > 20 ? "…" : ""}</span>
                            <button onClick={() => setDoc(key, null)} className="text-xs text-red-500 hover:underline">Remove</button>
                          </div>
                        ) : (
                          <label className="cursor-pointer">
                            <span className="text-sm font-semibold text-teal-700 border border-teal-300 rounded-lg px-4 py-2 hover:bg-teal-50 transition-colors">Choose File</span>
                            <input type="file" className="hidden" accept=".pdf,.jpg,.jpeg,.png" onChange={(e) => setDoc(key, e.target.files?.[0] ?? null)} />
                          </label>
                        )}
                      </div>
                    </div>
                  ))}
                </div>

                {/* Declaration */}
                <div className="border border-gray-200 rounded-xl p-5 bg-gray-50">
                  <h3 className="font-bold text-gray-800 mb-3">Declaration</h3>
                  <p className="text-sm text-gray-600 mb-4 leading-relaxed">
                    I hereby declare that all the information furnished above is true, correct and complete to the best of my knowledge and belief. I have not concealed or misrepresented any fact/information in this application. I understand that in case any information is found to be false or incorrect, my admission is liable to be cancelled.
                  </p>
                  <label className="flex items-start gap-3 cursor-pointer">
                    <input type="checkbox" checked={declared} onChange={(e) => { setDeclared(e.target.checked); setErrors((er) => ({ ...er, declaration: undefined })); }}
                      className="mt-0.5 w-4 h-4 text-teal-600 rounded" />
                    <span className="text-sm font-semibold text-gray-700">I accept the above declaration and confirm that all information provided is accurate.</span>
                  </label>
                  {errors.declaration && <p className="text-xs text-red-500 mt-2">{errors.declaration}</p>}
                </div>

                {errors.submit && (
                  <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-700">{errors.submit}</div>
                )}
              </div>
            )}
          </div>

          {/* Navigation */}
          <div className="px-8 py-5 bg-gray-50 border-t border-gray-200 flex justify-between items-center">
            <button onClick={back} disabled={step === 0}
              className="px-6 py-2.5 border border-gray-300 rounded-xl text-sm font-semibold text-gray-700 hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed transition-colors">
              ← Back
            </button>
            <span className="text-sm text-gray-400">Step {step + 1} of {STEPS.length}</span>
            {step < STEPS.length - 1 ? (
              <button onClick={next}
                className="px-6 py-2.5 bg-teal-700 text-white rounded-xl text-sm font-bold hover:bg-teal-800 transition-colors">
                Continue →
              </button>
            ) : (
              <button onClick={submit} disabled={submitting}
                className="px-8 py-2.5 bg-teal-700 text-white rounded-xl text-sm font-bold hover:bg-teal-800 disabled:opacity-60 flex items-center gap-2 transition-colors">
                {submitting && <svg className="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" /></svg>}
                {submitting ? "Submitting…" : "Submit Application"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
