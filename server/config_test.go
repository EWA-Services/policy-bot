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

package server

import (
	"testing"
	"time"

	"github.com/c2h5oh/datasize"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestParseConfigPolicyConfigCache(t *testing.T) {
	config, err := ParseConfig([]byte("cache:\n  policy_config_ttl: 1m\n  policy_config_max_size: 10MB\n"))

	require.NoError(t, err)
	assert.Equal(t, time.Minute, config.Cache.PolicyConfigTTL)
	assert.Equal(t, 10*datasize.MB, config.Cache.PolicyConfigMaxSize)
}
