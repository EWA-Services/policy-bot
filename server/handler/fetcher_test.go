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
	"runtime"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/go-github/v85/github"
	"github.com/palantir/go-githubapp/appconfig"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

type mockConfigLoader struct {
	loadConfig func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error)
}

func (s mockConfigLoader) LoadConfig(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
	return s.loadConfig(ctx, client, owner, repo, ref)
}

func TestConfigFetcherMarksSeenPolicy(t *testing.T) {
	cache := NewSeenPolicyCache()

	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				return appconfig.Config{
					Content: []byte("policy: ["),
					Source:  "testorg/testrepo@main",
					Path:    ".policy.yml",
				}, nil
			},
		},
		SeenPolicyCache: cache,
	}

	fc := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	require.Error(t, fc.ParseError)
	assert.True(t, fc.SeenPolicy)

	ok := cache.Get(SeenPolicyKey{
		Owner:      "testorg",
		Repository: "testrepo",
		BaseBranch: "main",
	})
	require.True(t, ok)
}

func TestConfigFetcherChecksSeenPolicyOnLoadError(t *testing.T) {
	cache := NewSeenPolicyCache()
	cache.Set(SeenPolicyKey{
		Owner:      "testorg",
		Repository: "testrepo",
		BaseBranch: "main",
	})

	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				return appconfig.Config{
					Source: "testorg/testrepo@main",
					Path:   ".policy.yml",
				}, errors.New("request failed")
			},
		},
		SeenPolicyCache: cache,
	}

	fc := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	require.Error(t, fc.LoadError)
	assert.True(t, fc.SeenPolicy)
}

func TestConfigFetcherScopesSeenPolicyByBranch(t *testing.T) {
	const releaseBranch = "release"

	cache := NewSeenPolicyCache()
	cache.Set(SeenPolicyKey{
		Owner:      "testorg",
		Repository: "testrepo",
		BaseBranch: "main",
	})

	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				return appconfig.Config{
					Source: "testorg/testrepo@" + releaseBranch,
					Path:   ".policy.yml",
				}, errors.New("request failed")
			},
		},
		SeenPolicyCache: cache,
	}

	fc := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", releaseBranch)
	require.Error(t, fc.LoadError)
	assert.False(t, fc.SeenPolicy)
}

func TestConfigFetcherCachesSuccessfulPolicyLoads(t *testing.T) {
	var calls int
	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				calls++
				return validAppConfig(owner, repo, ref), nil
			},
		},
		SeenPolicyCache:   NewSeenPolicyCache(),
		PolicyConfigCache: NewPolicyConfigCache(time.Minute, 1<<20),
	}

	first := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	second := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")

	require.NotNil(t, first.Config)
	require.NotNil(t, second.Config)
	assert.NotSame(t, first.Config, second.Config)
	assert.Equal(t, 1, calls)
}

func TestConfigFetcherDoesNotCachePolicyLoadErrors(t *testing.T) {
	var calls int
	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				calls++
				return appconfig.Config{}, errors.New("request failed")
			},
		},
		SeenPolicyCache:   NewSeenPolicyCache(),
		PolicyConfigCache: NewPolicyConfigCache(time.Minute, 1<<20),
	}

	first := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	second := fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")

	require.Error(t, first.LoadError)
	require.Error(t, second.LoadError)
	assert.Equal(t, 2, calls)
}

func TestConfigFetcherReloadsExpiredPolicyConfig(t *testing.T) {
	now := time.Date(2026, time.August, 11, 7, 0, 0, 0, time.UTC)
	cache := NewPolicyConfigCache(time.Minute, 1<<20)
	cache.now = func() time.Time { return now }

	var calls int
	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				calls++
				return validAppConfig(owner, repo, ref), nil
			},
		},
		SeenPolicyCache:   NewSeenPolicyCache(),
		PolicyConfigCache: cache,
	}

	fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	now = now.Add(time.Minute)
	fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")

	assert.Equal(t, 2, calls)
}

