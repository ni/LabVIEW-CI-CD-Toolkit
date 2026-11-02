import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const nodeRequire = createRequire(import.meta.url);
const workflowPath = path.join(root, '.github/workflows/vidiff-deploy.yml');
const workflowText = fs.readFileSync(workflowPath, 'utf8');

function extractScript(stepName) {
  const marker = `- name: ${stepName}`;
  const start = workflowText.indexOf(marker);
  assert.notEqual(start, -1, `missing step ${stepName}`);
  const scriptMarker = 'script: |';
  const scriptStart = workflowText.indexOf(scriptMarker, start);
  assert.notEqual(scriptStart, -1, `missing script for ${stepName}`);
  const bodyStart = scriptStart + scriptMarker.length + 1;
  const rest = workflowText.slice(bodyStart);
  const lines = rest.split('\n');
  const body = [];
  for (const line of lines) {
    if (line.startsWith('      - name: ')) break;
    if (line.startsWith('            ')) {
      body.push(line.slice(12));
      continue;
    }
    if (line.trim() === '') {
      body.push('');
      continue;
    }
    break;
  }
  return body.join('\n');
}

async function runGithubScript(script, { context, github, core, requireImpl, env = {} }) {
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const wrapped = `"use strict";\n${script}`;
  const fn = new AsyncFunction('github', 'context', 'core', 'require', wrapped);
  const previous = { ...process.env };
  for (const [key, value] of Object.entries(env)) process.env[key] = String(value);
  try {
    return await fn(github, context, core, requireImpl || nodeRequire);
  } finally {
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, previous);
  }
}

const downloadScript = extractScript('Download VIDiff artifact');
const commentScript = extractScript('Post PR comment');

function workflowRun(overrides = {}) {
  return {
    id: 71,
    name: 'VIDiff Report — Windows Container',
    path: '.github/workflows/vidiff-windows-container.yml',
    repository: { full_name: 'owner/repo' },
    head_repository: { full_name: 'owner/repo' },
    event: 'pull_request',
    head_sha: 'a'.repeat(40),
    conclusion: 'success',
    pull_requests: [{ number: 123 }],
    ...overrides,
  };
}

test('download script skips deploy when no matching artifact exists', async () => {
  const outputs = new Map();
  const warnings = [];
  const context = { repo: { owner: 'owner', repo: 'repo' }, payload: { workflow_run: workflowRun() } };
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [{ name: 'other', id: 99 }] } }),
        downloadArtifact: async () => {
          throw new Error('downloadArtifact should not run without a matching artifact');
        },
      },
      pulls: {
        get: async () => ({
          data: {
            base: { repo: { full_name: 'owner/repo' } },
            head: { repo: { full_name: 'owner/repo' }, sha: 'a'.repeat(40) },
          },
        }),
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: message => warnings.push(String(message)),
  };

  const result = await runGithubScript(downloadScript, { context, github, core });
  assert.equal(result, null);
  assert.equal(outputs.get('found'), 'false');
  assert.ok(warnings.some(message => message.includes('No VIDiff artifact found')));
});

test('download script writes ZIP and exposes trusted outputs for a matching artifact', async () => {
  const outputs = new Map();
  const writes = [];
  const context = { repo: { owner: 'owner', repo: 'repo' }, payload: { workflow_run: workflowRun() } };
  const expectedName = `vidiff-windows-${'a'.repeat(40)}`;
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async request => {
          assert.equal(request.run_id, 71);
          return { data: { artifacts: [{ name: expectedName, id: 22 }] } };
        },
        downloadArtifact: async request => {
          assert.equal(request.artifact_id, 22);
          return { data: new Uint8Array([80, 75, 3, 4]) };
        },
      },
      pulls: {
        get: async request => {
          assert.equal(request.pull_number, 123);
          return {
            data: {
              base: { repo: { full_name: 'owner/repo' } },
              head: { repo: { full_name: 'owner/repo' }, sha: 'a'.repeat(40) },
            },
          };
        },
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  const result = await runGithubScript(downloadScript, {
    context,
    github,
    core,
    requireImpl: name => {
      if (name !== 'fs') return nodeRequire(name);
      return {
        writeFileSync: (file, data) => writes.push({ file, data: Buffer.from(data) }),
      };
    },
  });

  assert.equal(result, expectedName);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].file, '/tmp/vidiff-artifact.zip');
  assert.deepEqual([...writes[0].data], [80, 75, 3, 4]);
  assert.equal(outputs.get('found'), 'true');
  assert.equal(outputs.get('platform'), 'windows');
  assert.equal(outputs.get('head_sha'), 'a'.repeat(40));
  assert.equal(outputs.get('pr_number'), '123');
  assert.equal(outputs.get('run_conclusion'), 'success');
  assert.equal(outputs.get('segment'), `pr-123`);
  assert.equal(outputs.get('artifact_name'), expectedName);
});

