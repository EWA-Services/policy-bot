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

func TestCheckRunProcessEvent(t *testing.T) {
	const (
		installationID = int64(1)
		repoID         = int64(2)
		owner          = "testorg"
		repo           = "testrepo"
		baseBranch     = "main"
		checkName      = "SonarCloud Code Analysis"
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
    - sonar
approval_rules:
  - name: sonar
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

	t.Run("reevaluates newly created checks", func(t *testing.T) {
		h := newHandler(checkName)
		evaluations := 0
		evaluate := func(context.Context, int64, common.Trigger, pull.Locator) error {
			evaluations++
			return nil
		}

		for _, action := range []string{"created"} {
			err := h.processEvent(context.Background(), newEvent(action, "failure", checkName), evaluate)
			require.NoError(t, err)
		}

		assert.Equal(t, 1, evaluations)
	})

	t.Run("finds open current-head PRs when check payload associations are empty", func(t *testing.T) {
		event := newEvent("completed", "failure", checkName)
		associated := event.CheckRun.PullRequests[0]
		associated.State = github.Ptr("open")
		event.CheckRun.PullRequests = nil
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
			assert.Equal(t, "/repos/testorg/testrepo/commits/0123456789abcdef/pulls", request.URL.Path)
			require.NoError(t, json.NewEncoder(w).Encode([]*github.PullRequest{associated}))
		}))
		defer server.Close()
		client, err := github.NewClient(
			github.WithHTTPClient(server.Client()),
			github.WithURLs(github.Ptr(server.URL+"/"), nil),
		)
		require.NoError(t, err)
		h := newHandler(checkName)
		h.ClientCreator = checkRunTestClientCreator{client: client}
		evaluations := 0
		err = h.processEvent(context.Background(), event, func(context.Context, int64, common.Trigger, pull.Locator) error {
			evaluations++
			return nil
		})
		require.NoError(t, err)
		assert.Equal(t, 1, evaluations)
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

type checkRunTestClientCreator struct {
	stubClientCreator
	client *github.Client
}

func (creator checkRunTestClientCreator) NewInstallationClient(int64) (*github.Client, error) {
	return creator.client, nil
}
