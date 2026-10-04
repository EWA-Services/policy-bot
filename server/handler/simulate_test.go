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
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/go-github/v92/github"
	"github.com/palantir/go-githubapp/githubapp"
	"github.com/palantir/policy-bot/policy/common"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"goji.io"
	"goji.io/pat"
)

func TestNewSimulationResponse(t *testing.T) {
	tests := map[string]struct {
		Result   *common.Result
		Expected *SimulationResponse
	}{
		"nil result": {
			Result:   nil,
			Expected: &SimulationResponse{},
		},
		"result with no errors and no children": {
			Result: &common.Result{
				Name:              "my-policy",
				Description:       "a policy",
				StatusDescription: "all rules approved",
				Status:            common.StatusApproved,
			},
			Expected: &SimulationResponse{
				Name:              "my-policy",
				Description:       "a policy",
				StatusDescription: "all rules approved",
				Status:            "approved",
			},
		},
		"result with top-level error": {
			Result: &common.Result{
				Name:   "my-policy",
				Status: common.StatusPending,
				Error:  fmt.Errorf("something broke"),
			},
			Expected: &SimulationResponse{
				Name:   "my-policy",
				Status: "pending",
				Error:  "something broke",
			},
		},
		"result with children": {
			Result: &common.Result{
				Name:              "or",
				Status:            common.StatusPending,
				StatusDescription: "None of the rules are satisfied",
				Children: []*common.Result{
					{Name: "rule-a", Status: common.StatusPending},
					{Name: "rule-b", Status: common.StatusApproved, Error: fmt.Errorf("rule-b failed")},
				},
			},
			Expected: &SimulationResponse{
				Name:              "or",
				Status:            "pending",
				StatusDescription: "None of the rules are satisfied",
				Children: []*SimulationResponse{
					{Name: "rule-a", Status: "pending"},
					{Name: "rule-b", Status: "approved", Error: "rule-b failed"},
				},
			},
		},
		"nested tree": {
			Result: &common.Result{
				Name:   "policy",
				Status: common.StatusPending,
				Children: []*common.Result{
					{
						Name:   "approval-rule",
						Status: common.StatusPending,
						Children: []*common.Result{
							{
								Name:   "or",
								Status: common.StatusPending,
								Children: []*common.Result{
									{Name: "rule-a", Status: common.StatusPending},
									{Name: "rule-b", Status: common.StatusSkipped, Error: fmt.Errorf("deep error")},
								},
							},
						},
					},
				},
			},
			Expected: &SimulationResponse{
				Name:   "policy",
				Status: "pending",
				Children: []*SimulationResponse{
					{
						Name:   "approval-rule",
						Status: "pending",
						Children: []*SimulationResponse{
							{
								Name:   "or",
								Status: "pending",
								Children: []*SimulationResponse{
									{Name: "rule-a", Status: "pending"},
									{Name: "rule-b", Status: "skipped", Error: "deep error"},
								},
							},
						},
					},
				},
			},
		},
	}

	for name, test := range tests {
		t.Run(name, func(t *testing.T) {
			response := newSimulationResponse(test.Result)
			assert.Equal(t, test.Expected, response)
		})
	}
}

func TestSimulationResponseJSON(t *testing.T) {
	t.Run("field names are correct", func(t *testing.T) {
		resp := &SimulationResponse{
			Name:              "my-policy",
			Description:       "a policy",
			StatusDescription: "all rules approved",
			Status:            "approved",
			Error:             "something broke",
			Children: []*SimulationResponse{
				{Name: "rule-a", Status: "approved"},
			},
		}

		data, err := json.Marshal(resp)
		require.NoError(t, err)

		var fields map[string]json.RawMessage
		err = json.Unmarshal(data, &fields)
		require.NoError(t, err)

		expectedKeys := []string{"name", "description", "status_description", "status", "error", "children"}
		for _, key := range expectedKeys {
			assert.Contains(t, fields, key, "missing expected JSON field %q", key)
		}
	})

	t.Run("children omitted when empty", func(t *testing.T) {
		resp := &SimulationResponse{
			Name:   "my-policy",
			Status: "approved",
		}

		data, err := json.Marshal(resp)
		require.NoError(t, err)

		var fields map[string]json.RawMessage
		err = json.Unmarshal(data, &fields)
		require.NoError(t, err)

		assert.NotContains(t, fields, "children")
	})
}

