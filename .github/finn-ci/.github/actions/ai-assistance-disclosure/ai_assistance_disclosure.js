const WORKFLOW_TRACE_HEADER = '### Workflow trace';
const WHY_NOT_HEADER = '### Why not AI-assisted';
const MIN_CHARS = 50;
const INTRO_MARKER = '<!-- ai-label-check:intro -->';
const INTRO_COMMENT_AUTHOR = 'github-actions[bot]';
const OPERATING_MODEL_URL = 'https://www.notion.so/362032aabccd8145964ff3b40790b4c7';
const AI_ASSISTED_LABEL = {
  name: 'ai-assisted',
  color: '0e8a16',
  description: 'PR was AI-assisted (Codex or other). Workflow trace required.',
};
const NOT_AI_ASSISTED_LABEL = {
  name: 'not-ai-assisted',
  color: 'fbca04',
  description: 'PR written without AI assistance. Why-not required.',
};
const LABELS = [AI_ASSISTED_LABEL, NOT_AI_ASSISTED_LABEL];
const AUTOMATION_SKIP_AUTHORS = new Set(['finn-devops']);
const AUTOMATION_TITLE_SKIP_AUTHORS = new Set(['github-actions[bot]']);
const AUTOMATION_SKIP_TITLE_PATTERNS = [/^ci:\s*sync\b/i, /^chore:\s*sync\b/i, /^sync\b/i];

function stripHtmlComments(markdown) {
  return markdown.replace(/<!--[\s\S]*?-->/g, '');
}

function extractSection(markdown, header) {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const start = lines.findIndex((line) => line === header);
  if (start === -1) {
    return '';
  }

  const sectionLines = [];
  for (const line of lines.slice(start + 1)) {
    if (/^#{1,6} /.test(line)) {
      break;
    }
    sectionLines.push(line);
  }
  return sectionLines.join('\n');
}

function countNonWhitespace(value) {
  return value.replace(/\s/g, '').length;
}

function hasMarkdownBulletList(value) {
  const contentLines = value.split('\n').filter((line) => line.trim().length > 0);
  let sawBullet = false;

  return (
    contentLines.length > 0 &&
    contentLines.every((line) => {
      if (/^\s*[-*+] \S/.test(line)) {
        sawBullet = true;
        return true;
      }
      return sawBullet && /^\s{2,}\S/.test(line);
    }) &&
    sawBullet
  );
}

function disclosureFailure(labels, body) {
  const labelNames = new Set(labels);
  const hasAiAssisted = labelNames.has(AI_ASSISTED_LABEL.name);
  const hasNotAiAssisted = labelNames.has(NOT_AI_ASSISTED_LABEL.name);

  if (hasAiAssisted && hasNotAiAssisted) {
    return {
      code: 'both-labels',
      message: 'Apply exactly one AI assistance label: both ai-assisted and not-ai-assisted are present.',
    };
  }

  if (!hasAiAssisted && !hasNotAiAssisted) {
    return {
      code: 'missing-label',
      message: 'Apply exactly one AI assistance label: ai-assisted or not-ai-assisted is required.',
    };
  }

  const expectedHeader = hasAiAssisted ? WORKFLOW_TRACE_HEADER : WHY_NOT_HEADER;
  const section = extractSection(stripHtmlComments(body || ''), expectedHeader);
  const nonWhitespaceChars = countNonWhitespace(section);

  if (nonWhitespaceChars < MIN_CHARS) {
    return {
      code: 'short-section',
      expectedHeader,
      nonWhitespaceChars,
      message:
        `${expectedHeader} must contain at least ${MIN_CHARS} non-whitespace characters ` +
        `after HTML comments are stripped; found ${nonWhitespaceChars}.`,
    };
  }

  if (hasAiAssisted && !hasMarkdownBulletList(section)) {
    return {
      code: 'workflow-trace-not-bulleted',
      expectedHeader,
      message: `${expectedHeader} must be a readable Markdown bullet list for ai-assisted PRs.`,
    };
  }

  return null;
}

function shouldSkipPullRequest(pullRequest) {
  if (pullRequest.draft) {
    return 'Draft PR: skipping AI Label Check.';
  }

  if (pullRequest.user && pullRequest.user.login === 'dependabot[bot]') {
    return 'Dependabot PR: skipping AI Label Check.';
  }

  if (isAutomationPullRequest(pullRequest)) {
    const author = (pullRequest.user && pullRequest.user.login) || 'unknown author';
    return `Automation PR by ${author}: skipping AI Label Check.`;
  }

  return null;
}

function isAutomationPullRequest(pullRequest) {
  const author = pullRequest.user && pullRequest.user.login;
  const title = pullRequest.title || '';

  return (
    (author && AUTOMATION_SKIP_AUTHORS.has(author)) ||
    (author &&
      AUTOMATION_TITLE_SKIP_AUTHORS.has(author) &&
      AUTOMATION_SKIP_TITLE_PATTERNS.some((pattern) => pattern.test(title)))
  );
}

function isAlreadyExistsError(error) {
  if (!error || error.status !== 422) {
    return false;
  }

  const errors = (error.response && error.response.data && error.response.data.errors) || [];
  return errors.some((entry) => entry && entry.code === 'already_exists');
}

// Treat a concurrent cleanup as success when another run already removed the comment.
function isNotFoundError(error) {
  return Boolean(error && error.status === 404);
}

