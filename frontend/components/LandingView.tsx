"use client";

import { ArrowLeft, ArrowRight, BellRing, ChevronDown, Gauge, KeyRound, Radar, ShieldCheck, Sparkles, Wallet } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { SirbLockup } from "@/components/brand/Logo";
import { api } from "@/lib/api";
import { I18nProvider, LangSwitch, useT } from "@/lib/i18n";
import type { PublicTerms, ScannerStatus } from "@/lib/types";

// key: [English, Arabic]
const L = {
  nav_scanner: ["Scanner", "الماسح"],
  nav_how: ["How it works", "إزاي بيشتغل"],
  nav_pricing: ["Pricing", "الأسعار"],
  nav_faq: ["FAQ", "الأسئلة"],
  signin: ["Sign in", "دخول"],
  start: ["Start free", "ابدأ مجاناً"],
  badge: ["Live on Solana", "مباشر · سولانا"],
  h1: ["Follow the smart flock.", "تابع السرب الذكي."],
  lead: [
    "Sirb finds the Solana wallets that keep winning and copies their trades in seconds — in your own wallet, with your budget, your loss limit, and your capital taken back at 2×.",
    "سرب بيلاقي المحافظ اللي بتكسب على سولانا، وبينسخ صفقاتها في ثواني بمحفظتك انت — بميزانيتك، وحد خسارتك، وسحب رأس المال عند الضعف.",
  ],
  cta_trial: ["Start {d} days free", "ابدأ {d} أيام مجاناً"],
  cta_how: ["How it works", "إزاي بيشتغل"],
  fine: ["Your wallet is yours · withdraw any time · crypto is high risk", "محفظتك ملكك · تسحب في أي وقت · الكريبتو مخاطرة عالية"],
  chip_t: ["The leader just bought", "القائد اشترى دلوقتي"],
  chip_s: ["The flock follows in seconds", "السرب بيتبعه في ثواني"],
  s_tokens: ["Tokens watched now", "عملات تحت المراقبة"],
  s_tokens_sub: ["security · liquidity · momentum", "أمان · سيولة · زخم"],
  s_signals: ["Signals · 24h", "إشارات آخر ٢٤ ساعة"],
  s_signals_sub: ["Alpha Score ≥ 75", "Alpha Score ≥ 75"],
  s_alpha: ["High-alpha tokens · 24h", "عملات قوية آخر ٢٤ ساعة"],
  s_alpha_sub: ["scored by our engine", "حسب تقييم المحرك"],
  s_cycle: ["Full scan every", "فحص كامل كل"],
  s_cycle_sub: ["last cycle {s}", "آخر دورة {s}"],
  how_t: ["How Sirb works", "إزاي سرب بيشتغل"],
  how1_t: ["The scanner finds the leaders", "الماسح بيلاقي القادة"],
  how1_p: ["Every minute we score thousands of tokens and follow the wallets that actually win — not the ones that make noise.",
    "كل دقيقة بنقيّم آلاف العملات ونتتبع المحافظ اللي بتكسب فعلاً، مش اللي بتعمل ضجة."],
  how2_t: ["The flock follows", "السرب بيتبعهم"],
  how2_p: ["The moment a leader buys, your wallet buys with the amount you set — and sells when the leader sells.",
    "أول ما القائد يشتري، محفظتك بتشتري بالمبلغ اللي انت حددته — وبتبيع لما هو يبيع."],
  how3_t: ["You stay in control", "انت متحكم"],
  how3_p: ["Daily loss limit, a budget for open trades, capital taken back at +100%, and a sell button any time.",
    "حد خسارة يومي، ميزانية للصفقات المفتوحة، سحب رأس المال عند ‎+100%‎، وزرار بيع في أي لحظة."],
  feat_t: ["Built for people who hate surprises", "معمول لناس مبتحبش المفاجآت"],
  f1_t: ["Your own wallet", "محفظتك انت"], f1_p: ["A wallet made for you; reveal its key once and import it in Phantom.", "محفظة معمولة ليك؛ تشوف مفتاحها مرة واحدة وتفتحها في Phantom."],
  f2_t: ["Safety screen", "فلتر أمان"], f2_p: ["Honeypots, mintable tokens and thin liquidity are skipped before any buy.", "العملات المفخخة والقابلة للطباعة والسيولة الضعيفة بتتعدّى قبل أي شراء."],
  f3_t: ["Telegram notes", "إشعارات تليجرام"], f3_p: ["Every buy and sell, a daily summary — in Arabic or English.", "كل شراء وبيع، وملخص يومي — بالعربي أو الإنجليزي."],
  f4_t: ["Two-step security", "حماية بخطوتين"], f4_p: ["Withdrawals and key reveals need your authenticator code.", "السحب وعرض المفتاح محتاجين كود التطبيق."],
  price_t: ["You pay when you win.", "بتدفع لما بتكسب."],
  price_p: ["A small fixed fee, and a share only of real profit above your best level so far (high-water mark). A losing month carries no share until it is won back.",
    "اشتراك صغير ثابت، ونسبة بس من الربح الحقيقي فوق أعلى مستوى وصلتله. الشهر الخسران مفيهوش نسبة لحد ما الخسارة تتعوض."],
  per_month: ["/ month", "/ شهرياً"],
  share: ["+ {p}% of the month's net profit above your high-water mark", "+ {p}٪ من صافي ربح الشهر فوق أعلى مستوى وصلتله"],
  li1: ["No share in a losing month until it's won back", "الشهر الخسران مفيهوش نسبة لحد ما تعوّض"],
  li2: ["Discount codes lower the fee", "أكواد الخصم بتنزل على الاشتراك"],
  li3: ["Telegram note for every trade", "إشعار تليجرام لكل صفقة"],
  li4: ["Your wallet, your key", "محفظتك ومفتاحك ملكك"],
  trial: ["First {d} days free", "أول {d} أيام مجاناً"],
  start_now: ["Start now", "ابدأ دلوقتي"],
  faq_t: ["Questions", "أسئلة"],
  q1: ["Who holds my money?", "مين ماسك فلوسي؟"], a1: ["You do. Sirb makes a wallet for you and trades from it; you can reveal its key, import it in Phantom and withdraw at any time.", "انت. سرب بيعملك محفظة وبيتداول منها؛ تقدر تشوف مفتاحها وتفتحها في Phantom وتسحب في أي وقت."],
  q2: ["Which wallets do I copy?", "بنسخ مين؟"], a2: ["Wallets chosen by their real results. They are never shown, so they can't be front-run.", "محافظ مختارة بنتايجها الحقيقية. مش بنعرضها علشان محدش يسبقها."],
  q3: ["Can I lose money?", "ممكن أخسر؟"], a3: ["Yes. Memecoins move fast and leaders lose too. Your daily loss limit and budget cap how much is at risk.", "أيوه. العملات دي سريعة جداً والقادة نفسهم بيخسروا. حد الخسارة اليومي والميزانية بيحددوا المعرّض للخطر."],
  q4: ["How is the fee taken?", "الرسوم بتتاخد إزاي؟"], a4: ["Once a month, in SOL from your trading wallet. You see the running bill on your page all month.", "مرة في الشهر، SOL من محفظة التداول. بتشوف الفاتورة أول بأول في صفحتك."],
  foot: ["© 2026 Sirb · Trading crypto carries high risk and you can lose your capital. Not investment advice.", "© 2026 سرب · التداول في العملات المشفرة فيه مخاطرة عالية وممكن تخسر رأس مالك. مش نصيحة استثمارية."],
} as const;
type K = keyof typeof L;

