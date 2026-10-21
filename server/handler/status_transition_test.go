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
	"testing"

	"github.com/stretchr/testify/require"
)

func TestStatusTransitionsPropagateEvaluationErrors(t *testing.T) {
	h := &Status{Base: Base{ClientCreator: failingClientCreator{}, PullOpts: &PullEvaluationOptions{StatusCheckContext: "policy-bot"}}}
	for _, state := range []string{"success", "pending", "failure", "error"} {
		t.Run(state, func(t *testing.T) {
			payload := []byte(`{"context":"SonarCloud Code Analysis","state":"` + state + `"}`)
			err := h.Handle(context.Background(), "status", "synthetic-delivery", payload)
			require.EqualError(t, err, "stub: no installation client")
		})
	}
}
