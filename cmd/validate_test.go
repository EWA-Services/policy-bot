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

package cmd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestValidationCmd(t *testing.T) {
	for _, test := range []struct {
		name      string
		policy    string
		wantError bool
	}{
		{"local policy", "policy:\n  approval: [approved]\napproval_rules:\n  - name: approved\n    requires:\n      count: 0\n", false},
		{"remote policy", "remote: testorg/policy-config\npath: .policy.yml\nref: main\n", false},
		{"remote without owner", "remote: policy-config\n", true},
		{"empty owner", "remote: /policy-config\n", true},
		{"empty repository", "remote: testorg/\n", true},
		{"empty remote", "remote: \"\"\n", true},
		{"invalid YAML", "policy: [\n", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "policy.yml")
			require.NoError(t, os.WriteFile(path, []byte(test.policy), 0600))
			originalPath := validateCmdConfig.Path
			validateCmdConfig.Path = path
			t.Cleanup(func() { validateCmdConfig.Path = originalPath })
			err := validationCmd(nil, nil)
			if test.wantError {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
			}
		})
	}
}
