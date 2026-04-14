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
  explanation?: string | null;
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
  recommendation_evidence?: {
    weak_prompt_buckets?: Record<string, number>;
    page_coverage?: Record<string, boolean | number | string>;
    example_prompts?: string[];
    competitor?: string;
    distilled_category?: string;
  };
  brief: ContentBrief | null;
};

export type AuditDetail = AuditSummary & {
  industry: string | null;
  competitor_domains: string[];
  competitor_scores: CompetitorScore[];
  prompts: PromptRow[];
  recommendations: Recommendation[];
  weak_prompt_buckets?: Record<string, number>;
  score_components?: {
    average_prompt_score?: number;
    target_mention_rate?: number;
    weighted_bucket_score?: number;
    bucket_scores?: Record<string, number>;
    bucket_counts?: Record<string, number>;
  };
  crawl_summary?: Record<string, unknown>;
  error_message: string | null;
};
