import Link from "next/link";
import { SignedInHome } from "@/components/dashboard/SignedInHome";
import { createClient } from "@/lib/supabase/server";

const exampleResults = [
  {
    query: "best support widget that lets me reply from telegram",
    named: "4 competitors, not you",
  },
  {
    query: "applicant tracking for small teams with ai resume scoring",
    named: "5 competitors, not you",
  },
  {
    query: "simple invoice tool for freelancers",
    named: "6 competitors, not you",
  },
];

export default async function Home() {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (user?.email) {
    return <SignedInHome email={user.email} />;
  }

  return (
    <main className="min-h-screen bg-white font-[family-name:var(--font-geist-sans)] text-neutral-900 antialiased">
      <header className="sticky top-0 z-10 border-b border-neutral-200 bg-white/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-3.5">
          <span className="flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 font-[family-name:var(--font-geist-mono)] text-[11px] font-semibold text-white">
              0/8
            </span>
            <span className="text-[15px] font-semibold tracking-tight">
              zeteum
            </span>
          </span>
          <nav className="flex items-center gap-5">
            <Link
              href="/login"
              className="text-sm font-medium text-neutral-600 hover:text-neutral-900"
            >
              Log in
            </Link>
            <Link
              href="/signup"
              className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-700"
            >
              Sign up free
            </Link>
          </nav>
        </div>
      </header>

      <section className="mx-auto max-w-5xl px-6 pb-24 pt-24 sm:pt-28">
        <div className="max-w-2xl">
          <p className="mb-5 font-[family-name:var(--font-geist-mono)] text-[13px] font-medium tracking-wide text-neutral-500">
            AI VISIBILITY AUDIT
          </p>
          <h1 className="text-4xl font-bold leading-[1.1] tracking-tight sm:text-5xl">
            Does ChatGPT know your product exists?
          </h1>
          <p className="mt-6 max-w-xl text-[17px] leading-relaxed text-neutral-500">
            Sign up free, enter your product URL, and get your AI visibility
            score in minutes: the buyer questions, every answer, and the
            sources it cited instead of you.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-4">
            <Link
              href="/signup"
              className="rounded-lg bg-neutral-900 px-6 py-3 text-[15px] font-medium text-white hover:bg-neutral-700"
            >
              Sign up free
            </Link>
            <Link
              href="/login"
              className="rounded-lg border border-neutral-300 px-6 py-3 text-[15px] font-medium text-neutral-700 hover:bg-neutral-50"
            >
              Log in
            </Link>
            <p className="text-[13.5px] leading-snug text-neutral-400">
              free audit
              <br />
              no credit card
            </p>
          </div>
        </div>
      </section>

      <section className="border-t border-neutral-200 bg-neutral-50">
        <div className="mx-auto max-w-5xl px-6 py-20">
          <div className="max-w-2xl">
            <p className="font-[family-name:var(--font-geist-mono)] text-[13px] font-medium tracking-wide text-neutral-500">
              WHAT A SCORE LOOKS LIKE
            </p>
            <div className="mt-6 rounded-xl border border-neutral-200 bg-white p-6 sm:p-8">
              <div className="flex items-baseline gap-3">
                <span className="font-[family-name:var(--font-geist-mono)] text-4xl font-semibold tracking-tight text-indigo-600">
                  0/8
                </span>
                <span className="text-[15px] text-neutral-500">
                  buyer questions where ChatGPT named a competitor instead
                </span>
              </div>
              <ul className="mt-7 space-y-4 border-t border-neutral-100 pt-6">
                {exampleResults.map((r) => (
                  <li key={r.query} className="flex items-start gap-3 text-sm">
                    <span className="mt-0.5 font-[family-name:var(--font-geist-mono)] text-indigo-600">
                      &times;
                    </span>
                    <div>
                      <p className="font-[family-name:var(--font-geist-mono)] text-[13px] text-neutral-800">
                        &ldquo;{r.query}&rdquo;
                      </p>
                      <p className="mt-0.5 text-[13px] text-neutral-400">
                        {r.named}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
              <p className="mt-7 border-t border-neutral-100 pt-5 text-[13.5px] text-neutral-500">
                Most products score zero and have no idea. Yours takes minutes
                to check.
              </p>
            </div>
          </div>
        </div>
      </section>

      <section className="border-t border-neutral-200">
        <div className="mx-auto max-w-5xl px-6 py-20">
          <p className="font-[family-name:var(--font-geist-mono)] text-[13px] font-medium tracking-wide text-neutral-500">
            HOW IT WORKS
          </p>
          <div className="mt-8 grid gap-10 sm:grid-cols-3">
            <div>
              <p className="font-[family-name:var(--font-geist-mono)] text-sm font-semibold text-indigo-600">
                01
              </p>
              <p className="mt-2 text-[15px] font-semibold">Enter your URL</p>
              <p className="mt-1.5 text-sm leading-relaxed text-neutral-500">
                We crawl your product and figure out what it does and who
                it&rsquo;s for.
              </p>
            </div>
            <div>
              <p className="font-[family-name:var(--font-geist-mono)] text-sm font-semibold text-indigo-600">
                02
              </p>
              <p className="mt-2 text-[15px] font-semibold">
                We ask the buyer questions
              </p>
              <p className="mt-1.5 text-sm leading-relaxed text-neutral-500">
                The unbranded questions a real customer would type, not your
                brand name.
              </p>
            </div>
            <div>
              <p className="font-[family-name:var(--font-geist-mono)] text-sm font-semibold text-indigo-600">
                03
              </p>
              <p className="mt-2 text-[15px] font-semibold">
                You get the score
              </p>
              <p className="mt-1.5 text-sm leading-relaxed text-neutral-500">
                Every answer, who got named instead, and the exact sources it
                cited.
              </p>
            </div>
          </div>
        </div>
      </section>

      <footer className="border-t border-neutral-200">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-6 py-8">
          <span className="text-sm font-semibold tracking-tight">zeteum</span>
          <span className="text-[13px] text-neutral-400">
            &copy; 2026 Zeteum
          </span>
        </div>
      </footer>
    </main>
  );
}
