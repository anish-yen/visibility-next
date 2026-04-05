export type AuditSummary = {
  id: string;
  primary_domain: string;
  status: string;
  stage: string;
  progress_percent: number;
  visibility_score: number | null;
  created_at: string;
};

export type CompetitorScore = {
  domain: string;
  score: number;
  label?: string | null;
};

export type PromptRow = {
  id: string;
  text: string;
  mentioned: boolean;
  score: number;
  intent?: string | null;
  answer_summary?: string | null;
  competitor_mentions?: string[] | null;
};

export type ContentBrief = {
  title: string;
  body: string;
};

export type Recommendation = {
  id: string;
  title: string;
  rationale: string;
  priority_score: number;
  brief: ContentBrief | null;
};

export type AuditDetail = AuditSummary & {
  industry: string | null;
  competitor_domains: string[];
  competitor_scores: CompetitorScore[];
  prompts: PromptRow[];
  recommendations: Recommendation[];
  error_message: string | null;
};
