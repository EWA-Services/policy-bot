// Copyright 2026 Palantir Technologies, Inc.
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
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/google/go-github/v92/github"
	"github.com/palantir/policy-bot/policy/reviewer"
	"github.com/palantir/policy-bot/pull"
	"github.com/palantir/policy-bot/pull/pulltest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestRequestReviewsDoesNotRequestReviewerWhoApprovedDuringEvaluation(t *testing.T) {
	const headSHA = "head"

	prctx := &reviewStateContext{
		Context: &pulltest.Context{
			HeadSHAValue: headSHA,
		},
	}

	reviews, err := prctx.Reviews()
	require.NoError(t, err)
	require.Empty(t, reviews)

	prctx.currentReviews = []*pull.Review{
		{Author: "reviewer", State: pull.ReviewApproved, SHA: headSHA},
	}

	var requestCount atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requestCount.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte("{}"))
	}))
	t.Cleanup(server.Close)

	baseURL := server.URL + "/"
	client, err := github.NewClient(github.WithURLs(&baseURL, &baseURL))
	require.NoError(t, err)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err = ec.requestMissingReviews(context.Background(), reviewer.Selection{Users: []string{"reviewer"}})
	require.NoError(t, err)
	assert.Zero(t, requestCount.Load())
}

type reviewStateContext struct {
	*pulltest.Context

	currentReviews []*pull.Review
	cachedReviews  []*pull.Review
	reviewsCached  bool
}

func (c *reviewStateContext) Reviews() ([]*pull.Review, error) {
	if !c.reviewsCached {
		c.cachedReviews = append([]*pull.Review(nil), c.currentReviews...)
		c.reviewsCached = true
	}
	return c.cachedReviews, nil
}

func (c *reviewStateContext) CurrentReviews() ([]*pull.Review, error) {
	return c.currentReviews, nil
}