function introCommentBody() {
  return `${INTRO_MARKER}
AI usage details are required on this PR.

Exactly one of \`ai-assisted\` or \`not-ai-assisted\` is required.

- \`ai-assisted\` requires \`${WORKFLOW_TRACE_HEADER}\`.
- \`${WORKFLOW_TRACE_HEADER}\` must be a Markdown bullet list. Emoji markers are encouraged for scanability, for example: 💬 Prompt, 🛠️ Built, 🔁 Adjusted, ✅ Verified.
- \`not-ai-assisted\` requires \`${WHY_NOT_HEADER}\`.

Operating model: ${OPERATING_MODEL_URL}`;
}

async function ensureLabels({ github, owner, repo }) {
  const existing = await github.paginate(github.rest.issues.listLabelsForRepo, {
    owner,
    repo,
    per_page: 100,
  });
  const existingByName = new Map(existing.map((label) => [label.name, label]));

  for (const label of LABELS) {
    const existingLabel = existingByName.get(label.name);
    if (!existingLabel) {
      try {
        await github.rest.issues.createLabel({
          owner,
          repo,
          name: label.name,
          color: label.color,
          description: label.description,
        });
      } catch (error) {
        if (!isAlreadyExistsError(error)) {
          throw error;
        }
      }
      continue;
    }

    if (existingLabel.color !== label.color || existingLabel.description !== label.description) {
      await github.rest.issues.updateLabel({
        owner,
        repo,
        name: label.name,
        color: label.color,
        description: label.description,
      });
    }
  }
}

async function listIntroComments({ github, owner, repo, issue_number }) {
  const comments = await github.paginate(github.rest.issues.listComments, {
    owner,
    repo,
    issue_number,
    per_page: 100,
  });

  return comments.filter(
    (comment) =>
      comment.user &&
      comment.user.login === INTRO_COMMENT_AUTHOR &&
      comment.body &&
      comment.body.includes(INTRO_MARKER),
  );
}

// Keep the oldest marked comment and remove stale duplicates left by earlier races.
async function consolidateIntroComments({ github, owner, repo, issue_number, comments }) {
  if (comments.length === 0) {
    return;
  }

  const [canonical, ...duplicates] = [...comments].sort((left, right) => left.id - right.id);
  await github.rest.issues.updateComment({
    owner,
    repo,
    comment_id: canonical.id,
    body: introCommentBody(),
  });

  for (const duplicate of duplicates) {
    try {
      await github.rest.issues.deleteComment({
        owner,
        repo,
        comment_id: duplicate.id,
      });
    } catch (error) {
      if (!isNotFoundError(error)) {
        throw error;
      }
    }
  }
}

async function upsertIntroComment({ github, owner, repo, issue_number }) {
  let comments = await listIntroComments({ github, owner, repo, issue_number });

  if (comments.length === 0) {
    await github.rest.issues.createComment({
      owner,
      repo,
      issue_number,
      body: introCommentBody(),
    });
    comments = await listIntroComments({ github, owner, repo, issue_number });
  }

  await consolidateIntroComments({ github, owner, repo, issue_number, comments });
}

// GitHub does not guarantee concurrency-group ordering, so validate the current API state.
async function fetchCurrentPullRequest({ github, owner, repo, pull_number }) {
  const response = await github.rest.pulls.get({ owner, repo, pull_number });
  return response.data;
}

async function run({ github, context, core }) {
  const eventPullRequest = context.payload.pull_request;
  if (!eventPullRequest) {
    core.setFailed('AI Label Check can only run for pull_request events.');
    return;
  }

  const { owner, repo } = context.repo;
  const issue_number = eventPullRequest.number;
  const pullRequest = await fetchCurrentPullRequest({
    github,
    owner,
    repo,
    pull_number: issue_number,
  });

  const skipReason = shouldSkipPullRequest(pullRequest);
  if (skipReason) {
    core.info(skipReason);
    return;
  }

  await ensureLabels({ github, owner, repo });
  await upsertIntroComment({ github, owner, repo, issue_number });

  const labels = pullRequest.labels.map((label) => label.name);
  const failure = disclosureFailure(labels, pullRequest.body || '');
  if (failure) {
    core.setFailed(failure.message);
    return;
  }

  core.info('AI Label Check passed.');
}

module.exports = {
  AI_ASSISTED_LABEL,
  AUTOMATION_SKIP_AUTHORS,
  AUTOMATION_SKIP_TITLE_PATTERNS,
  AUTOMATION_TITLE_SKIP_AUTHORS,
  INTRO_COMMENT_AUTHOR,
  INTRO_MARKER,
  LABELS,
  MIN_CHARS,
  NOT_AI_ASSISTED_LABEL,
  OPERATING_MODEL_URL,
  WHY_NOT_HEADER,
  WORKFLOW_TRACE_HEADER,
  countNonWhitespace,
  disclosureFailure,
  ensureLabels,
  extractSection,
  fetchCurrentPullRequest,
  hasMarkdownBulletList,
  introCommentBody,
  isNotFoundError,
  isAutomationPullRequest,
  isAlreadyExistsError,
  listIntroComments,
  consolidateIntroComments,
  run,
  shouldSkipPullRequest,
  stripHtmlComments,
  upsertIntroComment,
};
