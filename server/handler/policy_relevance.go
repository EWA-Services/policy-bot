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
	"github.com/palantir/policy-bot/policy"
	"github.com/palantir/policy-bot/policy/predicate"
)

// policyStatusNames returns the union of all status check names referenced by
// `has_status` and `has_successful_status` predicates anywhere in the policy:
// rule predicates (`if:`), rule conditions (`requires.conditions:`), and the
// disapproval predicates. The second return value reports whether the policy
// contains any `has_status` / `has_successful_status` block at all; when
// false, callers should NOT use the returned set to make skip decisions
// because there is no signal about which statuses the policy cares about.
//
// The function inspects every rule defined in `approval_rules`, not only the
// rules referenced from `policy.approval`. This is intentional and
// conservative: it avoids dropping events for rules that are defined but
// referenced via a remote / templated policy file, and it sidesteps having to
// re-implement the rule-name resolution in approval.Policy.Parse.
func policyStatusNames(cfg *policy.Config) (names map[string]struct{}, hasAny bool) {
	names = make(map[string]struct{})
	if cfg == nil {
		return names, false
	}

	for _, rule := range cfg.ApprovalRules {
		if rule == nil {
			continue
		}
		hasAny = collectStatusNames(&rule.Predicates, names) || hasAny
		hasAny = collectStatusNames(&rule.Requires.Conditions, names) || hasAny
	}
	if cfg.Policy.Disapproval != nil {
		hasAny = collectStatusNames(&cfg.Policy.Disapproval.Predicates, names) || hasAny
	}

	return names, hasAny
}

// policyWorkflowNames returns the union of all workflow names referenced by
// `has_workflow_result` predicates anywhere in the policy. The second return
// value reports whether the policy contains any `has_workflow_result` block at
// all; when false, callers should NOT use the returned set to make skip
// decisions.
func policyWorkflowNames(cfg *policy.Config) (names map[string]struct{}, hasAny bool) {
	names = make(map[string]struct{})
	if cfg == nil {
		return names, false
	}

	for _, rule := range cfg.ApprovalRules {
		if rule == nil {
			continue
		}
		hasAny = collectWorkflowNames(&rule.Predicates, names) || hasAny
		hasAny = collectWorkflowNames(&rule.Requires.Conditions, names) || hasAny
	}
	if cfg.Policy.Disapproval != nil {
		hasAny = collectWorkflowNames(&cfg.Policy.Disapproval.Predicates, names) || hasAny
	}

	return names, hasAny
}

// collectStatusNames adds status names from the supplied predicate bundle to
// out and returns true if the bundle contains at least one `has_status` or
// `has_successful_status` block.
func collectStatusNames(p *predicate.Predicates, out map[string]struct{}) bool {
	if p == nil {
		return false
	}
	found := false
	if p.HasStatus != nil {
		found = true
		for _, s := range p.HasStatus.Statuses {
			out[s] = struct{}{}
		}
	}
	if p.HasSuccessfulStatus != nil {
		found = true
		for _, s := range *p.HasSuccessfulStatus {
			out[s] = struct{}{}
		}
	}
	return found
}

// collectWorkflowNames adds workflow names from the supplied predicate bundle
// to out and returns true if the bundle contains at least one
// `has_workflow_result` block.
func collectWorkflowNames(p *predicate.Predicates, out map[string]struct{}) bool {
	if p == nil {
		return false
	}
	if p.HasWorkflowResult == nil {
		return false
	}
	for _, w := range p.HasWorkflowResult.Workflows {
		out[w] = struct{}{}
	}
	return true
}
