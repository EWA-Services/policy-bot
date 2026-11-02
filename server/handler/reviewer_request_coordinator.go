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
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/palantir/policy-bot/policy/reviewer"
	"github.com/palantir/policy-bot/pull"
)

const reviewerRequestRetention = time.Minute

// ReviewerRequestCoordinator serializes review assignment for each pull request.
// Its short-lived ledger covers concurrent evaluations until GitHub's timeline is visible.
type ReviewerRequestCoordinator struct {
	mu      sync.Mutex
	entries map[string]*reviewerRequestEntry
}

type reviewerRequestEntry struct {
	mu         sync.Mutex
	references int
	expiresAt  time.Time
	blockers   map[reviewerKey]*pull.Reviewer
}

type reviewerKey struct {
	type_ pull.ReviewerType
	name  string
}

type reviewerRequestLease struct {
	coordinator *ReviewerRequestCoordinator
	entry       *reviewerRequestEntry
}

func NewReviewerRequestCoordinator() *ReviewerRequestCoordinator {
	return &ReviewerRequestCoordinator{
		entries: make(map[string]*reviewerRequestEntry),
	}
}

func (c *ReviewerRequestCoordinator) Lock(owner, repository string, number int) *reviewerRequestLease {
	key := strings.ToLower(fmt.Sprintf("%s/%s#%d", owner, repository, number))

	c.mu.Lock()
	now := time.Now()
	for entryKey, entry := range c.entries {
		if entry.references == 0 && !entry.expiresAt.After(now) {
			delete(c.entries, entryKey)
		}
	}
	entry := c.entries[key]
	if entry == nil {
		entry = &reviewerRequestEntry{blockers: make(map[reviewerKey]*pull.Reviewer)}
		c.entries[key] = entry
	}
	entry.references++
	c.mu.Unlock()

	entry.mu.Lock()
	return &reviewerRequestLease{coordinator: c, entry: entry}
}

func (l *reviewerRequestLease) Blockers() []*pull.Reviewer {
	blockers := make([]*pull.Reviewer, 0, len(l.entry.blockers))
	for _, blocker := range l.entry.blockers {
		copy := *blocker
		blockers = append(blockers, &copy)
	}
	return blockers
}

func (l *reviewerRequestLease) MarkAssigned(selection reviewer.Selection) {
	for _, user := range selection.Users {
		l.markBlocker(pull.ReviewerUser, user)
	}
	for _, team := range selection.Teams {
		l.markBlocker(pull.ReviewerTeam, team)
	}
}

func (l *reviewerRequestLease) markBlocker(reviewerType pull.ReviewerType, name string) {
	key := reviewerKey{type_: reviewerType, name: name}
	l.entry.blockers[key] = &pull.Reviewer{Type: reviewerType, Name: name}
}

func (l *reviewerRequestLease) Unlock() {
	l.entry.mu.Unlock()

	l.coordinator.mu.Lock()
	l.entry.references--
	if l.entry.references == 0 {
		l.entry.expiresAt = time.Now().Add(reviewerRequestRetention)
	}
	l.coordinator.mu.Unlock()
}
