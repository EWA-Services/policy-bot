// Copyright 2022 Palantir Technologies, Inc.
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
	"math/rand"
	"net/http"
	"slices"

	"github.com/google/go-github/v92/github"
	"github.com/palantir/policy-bot/policy/common"
	"github.com/palantir/policy-bot/policy/reviewer"
	"github.com/pkg/errors"
	"github.com/rs/zerolog"
)

func (ec *EvalContext) requestReviewsForResult(ctx context.Context, trigger common.Trigger, result common.Result) error {
	logger := zerolog.Ctx(ctx)

	if ec.PullContext.IsDraft() || result.Status != common.StatusPending {
		return nil
	}

	// As of 2021-05-19, there are no predicates that use comments or reviews
	// to enable or disable rules. This means these events will never cause a
	// change in reviewer assignment and we can skip the whole process.
	reviewTrigger := ^(common.TriggerComment | common.TriggerReview)
	if !trigger.Matches(reviewTrigger) {
		return nil
	}

	if reqs := reviewer.FindRequests(&result); len(reqs) > 0 {
		logger.Debug().Msgf("Found %d pending rules with review requests enabled", len(reqs))
		return ec.requestReviews(ctx, reqs)
	}

	logger.Debug().Msg("No pending rules need additional reviewers, skipping reviewer assignment")
	return nil
}

func (ec *EvalContext) requestReviews(ctx context.Context, reqs []*common.Result) error {
	logger := zerolog.Ctx(ctx)

	// Seed the random source with the PR creation time so that repeated
	// evaluations produce the same set of reviewers. This is required to avoid
	// duplicate requests on later evaluations.
	r := rand.New(rand.NewSource(ec.PullContext.CreatedAt().UnixNano()))
	selection, err := reviewer.SelectReviewers(ctx, ec.PullContext, reqs, r)
	if err != nil {
		return errors.Wrap(err, "failed to select reviewers")
	}

	if selection.IsEmpty() {
		logger.Debug().Msg("No eligible users or teams found for review")
		return nil
	}

	return ec.reconcileReviewRequests(ctx, selection)
}

func (ec *EvalContext) reconcileReviewRequests(ctx context.Context, selection reviewer.Selection) error {
	logger := zerolog.Ctx(ctx)
	if ec.ReviewerRequests == nil {
		return errors.New("reviewer request coordinator is not configured")
	}

	lease := ec.ReviewerRequests.Lock(
		ec.PullContext.RepositoryOwner(),
		ec.PullContext.RepositoryName(),
		ec.PullContext.Number(),
	)
	defer lease.Unlock()

	blockers, err := ec.PullContext.ReviewerAssignmentBlockers()
	if err != nil {
		return errors.Wrap(err, "failed to read reviewer assignment blockers")
	}
	blockers = append(blockers, lease.Blockers()...)

	missing := selection.Difference(blockers)
	if missing.IsEmpty() {
		logger.Debug().Msg("All selected reviewers were already requested or reviewed")
		return nil
	}

	response, err := ec.requestReviewers(ctx, missing)
	if shouldRetainReviewerRequestAttempt(response) {
		lease.MarkAssigned(missing)
	}
	if err != nil {
		return errors.Wrap(err, "failed to request reviewers")
	}
	return nil
}

func (ec *EvalContext) requestReviewers(ctx context.Context, selection reviewer.Selection) (*github.Response, error) {
	req := selectionToReviewersRequest(selection)
	zerolog.Ctx(ctx).Debug().
		Strs("users", req.Reviewers).
		Strs("teams", req.TeamReviewers).
		Msgf("Requesting reviews from %d users and %d teams", len(req.Reviewers), len(req.TeamReviewers))

	_, response, err := ec.Client.PullRequests.RequestReviewers(
		ctx,
		ec.PullContext.RepositoryOwner(),
		ec.PullContext.RepositoryName(),
		ec.PullContext.Number(),
		req,
	)
	return response, err
}

func shouldRetainReviewerRequestAttempt(response *github.Response) bool {
	return response == nil ||
		(response.StatusCode >= http.StatusOK && response.StatusCode < http.StatusMultipleChoices) ||
		response.StatusCode >= http.StatusInternalServerError
}

func selectionToReviewersRequest(s reviewer.Selection) github.ReviewersRequest {
	req := github.ReviewersRequest{}

	if len(s.Users) > 0 {
		req.Reviewers = slices.Clone(s.Users)
	} else {
		req.Reviewers = []string{}
	}

	if len(s.Teams) > 0 {
		req.TeamReviewers = slices.Clone(s.Teams)
	} else {
		req.TeamReviewers = []string{}
	}

	// Order the reviewers and remove any duplicates because the Github API will error on duplicates as of Oct 2024
	slices.Sort(req.Reviewers)
	req.Reviewers = slices.Compact(req.Reviewers)
	slices.Sort(req.TeamReviewers)
	req.TeamReviewers = slices.Compact(req.TeamReviewers)

	return req
}
