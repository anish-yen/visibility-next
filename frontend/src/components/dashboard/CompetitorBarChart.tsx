"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CompetitorScore } from "@/types/audit";

type Props = {
  scores: CompetitorScore[];
};

export function CompetitorBarChart({ scores }: Props) {
  const data = scores.map((c) => {
    const full = c.label || c.domain;
    const name = full.length > 20 ? `${full.slice(0, 20)}…` : full;
    return {
      name,
      full,
      score: Math.round(c.score * 10) / 10,
    };
  });

  if (data.length === 0) {
    return (
      <p className="text-sm text-slate-500">No competitor data for this audit.</p>
    );
  }

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" vertical={false} />
          <XAxis
            dataKey="name"
            tick={{ fill: "#94a3b8", fontSize: 11 }}
            axisLine={{ stroke: "#475569" }}
            tickLine={false}
          />
          <YAxis
            domain={[0, 100]}
            tick={{ fill: "#94a3b8", fontSize: 11 }}
            axisLine={{ stroke: "#475569" }}
            tickLine={false}
            width={32}
          />
          <Tooltip
            contentStyle={{
              backgroundColor: "#0f172a",
              border: "1px solid #334155",
              borderRadius: "8px",
            }}
            labelStyle={{ color: "#e2e8f0" }}
            formatter={(value: number) => [`${value}`, "Simulated score"]}
            labelFormatter={(_, payload) =>
              (payload?.[0]?.payload as { full?: string })?.full || ""
            }
          />
          <Bar dataKey="score" fill="#6366f1" radius={[6, 6, 0, 0]} maxBarSize={48} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
