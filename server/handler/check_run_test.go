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
	"github.com/stretchr/testify/assert"
)

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