test('download script resolves PR association from commit when workflow_run.pull_requests is empty', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({ pull_requests: [] }),
    },
  };
  const expectedName = `vidiff-windows-${'a'.repeat(40)}`;
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [{ name: expectedName, id: 22 }] } }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      pulls: {
        get: async request => {
          assert.equal(request.pull_number, 456);
          return {
            data: {
              base: { repo: { full_name: 'owner/repo' } },
              head: { repo: { full_name: 'owner/repo' }, sha: 'a'.repeat(40) },
            },
          };
        },
      },
      repos: {
        listPullRequestsAssociatedWithCommit: async request => {
          assert.equal(request.commit_sha, 'a'.repeat(40));
          return {
            data: [
              {
                number: 456,
                base: { repo: { full_name: 'owner/repo' } },
                head: { sha: 'a'.repeat(40) },
              },
            ],
          };
        },
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  await runGithubScript(downloadScript, { context, github, core });

  assert.equal(outputs.get('pr_number'), '456');
  assert.equal(outputs.get('segment'), 'pr-456');
});

test('download script accepts workflow_dispatch artifact head SHA different from run head SHA', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        event: 'workflow_dispatch',
        head_sha: 'b'.repeat(40),
        pull_requests: [],
      }),
    },
  };
  const trustedTargetHead = 'c'.repeat(40);
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({
          data: { artifacts: [{ name: `vidiff-windows-${trustedTargetHead}`, id: 77 }] },
        }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      repos: {
        get: async () => ({ data: { default_branch: 'main' } }),
        compareCommitsWithBasehead: async request => {
          assert.ok([
            `${'b'.repeat(40)}...main`,
            `${trustedTargetHead}...main`,
          ].includes(request.basehead));
          return { data: { status: 'ahead' } };
        },
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  await runGithubScript(downloadScript, { context, github, core });

  assert.equal(outputs.get('found'), 'true');
  assert.equal(outputs.get('head_sha'), trustedTargetHead);
  assert.equal(outputs.get('segment'), `push-${trustedTargetHead}`);
  assert.equal(outputs.get('pr_number'), '');
  assert.equal(outputs.get('publish_pages'), 'true');
});

test('download script blocks Pages publish for fork pull request and exposes fallback links', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({ html_url: 'https://github.com/owner/repo/actions/runs/71' }),
    },
  };
  const expectedName = `vidiff-windows-${'a'.repeat(40)}`;
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [{ name: expectedName, id: 22 }] } }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      pulls: {
        get: async () => ({
          data: {
            base: { repo: { full_name: 'owner/repo' } },
            head: { repo: { full_name: 'fork/repo' }, sha: 'a'.repeat(40) },
          },
        }),
      },
    },
  };

  await runGithubScript(downloadScript, {
    context,
    github,
    core: {
      setOutput: (name, value) => outputs.set(name, String(value)),
      warning: () => {},
    },
  });

  assert.equal(outputs.get('publish_pages'), 'false');
  assert.equal(outputs.get('publish_reason'), 'untrusted-fork-pr');
  assert.equal(outputs.get('artifact_url'), 'https://github.com/owner/repo/actions/runs/71/artifacts/22');
  assert.equal(outputs.get('run_url'), 'https://github.com/owner/repo/actions/runs/71');
});

test('download script blocks Pages publish when PR head repository is unavailable', async () => {
  const outputs = new Map();
  const context = { repo: { owner: 'owner', repo: 'repo' }, payload: { workflow_run: workflowRun() } };
  const expectedName = `vidiff-windows-${'a'.repeat(40)}`;
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [{ name: expectedName, id: 22 }] } }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      pulls: {
        get: async () => ({
          data: {
            base: { repo: { full_name: 'owner/repo' } },
            head: { repo: null, sha: 'a'.repeat(40) },
          },
        }),
      },
    },
  };

  await runGithubScript(downloadScript, {
    context,
    github,
    core: {
      setOutput: (name, value) => outputs.set(name, String(value)),
      warning: () => {},
    },
  });

  assert.equal(outputs.get('publish_pages'), 'false');
  assert.equal(outputs.get('publish_reason'), 'untrusted-missing-pr-head-repo');
});

