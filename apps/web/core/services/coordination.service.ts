/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { isAxiosError } from "axios";
import { API_BASE_URL } from "@plane/constants";
import type {
  TAgentRun,
  TContractInput,
  TCoordinationAgent,
  TCoordinationOverview,
  TExecutionPolicy,
  TProjectRelease,
  TReviewDecision,
  TReviewRequirement,
  TWorkItemContract,
  TWorkItemQuestion,
} from "@plane/types";
import { APIService } from "@/services/api.service";

export class CoordinationService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  private path(workspace: string, project: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${project}/coordination/`;
  }

  async overview(workspace: string, project: string, issue: string): Promise<TCoordinationOverview | null> {
    try {
      return (await this.get(this.path(workspace, project) + `work-items/${issue}/overview/`)).data;
    } catch (error) {
      if (isAxiosError(error) && error.response?.status === 404) return null;
      throw error;
    }
  }
  async updateTask(
    workspace: string,
    project: string,
    issue: string,
    input: Partial<TContractInput>
  ): Promise<TContractInput & { spec_revision: number }> {
    return (
      await this.patch(`/api/workspaces/${encodeURIComponent(workspace)}/projects/${project}/issues/${issue}/`, input)
    ).data;
  }
  async authorizeAdvance(
    workspace: string,
    project: string,
    issue: string,
    revision: number,
    enabled: boolean
  ): Promise<TWorkItemContract> {
    return (
      await this.put(this.path(workspace, project) + `work-items/${issue}/auto-merge/`, {
        expected_revision: revision,
        enabled,
      })
    ).data;
  }
  async reviewers(
    workspace: string,
    project: string,
    issue: string,
    revision: number,
    reviewers: string[]
  ): Promise<TReviewRequirement[]> {
    return (
      await this.put(this.path(workspace, project) + `work-items/${issue}/reviewers/`, {
        expected_revision: revision,
        reviewers,
      })
    ).data;
  }
  async decide(
    workspace: string,
    project: string,
    issue: string,
    run: TAgentRun,
    decision: TReviewDecision["decision"],
    comment: string
  ): Promise<TReviewDecision> {
    return (
      await this.post(this.path(workspace, project) + `work-items/${issue}/decisions/`, {
        run: run.id,
        expected_revision: run.spec_revision,
        head_sha: run.head_sha,
        code_revision: run.code_revision,
        decision,
        comment,
      })
    ).data;
  }
  async answer(
    workspace: string,
    project: string,
    issue: string,
    question: string,
    text: string
  ): Promise<TWorkItemQuestion> {
    return (
      await this.post(this.path(workspace, project) + `work-items/${issue}/questions/${question}/answer/`, { text })
    ).data;
  }
  async cancel(workspace: string, project: string, run: string): Promise<TAgentRun> {
    return (await this.post(this.path(workspace, project) + `runs/${run}/cancel/`, {})).data;
  }
  async reconcile(workspace: string, project: string, run: string): Promise<TAgentRun> {
    return (await this.post(this.path(workspace, project) + `runs/${run}/reconcile/`, {})).data;
  }
  async releases(workspace: string, project: string): Promise<TProjectRelease[]> {
    return (await this.get(this.path(workspace, project) + "releases/")).data;
  }
  async createRelease(
    workspace: string,
    project: string,
    input: Pick<TProjectRelease, "identifier" | "name" | "target_date" | "description" | "status">
  ): Promise<TProjectRelease> {
    return (await this.post(this.path(workspace, project) + "releases/", input)).data;
  }
  async updateRelease(
    workspace: string,
    project: string,
    release: string,
    input: Partial<Pick<TProjectRelease, "name" | "identifier" | "status" | "target_date" | "description">>
  ): Promise<TProjectRelease> {
    return (await this.patch(this.path(workspace, project) + `releases/${release}/`, input)).data;
  }
  async policy(workspace: string, project: string): Promise<TExecutionPolicy> {
    return (await this.get(this.path(workspace, project) + "policy/")).data;
  }
  async savePolicy(
    workspace: string,
    project: string,
    input: Omit<TExecutionPolicy, "project">
  ): Promise<TExecutionPolicy> {
    return (await this.put(this.path(workspace, project) + "policy/", input)).data;
  }
  async agents(workspace: string, project: string): Promise<TCoordinationAgent[]> {
    return (await this.get(this.path(workspace, project) + "agents/")).data;
  }
}