function useL() {
  const { lang, dir } = useT();
  const i = lang === "ar" ? 1 : 0;
  const t = (k: K, v?: Record<string, string | number>) =>
    v ? L[k][i].replace(/\{(\w+)\}/g, (_, x) => String(v[x] ?? "")) : L[k][i];
  return { t, lang, dir, ar: lang === "ar" };
}

/** Fades sections in as they scroll into view. */
function Reveal({ children, className = "", delay = 0 }: { children: React.ReactNode; className?: string; delay?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const [on, setOn] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setOn(true);
      return;
    }
    const io = new IntersectionObserver(([e]) => e.isIntersecting && (setOn(true), io.disconnect()), { threshold: 0.15 });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return (
    <div ref={ref} className={`${className} transition duration-700 ease-out ${on ? "translate-y-0 opacity-100" : "translate-y-4 opacity-0"}`}
      style={{ transitionDelay: `${delay}ms` }}>
      {children}
    </div>
  );
}

/** The hero flock: birds drift along a dashed flight path; the gold leader is in front. Follows the pointer a little. */
function FlockScene() {
  const { t } = useL();
  const ref = useRef<HTMLDivElement>(null);
  const [tilt, setTilt] = useState({ x: 0, y: 0 });
  const birds: [number, number, number, string, number][] = [
    // x, y, scale, color, opacity
    [380, 92, 1.25, "var(--color-gold)", 1],
    [330, 138, 1.1, "var(--color-mint)", 1],
    [412, 152, 1.1, "var(--color-mint)", 1],
    [280, 184, 0.95, "var(--color-mint)", 0.75],
    [446, 210, 0.95, "var(--color-mint)", 0.75],
    [228, 228, 0.8, "var(--color-mint)", 0.5],
    [178, 268, 0.65, "var(--color-mint)", 0.32],
  ];
  return (
    <div ref={ref}
      onPointerMove={(e) => {
        const r = ref.current?.getBoundingClientRect();
        if (!r) return;
        setTilt({ x: ((e.clientX - r.left) / r.width - 0.5) * 10, y: ((e.clientY - r.top) / r.height - 0.5) * 10 });
      }}
      onPointerLeave={() => setTilt({ x: 0, y: 0 })}
      className="relative aspect-[560/440] w-full overflow-hidden rounded-[28px] border border-line bg-[radial-gradient(120%_90%_at_70%_20%,rgb(43_228_167/0.18),transparent_60%),radial-gradient(80%_70%_at_10%_100%,rgb(79_179_255/0.10),transparent_60%)] bg-surface"
      dir="ltr">
      <div className="absolute inset-0 opacity-[0.35] [background-image:radial-gradient(rgb(255_255_255/0.08)_1px,transparent_1px)] [background-size:22px_22px]" />
      <svg viewBox="0 0 560 440" className="absolute inset-0 size-full transition-transform duration-300 ease-out"
        style={{ transform: `translate(${tilt.x}px, ${tilt.y}px)` }} aria-hidden>
        <path d="M40 380 C150 360 230 270 300 210 C360 160 430 120 500 70" stroke="var(--color-mint)" strokeOpacity="0.28"
          strokeWidth="2" strokeDasharray="6 8" fill="none" className="sirb-trail" />
        {birds.map(([x, y, s, c, o], i) => (
          <g key={i} transform={`translate(${x} ${y}) scale(${s})`}>
            <path d="M-24 10 L0 -10 L24 10" stroke={c} strokeOpacity={o} strokeWidth={8} strokeLinecap="round"
              strokeLinejoin="round" fill="none" className="sirb-drift" style={{ animationDelay: `${i * 0.35}s` }} />
          </g>
        ))}
      </svg>
      <div className="absolute start-5 bottom-5 animate-rise rounded-2xl border border-line bg-surface/90 px-4 py-3 backdrop-blur [animation-delay:400ms]"
        dir="auto">
        <div className="flex items-center gap-2 text-[13px] font-medium text-ink">
          <span className="relative inline-flex size-2"><span className="absolute inset-0 animate-pulse-ring rounded-full bg-gold" /><span className="relative size-2 rounded-full bg-gold" /></span>
          {t("chip_t")}
        </div>
        <div className="mt-0.5 text-xs text-muted">{t("chip_s")}</div>
      </div>
    </div>
  );
}