func TestSimulateRequiresCallerRepositoryAccess(t *testing.T) {
	for _, test := range []struct {
		name            string
		tokenStatus     int
		wantStatus      int
		wantAppRequests int
	}{
		{"token cannot see repository", http.StatusNotFound, http.StatusNotFound, 0},
		{"token lacks repository scope", http.StatusForbidden, http.StatusNotFound, 0},
		{"authorized token reaches simulation", http.StatusOK, http.StatusBadRequest, 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			var appRequests atomic.Int32
			var callerPullReads atomic.Int32
			api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				if r.Header.Get("Authorization") == "Bearer installation" {
					appRequests.Add(1)
				}
				switch r.URL.Path {
				case "/user":
					_, err := fmt.Fprint(w, `{"login":"admin"}`)
					assert.NoError(t, err)
				case "/repos/testorg/private/pulls/1":
					if r.Header.Get("Authorization") == "Bearer caller" {
						callerPullReads.Add(1)
						w.WriteHeader(test.tokenStatus)
						if test.tokenStatus != http.StatusOK {
							_, err := fmt.Fprint(w, `{"message":"repository access denied"}`)
							assert.NoError(t, err)
							return
						}
					}
					_, err := fmt.Fprint(w, `{"number":1,"base":{"repo":{"id":1,"name":"private","owner":{"login":"testorg"}}},"head":{"sha":"abc"}}`)
					assert.NoError(t, err)
				case "/repos/testorg/private/collaborators/admin/permission":
					_, err := fmt.Fprint(w, `{"permission":"admin","user":{"login":"admin","permissions":{"admin":true}}}`)
					assert.NoError(t, err)
				default:
					http.NotFound(w, r)
				}
			}))
			defer api.Close()
			apiURL := api.URL + "/"
			newClient := func(token string) *github.Client {
				client, err := github.NewClient(github.WithHTTPClient(api.Client()), github.WithURLs(&apiURL, nil), github.WithAuthToken(token))
				require.NoError(t, err)
				return client
			}
			handler := &Simulate{Base: Base{
				ClientCreator: simulationAccessClients{tokenClient: newClient("caller"), installationClient: newClient("installation")},
				Installations: simulationAccessInstallations{},
			}}
			mux := goji.NewMux()
			mux.HandleFunc(pat.Post("/api/simulate/:owner/:repo/:number"), func(w http.ResponseWriter, r *http.Request) {
				require.NoError(t, handler.ServeHTTP(w, r))
			})
			request := httptest.NewRequest(http.MethodPost, "/api/simulate/testorg/private/1", strings.NewReader("invalid JSON"))
			request.Header.Set("Authorization", "Bearer caller")
			response := httptest.NewRecorder()
			mux.ServeHTTP(response, request)
			assert.Equal(t, test.wantStatus, response.Code)
			assert.Equal(t, int32(1), callerPullReads.Load())
			assert.Equal(t, int32(test.wantAppRequests), appRequests.Load())
		})
	}
}

type simulationAccessClients struct {
	stubClientCreator
	tokenClient        *github.Client
	installationClient *github.Client
}

func (c simulationAccessClients) NewTokenClient(_ string) (*github.Client, error) {
	return c.tokenClient, nil
}

func (c simulationAccessClients) NewInstallationClient(_ int64) (*github.Client, error) {
	return c.installationClient, nil
}

type simulationAccessInstallations struct {
	githubapp.InstallationsService
}

func (simulationAccessInstallations) GetByOwner(_ context.Context, _ string) (githubapp.Installation, error) {
	return githubapp.Installation{ID: 1}, nil
}
