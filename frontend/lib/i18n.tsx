"use client";

/**
 * Arabic / English for the users' pages (/login, /reset, /me). Arabic is the default; the choice is kept in the
 * browser and sent to the server so Telegram notes use the same language. Admin pages stay English.
 */
import { Languages } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export type Lang = "ar" | "en";
const KEY = "alpha-lang";

// key: [English, Arabic]. {name} placeholders are filled by t(key, {name: value}).
const DICT = {
  // common
  "c.loading": ["Loading…", "جارٍ التحميل…"],
  "c.error": ["Something went wrong", "حصل خطأ"],
  "c.failed": ["Failed", "فشلت العملية"],
  "c.save": ["Save", "حفظ"],
  "c.saved": ["Saved", "اتحفظ"],
  "c.copy": ["copy", "نسخ"],
  "c.copied": ["copied", "اتنسخ"],
  "c.password": ["Your password", "كلمة السر"],
  "c.code": ["Code", "الكود"],
  "c.lang": ["العربية", "English"],

  // login
  "l.signin": ["Sign in", "تسجيل الدخول"],
  "l.signup_title": ["Create an account", "إنشاء حساب"],
  "l.forgot_title": ["Forgot your password?", "نسيت كلمة السر؟"],
  "l.forgot_hint": ["Enter your email. If it is registered, you get a link to choose a new password (it works once, for one hour).",
    "اكتب إيميلك. لو متسجل، هيوصلك لينك تختار بيه كلمة سر جديدة (بيشتغل مرة واحدة، لمدة ساعة)."],
  "l.signup_hint": ["After you sign up, the admin reviews your account. Once approved you get your own trading wallet.",
    "بعد التسجيل، الأدمن بيراجع حسابك. أول ما يتوافق عليه هيبقى ليك محفظة تداول خاصة بيك."],
  "l.name": ["Name", "الاسم"],
  "l.email": ["Email", "الإيميل"],
  "l.password": ["Password", "كلمة السر"],
  "l.repeat": ["Repeat password (10+ characters)", "أعد كتابة كلمة السر (10 حروف أو أكتر)"],
  "l.mismatch": ["The two passwords are different.", "كلمتين السر مش زي بعض."],
  "l.signup": ["Sign up", "تسجيل"],
  "l.send_link": ["Send me a reset link", "ابعتلي لينك تغيير كلمة السر"],
  "l.forgot": ["Forgot your password?", "نسيت كلمة السر؟"],
  "l.to_signup": ["No account yet? Sign up", "معندكش حساب؟ سجل"],
  "l.to_signin": ["Already have an account? Sign in", "عندك حساب؟ سجل دخول"],
  "l.reset_sent": ["If this email is registered, a reset link is on its way.", "لو الإيميل ده متسجل، لينك تغيير كلمة السر في الطريق."],

  // reset
  "r.title": ["Choose a new password", "اختار كلمة سر جديدة"],
  "r.done": ["Your password is changed and you are signed in.", "كلمة السر اتغيرت وانت داخل على حسابك."],
  "r.locked": ["For your safety, withdrawals stay locked for {h} hours.", "للأمان، السحب هيفضل مقفول {h} ساعة."],
  "r.go": ["Go to my trading", "روح لصفحة التداول"],
  "r.no_token": ["This link has no token. Ask for a new one on the sign-in page.", "اللينك ده ناقص. اطلب لينك جديد من صفحة الدخول."],
  "r.new": ["New password (10+ characters)", "كلمة سر جديدة (10 حروف أو أكتر)"],
  "r.repeat": ["Repeat it", "أعد كتابتها"],
  "r.save": ["Save new password", "احفظ كلمة السر الجديدة"],

  // me: header + stats
  "m.title": ["My trading", "تداولي"],
  "m.signout": ["Sign out", "خروج"],
  "m.pending_title": ["Waiting for approval", "في انتظار الموافقة"],
  "m.pending": ["Your account is registered. Once the admin approves it, your trading wallet appears here with its deposit address.",
    "حسابك اتسجل. أول ما الأدمن يوافق عليه، محفظة التداول بتاعتك هتظهر هنا ومعاها عنوان الإيداع."],
  "m.pnl_now": ["P&L now", "الربح والخسارة دلوقتي"],
  "m.pnl_sub": ["realized {r} · open {o}", "متحقق {r} · مفتوح {o}"],
  "m.last24": ["Last 24h", "آخر 24 ساعة"],
  "m.realized": ["realized", "متحقق"],
  "m.in_open": ["In open trades", "في صفقات مفتوحة"],
  "m.in_open_sub": ["{n} open · budget {b}", "{n} مفتوحة · الميزانية {b}"],
  "m.wallet": ["Wallet", "المحفظة"],
  "m.open_trades": ["Open trades", "الصفقات المفتوحة"],
  "m.closed_trades": ["Closed trades", "الصفقات المقفولة"],
  "m.sell_q": ["Sell {p}% of ${s}?", "تبيع {p}% من ${s}؟"],
  "m.sell_fail": ["Could not sell", "مقدرناش نبيع"],

  // positions table
  "p.none_open": ["No open trades.", "مفيش صفقات مفتوحة."],
  "p.none_closed": ["No closed trades yet.", "لسه مفيش صفقات مقفولة."],
  "p.token": ["Token", "العملة"],
  "p.cost": ["Cost", "التكلفة"],
  "p.value": ["Value now", "القيمة دلوقتي"],
  "p.sold_for": ["Sold for", "اتباعت بـ"],
  "p.pnl": ["P&L", "الربح/الخسارة"],
  "p.sell": ["Sell", "بيع"],
  "p.exit": ["Exit", "سبب الخروج"],
  "p.selling": ["selling {p}%…", "جاري بيع {p}%…"],
  "x.exit signal": ["exit signal", "إشارة خروج"],
  "x.partial exit": ["partial exit", "خروج جزئي"],
  "x.capital taken": ["capital taken", "سحب رأس المال"],
  "x.sold by hand": ["sold by hand", "اتباعت يدوي"],
  "x.sold from page": ["sold from page", "اتباعت من الصفحة"],
  "x.max hold time": ["max hold time", "انتهت مدة الاحتفاظ"],
  "x.time limit": ["time limit, no profit", "انتهى الوقت من غير ربح"],

  // performance
  // billing
  "b.title": ["Fees", "الرسوم"],
  "b.terms": ["I agree to the fees: ${fee} a month plus {pct}% of each month's net profit above my best month so far (no share in a losing month until it is won back), taken in SOL from my trading wallet. The first {trial} days are free. If a bill stays unpaid {grace} days, new buys pause until it is paid.",
    "موافق على الرسوم: ${fee} في الشهر + {pct}% من صافي ربح الشهر فوق أعلى مستوى وصلتله قبل كده (الشهر الخسران مفيهوش نسبة لحد ما الخسارة تتعوض)، بتتخصم SOL من محفظة التداول بتاعتي. أول {trial} أيام مجاناً. لو الفاتورة فضلت مش مدفوعة {grace} أيام، الشراء الجديد بيقف لحد ما تتدفع."],
  "b.must": ["Accept the fees to sign up.", "لازم توافق على الرسوم علشان تسجل."],
  "b.trial": ["Free trial until {d}", "فترة مجانية لحد {d}"],
  "b.period": ["This month: {a} → {b}", "الشهر الحالي: من {a} إلى {b}"],
  "b.fee": ["Monthly fee", "الاشتراك الشهري"],
  "b.profit": ["Profit this month (above your best)", "ربح الشهر (فوق أعلى مستوى)"],
  "b.share": ["Profit share ({pct}%)", "نسبة الربح ({pct}%)"],
  "b.est": ["Bill if the month ended now", "الفاتورة لو الشهر خلص دلوقتي"],
  "b.how": ["Taken in SOL from your trading wallet when the month ends. Keep some SOL there.",
    "بتتخصم SOL من محفظة التداول آخر الشهر. سيب فيها SOL كفاية."],
  "b.hold": ["A bill is unpaid: new buys are paused. Add SOL to your trading wallet; it is collected within the hour and buying resumes.",
    "فيه فاتورة مش مدفوعة: الشراء الجديد واقف. ضيف SOL لمحفظة التداول، وهتتخصم خلال ساعة والشراء يرجع."],
  "b.history": ["Past bills", "الفواتير السابقة"],
  "b.coupon": ["Discount code (optional)", "كود خصم (اختياري)"],
  "b.coupon_apply": ["Apply", "تطبيق"],
  "b.coupon_ph": ["Discount code", "كود الخصم"],
  "b.coupon_on": ["Code {c}: {p}% off the monthly fee", "كود {c}: خصم {p}% على الاشتراك الشهري"],
  "b.coupon_left": [" ({n} more months)", " (فاضل {n} شهور)"],
  "b.coupon_ok": ["Code applied.", "الكود اتطبق."],
  "b.discount": ["Discount ({c})", "خصم ({c})"],
  "b.coupon_hint": ["A code lowers the monthly fee only, not the profit share.", "الكود بيخصم من الاشتراك الشهري بس، مش من نسبة الربح."],
  "b.s.paid": ["paid", "مدفوعة"],
  "b.s.due": ["due", "مستحقة"],
  "b.s.waived": ["waived", "معفاة"],
  "b.s.check": ["being checked", "تحت المراجعة"],
  "b.s.sending": ["being checked", "تحت المراجعة"],

  "f.title": ["Performance", "الأداء"],
  "f.none": ["No closed trades yet. Results show here after the first one closes.", "لسه مفيش صفقات مقفولة. النتايج هتظهر هنا بعد أول صفقة تتقفل."],
  "f.total": ["Total result", "النتيجة الكلية"],
  "f.roi": ["Return on trades", "العائد على الصفقات"],
  "f.winrate": ["Win rate", "نسبة الكسب"],
  "f.winrate_v": ["{p}% of {n}", "{p}% من {n}"],
  "f.avgwin": ["Average win", "متوسط المكسب"],
  "f.avgloss": ["Average loss", "متوسط الخسارة"],
  "f.bestworst": ["Best / worst", "الأحسن / الأسوأ"],
  "f.daily": ["Result per day, last 30 days", "النتيجة كل يوم، آخر 30 يوم"],
  "f.chart": ["Profit and loss per day, last 30 days", "الربح والخسارة كل يوم، آخر 30 يوم"],
  "f.trades": ["trades", "صفقات"],

  // settings
  "s.title": ["Trading settings", "إعدادات التداول"],
  "s.active": ["ACTIVE", "شغال"],
  "s.paused": ["PAUSED", "متوقف"],
  "s.pause": ["Pause", "إيقاف مؤقت"],
  "s.resume": ["Resume", "تشغيل"],
  "s.paused_ok": ["Paused: no new trades, open ones still close", "اتوقف: مفيش صفقات جديدة، والمفتوحة هتتقفل عادي"],
  "s.resumed": ["Resumed", "رجع يشتغل"],
  "s.entry": ["Entry per trade", "مبلغ الدخول في الصفقة"],
  "s.entry_h": ["USD per trade", "بالدولار لكل صفقة"],
  "s.budget": ["Trading budget", "ميزانية التداول"],
  "s.budget_h": ["max USD in open trades", "أقصى مبلغ في صفقات مفتوحة"],
  "s.loss": ["Max loss / 24h", "أقصى خسارة في 24 ساعة"],
  "s.loss_h": ["new trades stop after this", "الصفقات الجديدة بتقف بعدها"],
  "s.cap": ["max {v}", "الحد الأقصى {v}"],
  "s.take": ["Take capital at", "اسحب رأس المال عند"],
  "s.take_h": ["get the cost back at this gain, the rest rides", "يرجعلك رأس المال عند الربح ده، والباقي يكمل"],
  "s.save_fail": ["Could not save", "مقدرناش نحفظ"],

  // wallet
  "w.title": ["Your trading wallet", "محفظة التداول بتاعتك"],
  "w.deposit": ["Deposit address (Solana, send SOL)", "عنوان الإيداع (سولانا، ابعت SOL)"],
  "w.balance": ["Balance", "الرصيد"],
  "w.key_warn": ["Your private key. Shown only this once: save it now and import it in Phantom to see your tokens. Anyone with it controls the wallet.",
    "المفتاح الخاص بتاعك. بيظهر المرة دي بس: احفظه دلوقتي واعمله import في Phantom عشان تشوف عملاتك. أي حد معاه المفتاح ده يتحكم في المحفظة."],
  "w.saved_q": ["Did you save it? It can't be shown again.", "حفظته؟ مش هيظهر تاني."],
  "w.saved": ["I saved it", "حفظته"],
  "w.withdraw_t": ["Withdraw SOL", "سحب SOL"],
  "w.to": ["Send to (your Solana address)", "ابعت على (عنوان سولانا بتاعك)"],
  "w.amount": ["Amount in SOL (empty = all)", "المبلغ بالـ SOL (فاضي = الكل)"],
  "w.keep": ["Tokens of open trades stay in the wallet; 0.01 SOL stays for their fees.", "عملات الصفقات المفتوحة بتفضل في المحفظة، و0.01 SOL بيفضلوا لرسوم بيعها."],
  "w.confirm": ["Confirm with your password", "أكد بكلمة السر"],
  "w.confirm_code": ["Confirm with your password and code", "أكد بكلمة السر والكود"],
  "w.acct_pw": ["Your account password", "كلمة سر حسابك"],
  "w.code": ["2-step code", "كود التحقق"],
  "w.need_2fa": ["Withdrawals need two-step codes: turn them on in Security below.", "السحب محتاج التحقق بخطوتين: شغله من قسم الأمان تحت."],
  "w.withdraw": ["Withdraw", "اسحب"],
  "w.withdraw_q": ["Send {a} SOL to {to}? This can't be undone.", "تبعت {a} SOL على {to}؟ العملية دي مينفعش ترجع."],
  "w.all": ["all available", "كل المتاح"],
  "w.sent": ["Sent {a} SOL · {s}…", "اتبعت {a} SOL · {s}…"],
  "w.withdraw_fail": ["Withdrawal failed", "السحب فشل"],
  "w.show_key": ["Show private key (once)", "اعرض المفتاح الخاص (مرة واحدة)"],
  "w.key_fail": ["Could not show the key", "مقدرناش نعرض المفتاح"],

  // telegram
  "t.title": ["Telegram notifications", "إشعارات تيليجرام"],
  "t.on": ["CONNECTED", "متوصل"],
  "t.off": ["OFF", "مقفول"],
  "t.hint": ["Get a message when a trade opens or closes, and a summary every day.", "هتوصلك رسالة لما صفقة تتفتح أو تتقفل، وملخص كل يوم."],
  "t.connect": ["Connect Telegram", "وصّل تيليجرام"],
  "t.opened": ["Telegram opened: press Start in the chat with the bot, then come back here.", "تيليجرام اتفتح: دوس Start في الشات مع البوت، وبعدين ارجع هنا."],
  "t.link_fail": ["Could not make a link", "مقدرناش نعمل لينك"],
  "t.daily": ["Daily summary", "ملخص يومي"],
  "t.disconnect": ["Disconnect", "افصل"],

  // security
  "z.title": ["Security", "الأمان"],
  "z.on": ["2-STEP ON", "التحقق شغال"],
  "z.off": ["2-STEP OFF", "التحقق مقفول"],
  "z.locked": ["Withdrawals are locked for {h} more hours after a password or two-step reset. Trading keeps working.",
    "السحب مقفول {h} ساعة كمان بعد تغيير كلمة السر أو التحقق. التداول شغال عادي."],
  "z.codes": ["Two-step codes (needed to withdraw)", "التحقق بخطوتين (لازم للسحب)"],
  "z.hint": ["Use an authenticator app (Google Authenticator, Authy, 1Password…). Every withdrawal will ask for the 6-digit code it shows, so a stolen password alone can't move your money.",
    "استخدم تطبيق تحقق (Google Authenticator أو Authy أو 1Password…). كل سحب هيطلب الكود اللي فيه 6 أرقام، فلو حد سرق كلمة السر لوحدها مش هيقدر يحرك فلوسك."],
  "z.setup": ["Set up two-step codes", "شغّل التحقق بخطوتين"],
  "z.step1": ["1. Scan this with your authenticator app (or type the key).", "1. امسح الكود ده بتطبيق التحقق (أو اكتب المفتاح)."],
  "z.qr_alt": ["QR code for your authenticator app", "كود QR لتطبيق التحقق"],
  "z.step2": ["2. Enter the 6-digit code it shows now.", "2. اكتب الكود اللي ظاهر دلوقتي (6 أرقام)."],
  "z.turn_on": ["Turn on", "شغّل"],
  "z.on_ok": ["Two-step codes are on", "التحقق بخطوتين اشتغل"],
  "z.on_text": ["On. Lost your phone? Ask the admin to reset it (withdrawals then wait 24 h).", "شغال. لو ضاع موبايلك، اطلب من الأدمن يلغيه (والسحب هيستنى 24 ساعة)."],
  "z.turn_off": ["Turn off", "إلغاء"],
  "z.off_q": ["Turn off two-step codes? Withdrawals will be locked for 24 hours.", "تلغي التحقق بخطوتين؟ السحب هيتقفل 24 ساعة."],
  "z.off_btn": ["Turn off two-step codes", "ألغِ التحقق بخطوتين"],
  "z.off_ok": ["Two-step codes are off", "التحقق بخطوتين اتلغى"],
  "z.pw_title": ["Change password", "تغيير كلمة السر"],
  "z.pw_cur": ["Current password", "كلمة السر الحالية"],
  "z.pw_new": ["New password (10+ characters)", "كلمة سر جديدة (10 حروف أو أكتر)"],
  "z.pw_ok": ["Password changed; other devices are signed out", "كلمة السر اتغيرت، والأجهزة التانية اتعملها خروج"],
} as const satisfies Record<string, readonly [string, string]>;