test('download script blocks workflow_dispatch publish when target is outside default-branch history', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        event: 'workflow_dispatch',
        head_sha: 'b'.repeat(40),
        pull_requests: [],
      }),
    },
  };
  const trustedTargetHead = 'c'.repeat(40);
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({
          data: { artifacts: [{ name: `vidiff-windows-${trustedTargetHead}`, id: 77 }] },
        }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      repos: {
        get: async () => ({ data: { default_branch: 'main' } }),
        compareCommitsWithBasehead: async request => {
          if (request.basehead === `${'b'.repeat(40)}...main`) return { data: { status: 'ahead' } };
          if (request.basehead === `${trustedTargetHead}...main`) return { data: { status: 'diverged' } };
          throw new Error(`unexpected basehead ${request.basehead}`);
        },
      },
    },
  };

  await runGithubScript(downloadScript, {
    context,
    github,
    core: {
      setOutput: (name, value) => outputs.set(name, String(value)),
      warning: () => {},
    },
  });

  assert.equal(outputs.get('publish_pages'), 'false');
  assert.equal(outputs.get('publish_reason'), 'untrusted-workflow-dispatch-target');
  assert.equal(outputs.get('head_sha'), trustedTargetHead);
});

test('download script blocks workflow_dispatch publish when producer head is outside default-branch history', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        event: 'workflow_dispatch',
        head_sha: 'b'.repeat(40),
        pull_requests: [],
      }),
    },
  };
  const trustedTargetHead = 'c'.repeat(40);
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({
          data: { artifacts: [{ name: `vidiff-windows-${trustedTargetHead}`, id: 77 }] },
        }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      repos: {
        get: async () => ({ data: { default_branch: 'main' } }),
        compareCommitsWithBasehead: async request => {
          if (request.basehead === `${'b'.repeat(40)}...main`) return { data: { status: 'diverged' } };
          if (request.basehead === `${trustedTargetHead}...main`) return { data: { status: 'ahead' } };
          throw new Error(`unexpected basehead ${request.basehead}`);
        },
      },
    },
  };

  await runGithubScript(downloadScript, {
    context,
    github,
    core: {
      setOutput: (name, value) => outputs.set(name, String(value)),
      warning: () => {},
    },
  });

  assert.equal(outputs.get('publish_pages'), 'false');
  assert.equal(outputs.get('publish_reason'), 'untrusted-workflow-dispatch-producer');
  assert.equal(outputs.get('head_sha'), trustedTargetHead);
});

test('download script blocks workflow_dispatch publish when producer head repository is unknown', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        event: 'workflow_dispatch',
        head_sha: 'b'.repeat(40),
        pull_requests: [],
        head_repository: null,
      }),
    },
  };
  const trustedTargetHead = 'c'.repeat(40);
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({
          data: { artifacts: [{ name: `vidiff-windows-${trustedTargetHead}`, id: 77 }] },
        }),
        downloadArtifact: async () => ({ data: new Uint8Array([80, 75, 3, 4]) }),
      },
      repos: {
        get: async () => ({ data: { default_branch: 'main' } }),
        compareCommitsWithBasehead: async () => ({ data: { status: 'ahead' } }),
      },
    },
  };

  await runGithubScript(downloadScript, {
    context,
    github,
    core: {
      setOutput: (name, value) => outputs.set(name, String(value)),
      warning: () => {},
    },
  });

  assert.equal(outputs.get('publish_pages'), 'false');
  assert.equal(outputs.get('publish_reason'), 'untrusted-workflow-dispatch-producer');
});

test('download script rejects untrusted workflow provenance', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: { workflow_run: workflowRun({ path: '.github/workflows/unexpected.yml' }) },
  };
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [] } }),
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  await assert.rejects(
    () => runGithubScript(downloadScript, { context, github, core }),
    /Unexpected workflow_run.path/
  );
  assert.equal(outputs.size, 0);
});

test('download script rejects workflow path prefix spoof with same workflow name', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        path: '.github/workflows/vidiff-windows-container.yml.evil.yml',
      }),
    },
  };
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [] } }),
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  await assert.rejects(
    () => runGithubScript(downloadScript, { context, github, core }),
    /Unexpected workflow_run.path/
  );
  assert.equal(outputs.size, 0);
});

test('download script rejects workflow path suffix spoof that appends refs to file path', async () => {
  const outputs = new Map();
  const context = {
    repo: { owner: 'owner', repo: 'repo' },
    payload: {
      workflow_run: workflowRun({
        path: '.github/workflows/vidiff-windows-container.yml@refs/heads/main',
      }),
    },
  };
  const github = {
    rest: {
      actions: {
        listWorkflowRunArtifacts: async () => ({ data: { artifacts: [] } }),
      },
    },
  };
  const core = {
    setOutput: (name, value) => outputs.set(name, String(value)),
    warning: () => {},
  };

  await assert.rejects(
    () => runGithubScript(downloadScript, { context, github, core }),
    /Unexpected workflow_run.path/
  );
  assert.equal(outputs.size, 0);
});