function Stat({ label, value, sub, delay }: { label: string; value: string; sub: string; delay: number }) {
  return (
    <Reveal delay={delay} className="card group p-5 transition hover:-translate-y-0.5 hover:border-mint/30">
      <div className="text-[11px] font-medium tracking-[0.08em] text-muted uppercase">{label}</div>
      <div className="num mt-2 text-[28px] leading-none font-medium text-mint">{value}</div>
      <div className="mt-2 text-xs text-faint">{sub}</div>
    </Reveal>
  );
}

function Landing() {
  const { t, ar } = useL();
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [terms, setTerms] = useState<PublicTerms | null>(null);
  useEffect(() => {
    api.status().then(setStatus).catch(() => undefined);
    api.terms().then(setTerms).catch(() => undefined);
    const id = setInterval(() => api.status().then(setStatus).catch(() => undefined), 30_000);
    return () => clearInterval(id);
  }, []);
  const fee = terms?.fee_usd ?? 59;
  const share = terms?.profit_pct ?? 15;
  const trial = terms?.trial_days ?? 7;
  const Arrow = ar ? ArrowLeft : ArrowRight;
  const n = (v: number | null | undefined, d = 0) => (v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: d }));
  const signup = "/login?signup=1";

  return (
    <div className="min-h-dvh">
      {/* nav */}
      <header className="glass sticky top-0 z-40">
        <div className="mx-auto flex max-w-[1240px] items-center gap-6 px-4 py-3 sm:px-6">
          <SirbLockup size={28} href="/" />
          <nav className="hidden flex-1 justify-center gap-7 text-sm text-muted md:flex">
            <a href="/scanner" className="hover:text-ink">{t("nav_scanner")}</a>
            <a href="#how" className="hover:text-ink">{t("nav_how")}</a>
            <a href="#pricing" className="hover:text-ink">{t("nav_pricing")}</a>
            <a href="#faq" className="hover:text-ink">{t("nav_faq")}</a>
          </nav>
          <div className="ms-auto flex items-center gap-2 md:ms-0">
            <LangSwitch />
            <a href="/login" className="hidden rounded-xl px-3 py-2 text-sm text-muted hover:text-ink sm:inline">{t("signin")}</a>
            <a href={signup} className="rounded-xl bg-mint px-4 py-2 text-sm font-semibold text-carbon transition hover:brightness-110">
              {t("start")}
            </a>
          </div>
        </div>
      </header>

      {/* hero */}
      <section className="mx-auto grid max-w-[1240px] items-center gap-12 px-4 pt-14 pb-16 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-24">
        <div className="animate-rise">
          <span className="inline-flex items-center gap-2 rounded-full bg-mint-soft px-3 py-1 text-xs font-medium text-mint">
            <span className="relative inline-flex size-1.5"><span className="absolute inset-0 animate-pulse-ring rounded-full bg-mint" /><span className="relative size-1.5 rounded-full bg-mint" /></span>
            {t("badge")}
          </span>
          <h1 className="mt-5 text-[44px] leading-[1.1] font-bold tracking-tight text-ink sm:text-[64px]">
            {t("h1")}
          </h1>
          <p className="mt-5 max-w-[600px] text-lg leading-relaxed text-muted">{t("lead")}</p>
          <div className="mt-8 flex flex-wrap gap-3">
            <a href={signup}
              className="group inline-flex items-center gap-2 rounded-xl bg-mint px-5 py-3 font-semibold text-carbon shadow-[0_0_0_0_rgb(43_228_167/0.4)] transition hover:shadow-[0_0_32px_-4px_rgb(43_228_167/0.55)]">
              {t("cta_trial", { d: trial })}
              <Arrow className="size-4 transition group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5" />
            </a>
            <a href="#how" className="rounded-xl border border-line bg-raised px-5 py-3 font-medium text-ink transition hover:border-line-strong">
              {t("cta_how")}
            </a>
          </div>
          <p className="mt-4 text-xs text-faint">{t("fine")}</p>
        </div>
        <div className="animate-rise [animation-delay:150ms]"><FlockScene /></div>
      </section>

      {/* live stats */}
      <section className="mx-auto grid max-w-[1240px] gap-4 px-4 pb-20 sm:grid-cols-2 sm:px-6 lg:grid-cols-4">
        <Stat delay={0} label={t("s_tokens")} value={n(status?.active_tokens)} sub={t("s_tokens_sub")} />
        <Stat delay={80} label={t("s_signals")} value={n(status?.signals_24h)} sub={t("s_signals_sub")} />
        <Stat delay={160} label={t("s_alpha")} value={n(status?.high_alpha_24h)} sub={t("s_alpha_sub")} />
        <Stat delay={240} label={t("s_cycle")} value={`${status?.scan_interval_seconds ?? 60}s`}
          sub={t("s_cycle_sub", { s: status?.last_cycle_seconds != null ? `${status.last_cycle_seconds.toFixed(1)}s` : "—" })} />
      </section>

      {/* how it works */}
      <section id="how" className="scroll-mt-20 border-y border-line bg-surface/60 py-20">
        <div className="mx-auto max-w-[1240px] px-4 sm:px-6">
          <Reveal><h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t("how_t")}</h2></Reveal>
          <div className="mt-10 grid gap-4 md:grid-cols-3">
            {([[Radar, "how1_t", "how1_p"], [Sparkles, "how2_t", "how2_p"], [Gauge, "how3_t", "how3_p"]] as const).map(([Icon, h, p], i) => (
              <Reveal key={h} delay={i * 100} className="group relative overflow-hidden rounded-2xl border border-line bg-carbon p-7 transition hover:-translate-y-1 hover:border-mint/30">
                <div className="absolute -top-10 -end-10 size-32 rounded-full bg-mint/10 blur-2xl transition group-hover:bg-mint/20" />
                <div className="flex items-center gap-3">
                  <span className="num text-2xl font-semibold text-mint">{i + 1}</span>
                  <Icon className="size-5 text-muted transition group-hover:text-mint" />
                </div>
                <h3 className="mt-4 text-xl font-semibold">{t(h)}</h3>
                <p className="mt-2 leading-relaxed text-muted">{t(p)}</p>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      {/* features */}
      <section className="mx-auto max-w-[1240px] px-4 py-20 sm:px-6">
        <Reveal><h2 className="max-w-[640px] text-3xl font-semibold tracking-tight sm:text-4xl">{t("feat_t")}</h2></Reveal>
        <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {([[Wallet, "f1_t", "f1_p"], [ShieldCheck, "f2_t", "f2_p"], [BellRing, "f3_t", "f3_p"], [KeyRound, "f4_t", "f4_p"]] as const).map(([Icon, h, p], i) => (
            <Reveal key={h} delay={i * 80} className="card p-6 transition hover:border-line-strong">
              <span className="grid size-10 place-items-center rounded-xl bg-mint-soft text-mint"><Icon className="size-5" /></span>
              <h3 className="mt-4 font-semibold">{t(h)}</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-muted">{t(p)}</p>
            </Reveal>
          ))}
        </div>
      </section>

      {/* pricing */}
      <section id="pricing" className="mx-auto grid max-w-[1240px] scroll-mt-20 items-center gap-12 px-4 py-24 sm:px-6 lg:grid-cols-2">
        <Reveal>
          <h2 className="text-3xl font-semibold tracking-tight sm:text-4xl">{t("price_t")}</h2>
          <p className="mt-4 max-w-[560px] text-lg leading-relaxed text-muted">{t("price_p")}</p>
        </Reveal>
        <Reveal delay={120} className="relative">
          <div className="absolute -inset-px rounded-[26px] bg-gradient-to-br from-mint/50 via-mint/10 to-gold/40 opacity-70 blur-[1px]" />
          <div className="relative rounded-[24px] bg-surface p-8">
            <span className="rounded-full bg-gold-soft px-3 py-1 text-xs font-medium text-gold">{t("trial", { d: trial })}</span>
            <div className="mt-5 flex items-baseline gap-2">
              <span className="num text-6xl font-semibold text-ink">${fee}</span>
              <span className="text-muted">{t("per_month")}</span>
            </div>
            <p className="mt-3 font-medium text-mint">{t("share", { p: share })}</p>
            <ul className="mt-6 space-y-3 text-sm text-muted">
              {(["li1", "li2", "li3", "li4"] as const).map((k) => (
                <li key={k} className="flex items-center gap-2.5">
                  <span className="grid size-5 place-items-center rounded-full bg-mint-soft text-[11px] text-mint">✓</span>{t(k)}
                </li>
              ))}
            </ul>
            <a href={signup} className="mt-8 block rounded-xl bg-mint px-5 py-3 text-center font-semibold text-carbon transition hover:brightness-110">
              {t("start_now")}
            </a>
          </div>
        </Reveal>
      </section>

      {/* faq */}
      <section id="faq" className="mx-auto max-w-[860px] scroll-mt-20 px-4 pb-24 sm:px-6">
        <Reveal><h2 className="text-3xl font-semibold tracking-tight">{t("faq_t")}</h2></Reveal>
        <div className="mt-8 divide-y divide-line rounded-2xl border border-line bg-surface">
          {(["1", "2", "3", "4"] as const).map((k) => (
            <details key={k} className="group px-6 py-5 [&_summary::-webkit-details-marker]:hidden">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 font-medium">
                {t(`q${k}` as K)}
                <ChevronDown className="size-4 shrink-0 text-muted transition group-open:rotate-180" />
              </summary>
              <p className="mt-3 leading-relaxed text-muted">{t(`a${k}` as K)}</p>
            </details>
          ))}
        </div>
      </section>

      <footer className="border-t border-line">
        <div className="mx-auto flex max-w-[1240px] flex-col items-start gap-4 px-4 py-10 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <SirbLockup size={24} />
          <p className="max-w-[720px] text-xs leading-relaxed text-faint">{t("foot")}</p>
        </div>
      </footer>
    </div>
  );
}

export function LandingView() {
  return (
    <I18nProvider>
      <Landing />
    </I18nProvider>
  );
}
