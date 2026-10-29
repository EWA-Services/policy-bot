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
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/google/go-github/v92/github"
	"github.com/palantir/go-githubapp/appconfig"
	"github.com/palantir/policy-bot/policy/common"
	"github.com/palantir/policy-bot/pull"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestShouldSkipWorkflowRun(t *testing.T) {
	const (
		installationID = int64(1)
		owner          = "testorg"
		repo           = "testrepo"
		baseBranch     = "main"
		// `has_workflow_result.workflows` policy entries are workflow file
		// paths (e.g. ".github/workflows/ci.yml"), not display names. The
		// pre-check must compare paths to paths.
		workflowPath        = ".github/workflows/ci.yml"
		workflowDisplayName = "CI"
	)

	newHandler := func(loader mockConfigLoader) *WorkflowRun {
		return &WorkflowRun{
			Base: Base{
				ClientCreator: stubClientCreator{},
				ConfigFetcher: &ConfigFetcher{
					Loader:          loader,
					SeenPolicyCache: NewSeenPolicyCache(),
				},
			},
		}
	}

	t.Run("returns false for empty workflowPath", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				t.Fatal("loader should not be called when workflowPath is empty")
				return appconfig.Config{}, nil
			},
		})
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, ""))
	})

	t.Run("returns false for empty baseBranch", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				t.Fatal("loader should not be called when baseBranch is empty")
				return appconfig.Config{}, nil
			},
		})
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, "", workflowPath))
	})

	t.Run("returns true when repo has no policy file (Config==nil)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{}, nil
			},
		})
		assert.True(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})

	t.Run("returns true when policy has no has_workflow_result blocks", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte(`
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_status:
        statuses: ["some-status"]
        conclusions: ["success"]
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.True(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})

	t.Run("returns true when workflow path is not in policy's workflow list", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte(`
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_workflow_result:
        workflows:
          - ".github/workflows/deploy.yml"
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.True(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, ".github/workflows/unrelated.yml"))
	})

	t.Run("returns false when workflow path IS in policy's workflow list", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte(`
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_workflow_result:
        workflows:
          - ".github/workflows/ci.yml"
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})

	// Regression for the path-vs-name mismatch caught on PR #2: confirm that
	// passing the display name instead of the path does NOT match a
	// policy entry that lists the path. This guards against a future
	// refactor accidentally going back to GetName().
	t.Run("returns true when caller passes display name instead of path (path-only matching)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte(`
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_workflow_result:
        workflows:
          - ".github/workflows/ci.yml"
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		// The display name "CI" does NOT equal the path
		// ".github/workflows/ci.yml" in the policy, so the function returns
		// true (skip). In production the handler must pass the PATH, not
		// this display name — that's enforced by the Handle method.
		assert.True(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowDisplayName))
	})

	t.Run("returns false on LoadError (fail open)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, errors.New("transient github failure")
			},
		})
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})

	t.Run("returns false on ParseError (fail open)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte("policy: ["),
					Source:  "testorg/testrepo@main",
					Path:    ".policy.yml",
				}, nil
			},
		})
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})

	t.Run("returns false when client creation fails (fail open)", func(t *testing.T) {
		h := &WorkflowRun{
			Base: Base{
				ClientCreator: failingClientCreator{},
				ConfigFetcher: &ConfigFetcher{
					Loader: mockConfigLoader{
						loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
							t.Fatal("loader should not be called when client creation fails")
							return appconfig.Config{}, nil
						},
					},
					SeenPolicyCache: NewSeenPolicyCache(),
				},
			},
		}
		assert.False(t, h.shouldSkipWorkflowRun(context.Background(), installationID, owner, repo, baseBranch, workflowPath))
	})
}

func TestWorkflowRunTransitions(t *testing.T) {
	h := &WorkflowRun{Base: Base{ClientCreator: stubClientCreator{}, ConfigFetcher: &ConfigFetcher{
		Loader: mockConfigLoader{loadConfig: func(context.Context, *github.Client, string, string, string) (appconfig.Config, error) {
			return appconfig.Config{Content: []byte(`
policy:
  approval: [CI]
approval_rules:
  - name: CI
    requires:
      count: 0
      conditions:
        has_workflow_result:
          workflows: [.github/workflows/ci.yml]
          conclusions: [success]
`), Source: "testorg/testrepo@main", Path: ".policy.yml"}, nil
		}}, SeenPolicyCache: NewSeenPolicyCache(),
	}}}
	for _, action := range []string{"requested", "in_progress", "completed", "unrelated"} {
		t.Run(action, func(t *testing.T) {
			calls := 0
			event := github.WorkflowRunEvent{
				Action: github.Ptr(action), Installation: &github.Installation{ID: github.Ptr(int64(1))},
				Repo:        &github.Repository{ID: github.Ptr(int64(2)), Name: github.Ptr("testrepo"), Owner: &github.User{Login: github.Ptr("testorg")}},
				WorkflowRun: &github.WorkflowRun{Path: github.Ptr(".github/workflows/ci.yml"), HeadSHA: github.Ptr("same-head"), PullRequests: []*github.PullRequest{{Number: github.Ptr(42), Base: &github.PullRequestBranch{Ref: github.Ptr("main"), Repo: &github.Repository{ID: github.Ptr(int64(2))}}}}},
			}
			err := h.processEvent(context.Background(), event, func(ctx context.Context, _ int64, trigger common.Trigger, loc pull.Locator) error {
				calls++
				assert.Equal(t, common.TriggerStatus, trigger)
				assert.Equal(t, 42, loc.Number)
				run, ok := pull.WorkflowRunFromContext(ctx)
				assert.True(t, ok)
				assert.Same(t, event.WorkflowRun, run)
				return nil
			})
			assert.NoError(t, err)
			expected := 1
			if action == "unrelated" {
				expected = 0
			}
			assert.Equal(t, expected, calls)
		})
	}
}

