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
	"testing"

	"github.com/palantir/policy-bot/policy"
	"github.com/palantir/policy-bot/policy/approval"
	"github.com/palantir/policy-bot/policy/disapproval"
	"github.com/palantir/policy-bot/policy/predicate"
	"github.com/stretchr/testify/assert"
	"gopkg.in/yaml.v2"
)

// parseConfig parses a YAML .policy.yml fragment into a policy.Config the same
// way ConfigFetcher does at runtime.
func parseConfig(t *testing.T, src string) *policy.Config {
	t.Helper()
	var cfg policy.Config
	if err := yaml.UnmarshalStrict([]byte(src), &cfg); err != nil {
		t.Fatalf("failed to parse policy fragment: %v", err)
	}
	return &cfg
}

func TestPolicyStatusNames(t *testing.T) {
	t.Run("nil config returns empty", func(t *testing.T) {
		names, hasAny := policyStatusNames(nil)
		assert.False(t, hasAny)
		assert.Empty(t, names)
	})

	t.Run("policy with no has_status blocks returns hasAny=false", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    requires:
      count: 1
      teams: ["org/team"]
`)
		names, hasAny := policyStatusNames(cfg)
		assert.False(t, hasAny)
		assert.Empty(t, names)
	})

	t.Run("collects names from has_status in rule predicates", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_status:
        statuses:
          - "Validate PR Title"
          - "GitGuardian Security Checks"
        conclusions: ["success"]
    requires:
      count: 0
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "Validate PR Title")
		assert.Contains(t, names, "GitGuardian Security Checks")
		assert.Len(t, names, 2)
	})

	t.Run("collects names from has_successful_status (deprecated form)", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_successful_status:
        - "ci/build"
        - "ci/test"
    requires:
      count: 0
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "ci/build")
		assert.Contains(t, names, "ci/test")
		assert.Len(t, names, 2)
	})

	t.Run("collects names from requires.conditions", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    requires:
      count: 1
      teams: ["org/team"]
      conditions:
        has_status:
          statuses: ["ci/required"]
          conclusions: ["success"]
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "ci/required")
	})

	t.Run("collects names from disapproval predicates", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  disapproval:
    if:
      has_status:
        statuses: ["broken/check"]
        conclusions: ["failure"]
    requires:
      users: ["someone"]
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "broken/check")
	})

	t.Run("dedupes names across rules", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - or:
        - rule1
        - rule2
approval_rules:
  - name: rule1
    if:
      has_status:
        statuses: ["ci/test"]
    requires:
      count: 0
  - name: rule2
    if:
      has_status:
        statuses: ["ci/test", "ci/lint"]
    requires:
      count: 0
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Len(t, names, 2)
		assert.Contains(t, names, "ci/test")
		assert.Contains(t, names, "ci/lint")
	})

	t.Run("collects from unreferenced rules too (conservative)", func(t *testing.T) {
		// rule2 is defined but not referenced from policy.approval. We still
		// collect its names — better to skip a real event less aggressively
		// than to drop one for a rule referenced via remote/templated policy.
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_status:
        statuses: ["ci/referenced"]
    requires:
      count: 0
  - name: rule2
    if:
      has_status:
        statuses: ["ci/unreferenced"]
    requires:
      count: 0
`)
		names, hasAny := policyStatusNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "ci/referenced")
		assert.Contains(t, names, "ci/unreferenced")
	})
}

func TestPolicyWorkflowNames(t *testing.T) {
	t.Run("nil config returns empty", func(t *testing.T) {
		names, hasAny := policyWorkflowNames(nil)
		assert.False(t, hasAny)
		assert.Empty(t, names)
	})

	t.Run("policy with no has_workflow_result returns hasAny=false", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_status:
        statuses: ["ci/test"]
    requires:
      count: 0
`)
		names, hasAny := policyWorkflowNames(cfg)
		assert.False(t, hasAny)
		assert.Empty(t, names)
	})

	t.Run("collects names from has_workflow_result in rule predicates", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    if:
      has_workflow_result:
        workflows:
          - "CI"
          - "Deploy"
    requires:
      count: 0
`)
		names, hasAny := policyWorkflowNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "CI")
		assert.Contains(t, names, "Deploy")
		assert.Len(t, names, 2)
	})

	t.Run("collects names from requires.conditions", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  approval:
    - rule1
approval_rules:
  - name: rule1
    requires:
      count: 1
      teams: ["org/team"]
      conditions:
        has_workflow_result:
          workflows: ["RequiredWorkflow"]
`)
		names, hasAny := policyWorkflowNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "RequiredWorkflow")
	})

	t.Run("collects from disapproval predicates", func(t *testing.T) {
		cfg := parseConfig(t, `
policy:
  disapproval:
    if:
      has_workflow_result:
        workflows: ["BlockingWorkflow"]
    requires:
      users: ["someone"]
`)
		names, hasAny := policyWorkflowNames(cfg)
		assert.True(t, hasAny)
		assert.Contains(t, names, "BlockingWorkflow")
	})
}

// TestCollectStatusNames_NilDisapproval guards against a nil-deref regression:
// policy.Config.Policy.Disapproval is a pointer and may be nil for repos that
// only define approval rules.
func TestCollectStatusNames_NilDisapproval(t *testing.T) {
	cfg := &policy.Config{
		Policy: policy.Policy{
			Disapproval: nil,
		},
		ApprovalRules: []*approval.Rule{
			{
				Name: "rule1",
				Predicates: predicate.Predicates{
					HasStatus: predicate.NewHasStatus([]string{"x"}, []string{"success"}),
				},
			},
		},
	}
	names, hasAny := policyStatusNames(cfg)
	assert.True(t, hasAny)
	assert.Contains(t, names, "x")
}

// TestCollectStatusNames_NilRuleInSlice guards against the case where a rule
// pointer in ApprovalRules is nil (defensive — should not happen via YAML
// parse, but cheap to handle).
func TestCollectStatusNames_NilRuleInSlice(t *testing.T) {
	cfg := &policy.Config{
		ApprovalRules: []*approval.Rule{nil},
	}
	names, hasAny := policyStatusNames(cfg)
	assert.False(t, hasAny)
	assert.Empty(t, names)
}

// TestCollectStatusNames_OnlyDisapproval verifies that a policy with only a
// disapproval `has_status` block (no approval rules) still produces names.
func TestCollectStatusNames_OnlyDisapproval(t *testing.T) {
	cfg := &policy.Config{
		Policy: policy.Policy{
			Disapproval: &disapproval.Policy{
				Predicates: predicate.Predicates{
					HasStatus: predicate.NewHasStatus([]string{"never-ready"}, []string{"failure"}),
				},
			},
		},
	}
	names, hasAny := policyStatusNames(cfg)
	assert.True(t, hasAny)
	assert.Contains(t, names, "never-ready")
}