export type Key = keyof typeof DICT;

// Messages the server sends, shown in Arabic when they are known (prefix match); anything else stays as sent.
const SERVER_AR: [RegExp, string][] = [
  [/^Accept the fees and terms/, "لازم توافق على الرسوم علشان تسجل"],
  [/^This code is not valid/, "الكود ده مش صحيح"],
  [/^This code has expired/, "الكود ده انتهى"],
  [/^This code has been used up/, "الكود ده خلص استخدامه"],
  [/^You already have a discount code/, "عندك كود خصم شغال بالفعل"],
  [/^Too many wrong codes/, "أكواد غلط كتير، جرب تاني بعد شوية"],
  [/^Wrong email or password/, "الإيميل أو كلمة السر غلط"],
  [/^Wrong password/, "كلمة السر غلط"],
  [/^Current password is wrong/, "كلمة السر الحالية غلط"],
  [/^Too many wrong (passwords|codes)/, "محاولات غلط كتير، جرب تاني بعد 15 دقيقة"],
  [/^Too many/, "طلبات كتير، جرب تاني بعد شوية"],
  [/^This email is already registered/, "الإيميل ده متسجل قبل كده: سجل دخول"],
  [/^Enter a valid email/, "اكتب إيميل صحيح"],
  [/^password must be at least (\d+)/, "كلمة السر لازم تكون $1 حروف على الأقل"],
  [/^This account is blocked/, "الحساب ده موقوف"],
  [/^Sign-ups are closed/, "التسجيل مقفول حالياً"],
  [/^Not signed in|^Session expired/, "لازم تسجل دخول تاني"],
  [/^This link is invalid, used or expired/, "اللينك ده مش صالح أو اتستخدم أو انتهى: اطلب لينك جديد"],
  [/^Your account is waiting for approval/, "حسابك لسه مستني الموافقة"],
  [/^Turn on two-step codes first/, "شغّل التحقق بخطوتين الأول (قسم الأمان)"],
  [/^Wrong or already used code/, "الكود غلط أو اتستخدم: استنى الكود اللي بعده في التطبيق"],
  [/^That code doesn't match/, "الكود مش مطابق: اتأكد من ساعة الموبايل وجرب الكود اللي بعده"],
  [/^Withdrawals are locked for (\d+)/, "السحب مقفول $1 ساعة كمان بعد تغيير كلمة السر أو التحقق (للأمان)"],
  [/^Withdrawals work only when trading is live/, "السحب بيشتغل بس والتداول live"],
  [/^Enter a valid Solana address/, "اكتب عنوان سولانا صحيح"],
  [/^That is the trading wallet itself/, "ده عنوان محفظة التداول نفسها"],
  [/^You can send at most ([\d.]+) SOL/, "أقصى مبلغ تقدر تسحبه $1 SOL"],
  [/^Withdrawal failed/, "السحب فشل"],
  [/^The key was already shown once/, "المفتاح اتعرض قبل كده مرة"],
  [/^(entry per trade|trading budget|max loss) can be at most \$([\d,]+)/, "الحد الأقصى المسموح ليك $$$2"],
  [/^Telegram is not set up/, "تيليجرام مش متظبط على الموقع"],
  [/^Selling (\d+)% of (\S+)/, "جاري بيع $1% من $2"],
  [/^Already selling/, "البيع شغال بالفعل"],
  [/^Position is not open|^No such position/, "الصفقة دي مش مفتوحة"],
  [/^If this email is registered/, "لو الإيميل ده متسجل، لينك تغيير كلمة السر في الطريق."],
  [/^Two-step codes are already on/, "التحقق بخطوتين شغال بالفعل"],
];

function fill(s: string, vars?: Record<string, string | number>) {
  return vars ? s.replace(/\{(\w+)\}/g, (_, k) => String(vars[k] ?? `{${k}}`)) : s;
}

interface Ctx {
  lang: Lang;
  dir: "rtl" | "ltr";
  setLang: (l: Lang) => void;
  t: (key: Key, vars?: Record<string, string | number>) => string;
  srv: (message: string) => string;          // server message in the current language when known
  exit: (reason: string | null) => string;   // exit reason words
}

const I18n = createContext<Ctx | null>(null);

export function readLang(): Lang {
  try {
    const v = localStorage.getItem(KEY);
    return v === "en" ? "en" : "ar";
  } catch {
    return "ar";
  }
}

export function I18nProvider({ children, onChange }: { children: React.ReactNode; onChange?: (l: Lang) => void }) {
  const [lang, setLangState] = useState<Lang>("ar");
  useEffect(() => setLangState(readLang()), []);
  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try {
      localStorage.setItem(KEY, l);
    } catch {
      /* storage unavailable */
    }
    onChange?.(l);
  }, [onChange]);
  const value = useMemo<Ctx>(() => {
    const i = lang === "ar" ? 1 : 0;
    return {
      lang,
      dir: lang === "ar" ? "rtl" : "ltr",
      setLang,
      t: (key, vars) => fill(DICT[key][i], vars),
      srv: (m) => {
        if (lang !== "ar" || !m) return m;
        for (const [re, ar] of SERVER_AR) {
          const mm = m.match(re);
          if (mm) return ar.replace(/\$\$|\$(\d)/g, (x, n) => (x === "$$" ? "$" : (mm[Number(n)] ?? "")));
        }
        return m;
      },
      exit: (r) => {
        if (!r) return "";
        const k = `x.${r}` as Key;
        return k in DICT ? DICT[k][i] : r;
      },
    };
  }, [lang, setLang]);
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
  return (
    <I18n.Provider value={value}>
      <div dir={value.dir} lang={lang} className={lang === "ar" ? "font-arabic" : ""}>{children}</div>
    </I18n.Provider>
  );
}

export function useT(): Ctx {
  const c = useContext(I18n);
  if (!c) throw new Error("useT outside I18nProvider");
  return c;
}

export function LangSwitch() {
  const { lang, setLang, t } = useT();
  return (
    <button type="button" onClick={() => setLang(lang === "ar" ? "en" : "ar")}
      className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-raised hover:text-ink"
      aria-label={lang === "ar" ? "Switch to English" : "التحويل للعربية"}>
      <Languages className="size-3.5" /> {t("c.lang")}
    </button>
  );
}