test('workflow uses isolated Python for metadata validation helper', () => {
  assert.match(
    workflowText,
    /python3 -I -S \.github\/labview\/validate-vidiff-deploy-metadata\.py \/tmp\/vidiff-report\/vidiff-meta\.json/
  );
});

test('workflow uses hardened Python ZIP extractor instead of unzip', () => {
  assert.match(
    workflowText,
    /python3 -I -S \.github\/labview\/extract-vidiff-artifact\.py \/tmp\/vidiff-artifact\.zip \/tmp\/vidiff-report/
  );
  assert.doesNotMatch(workflowText, /\bunzip\b/);
});

test('workflow gates merge and Pages deploy behind trusted publication policy', () => {
  assert.match(
    workflowText,
    /name: Merge with existing report index \(keep prior diff entries\)[\s\S]*?steps\.artifact\.outputs\.publish_pages == 'true'/
  );
  assert.match(
    workflowText,
    /name: Deploy VIDiff report to GitHub Pages[\s\S]*?steps\.artifact\.outputs\.publish_pages == 'true'/
  );
});

test('comment script updates existing bot comment for the same platform', async () => {
  const updates = [];
  const creations = [];
  const context = { repo: { owner: 'owner', repo: 'repo' } };
  const github = {
    rest: {
      issues: {
        listComments: async request => {
          assert.equal(request.issue_number, 123);
          return {
            data: [
              { id: 7, user: { login: 'github-actions[bot]' }, body: '## VIDiff Report (windows)\nold' },
            ],
          };
        },
        updateComment: async request => updates.push(request),
        createComment: async request => creations.push(request),
      },
    },
  };

  await runGithubScript(commentScript, {
    context,
    github,
    core: {},
    env: {
      PR_NUMBER: '123',
      PLATFORM: 'windows',
      OUTCOME: 'success',
      REPORT_URL: 'https://example.invalid/vidiff/pr-123/windows/vidiff/index.html',
    },
  });

  assert.equal(updates.length, 1);
  assert.equal(creations.length, 0);
  assert.equal(updates[0].comment_id, 7);
  assert.match(updates[0].body, /✅ Passed/);
  assert.match(updates[0].body, /View diff report/);
});

test('comment script creates a new comment when no platform-matched bot comment exists', async () => {
  const updates = [];
  const creations = [];
  const context = { repo: { owner: 'owner', repo: 'repo' } };
  const github = {
    rest: {
      issues: {
        listComments: async () => ({ data: [{ id: 9, user: { login: 'other-bot' }, body: 'ignore' }] }),
        updateComment: async request => updates.push(request),
        createComment: async request => creations.push(request),
      },
    },
  };

  await runGithubScript(commentScript, {
    context,
    github,
    core: {},
    env: {
      PR_NUMBER: '123',
      PLATFORM: 'linux',
      OUTCOME: 'failure',
      REPORT_URL: 'https://example.invalid/vidiff/pr-123/linux/vidiff/index.html',
    },
  });

  assert.equal(updates.length, 0);
  assert.equal(creations.length, 1);
  assert.equal(creations[0].issue_number, 123);
  assert.match(creations[0].body, /❌ Failed/);
  assert.match(creations[0].body, /VIDiff Report \(linux\)/);
});

test('comment script reports artifact and run links when Pages publication is blocked', async () => {
  const creations = [];
  const context = { repo: { owner: 'owner', repo: 'repo' } };
  const github = {
    rest: {
      issues: {
        listComments: async () => ({ data: [] }),
        updateComment: async () => {
          throw new Error('updateComment should not run when no existing bot comment exists');
        },
        createComment: async request => creations.push(request),
      },
    },
  };

  await runGithubScript(commentScript, {
    context,
    github,
    core: {},
    env: {
      PR_NUMBER: '123',
      PLATFORM: 'windows',
      OUTCOME: 'failure',
      REPORT_URL: 'https://github.com/owner/repo/actions/runs/71/artifacts/22',
      RUN_URL: 'https://github.com/owner/repo/actions/runs/71',
      PUBLISH_PAGES: 'false',
      PUBLISH_REASON: 'untrusted-fork-pr',
    },
  });

  assert.equal(creations.length, 1);
  assert.match(creations[0].body, /View diff report\]\(https:\/\/github\.com\/owner\/repo\/actions\/runs\/71\/artifacts\/22\)/);
  assert.match(creations[0].body, /Open run logs\]\(https:\/\/github\.com\/owner\/repo\/actions\/runs\/71\)/);
  assert.match(creations[0].body, /Pages publication skipped: untrusted-fork-pr\./);
  assert.doesNotMatch(creations[0].body, /github\.io/);
});
