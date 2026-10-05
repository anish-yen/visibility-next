import Link from "next/link";
import { DashboardFlow } from "./DashboardFlow";

type Props = {
  email: string;
};

export function SignedInHome({ email }: Props) {
  return (
    <div className="min-h-screen bg-white font-[family-name:var(--font-geist-sans)] text-neutral-900 antialiased">
      <header className="sticky top-0 z-10 border-b border-neutral-200 bg-white/85 backdrop-blur-md">
        <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-6 py-3.5">
          <Link href="/" className="flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 font-[family-name:var(--font-geist-mono)] text-[11px] font-semibold text-white">
              0/8
            </span>
            <span className="text-[15px] font-semibold tracking-tight">
              zeteum
            </span>
          </Link>
          <div className="flex items-center gap-5">
            <p className="hidden text-[13px] text-neutral-500 sm:block">
              {email}
            </p>
            <form action="/auth/signout" method="post">
              <button
                type="submit"
                className="text-sm font-medium text-neutral-600 hover:text-neutral-900"
              >
                Sign out
              </button>
            </form>
          </div>
        </div>
      </header>
      <div className="px-6">
        <DashboardFlow />
      </div>
    </div>
  );
}
