import Link from "next/link";
import { DashboardFlow } from "./DashboardFlow";

type Props = {
  email: string;
};

export function SignedInHome({ email }: Props) {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-4 px-6 py-4">
          <div>
            <Link href="/" className="text-sm font-semibold text-white">
              AI Visibility Auditor
            </Link>
            <p className="text-xs text-slate-500">{email}</p>
          </div>
          <form action="/auth/signout" method="post">
            <button
              type="submit"
              className="text-sm text-slate-400 hover:text-white"
            >
              Sign out
            </button>
          </form>
        </div>
      </header>
      <div className="px-6">
        <DashboardFlow />
      </div>
    </div>
  );
}
