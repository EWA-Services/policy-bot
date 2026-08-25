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
	"errors"
	"testing"

	"github.com/google/go-github/v85/github"
	"github.com/palantir/go-githubapp/appconfig"
	"github.com/palantir/policy-bot/policy/common"
	"github.com/palantir/policy-bot/pull"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestCheckRunProcessEvent(t *testing.T) {
	const (
		installationID = int64(1)
		repoID         = int64(2)
		owner          = "testorg"
		repo           = "testrepo"
		baseBranch     = "main"
		checkName      = "Reviewable PR Size"
		headSHA        = "0123456789abcdef"
		prNumber       = 42
	)

	newHandler := func(policyCheckName string) *CheckRun {
		return &CheckRun{
			Base: Base{
				ClientCreator: stubClientCreator{},
				ConfigFetcher: &ConfigFetcher{
					Loader: mockConfigLoader{
						loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
							return appconfig.Config{
								Content: []byte(`
policy:
  approval:
    - size
approval_rules:
  - name: size
    if:
      has_status:
        statuses:
          - "` + policyCheckName + `"
        conclusions: ["success"]
    requires:
      count: 0
`),
								Source: owner + "/" + repo + "@" + baseBranch,
								Path:   ".policy.yml",
							}, nil
						},
					},
					SeenPolicyCache: NewSeenPolicyCache(),
				},
			},
		}
	}

	newEvent := func(action, conclusion, eventCheckName string) github.CheckRunEvent {
		return github.CheckRunEvent{
			Action: github.Ptr(action),
			CheckRun: &github.CheckRun{
				Name:       github.Ptr(eventCheckName),
				HeadSHA:    github.Ptr(headSHA),
				Conclusion: github.Ptr(conclusion),
				PullRequests: []*github.PullRequest{
					{
						Number: github.Ptr(prNumber),
						Base: &github.PullRequestBranch{
							Ref:  github.Ptr(baseBranch),
							Repo: &github.Repository{ID: github.Ptr(repoID)},
						},
						Head: &github.PullRequestBranch{SHA: github.Ptr(headSHA)},
					},
				},
			},
			Repo: &github.Repository{
				ID:    github.Ptr(repoID),
				Name:  github.Ptr(repo),
				Owner: &github.User{Login: github.Ptr(owner)},
			},
			Installation: &github.Installation{ID: github.Ptr(installationID)},
		}
	}

	t.Run("reevaluates success-to-failure and failure-to-success on the same SHA", func(t *testing.T) {
		h := newHandler(checkName)
		var evaluated []pull.Locator
		evaluate := func(_ context.Context, gotInstallationID int64, trigger common.Trigger, loc pull.Locator) error {
			assert.Equal(t, installationID, gotInstallationID)
			assert.Equal(t, common.TriggerStatus, trigger)
			evaluated = append(evaluated, loc)
			return nil
		}

		for _, conclusion := range []string{"success", "failure", "success"} {
			err := h.processEvent(context.Background(), newEvent("completed", conclusion, checkName), evaluate)
			require.NoError(t, err)
		}

		require.Len(t, evaluated, 3)
		for _, loc := range evaluated {
			assert.Equal(t, owner, loc.Owner)
			assert.Equal(t, repo, loc.Repo)
			assert.Equal(t, prNumber, loc.Number)
			assert.Equal(t, headSHA, loc.Value.GetHead().GetSHA())
		}
	})

	t.Run("ignores events that are not completed", func(t *testing.T) {
		h := newHandler(checkName)
		evaluations := 0
		evaluate := func(context.Context, int64, common.Trigger, pull.Locator) error {
			evaluations++
			return nil
		}

		for _, action := range []string{"created", "rerequested"} {
			err := h.processEvent(context.Background(), newEvent(action, "failure", checkName), evaluate)
			require.NoError(t, err)
		}

		assert.Zero(t, evaluations)
	})

	t.Run("skips a completed check that is not referenced by policy", func(t *testing.T) {
		h := newHandler("Required Check")
		evaluations := 0
		evaluate := func(context.Context, int64, common.Trigger, pull.Locator) error {
			evaluations++
			return nil
		}

		err := h.processEvent(context.Background(), newEvent("completed", "failure", "Unrelated Check"), evaluate)
		require.NoError(t, err)

		assert.Zero(t, evaluations)
	})
}

func TestShouldSkipCheckRun(t *testing.T) {
	const (
		installationID = int64(1)
		owner          = "testorg"
		repo           = "testrepo"
		baseBranch     = "main"
		checkName      = "Validate PR Title"
	)

	newHandler := func(loader mockConfigLoader) *CheckRun {
		return &CheckRun{
			Base: Base{
				ClientCreator: stubClientCreator{},
				ConfigFetcher: &ConfigFetcher{
					Loader:          loader,
					SeenPolicyCache: NewSeenPolicyCache(),
				},
			},
		}
	}

	t.Run("returns false for empty checkName", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				t.Fatal("loader should not be called when checkName is empty")
				return appconfig.Config{}, nil
			},
		})
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, ""))
	})

	t.Run("returns false for empty baseBranch", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				t.Fatal("loader should not be called when baseBranch is empty")
				return appconfig.Config{}, nil
			},
		})
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, "", checkName))
	})

	t.Run("returns true when repo has no policy file (Config==nil)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				// No content, no source, no path → IsUndefined() → Config stays nil.
				return appconfig.Config{}, nil
			},
		})
		assert.True(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
	})

	t.Run("returns true when policy has no has_status blocks", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte(`
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    requires:
      count: 1
      teams: ["testorg/team"]
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.True(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
	})

	t.Run("returns true when check name is not in policy's status list", func(t *testing.T) {
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
        statuses:
          - "GitGuardian Security Checks"
        conclusions: ["success"]
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.True(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, "Unrelated Check"))
	})

	t.Run("returns false when check name IS in policy's status list", func(t *testing.T) {
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
        statuses:
          - "Validate PR Title"
        conclusions: ["success"]
    requires:
      count: 0
`),
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, nil
			},
		})
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
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
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
	})

	t.Run("returns false on ParseError (fail open)", func(t *testing.T) {
		h := newHandler(mockConfigLoader{
			loadConfig: func(_ context.Context, _ *github.Client, _, _, _ string) (appconfig.Config, error) {
				// Malformed YAML triggers ParseError, not LoadError.
				return appconfig.Config{
					Content: []byte("policy: ["),
					Source:  "testorg/testrepo@main",
					Path:    ".policy.yml",
				}, nil
			},
		})
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
	})

	t.Run("returns false when client creation fails (fail open)", func(t *testing.T) {
		h := &CheckRun{
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
		assert.False(t, h.shouldSkipCheckRun(context.Background(), installationID, owner, repo, baseBranch, checkName))
	})
}
