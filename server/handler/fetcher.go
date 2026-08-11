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
	"errors"
	"net/http"
	"os"
	"time"

	"github.com/google/go-github/v85/github"
	"github.com/palantir/go-githubapp/appconfig"
	"github.com/palantir/policy-bot/policy"
	"gopkg.in/yaml.v2"
)

// ConfigLoader allows ConfigFetcher to unit test policy loading decisions
type ConfigLoader interface {
	LoadConfig(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error)
}

type FetchedConfig struct {
	Config     *policy.Config
	LoadError  error
	ParseError error

	Source string
	Path   string

	SeenPolicy bool
}

type ConfigFetcher struct {
	Loader            ConfigLoader
	SeenPolicyCache   *SeenPolicyCache
	PolicyConfigCache *PolicyConfigCache
}

func (cf *ConfigFetcher) ConfigForRepositoryBranch(ctx context.Context, client *github.Client, owner, repository, branch string) FetchedConfig {
	key := SeenPolicyKey{
		Owner:      owner,
		Repository: repository,
		BaseBranch: branch,
	}

	load := func(loadCtx context.Context) (appconfig.Config, error) {
		return cf.loadConfigWithRetries(loadCtx, client, owner, repository, branch)
	}

	var c appconfig.Config
	var err error
	if cf.PolicyConfigCache == nil {
		c, err = load(ctx)
	} else {
		c, err = cf.PolicyConfigCache.load(ctx, key, load)
	}

	fc := FetchedConfig{
		Source: c.Source,
		Path:   c.Path,
	}
	if err != nil {
		fc.SeenPolicy = cf.SeenPolicyCache.Get(key)
		fc.LoadError = err
		return fc
	}
	if c.IsUndefined() {
		return fc
	}

	cf.SeenPolicyCache.Set(key)
	// Mark the branch as having seen a policy even if parsing fails below.
	fc.SeenPolicy = true

	var pc policy.Config
	if err := yaml.UnmarshalStrict(c.Content, &pc); err != nil {
		fc.ParseError = err
	} else {
		fc.Config = &pc
	}
	return fc
}

func (cf *ConfigFetcher) loadConfigWithRetries(
	ctx context.Context,
	client *github.Client,
	owner, repository, branch string,
) (appconfig.Config, error) {
	retries := 0
	delay := 1 * time.Second
	for {
		config, err := cf.Loader.LoadConfig(ctx, client, owner, repository, branch)
		if err == nil || (!os.IsTimeout(err) && !isServerError(err)) {
			return config, err
		}

		retries++
		if retries > 3 {
			return config, err
		}

		select {
		case <-ctx.Done():
			return config, ctx.Err()
		case <-time.After(delay):
			delay *= 2
		}
	}
}

func isServerError(err error) bool {
	var ghErr *github.ErrorResponse
	if errors.As(err, &ghErr) {
		switch ghErr.Response.StatusCode {
		case http.StatusInternalServerError, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			return true
		}
	}
	return false
}
