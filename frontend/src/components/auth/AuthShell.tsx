import Link from "next/link";

type AuthShellProps = {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
};

export function AuthShell({ title, subtitle, children }: AuthShellProps) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-white px-4 py-12 font-[family-name:var(--font-geist-sans)] text-neutral-900 antialiased">
      <div className="w-full max-w-md space-y-8">
        <div className="text-center">
          <Link href="/" className="inline-flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-600 font-[family-name:var(--font-geist-mono)] text-[11px] font-semibold text-white">
              0/8
            </span>
            <span className="text-[15px] font-semibold tracking-tight">
              zeteum
            </span>
          </Link>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-neutral-900">
            {title}
          </h1>
          {subtitle ? (
            <p className="mt-2 text-sm text-neutral-500">{subtitle}</p>
          ) : null}
        </div>
        <div className="rounded-xl border border-neutral-200 bg-neutral-50 p-8 shadow-sm">
          {children}
        </div>
      </div>
    </div>
  );
}
