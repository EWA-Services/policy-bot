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
	"errors"

	"github.com/google/go-github/v85/github"
	"github.com/palantir/go-githubapp/githubapp"
	"github.com/shurcooL/githubv4"
	"golang.org/x/oauth2"
)

// stubClientCreator implements githubapp.ClientCreator with zero-value clients.
// It is the minimum surface needed for handler unit tests that only exercise
// code paths up to (but not inside) GitHub API calls. The returned clients are
// never actually used to send requests by the early-exit pre-check, but they
// must be non-nil so the handler advances past the client-creation guard.
type stubClientCreator struct{}

var _ githubapp.ClientCreator = stubClientCreator{}

func (stubClientCreator) NewAppClient() (*github.Client, error) {
	return github.NewClient(nil), nil
}

func (stubClientCreator) NewAppV4Client() (*githubv4.Client, error) {
	return &githubv4.Client{}, nil
}

func (stubClientCreator) NewInstallationClient(_ int64) (*github.Client, error) {
	return github.NewClient(nil), nil
}

func (stubClientCreator) NewInstallationV4Client(_ int64) (*githubv4.Client, error) {
	return &githubv4.Client{}, nil
}

func (stubClientCreator) NewTokenSourceClient(_ oauth2.TokenSource) (*github.Client, error) {
	return github.NewClient(nil), nil
}

func (stubClientCreator) NewTokenSourceV4Client(_ oauth2.TokenSource) (*githubv4.Client, error) {
	return &githubv4.Client{}, nil
}

func (stubClientCreator) NewTokenClient(_ string) (*github.Client, error) {
	return github.NewClient(nil), nil
}

func (stubClientCreator) NewTokenV4Client(_ string) (*githubv4.Client, error) {
	return &githubv4.Client{}, nil
}

// failingClientCreator returns errors from NewInstallationClient so tests can
// exercise the fail-open behaviour when client creation breaks.
type failingClientCreator struct{}

var _ githubapp.ClientCreator = failingClientCreator{}

func (failingClientCreator) NewAppClient() (*github.Client, error) {
	return nil, errors.New("stub: no app client")
}

func (failingClientCreator) NewAppV4Client() (*githubv4.Client, error) {
	return nil, errors.New("stub: no app v4 client")
}

func (failingClientCreator) NewInstallationClient(_ int64) (*github.Client, error) {
	return nil, errors.New("stub: no installation client")
}

func (failingClientCreator) NewInstallationV4Client(_ int64) (*githubv4.Client, error) {
	return nil, errors.New("stub: no installation v4 client")
}

func (failingClientCreator) NewTokenSourceClient(_ oauth2.TokenSource) (*github.Client, error) {
	return nil, errors.New("stub: no token source client")
}

func (failingClientCreator) NewTokenSourceV4Client(_ oauth2.TokenSource) (*githubv4.Client, error) {
	return nil, errors.New("stub: no token source v4 client")
}

func (failingClientCreator) NewTokenClient(_ string) (*github.Client, error) {
	return nil, errors.New("stub: no token client")
}

func (failingClientCreator) NewTokenV4Client(_ string) (*githubv4.Client, error) {
	return nil, errors.New("stub: no token v4 client")
}
