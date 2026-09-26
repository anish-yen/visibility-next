import Link from "next/link";
import { SignedInHome } from "@/components/dashboard/SignedInHome";
import { createClient } from "@/lib/supabase/server";

export default async function Home() {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (user?.email) {
    return <SignedInHome email={user.email} />;
  }

  return (
    <main className="min-h-screen bg-slate-950 px-6 py-16 text-slate-100">
      <div className="mx-auto max-w-2xl space-y-8">
        <p className="text-sm font-medium text-indigo-400">
          Zeteum
        </p>
        <h1 className="text-3xl font-semibold tracking-tight">
          Measure how you show up in AI-style answers
        </h1>
        <p className="text-slate-400">
          Sign up free, enter your product URL, and get your AI visibility
          score: the 8 buyer questions, every answer, and the sources ChatGPT
          cited instead of you.
        </p>
        <div className="flex flex-wrap gap-3">
          <Link
            href="/login"
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500"
          >
            Log in
          </Link>
          <Link
            href="/signup"
            className="rounded-lg border border-slate-700 px-4 py-2 text-sm font-medium text-slate-200 hover:bg-slate-800"
          >
            Sign up
          </Link>
        </div>
      </div>
    </main>
  );
}