func TestConfigFetcherCoalescesConcurrentPolicyLoads(t *testing.T) {
	previousMaxProcs := runtime.GOMAXPROCS(1)
	t.Cleanup(func() { runtime.GOMAXPROCS(previousMaxProcs) })

	const callers = 10
	var ready sync.WaitGroup
	ready.Add(callers)
	var calls atomic.Int32

	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				calls.Add(1)
				ready.Wait()
				return validAppConfig(owner, repo, ref), nil
			},
		},
		SeenPolicyCache:   NewSeenPolicyCache(),
		PolicyConfigCache: NewPolicyConfigCache(time.Minute, 1<<20),
	}

	results := make(chan FetchedConfig, callers)
	for range callers {
		go func() {
			ready.Done()
			results <- fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
		}()
	}

	for range callers {
		result := <-results
		require.NotNil(t, result.Config)
	}
	assert.Equal(t, int32(1), calls.Load())
}

func TestConfigFetcherCancellationDoesNotCancelSharedPolicyLoad(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	var calls atomic.Int32

	fetcher := ConfigFetcher{
		Loader: mockConfigLoader{
			loadConfig: func(ctx context.Context, client *github.Client, owner, repo, ref string) (appconfig.Config, error) {
				calls.Add(1)
				close(started)
				<-release
				return validAppConfig(owner, repo, ref), nil
			},
		},
		SeenPolicyCache:   NewSeenPolicyCache(),
		PolicyConfigCache: NewPolicyConfigCache(time.Minute, 1<<20),
	}

	ctx, cancel := context.WithCancel(context.Background())
	leaderResult := make(chan FetchedConfig, 1)
	go func() {
		leaderResult <- fetcher.ConfigForRepositoryBranch(ctx, nil, "testorg", "testrepo", "main")
	}()
	<-started
	cancel()

	leader := <-leaderResult
	require.ErrorIs(t, leader.LoadError, context.Canceled)

	waiterResult := make(chan FetchedConfig, 1)
	go func() {
		waiterResult <- fetcher.ConfigForRepositoryBranch(context.Background(), nil, "testorg", "testrepo", "main")
	}()
	close(release)

	waiter := <-waiterResult
	require.NoError(t, waiter.LoadError)
	require.NotNil(t, waiter.Config)
	assert.Equal(t, int32(1), calls.Load())
}

func TestPolicyConfigCacheEvictsEntriesOverSizeLimit(t *testing.T) {
	first := validAppConfig("testorg", "first", "main")
	second := validAppConfig("testorg", "second", "main")
	maxSize := policyConfigCacheEntrySize(SeenPolicyKey{
		Owner:      "testorg",
		Repository: "first",
		BaseBranch: "main",
	}, first)
	cache := NewPolicyConfigCache(time.Minute, maxSize)

	var firstCalls atomic.Int32
	firstLoader := func(context.Context) (appconfig.Config, error) {
		firstCalls.Add(1)
		return first, nil
	}
	var secondCalls atomic.Int32
	secondLoader := func(context.Context) (appconfig.Config, error) {
		secondCalls.Add(1)
		return second, nil
	}
	firstKey := SeenPolicyKey{Owner: "testorg", Repository: "first", BaseBranch: "main"}
	secondKey := SeenPolicyKey{Owner: "testorg", Repository: "second", BaseBranch: "main"}

	_, err := cache.load(context.Background(), firstKey, firstLoader)
	require.NoError(t, err)
	_, err = cache.load(context.Background(), secondKey, secondLoader)
	require.NoError(t, err)
	_, err = cache.load(context.Background(), firstKey, firstLoader)
	require.NoError(t, err)

	assert.Equal(t, int32(2), firstCalls.Load())
	assert.Equal(t, int32(1), secondCalls.Load())
	assert.LessOrEqual(t, cache.size, maxSize)
}

func validAppConfig(owner, repo, ref string) appconfig.Config {
	return appconfig.Config{
		Content: []byte("policy:\n  approval: []\n"),
		Source:  owner + "/" + repo + "@" + ref,
		Path:    ".policy.yml",
	}
}
