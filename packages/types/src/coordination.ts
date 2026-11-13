/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

export type TAcceptanceCriterion = { id: string; description: string };
export type TContractStatus =
  | "draft"
  | "ready"
  | "in_progress"
  | "awaiting_input"
  | "review"
  | "merging"
  | "merged"
  | "cancelled";
export type TWorkItemContract = {
  id: string;
  issue: string;
  project: string;
  owner: string;
  owner_ids: string[];
  release: string | null;
  requirements: string;
  definition_of_done: TAcceptanceCriterion[];
  spec_revision: number;
  status: TContractStatus;
  auto_merge_authorized: boolean;
  auto_merge_revision: number | null;
  auto_merge_authorized_by: string | null;
  auto_merge_authorized_at: string | null;
  merged_sha: string;
  merged_at: string | null;
  updated_at: string;
};
export type TContractInput = Pick<TWorkItemContract, "requirements" | "definition_of_done"> & {
  release_id?: string | null;
  expected_revision?: number;
};
export type TProjectRelease = {
  id: string;
  project: string;
  identifier: string;
  name: string;
  description: string;
  target_date: string | null;
  status: "planned" | "active" | "released" | "cancelled";
  created_at: string;
  updated_at: string;
};
export type TExecutionPolicy = {
  project: string;
  enabled: boolean;
};
export type TAgentRun = {
  id: string;
  contract: string;
  agent: string;
  worker_id: string;
  attempt: number;
  spec_revision: number;
  spec_snapshot: {
    issue_id: string;
    owner_id: string;
    owner_ids: string[];
    name: string;
    description_html: string;
    requirements: string;
    definition_of_done: TAcceptanceCriterion[];
    release_id: string | null;
    module_ids: string[];
  };
  repository: string;
  base_branch: string;
  status: "active" | "merging" | "merged" | "cancelled" | "expired";
  last_heartbeat: string;
  finished_at: string | null;
  head_sha: string;
  code_revision: number;
  pull_request: number | null;
  last_error: string;
  created_at: string;
};
export type TWorkItemQuestion = {
  id: string;
  run: string;
  spec_revision: number;
  text: string;
  blocking: boolean;
  respondent: string;
  question_comment: string;
  answer_comment: string | null;
  answer: string;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string;
};
export type TRunChild = {
  id: string;
  issue: string;
  run: string;
  owner: string;
  release: string | null;
  spec_revision: number;
  requirements: string;
  definition_of_done: TAcceptanceCriterion[];
  required: boolean;
  name: string;
  state_group: string;
};
export type TRunEvidence = {
  id: string;
  run: string;
  spec_revision: number;
  head_sha: string;
  kind: "test" | "screenshot" | "document" | "note";
  outcome: "passed" | "failed" | "info";
  summary: string;
  criterion_ids: string[];
  url: string;
  asset: string | null;
  asset_url: string | null;
  created_by: string | null;
  created_at: string;
};
export type TReviewRequirement = { id: string; reviewer: string; requested_at: string; is_active: boolean };
export type TReviewDecision = {
  id: string;
  run: string;
  reviewer: string;
  spec_revision: number;
  code_revision: number;
  head_sha: string;
  pull_request: number;
  decision: "approve" | "changes_requested";
  comment: string;
  created_at: string;
};
export type TMergeAttempt = {
  id: string;
  run: string;
  head_sha: string;
  code_revision: number;
  pull_request: number;
  status: "prepared" | "uncertain" | "failed" | "confirmed";
  merge_sha: string;
  error: string;
  created_at: string;
  finished_at: string | null;
};
export type TCoordinationOverview = {
  inherited_from: string | null;
  contract: TWorkItemContract;
  runs: TAgentRun[];
  questions: TWorkItemQuestion[];
  children: TRunChild[];
  evidence: TRunEvidence[];
  reviewers: TReviewRequirement[];
  decisions: TReviewDecision[];
  merges: TMergeAttempt[];
};
export type TCoordinationAgent = {
  id: string;
  agent: string;
  name: string;
  role: "dispatcher" | "worker";
  is_active: boolean;
};
export type TAgentCredential = {
  id: string;
  label: string;
  is_active: boolean;
  expired_at: string | null;
  created_at: string;
  last_used: string | null;
};
export type TAgentCredentialSecret = TAgentCredential & { token: string };
export type TMainAgentRegistration = {
  agent: TCoordinationAgent;
  credential: TAgentCredentialSecret;
};