func TestUnassociatedWorkflowLookupOnlyForPullRequests(t *testing.T) {
	for _, kind := range []string{"push", "pull_request"} {
		t.Run(kind, func(t *testing.T) {
			h := &WorkflowRun{Base: Base{ClientCreator: failingClientCreator{}}}
			event := github.WorkflowRunEvent{
				Action: github.Ptr("in_progress"), Installation: &github.Installation{ID: github.Ptr(int64(1))},
				Repo:        &github.Repository{ID: github.Ptr(int64(2)), Name: github.Ptr("testrepo"), Owner: &github.User{Login: github.Ptr("testorg")}},
				WorkflowRun: &github.WorkflowRun{Event: github.Ptr(kind), Path: github.Ptr(".github/workflows/ci.yml"), HeadSHA: github.Ptr("same-head")},
			}
			err := h.processEvent(context.Background(), event, func(context.Context, int64, common.Trigger, pull.Locator) error {
				t.Fatal("cannot evaluate a PR before its association is known")
				return nil
			})
			if kind == "push" {
				assert.NoError(t, err)
			} else {
				assert.Error(t, err)
			}
		})
	}
}

func TestUnassociatedWorkflowLookup(t *testing.T) {
	pullRequest := func(number int, state, head string) *github.PullRequest {
		return &github.PullRequest{Number: github.Ptr(number), State: github.Ptr(state), Head: &github.PullRequestBranch{SHA: github.Ptr(head)}, Base: &github.PullRequestBranch{Ref: github.Ptr("main"), Repo: &github.Repository{ID: github.Ptr(int64(2))}}}
	}
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		assert.Equal(t, "/repos/testorg/testrepo/commits/same-head/pulls", request.URL.Path)
		switch request.URL.Query().Get("page") {
		case "", "1":
			w.Header().Set("Link", "<"+server.URL+request.URL.Path+"?page=2>; rel=\"next\"")
			require.NoError(t, json.NewEncoder(w).Encode([]*github.PullRequest{pullRequest(1, "open", "same-head"), pullRequest(2, "closed", "same-head")}))
		case "2":
			require.NoError(t, json.NewEncoder(w).Encode([]*github.PullRequest{pullRequest(3, "open", "other-head"), pullRequest(4, "open", "same-head")}))
		default:
			t.Fatalf("unexpected page %q", request.URL.Query().Get("page"))
		}
	}))
	defer server.Close()
	client, err := github.NewClient(github.WithHTTPClient(server.Client()), github.WithURLs(github.Ptr(server.URL+"/"), nil))
	require.NoError(t, err)
	h := &WorkflowRun{Base: Base{ClientCreator: workflowRunTestClientCreator{client: client}}}
	event := github.WorkflowRunEvent{
		Action:       github.Ptr("in_progress"),
		Installation: &github.Installation{ID: github.Ptr(int64(1))},
		Repo:         &github.Repository{ID: github.Ptr(int64(2)), Name: github.Ptr("testrepo"), Owner: &github.User{Login: github.Ptr("testorg")}},
		WorkflowRun:  &github.WorkflowRun{Event: github.Ptr("pull_request"), HeadSHA: github.Ptr("same-head")},
	}
	var evaluated []int
	err = h.processEvent(context.Background(), event, func(_ context.Context, _ int64, _ common.Trigger, loc pull.Locator) error {
		evaluated = append(evaluated, loc.Number)
		return nil
	})
	require.NoError(t, err)
	assert.ElementsMatch(t, []int{1, 4}, evaluated)
}

func TestUnassociatedWorkflowLookupFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "unavailable", http.StatusInternalServerError)
	}))
	defer server.Close()
	client, err := github.NewClient(github.WithHTTPClient(server.Client()), github.WithURLs(github.Ptr(server.URL+"/"), nil))
	require.NoError(t, err)
	h := &WorkflowRun{Base: Base{ClientCreator: workflowRunTestClientCreator{client: client}}}
	event := github.WorkflowRunEvent{
		Action:       github.Ptr("in_progress"),
		Installation: &github.Installation{ID: github.Ptr(int64(1))},
		Repo:         &github.Repository{ID: github.Ptr(int64(2)), Name: github.Ptr("testrepo"), Owner: &github.User{Login: github.Ptr("testorg")}},
		WorkflowRun:  &github.WorkflowRun{Event: github.Ptr("pull_request"), HeadSHA: github.Ptr("same-head")},
	}
	evaluated := false
	err = h.processEvent(context.Background(), event, func(context.Context, int64, common.Trigger, pull.Locator) error {
		evaluated = true
		return nil
	})
	assert.ErrorContains(t, err, "failed to find pull requests for workflow run")
	assert.False(t, evaluated)
}

type workflowRunTestClientCreator struct {
	stubClientCreator
	client *github.Client
}

func (creator workflowRunTestClientCreator) NewInstallationClient(int64) (*github.Client, error) {
	return creator.client, nil
}
