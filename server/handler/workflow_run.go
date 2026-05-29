// Copyright 2018 Palantir Technologies, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package handler

import (
	"context"
	"encoding/json"

	"github.com/google/go-github/v85/github"
	"github.com/palantir/go-githubapp/githubapp"
	"github.com/palantir/policy-bot/policy/common"
	"github.com/palantir/policy-bot/pull"
	"github.com/pkg/errors"
)

type WorkflowRun struct {
	Base
}

func (h *WorkflowRun) Handles() []string { return []string{"workflow_run"} }

func (h *WorkflowRun) Handle(ctx context.Context, eventType, deliveryID string, payload []byte) error {
	// https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#workflow_run
	// https://docs.github.com/en/webhooks/webhook-events-and-payloads?actionType=completed#workflow_run
	var event github.WorkflowRunEvent
	if err := json.Unmarshal(payload, &event); err != nil {
		return errors.Wrap(err, "failed to parse workflow_run event payload")
	}

	if event.GetAction() != "completed" {
		return nil
	}

	repo := event.GetRepo()
	repoID := repo.GetID()
	ownerName := repo.GetOwner().GetLogin()
	repoName := repo.GetName()
	workflowName := event.GetWorkflowRun().GetName()
	commitSHA := event.GetWorkflowRun().GetHeadSHA()
	installationID := githubapp.GetInstallationIDFromEvent(&event)

	ctx, logger := githubapp.PrepareRepoContext(ctx, installationID, repo)

	evaluationFailures := 0
	for _, pr := range event.GetWorkflowRun().PullRequests {
		// The `workflow_run` event includes pull requests that contain the SHA
		// which is being checked. These can be pull requests _from_ our
		// repository _to_ another one, for example if it's been forked and
		// there's a PR to merge changes from our repo into the fork. We don't
		// want to try to evaluate the policy for such PRs as they're nothing to
		// do with us.
		prBaseRepo := pr.GetBase().GetRepo()
		if prBaseRepo.GetID() != repoID {
			logger.Debug().Msgf("Skipping pull request '%d' from different repository '%s'", pr.GetNumber(), prBaseRepo.GetURL())
			continue
		}

		// Cheap pre-check: skip the full evaluation when the policy on the
		// PR's base branch cannot possibly be affected by this workflow_run.
		// Covers three known-terminal cases: no `.policy.yml` at all,
		// policy has no `has_workflow_result` blocks, or the workflow name
		// isn't listed. Each skip saves 3-5 GitHub API calls. Fails open
		// in every uncertain case (load/parse error, empty identifiers).
		baseBranch := pr.GetBase().GetRef()
		if h.shouldSkipWorkflowRun(ctx, installationID, ownerName, repoName, baseBranch, workflowName) {
			logger.Info().
				Str("workflow_name", workflowName).
				Str("base_branch", baseBranch).
				Int("pr_number", pr.GetNumber()).
				Msg("Skipping workflow_run evaluation: not policy-relevant")
			continue
		}

		if err := h.Evaluate(ctx, installationID, common.TriggerStatus, pull.Locator{
			Owner:  ownerName,
			Repo:   repoName,
			Number: pr.GetNumber(),
			Value:  pr,
		}); err != nil {
			evaluationFailures++
			logger.Error().Err(err).Msgf("Failed to evaluate pull request '%d' for SHA '%s'", pr.GetNumber(), commitSHA)
		}
	}
	if evaluationFailures == 0 {
		return nil
	}

	return errors.Errorf("failed to evaluate %d pull requests", evaluationFailures)
}

// shouldSkipWorkflowRun returns true when we are confident that the policy on
// baseBranch cannot be affected by a workflow_run with the given name. Three
// known-terminal cases warrant a skip:
//
//   - The repo has no `.policy.yml` at all (`Config == nil`). PolicyBot has
//     nothing to evaluate; the existing code path would still pay 3-5 GitHub
//     API calls to discover that. Early-exiting here is strictly an
//     optimisation, never a behaviour change.
//   - The policy file exists but contains no `has_workflow_result` block
//     anywhere (`hasAny == false`). No workflow_run event can change its
//     outcome.
//   - The policy contains `has_workflow_result` blocks, but none reference
//     this event's workflow name.
//
// Returns false (fall-through to the full evaluation) for uncertain cases:
// policy fetch failed, policy parse failed, or required identifiers are
// empty. Never silently drops a relevant event.
func (h *WorkflowRun) shouldSkipWorkflowRun(ctx context.Context, installationID int64, owner, repo, baseBranch, workflowName string) bool {
	if baseBranch == "" || workflowName == "" {
		return false
	}

	client, err := h.NewInstallationClient(installationID)
	if err != nil {
		return false
	}

	fetched := h.ConfigFetcher.ConfigForRepositoryBranch(ctx, client, owner, repo, baseBranch)
	if fetched.LoadError != nil || fetched.ParseError != nil {
		return false
	}
	if fetched.Config == nil {
		// No `.policy.yml` in the repo. Nothing the bot can evaluate, skip.
		return true
	}

	names, hasAny := policyWorkflowNames(fetched.Config)
	if !hasAny {
		// Policy exists but has no workflow predicates anywhere. No
		// workflow_run event can possibly change its outcome, skip.
		return true
	}

	_, listed := names[workflowName]
	return !listed
}
