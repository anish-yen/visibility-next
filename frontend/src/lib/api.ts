import { createClient } from "@/lib/supabase/client";
import type { AuditDetail, AuditSummary } from "@/types/audit";

const base = () =>
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") || "http://localhost:8000";

async function readError(res: Response): Promise<string> {
  const j = await res.json().catch(() => ({}));
  const d = j.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d))
    return d.map((x: { msg?: string }) => x.msg).filter(Boolean).join(", ");
  return res.statusText || "Request failed";
}

async function getAccessTokenOrRedirectToLogin(): Promise<string> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;
  if (!token) {
    if (typeof window !== "undefined") {
      window.location.href = "/login";
    }
    throw new Error("Not authenticated");
  }
  return token;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await getAccessTokenOrRedirectToLogin();
  const res = await fetch(`${base()}${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!res.ok) throw new Error(await readError(res));
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function listAudits() {
  return apiFetch<AuditSummary[]>("/audits");
}

export function getAudit(id: string) {
  return apiFetch<AuditDetail>(`/audits/${id}`);
}

export function createAudit(body: {
  primary_domain: string;
  competitor_domains: string[];
  industry: string | null;
}) {
  return apiFetch<AuditSummary>("/audits", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function generateBrief(auditId: string, recommendationId: string) {
  return apiFetch<{ recommendation_id: string; brief: { title: string; body: string } }>(
    `/audits/${auditId}/recommendations/${recommendationId}/brief`,
    { method: "POST" },
  );
}
