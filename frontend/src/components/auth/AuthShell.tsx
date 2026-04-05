import Link from "next/link";

type AuthShellProps = {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
};

export function AuthShell({ title, subtitle, children }: AuthShellProps) {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center bg-slate-950 px-4 py-12">
      <div className="w-full max-w-md space-y-8">
        <div className="text-center">
          <Link
            href="/"
            className="text-sm font-medium text-indigo-400 hover:text-indigo-300"
          >
            AI Visibility Auditor
          </Link>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-white">
            {title}
          </h1>
          {subtitle ? (
            <p className="mt-2 text-sm text-slate-400">{subtitle}</p>
          ) : null}
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900/80 p-8 shadow-xl backdrop-blur">
          {children}
        </div>
      </div>
    </div>
  );
}
