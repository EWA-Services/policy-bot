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
	"container/list"
	"context"
	"sync"
	"time"

	"github.com/palantir/go-githubapp/appconfig"
)

// PolicyConfigCache bounds repeated GitHub reads of a repository branch's policy configuration.
type PolicyConfigCache struct {
	mu       sync.Mutex
	ttl      time.Duration
	maxSize  int64
	size     int64
	now      func() time.Time
	entries  map[SeenPolicyKey]*list.Element
	lru      *list.List
	inFlight map[SeenPolicyKey]*policyConfigLoad
}

type policyConfigCacheEntry struct {
	key       SeenPolicyKey
	config    appconfig.Config
	expiresAt time.Time
	size      int64
}

type policyConfigLoad struct {
	done   chan struct{}
	config appconfig.Config
	err    error
}

// NewPolicyConfigCache creates a byte-size-bounded cache whose entries expire after ttl.
func NewPolicyConfigCache(ttl time.Duration, maxSize int64) *PolicyConfigCache {
	return &PolicyConfigCache{
		ttl:      ttl,
		maxSize:  maxSize,
		now:      time.Now,
		entries:  make(map[SeenPolicyKey]*list.Element),
		lru:      list.New(),
		inFlight: make(map[SeenPolicyKey]*policyConfigLoad),
	}
}

func (c *PolicyConfigCache) load(
	ctx context.Context,
	key SeenPolicyKey,
	loader func(context.Context) (appconfig.Config, error),
) (appconfig.Config, error) {
	if c == nil || c.ttl <= 0 || c.maxSize <= 0 {
		return loader(ctx)
	}

	c.mu.Lock()
	now := c.now()
	if element, ok := c.entries[key]; ok {
		entry := element.Value.(*policyConfigCacheEntry)
		if now.Before(entry.expiresAt) {
			c.lru.MoveToBack(element)
			config := cloneAppConfig(entry.config)
			c.mu.Unlock()
			return config, nil
		}
		c.removeElement(element)
	}
	for _, element := range c.entries {
		entry := element.Value.(*policyConfigCacheEntry)
		if !now.Before(entry.expiresAt) {
			c.removeElement(element)
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

	go c.loadAndStore(context.WithoutCancel(ctx), key, current, loader)

	select {
	case <-ctx.Done():
		return appconfig.Config{}, ctx.Err()
	case <-current.done:
		return cloneAppConfig(current.config), current.err
	}
}

func (c *PolicyConfigCache) loadAndStore(
	ctx context.Context,
	key SeenPolicyKey,
	current *policyConfigLoad,
	loader func(context.Context) (appconfig.Config, error),
) {
	config, err := loader(ctx)

	c.mu.Lock()
	current.config = cloneAppConfig(config)
	current.err = err
	if err == nil {
		entry := &policyConfigCacheEntry{
			key:       key,
			config:    cloneAppConfig(config),
			expiresAt: c.now().Add(c.ttl),
			size:      policyConfigCacheEntrySize(key, config),
		}
		c.entries[key] = c.lru.PushBack(entry)
		c.size += entry.size
		for c.size > c.maxSize {
			c.removeElement(c.lru.Front())
		}
	}
	delete(c.inFlight, key)
	close(current.done)
	c.mu.Unlock()
}

func (c *PolicyConfigCache) removeElement(element *list.Element) {
	entry := element.Value.(*policyConfigCacheEntry)
	delete(c.entries, entry.key)
	c.lru.Remove(element)
	c.size -= entry.size
}

func cloneAppConfig(config appconfig.Config) appconfig.Config {
	config.Content = bytes.Clone(config.Content)
	return config
}

// Rough estimate of the map, list element, entry, key strings, metadata strings,
// and content retained for one cached policy.
const policyConfigCacheEntryOverhead int64 = 256

func policyConfigCacheEntrySize(key SeenPolicyKey, config appconfig.Config) int64 {
	return policyConfigCacheEntryOverhead + int64(
		len(key.Owner)+len(key.Repository)+len(key.BaseBranch)+
			len(config.Content)+len(config.Source)+len(config.Path),
	)
}
