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
	"bytes"
	"context"
	"sync"
	"time"

	"github.com/palantir/go-githubapp/appconfig"
)

// PolicyConfigCache bounds repeated GitHub reads of a repository branch's policy configuration.
type PolicyConfigCache struct {
	mu       sync.Mutex
	ttl      time.Duration
	now      func() time.Time
	entries  map[SeenPolicyKey]policyConfigCacheEntry
	inFlight map[SeenPolicyKey]*policyConfigLoad
}

type policyConfigCacheEntry struct {
	config    appconfig.Config
	expiresAt time.Time
}

type policyConfigLoad struct {
	done   chan struct{}
	config appconfig.Config
	err    error
}

// NewPolicyConfigCache creates a cache whose entries expire after ttl.
func NewPolicyConfigCache(ttl time.Duration) *PolicyConfigCache {
	return &PolicyConfigCache{
		ttl:      ttl,
		now:      time.Now,
		entries:  make(map[SeenPolicyKey]policyConfigCacheEntry),
		inFlight: make(map[SeenPolicyKey]*policyConfigLoad),
	}
}

func (c *PolicyConfigCache) load(
	ctx context.Context,
	key SeenPolicyKey,
	loader func() (appconfig.Config, error),
) (appconfig.Config, error) {
	if c == nil || c.ttl <= 0 {
		return loader()
	}

	c.mu.Lock()
	now := c.now()
	if entry, ok := c.entries[key]; ok && now.Before(entry.expiresAt) {
		config := cloneAppConfig(entry.config)
		c.mu.Unlock()
		return config, nil
	}
	delete(c.entries, key)
	for cachedKey, entry := range c.entries {
		if !now.Before(entry.expiresAt) {
			delete(c.entries, cachedKey)
		}
	}

	if current, ok := c.inFlight[key]; ok {
		c.mu.Unlock()
		select {
		case <-ctx.Done():
			return appconfig.Config{}, ctx.Err()
		case <-current.done:
			return cloneAppConfig(current.config), current.err
		}
	}

	current := &policyConfigLoad{done: make(chan struct{})}
	c.inFlight[key] = current
	c.mu.Unlock()

	config, err := loader()

	c.mu.Lock()
	current.config = cloneAppConfig(config)
	current.err = err
	if err == nil {
		c.entries[key] = policyConfigCacheEntry{
			config:    cloneAppConfig(config),
			expiresAt: c.now().Add(c.ttl),
		}
	}
	delete(c.inFlight, key)
	close(current.done)
	c.mu.Unlock()

	return config, err
}

func cloneAppConfig(config appconfig.Config) appconfig.Config {
	config.Content = bytes.Clone(config.Content)
	return config
}
