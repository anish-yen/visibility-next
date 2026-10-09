"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createClient } from "@/lib/supabase/client";
import {
  generateBrief,
  getAudit,
  listAudits,
  startCheckout,
} from "@/lib/api";
import type { AuditDetail } from "@/types/audit";
import { CompetitorBarChart } from "./CompetitorBarChart";

const WEAK_BUCKET_THRESHOLD = 0.65;

const STAGE_FLOW = [
  { key: "crawling", label: "Crawling" },
  { key: "generating_prompts", label: "Generating prompts" },
  { key: "evaluating", label: "Evaluating" },
  { key: "analyzing", label: "Analyzing" },
  { key: "completed", label: "Done" },
] as const;

type View = "create" | "awaiting-payment" | "running" | "dashboard";

function stageIndex(stage: string): number {
  const i = STAGE_FLOW.findIndex((s) => s.key === stage);
  return i < 0 ? 0 : i;
}

function formatBucketLabel(bucket: string): string {
  return bucket.replace(/_/g, " ");
}

export function DashboardFlow() {
  const [sessionChecked, setSessionChecked] = useState(false);
  const [bootError, setBootError] = useState<string | null>(null);

  const [checkoutParam, setCheckoutParam] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("checkout");
  });
  const [paymentTimedOut, setPaymentTimedOut] = useState(false);
  // Snapshot, not state: once the awaiting-payment poll resolves it sets
  // view directly, so the one-shot boot-list fetch below should stay
  // skipped for the rest of this mount rather than re-firing (and flashing
  // a loading screen over the just-resolved dashboard/running view).
  const skipBootListFetch = useRef(checkoutParam === "success");

  // The normal boot-list fetch is skipped while checkoutParam === "success"
  // (the awaiting-payment poll effect takes over instead), so there's no
  // list-loading spinner to wait out in that case.
  const [loadingList, setLoadingList] = useState(() => checkoutParam !== "success");

  const [view, setView] = useState<View>(
    checkoutParam === "success" ? "awaiting-payment" : "create",
  );
  const [activeAuditId, setActiveAuditId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AuditDetail | null>(null);
  const [wantsNewAudit, setWantsNewAudit] = useState(false);

  const [primary, setPrimary] = useState("");
  const [c1, setC1] = useState("");
  const [c2, setC2] = useState("");
  const [c3, setC3] = useState("");
  const [industry, setIndustry] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const [selectedRecId, setSelectedRecId] = useState<string | null>(null);
  const [briefLoading, setBriefLoading] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);

  useEffect(() => {
    const supabase = createClient();
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!session?.access_token) {
        window.location.href = "/login";
        return;
      }
      setSessionChecked(true);
    });
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      const token = session?.access_token;
      if (!token) {
        window.location.href = "/login";
      }
    });
    return () => subscription.unsubscribe();
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.has("checkout")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("checkout");
      url.searchParams.delete("session_id");
      window.history.replaceState({}, "", url.toString());
    }
  }, []);

  useEffect(() => {
    if (!sessionChecked) {
      return;
    }
    // While awaiting-payment is resolving via its own poll below, don't let
    // this one-shot list fetch race it back to the "create" view.
    if (skipBootListFetch.current) {
      return;
    }

    let cancelled = false;

    (async () => {
      setLoadingList(true);
      setBootError(null);
      try {
        const list = await listAudits();
        if (cancelled) return;

        if (wantsNewAudit) {
          setView("create");
          setActiveAuditId(null);
          setDetail(null);
        } else if (list.length === 0) {
          setView("create");
          setActiveAuditId(null);
          setDetail(null);
        } else {
          const latest = list[0];
          setActiveAuditId(latest.id);
          if (latest.status === "running" || latest.status === "failed") {
            setView("running");
          } else {
            setView("dashboard");
          }
          const d = await getAudit(latest.id);
          if (!cancelled) setDetail(d);
        }
      } catch (e) {
        if (!cancelled) {
          setBootError(e instanceof Error ? e.message : "Failed to load audits");
          setView("create");
        }
      } finally {
        if (!cancelled) setLoadingList(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [sessionChecked, wantsNewAudit]);

  useEffect(() => {
    if (!sessionChecked || view !== "awaiting-payment") return;

    let cancelled = false;
    let attempts = 0;
    const maxAttempts = 20; // ~40s at 2s/attempt - the Stripe webhook usually lands in a couple of seconds

    async function tick() {
      attempts += 1;
      try {
        const list = await listAudits();
        if (cancelled) return;
        if (list.length > 0) {
          const latest = list[0];
          setActiveAuditId(latest.id);
          setCheckoutParam(null);
          setView(latest.status === "completed" ? "dashboard" : "running");
          const d = await getAudit(latest.id);
          if (!cancelled) setDetail(d);
          clearInterval(intervalId);
          return;
        }
      } catch {
        /* keep polling - a transient failure here shouldn't give up early */
      }
      if (attempts >= maxAttempts) {
        setPaymentTimedOut(true);
        clearInterval(intervalId);
      }
    }

    const intervalId = setInterval(() => void tick(), 2000);
    void tick();
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [sessionChecked, view]);

  useEffect(() => {
    if (!sessionChecked || !activeAuditId || view !== "running") return;

    async function tick() {
      try {
        const d = await getAudit(activeAuditId!);
        setDetail(d);
        if (d.status === "completed") {
          setView("dashboard");
          clearInterval(intervalId);
        }
        if (d.status === "failed") clearInterval(intervalId);
      } catch {
        /* keep last good detail */
      }
    }

    const intervalId = setInterval(() => void tick(), 3000);
    void tick();
    return () => clearInterval(intervalId);
  }, [sessionChecked, activeAuditId, view]);

  useEffect(() => {
    if (!sessionChecked || !activeAuditId || view !== "dashboard") return;
    let cancelled = false;
    (async () => {
      try {
        const d = await getAudit(activeAuditId!);
        if (!cancelled) setDetail(d);
      } catch {
        /* ignore */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionChecked, activeAuditId, view]);

  const sortedRecs = useMemo(() => {
    if (!detail?.recommendations) return [];
    return [...detail.recommendations].sort(
      (a, b) => b.priority_score - a.priority_score,
    );
  }, [detail]);

  async function onSubmitAudit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    const domain = primary.trim();
    if (!domain) {
      setFormError("Primary domain is required.");
      return;
    }
    const competitors = [c1, c2, c3].map((s) => s.trim()).filter(Boolean);
    setSubmitting(true);
    try {
      const res = await startCheckout({
        primary_domain: domain,
        competitor_domains: competitors,
        industry: industry.trim() || null,
      });
      if (res.bypassed && res.audit) {
        setWantsNewAudit(false);
        setActiveAuditId(res.audit.id);
        setView("running");
        const d = await getAudit(res.audit.id);
        setDetail(d);
      } else if (res.checkout_url) {
        window.location.href = res.checkout_url;
      } else {
        setFormError("Could not start checkout.");
      }
    } catch (err) {
      setFormError(err instanceof Error ? err.message : "Could not start audit");
    } finally {
      setSubmitting(false);
    }
  }

  async function onSelectRecommendation(recId: string) {
    if (!activeAuditId) return;
    setSelectedRecId(recId);
    setBriefError(null);
    const rec = detail?.recommendations.find((r) => r.id === recId);
    if (rec?.brief) return;
    setBriefLoading(true);
    try {
      const res = await generateBrief(activeAuditId, recId);
      setDetail((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          recommendations: prev.recommendations.map((r) =>
            r.id === recId ? { ...r, brief: res.brief } : r,
          ),
        };
      });
    } catch (e) {
      setBriefError(e instanceof Error ? e.message : "Brief failed");
    } finally {
      setBriefLoading(false);
    }
  }

  const selectedRec = sortedRecs.find((r) => r.id === selectedRecId);
  const weakBucketChips = useMemo(() => {
    if (!detail?.weak_prompt_buckets) return [];
    return Object.entries(detail.weak_prompt_buckets)
      .filter(([, value]) => value < WEAK_BUCKET_THRESHOLD)
      .sort((a, b) => a[1] - b[1])
      .slice(0, 3);
  }, [detail]);

  const unbrandedPrompts = useMemo(
    () => detail?.prompts.filter((p) => !p.names_brand) ?? [],
    [detail]
  );
  const brandedPrompts = useMemo(
    () => detail?.prompts.filter((p) => p.names_brand) ?? [],
    [detail]
  );

  async function cancelNewAudit() {
    setWantsNewAudit(false);
  }

  if (!sessionChecked) {
    return (
      <div className="flex justify-center py-24 text-neutral-500">
        Loading session…
      </div>
    );
  }

  if (loadingList) {
    return (
      <div className="flex justify-center py-24 text-neutral-500">
        Loading audits…
      </div>
    );
  }

  if (bootError) {
    return (
      <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        {bootError}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-10 pb-20 pt-8">
      {view === "awaiting-payment" ? (
        <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8 text-center">
          <h2 className="text-xl font-semibold text-neutral-900">Payment confirmed</h2>
          <p className="mt-2 text-sm text-neutral-500">
            Starting your audit now — this usually takes a few seconds.
          </p>
          {paymentTimedOut ? (
            <div className="mt-6 space-y-3">
              <p className="text-sm text-amber-700">
                Still waiting to hear back from payment confirmation. If this
                persists, refresh the page.
              </p>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-semibold text-white hover:bg-neutral-700"
              >
                Refresh
              </button>
            </div>
          ) : (
            <p className="mt-6 text-sm text-neutral-500">Checking…</p>
          )}
        </section>
      ) : null}

      {view === "create" ? (
        <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
          <h2 className="text-xl font-semibold text-neutral-900">Run an audit</h2>
          <p className="mt-1 text-sm text-neutral-500">
            Enter your site and up to three competitors. We will crawl, generate
            prompts, and simulate AI-style visibility (demo pipeline).
          </p>
          <form onSubmit={onSubmitAudit} className="mt-8 space-y-5">
            {checkoutParam === "cancelled" ? (
              <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900">
                Checkout was cancelled — no charge was made. You can try again
                below.
              </p>
            ) : null}
            {formError ? (
              <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
                {formError}
              </p>
            ) : null}
            <div>
              <label className="block text-sm font-medium text-neutral-700">
                Primary domain <span className="text-red-700">*</span>
              </label>
              <input
                required
                value={primary}
                onChange={(e) => setPrimary(e.target.value)}
                placeholder="example.com"
                className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-neutral-900 placeholder:text-neutral-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <label className="block text-sm font-medium text-neutral-700">
                  Competitor 1
                </label>
                <input
                  value={c1}
                  onChange={(e) => setC1(e.target.value)}
                  placeholder="competitor.com"
                  className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-neutral-900 placeholder:text-neutral-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-neutral-700">
                  Competitor 2
                </label>
                <input
                  value={c2}
                  onChange={(e) => setC2(e.target.value)}
                  placeholder="optional"
                  className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-neutral-900 placeholder:text-neutral-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-neutral-700">
                  Competitor 3
                </label>
                <input
                  value={c3}
                  onChange={(e) => setC3(e.target.value)}
                  placeholder="optional"
                  className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-neutral-900 placeholder:text-neutral-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                />
              </div>
            </div>
            <div>
              <label className="block text-sm font-medium text-neutral-700">
                Industry (optional)
              </label>
              <input
                value={industry}
                onChange={(e) => setIndustry(e.target.value)}
                placeholder="e.g. payroll software, dental practices"
                className="mt-1 w-full rounded-lg border border-neutral-300 bg-white px-3 py-2 text-neutral-900 placeholder:text-neutral-400 focus:border-indigo-600 focus:outline-none focus:ring-1 focus:ring-indigo-500"
              />
            </div>
            <div className="flex flex-wrap gap-3 pt-2">
              <button
                type="submit"
                disabled={submitting}
                className="rounded-lg bg-neutral-900 px-5 py-2.5 text-sm font-semibold text-white hover:bg-neutral-700 disabled:opacity-50"
              >
                {submitting ? "Starting…" : "Run audit"}
              </button>
              {wantsNewAudit ? (
                <button
                  type="button"
                  onClick={cancelNewAudit}
                  className="rounded-lg border border-neutral-300 px-5 py-2.5 text-sm text-neutral-700 hover:bg-neutral-100"
                >
                  Cancel
                </button>
              ) : null}
            </div>
          </form>
        </section>
      ) : null}

      {view === "running" && detail ? (
        <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
          <h2 className="text-xl font-semibold text-neutral-900">Audit in progress</h2>
          <p className="mt-1 font-mono text-sm text-indigo-700">
            {detail.primary_domain}
          </p>
          {detail.status === "failed" ? (
            <p className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-700">
              {detail.error_message || "Audit failed."}
            </p>
          ) : null}

          <div className="mt-8">
            <div className="mb-2 flex justify-between text-xs text-neutral-500">
              <span>Progress</span>
              <span>{detail.progress_percent}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-neutral-100">
              <div
                className="h-full rounded-full bg-indigo-600 transition-all duration-500"
                style={{ width: `${detail.progress_percent}%` }}
              />
            </div>
          </div>

          <ol className="mt-10 grid gap-3 sm:grid-cols-5">
            {STAGE_FLOW.map((s, idx) => {
              const activeIdx = stageIndex(detail.stage);
              const done =
                detail.status !== "failed" &&
                (idx < activeIdx || detail.stage === "completed");
              const current =
                detail.status !== "failed" && detail.stage === s.key;
              return (
                <li
                  key={s.key}
                  className={`rounded-lg border px-3 py-3 text-center text-xs font-medium ${
                    detail.status === "failed"
                      ? "border-neutral-200 bg-neutral-50 text-neutral-400"
                      : done
                        ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                        : current
                          ? "border-indigo-600 bg-indigo-50 text-indigo-700"
                          : "border-neutral-200 bg-neutral-50 text-neutral-500"
                  }`}
                >
                  {s.label}
                </li>
              );
            })}
          </ol>
          {detail.status !== "failed" ? (
            <p className="mt-6 text-center text-sm text-neutral-500">
              Checking every 3 seconds…
            </p>
          ) : (
            <div className="mt-6 space-y-4 text-center">
              <p className="text-sm text-neutral-500">
                This run stopped. You can start a new audit below.
              </p>
              <button
                type="button"
                onClick={() => {
                  setWantsNewAudit(true);
                  setPrimary("");
                  setC1("");
                  setC2("");
                  setC3("");
                  setIndustry("");
                  setFormError(null);
                  setView("create");
                  setDetail(null);
                  setActiveAuditId(null);
                }}
                className="rounded-lg bg-neutral-900 px-4 py-2 text-sm font-semibold text-white hover:bg-neutral-700"
              >
                Start new audit
              </button>
            </div>
          )}
        </section>
      ) : null}

      {view === "dashboard" ? (
        detail ? (
          <div className="space-y-10">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <h2 className="text-2xl font-semibold text-neutral-900">Results</h2>
                <p className="mt-1 font-mono text-sm text-neutral-500">
                  {detail.primary_domain}
                  {detail.industry ? (
                    <span className="text-neutral-500"> · {detail.industry}</span>
                  ) : null}
                </p>
              </div>
              <button
                type="button"
                onClick={() => {
                  setWantsNewAudit(true);
                  setPrimary("");
                  setC1("");
                  setC2("");
                  setC3("");
                  setIndustry("");
                  setSelectedRecId(null);
                  setBriefError(null);
                  setView("create");
                  setDetail(null);
                  setActiveAuditId(null);
                }}
                className="rounded-lg border border-neutral-300 px-4 py-2 text-sm font-medium text-neutral-800 hover:bg-neutral-100"
              >
                New audit
              </button>
            </div>

            <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-neutral-500">
                    Visibility score
                  </p>
                  <p className="mt-2 text-5xl font-semibold tracking-tight text-neutral-900">
                    {detail.visibility_score != null
                      ? Math.round(detail.visibility_score)
                      : "—"}
                    <span className="text-2xl text-neutral-500">/100</span>
                  </p>
                </div>
                {weakBucketChips.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {weakBucketChips.map(([bucket, value]) => (
                      <span
                        key={bucket}
                        className="rounded-full border border-neutral-300 bg-neutral-50 px-3 py-1 text-xs text-neutral-700"
                      >
                        Weak {formatBucketLabel(bucket)} · {value.toFixed(2)}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              <p className="mt-2 max-w-xl text-sm text-neutral-500">
                Measured from real Gemini answers to each buyer prompt, graded for
                brand mentions. Not a live ChatGPT or Perplexity measurement.
              </p>
              {detail.score_components?.bucket_scores ? (
                <p className="mt-3 text-xs text-neutral-500">
                  Built from prompt-level mention strength, competitor presence,
                  intent fit, and bucket-level performance.
                </p>
              ) : null}
            </section>

            <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
              <h3 className="text-lg font-semibold text-neutral-900">
                Competitor comparison
              </h3>
              <p className="mt-1 text-sm text-neutral-500">
                Visibility scores by entity
              </p>
              <div className="mt-6">
                <CompetitorBarChart scores={detail.competitor_scores} />
              </div>
            </section>

            <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
              <h3 className="text-lg font-semibold text-neutral-900">Prompts</h3>
              <p className="mt-1 text-sm text-neutral-500">
                Unbranded buyer-style prompts and whether your brand appeared in the real AI answer. These are the only prompts counted toward your visibility score.
              </p>
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-neutral-200 text-neutral-500">
                      <th className="pb-3 pr-4 font-medium">Prompt</th>
                      <th className="pb-3 pr-4 font-medium">Type</th>
                      <th className="pb-3 pr-4 font-medium">Mentioned</th>
                      <th className="pb-3 font-medium">Score</th>
                    </tr>
                  </thead>
                  <tbody className="text-neutral-700">
                    {unbrandedPrompts.map((p) => (
                      <tr key={p.id} className="border-b border-neutral-200">
                        <td className="py-3 pr-4 align-top">
                          <div className="max-w-xl leading-relaxed text-neutral-800">
                            {p.text}
                          </div>
                          {p.explanation ? (
                            <div className="mt-1 text-xs text-neutral-500">
                              {p.explanation}
                            </div>
                          ) : null}
                        </td>
                        <td className="py-3 pr-4 align-top">
                          <span className="rounded-full border border-neutral-300 bg-neutral-50 px-2.5 py-1 text-xs text-neutral-700 capitalize">
                            {formatBucketLabel(p.intent || "general")}
                          </span>
                        </td>
                        <td className="py-3 pr-4">
                          <span
                            className={
                              p.mentioned ? "text-emerald-600" : "text-neutral-500"
                            }
                          >
                            {p.mentioned ? "Yes" : "No"}
                          </span>
                        </td>
                        <td className="py-3 tabular-nums">
                          <span className="font-medium text-neutral-800">
                            {p.score.toFixed(2)}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>

            {brandedPrompts.length > 0 ? (
              <section className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8 opacity-80">
                <h3 className="text-lg font-semibold text-neutral-900">
                  Branded prompts (reference only)
                </h3>
                <p className="mt-1 text-sm text-neutral-500">
                  These prompts name your brand or a competitor directly, so a mention is close to guaranteed. Shown for reference only — excluded from your visibility score.
                </p>
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-left text-sm">
                    <thead>
                      <tr className="border-b border-neutral-200 text-neutral-500">
                        <th className="pb-3 pr-4 font-medium">Prompt</th>
                        <th className="pb-3 pr-4 font-medium">Type</th>
                        <th className="pb-3 pr-4 font-medium">Mentioned</th>
                        <th className="pb-3 font-medium">Score</th>
                      </tr>
                    </thead>
                    <tbody className="text-neutral-700">
                      {brandedPrompts.map((p) => (
                        <tr key={p.id} className="border-b border-neutral-200">
                          <td className="py-3 pr-4 align-top">
                            <div className="max-w-xl leading-relaxed text-neutral-800">
                              {p.text}
                            </div>
                            {p.explanation ? (
                              <div className="mt-1 text-xs text-neutral-500">
                                {p.explanation}
                              </div>
                            ) : null}
                          </td>
                          <td className="py-3 pr-4 align-top">
                            <span className="rounded-full border border-neutral-300 bg-neutral-50 px-2.5 py-1 text-xs text-neutral-700 capitalize">
                              {formatBucketLabel(p.intent || "general")}
                            </span>
                          </td>
                          <td className="py-3 pr-4">
                            <span
                              className={
                                p.mentioned ? "text-emerald-600" : "text-neutral-500"
                              }
                            >
                              {p.mentioned ? "Yes" : "No"}
                            </span>
                          </td>
                          <td className="py-3 tabular-nums">
                            <span className="font-medium text-neutral-800">
                              {p.score.toFixed(2)}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            ) : null}

            <section className="grid gap-8 lg:grid-cols-2">
              <div className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
                <h3 className="text-lg font-semibold text-neutral-900">Recommendations</h3>
                <p className="mt-1 text-sm text-neutral-500">
                  Ranked by priority score · click to load content brief
                </p>
                <ul className="mt-4 space-y-2">
                  {sortedRecs.map((r) => (
                    <li key={r.id}>
                      <button
                        type="button"
                        onClick={() => void onSelectRecommendation(r.id)}
                        className={`w-full rounded-lg border px-4 py-3 text-left text-sm transition ${
                          selectedRecId === r.id
                            ? "border-indigo-600 bg-indigo-50 text-neutral-900"
                            : "border-neutral-200 bg-neutral-50 text-neutral-700 hover:border-neutral-400"
                        }`}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <span className="font-medium">{r.title}</span>
                          <span className="shrink-0 whitespace-nowrap rounded-full border border-neutral-300 px-2 py-0.5 text-[11px] text-neutral-500">
                            Priority {r.priority_score.toFixed(2)}
                          </span>
                        </div>
                        <span className="mt-2 block text-xs leading-relaxed text-neutral-500">
                          {r.rationale}
                        </span>
                        {r.recommendation_evidence?.weak_prompt_buckets ? (
                          <div className="mt-2 flex flex-wrap gap-2">
                            {Object.entries(r.recommendation_evidence.weak_prompt_buckets)
                              .slice(0, 2)
                              .map(([bucket, value]) => (
                                <span
                                  key={bucket}
                                  className="rounded-full border border-neutral-300 bg-neutral-50 px-2 py-0.5 text-[11px] text-neutral-500"
                                >
                                  {formatBucketLabel(bucket)} {value.toFixed(2)}
                                </span>
                              ))}
                          </div>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-2xl border border-neutral-200 bg-neutral-50 p-8">
                <h3 className="text-lg font-semibold text-neutral-900">Content brief</h3>
                {!selectedRecId ? (
                  <p className="mt-4 text-sm text-neutral-500">
                    Select a recommendation to view or generate a brief.
                  </p>
                ) : briefLoading ? (
                  <p className="mt-4 text-sm text-neutral-500">Generating brief…</p>
                ) : briefError ? (
                  <p className="mt-4 text-sm text-red-700">{briefError}</p>
                ) : selectedRec?.brief ? (
                  <article className="mt-4 space-y-4">
                    <h4 className="text-base font-semibold text-indigo-700">
                      {selectedRec.brief.title}
                    </h4>
                    <div className="rounded-xl border border-neutral-200 bg-neutral-50 p-4">
                      {selectedRec.recommendation_evidence?.example_prompts?.length ? (
                        <div className="mb-4">
                          <p className="text-xs font-medium uppercase tracking-wide text-neutral-500">
                            Prompt evidence
                          </p>
                          <div className="mt-2 flex flex-wrap gap-2">
                            {selectedRec.recommendation_evidence.example_prompts
                              .slice(0, 2)
                              .map((prompt) => (
                                <span
                                  key={prompt}
                                  className="rounded-full border border-neutral-300 bg-neutral-50 px-3 py-1 text-xs text-neutral-700"
                                >
                                  {prompt}
                                </span>
                              ))}
                          </div>
                        </div>
                      ) : null}
                      <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-neutral-700">
                      {selectedRec.brief.body}
                      </pre>
                    </div>
                  </article>
                ) : (
                  <p className="mt-4 text-sm text-neutral-500">Loading…</p>
                )}
              </div>
            </section>
          </div>
        ) : (
          <div className="py-16 text-center text-neutral-500">Loading results…</div>
        )
      ) : null}
    </div>
  );
}
