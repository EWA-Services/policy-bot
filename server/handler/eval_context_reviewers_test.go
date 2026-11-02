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
	"encoding/json"
	stderrors "errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"

	"github.com/google/go-github/v92/github"
	"github.com/palantir/policy-bot/policy/reviewer"
	"github.com/palantir/policy-bot/pull"
	"github.com/palantir/policy-bot/pull/pulltest"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestReconcileReviewRequestsDoesNotRepeatHistoricalRequest(t *testing.T) {
	prctx := newReviewerHistoryContext([]*pull.Reviewer{{Type: pull.ReviewerUser, Name: "late-approver"}})
	client, requests := newReviewerRequestClient(t)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{Users: []string{"late-approver"}})

	require.NoError(t, err)
	assert.Empty(t, requests.all())
	assert.Equal(t, 1, prctx.calls)
}

func TestReconcileReviewRequestsPostsMissingReviewers(t *testing.T) {
	prctx := newReviewerHistoryContext(nil)
	client, requests := newReviewerRequestClient(t, http.StatusCreated)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{
		Users: []string{"zoe", "amy", "amy"},
		Teams: []string{"beta", "alpha", "alpha"},
	})

	require.NoError(t, err)
	assert.Equal(t, []recordedReviewerRequest{{
		Method: http.MethodPost,
		Path:   "/repos/pulltest/context/pulls/1/requested_reviewers",
		Body: github.ReviewersRequest{
			Reviewers:     []string{"amy", "zoe"},
			TeamReviewers: []string{"alpha", "beta"},
		},
	}}, requests.all())
}

func TestReconcileReviewRequestsConvergesAfterConcurrentRequest(t *testing.T) {
	desiredHistory := []*pull.Reviewer{
		{Type: pull.ReviewerUser, Name: "amy"},
		{Type: pull.ReviewerTeam, Name: "platform"},
	}
	prctx := newReviewerHistoryContext(nil, desiredHistory)
	client, requests := newReviewerRequestClient(t, http.StatusUnprocessableEntity)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{
		Users: []string{"amy"},
		Teams: []string{"platform"},
	})

	require.NoError(t, err)
	assert.Len(t, requests.all(), 1)
	assert.Equal(t, 2, prctx.calls)
}

func TestReconcileReviewRequestsRetriesPartialProgress(t *testing.T) {
	prctx := newReviewerHistoryContext(
		nil,
		[]*pull.Reviewer{{Type: pull.ReviewerUser, Name: "amy"}},
		[]*pull.Reviewer{
			{Type: pull.ReviewerUser, Name: "amy"},
			{Type: pull.ReviewerUser, Name: "zoe"},
		},
	)
	client, requests := newReviewerRequestClient(
		t,
		http.StatusUnprocessableEntity,
		http.StatusUnprocessableEntity,
	)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{Users: []string{"amy", "zoe"}})

	require.NoError(t, err)
	require.Len(t, requests.all(), 2)
	assert.Equal(t, []string{"amy", "zoe"}, requests.all()[0].Body.Reviewers)
	assert.Equal(t, []string{"zoe"}, requests.all()[1].Body.Reviewers)
	assert.Equal(t, 3, prctx.calls)
}

func TestReconcileReviewRequestsReturnsOriginalErrorWithoutProgress(t *testing.T) {
	prctx := newReviewerHistoryContext(nil, nil)
	client, requests := newReviewerRequestClient(t, http.StatusUnprocessableEntity)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{Users: []string{"amy"}})

	var githubError *github.ErrorResponse
	require.ErrorAs(t, err, &githubError)
	assert.Equal(t, "request failed 1", githubError.Message)
	assert.Len(t, requests.all(), 1)
	assert.Equal(t, 2, prctx.calls)
}

func TestReconcileReviewRequestsRetainsRequestAndReadErrors(t *testing.T) {
	historyError := stderrors.New("reviewer history unavailable")
	prctx := newReviewerHistoryResultContext(
		reviewerHistoryResult{},
		reviewerHistoryResult{err: historyError},
	)
	client, requests := newReviewerRequestClient(t, http.StatusUnprocessableEntity)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{Users: []string{"amy"}})

	var githubError *github.ErrorResponse
	require.ErrorAs(t, err, &githubError)
	assert.Equal(t, "request failed 1", githubError.Message)
	assert.ErrorIs(t, err, historyError)
	assert.Len(t, requests.all(), 1)
}

func TestReconcileReviewRequestsFailsClosedOnInitialReadError(t *testing.T) {
	historyError := stderrors.New("reviewer history unavailable")
	prctx := newReviewerHistoryResultContext(reviewerHistoryResult{err: historyError})
	client, requests := newReviewerRequestClient(t, http.StatusCreated)

	ec := &EvalContext{Client: client, PullContext: prctx}
	err := ec.reconcileReviewRequests(context.Background(), reviewer.Selection{Users: []string{"amy"}})

	assert.ErrorIs(t, err, historyError)
	assert.Empty(t, requests.all())
}

type reviewerHistoryContext struct {
	*pulltest.Context
	results []reviewerHistoryResult
	calls   int
}

type reviewerHistoryResult struct {
	history []*pull.Reviewer
	err     error
}

func newReviewerHistoryContext(histories ...[]*pull.Reviewer) *reviewerHistoryContext {
	results := make([]reviewerHistoryResult, len(histories))
	for i, history := range histories {
		results[i] = reviewerHistoryResult{history: history}
	}
	return newReviewerHistoryResultContext(results...)
}

func newReviewerHistoryResultContext(results ...reviewerHistoryResult) *reviewerHistoryContext {
	return &reviewerHistoryContext{Context: &pulltest.Context{}, results: results}
}

func (c *reviewerHistoryContext) ReviewerRequestHistory() ([]*pull.Reviewer, error) {
	if c.calls >= len(c.results) {
		return nil, stderrors.New("unexpected reviewer history read")
	}
	result := c.results[c.calls]
	c.calls++
	return result.history, result.err
}

type recordedReviewerRequest struct {
	Method string
	Path   string
	Body   github.ReviewersRequest
}

type reviewerRequestRecorder struct {
	mu       sync.Mutex
	statuses []int
	requests []recordedReviewerRequest
}

func newReviewerRequestClient(t *testing.T, statuses ...int) (*github.Client, *reviewerRequestRecorder) {
	t.Helper()
	recorder := &reviewerRequestRecorder{statuses: statuses}
	server := httptest.NewServer(http.HandlerFunc(recorder.serveHTTP))
	t.Cleanup(server.Close)

	baseURL := server.URL + "/"
	client, err := github.NewClient(github.WithURLs(&baseURL, &baseURL))
	require.NoError(t, err)
	return client, recorder
}

func (r *reviewerRequestRecorder) serveHTTP(w http.ResponseWriter, request *http.Request) {
	var body github.ReviewersRequest
	decodeError := json.NewDecoder(request.Body).Decode(&body)

	r.mu.Lock()
	index := len(r.requests)
	r.requests = append(r.requests, recordedReviewerRequest{
		Method: request.Method,
		Path:   request.URL.Path,
		Body:   body,
	})
	status := http.StatusInternalServerError
	if decodeError == nil && index < len(r.statuses) {
		status = r.statuses[index]
	}
	r.mu.Unlock()

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if status >= http.StatusBadRequest {
		_, _ = fmt.Fprintf(w, `{"message":"request failed %d"}`, index+1)
		return
	}
	_, _ = w.Write([]byte("{}"))
}

func (r *reviewerRequestRecorder) all() []recordedReviewerRequest {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]recordedReviewerRequest(nil), r.requests...)
}
