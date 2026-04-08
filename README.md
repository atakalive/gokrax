# gokrax

[English](README.md) | [日本語](README_ja.md)

An automated development pipeline that drives GitLab Issues through to merge.

Manages LLM agent-driven design, implementation, and review via a state machine — takes Issues as input and produces reviewed code as output.


**Repositories:**
- **GitHub (stable):** <https://github.com/atakalive/gokrax> — Manually synced from GitLab
- **GitLab (development):** <https://gitlab.com/atakalive/gokrax> — Development activity, [demo of gokrax developing itself (Japanese)](https://gitlab.com/atakalive/gokrax/-/work_items?sort=created_date&state=all&first_page_size=100)

---

## Features

- **Fully automated pipeline** — Automatically drives Issue → design/review → implementation → review → merge via a state machine. Supports sequential processing of multiple Issues
- **Ensemble review** — Runs multiple LLM reviewers in parallel across different providers, models, and review perspectives to improve code quality
- **Risk assessment and hold** — Can automatically hold high-risk changes based on project-specific risk definitions
- **Spec Mode** — Runs spec review and revision through to Issue decomposition and automatic queue generation in a single pass
- **Discord notifications and control** — Receive progress notifications and execute basic commands from anywhere
- **Automatic work history accumulation** — Design discussions, review comments, and revision history are preserved as Issues and comments, serving as both an audit trail and reference material

**→ [Quick Start](docs/quick_start.md)**

---

## Table of Contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Setup](#setup)
- [Basic Usage](#basic-usage)
- [Pipeline State Transitions](#pipeline-state-transitions)
- [Ensemble Review](#ensemble-review)
- [Configuration](#configuration)
- [Spec Mode](#spec-mode)
- [Directory Structure](#directory-structure)
- [Uninstallation](#uninstallation)
- [Limitations](#limitations)
- [Future Work](#future-work)
- [Name](#name)
- [License](#license)

---

## Overview

gokrax approaches the quality of code produced by automated development pipelines. Even similar bugs can vary in severity across projects. By accounting for such domain-specific risks, gokrax aims to reduce the frequency of unacceptable issues being introduced into the codebase. Specifically, it is a tool designed to prevent critical bugs from slipping through, even when a developer working across multiple specialized domains cannot constantly keep track of all code themselves.

What the user primarily does is raise feature requests and similar proposals, and adjust the effort level for each (e.g., selecting which models to deploy) based on the difficulty and importance of the task.

gokrax automates the following pipeline:

```
Issue → Design Plan → Design Review → Implementation → Code Review → Merge
```

Each stage is executed by LLM agents, and the pipeline advances to the next stage automatically once completion reports are collected and transition conditions are met.

If critical issues (P0/P1) are raised during review, the pipeline enters a revision loop, iterating until the required number of approvals (all reviewers by default) is obtained. If the revision loop reaches the maximum number of iterations, the pipeline halts.

## Requirements

- **OS**: Linux (including WSL2), macOS
- **Remote operation**: Possible via Discord regardless of OS
- **Python**: 3.11 or higher. External dependencies: `requests`, `PyYAML`
- **Agent framework**: [openclaw](https://github.com/openclaw/openclaw), [pi-coding-agent](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent), or [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code). Used for LLM agent authentication and prompt dispatch for design, revision, and review
- **GitLab**: Issue tracker and code hosting. Requires git push access to managed projects (SSH key or HTTPS token)
- **[glab CLI](https://gitlab.com/gitlab-org/cli)**: Used for GitLab operations (Issue retrieval/editing, comment retrieval/posting, Issue closing)
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)**: Called internally as the implementation agent (recommended)
- **Discord bot token**: For progress notifications (recommended). For a progress monitoring GUI, see [WatcherB](https://github.com/atakalive/WatcherB)

### LLM Providers

gokrax is not tied to any specific LLM provider — any provider that openclaw, pi, or cc can authenticate with is supported:

- Anthropic (Claude)
- Google (Gemini)
- OpenAI (ChatGPT)
- GitHub (GitHub Copilot) etc.
- Local models (llama.cpp, vLLM, etc. — requires configuration)

Different providers, models, and review perspectives can be assigned to implementation agents and reviewer agents independently.

### Hardware Requirements

gokrax itself has virtually no computational overhead (state management and process spawning only).

## Setup

For a minimal setup to get started quickly, see **[Quick Start](docs/quick_start.md)**.

Prerequisites:
- A GitLab account with an SSH key registered (required because the pipeline automatically runs git push — see [Quick Start](docs/quick_start.md#registering-an-ssh-key-gitlab) for instructions)
- An account with any LLM provider

After completing the configuration in each section below, gokrax will be ready to use.

gokrax itself is a simple collection of Python scripts — installation consists only of `git clone` and installing dependencies. To uninstall, remove the crontab entry used for state transition monitoring (described later).

### Installing gokrax

```bash
# GitHub
git clone https://github.com/atakalive/gokrax.git

cd gokrax
pip install -r requirements.txt
# If you get an "externally managed" error: pip install -r requirements.txt --break-system-packages
python3 update_settings.py   # Generates settings.py from settings.example.py
# Edit settings.py (agent config, Discord settings, etc.)

# Create a symlink on PATH (required: agents invoke the gokrax command internally)
chmod +x gokrax.py
mkdir -p ~/.local/bin
ln -s "$(realpath gokrax.py)" ~/.local/bin/gokrax
# If ~/.local/bin is not on PATH: echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

`update_settings.py` copies `settings.example.py` to `settings.py` on the first run. When re-run after an update (`git pull`), it appends only newly added settings to the end of `settings.py` (existing settings are not modified).

For details on key configuration items in `settings.py`, see the [Configuration](#configuration) section.

### Installing an Agent Framework

gokrax requires a backend for agent provider authentication and prompt dispatch. Any of the following can be used, or in combination:

- openclaw
- pi-coding-agent
- Claude Code CLI (cc backend)

If openclaw is already running in your environment, using it directly is the easiest option. Otherwise, pi is simpler to set up. Claude Code CLI can also be used as a backend for all agent roles, not just implementation.

Note that gokrax requires at least 2 agents (an implementer and a reviewer).

### Setting Up openclaw

Follow the official documentation to complete openclaw agent authentication. Grant read and exec permissions to agents participating in gokrax (required for commands like `gokrax review`).

openclaw: <https://github.com/openclaw/openclaw>

### Setting Up pi

pi has a very simple authentication process. It is lightweight, making for a minimal gokrax setup. However, it currently does not support direct intervention by talking to agents during their work.

Supported providers: Anthropic, GitHub, Google, OpenAI.

pi-coding-agent: <https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent>

```bash
# On WSL, install Node.js via nvm first (to prevent the Windows npm from being used)
# curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash && source ~/.bashrc && nvm install --lts

npm install -g @mariozechner/pi-coding-agent
```

#### LLM Provider Authentication

Launch pi and authenticate with your provider:
```bash
pi
```

Authenticate with the provider you want to use:
```
/login
```

#### Creating Agent Profiles

Create a profile for each agent under `agents/{name}/`, copying files from `agents/example/`. Agent roles and guidelines are defined here:

```
agents/
├── reviewer1/
│   ├── IDENTITY.md       # Name, etc.
│   ├── INSTRUCTION.md    # Role, rules, review guidelines
│   ├── MEMORY.md         # Lessons learned, known issues
│   ├── AGENTS.md         # Auto-generated from IDENTITY + INSTRUCTION + MEMORY
│   └── .agents_hash      # Used to detect content changes (auto-generated)
├── reviewer2/
│   └── ...
└── impl1/
    └── ...
```

`AGENTS.md` is auto-generated from `IDENTITY.md`, `INSTRUCTION.md`, and `MEMORY.md` (only when content changes). There is no need to edit `AGENTS.md` directly.

#### Per-Agent Model Configuration

Configure the provider, model, thinking level, and available tools for each agent in `agents/config_pi.json`. Reviewers also need `bash` to report completion to gokrax (`INSTRUCTION.md` instructs them not to write to the repository). No tool specification is needed for implementers (all tools are permitted).

Run `pi --list-models` to list currently available providers and models.

Example configuration:
```json
{
  "reviewer1": {
    "provider": "google-gemini-cli",
    "model": "gemini-2.5-pro",
    "thinking": "low",
    "tools": "read,bash,grep,find,ls"
  },
  "reviewer2": {
    "provider": "openai-codex",
    "model": "gpt-5.4",
    "thinking": "low",
    "tools": "read,bash,grep,find,ls"
  },
  "impl1": {
    "provider": "anthropic",
    "model": "claude-opus-4-6",
    "thinking": "low"
  }
}
```

### Installing glab CLI

Used for GitLab Issue operations.

```bash
# Homebrew (if not installed — https://brew.sh)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# After installation, follow the displayed Next steps to add it to PATH

brew install glab        # apt install glab installs an outdated version that does not work
glab auth login          # Authenticate with your GitLab account
```

Details: <https://gitlab.com/gitlab-org/cli>

### Installing Claude Code CLI (Recommended)

Claude Code CLI is used during the implementation phase. If you specify `--no-cc` in batch execution options, the implementer agent handles implementation directly, so operation without Claude Code is possible.

```bash
npm install -g @anthropic-ai/claude-code
claude /login   # Authenticate with your Anthropic account
```

Details: <https://docs.anthropic.com/en/docs/claude-code>

### Discord Notification Setup (Recommended)

gokrax posts progress notifications to a Discord channel (using the Discord API directly). For a minimal setup without Discord, monitor progress via log files (`tail -f /tmp/gokrax-watchdog.log`).

1. Create a notification channel in your Discord server.
   If you don't have a server yet: Open [Discord](https://discord.com/), click "+" in the left sidebar → "Create My Own" → "For me and my friends" → enter a server name and create it.
   Create a text channel (e.g., `#gokrax`) in the server. Click "+" in the channel list or right-click → "Create Channel" → select Text Channel.
2. Create a Discord bot for notifications (you can also reuse an existing bot).
   To create a new one: Open the [Discord Developer Portal](https://discord.com/developers/applications), click "New Application" → name it (e.g., `gokrax-notify`).
   - In the left menu, go to "Bot" → under "Privileged Gateway Intents", enable **Message Content Intent** (required for reading user replies during merge approval).
   - On the same "Bot" page, click "Reset Token" → copy the bot token → set it as `DISCORD_BOT_TOKEN` in `settings.py`.
   - In the left menu, go to "OAuth2" → "OAuth2 URL Generator" → under SCOPES check `bot` → under BOT PERMISSIONS check `Send Messages` and `Read Message History` → open the generated URL in your browser to invite the bot to your server.
3. Get the bot's user ID.
   - Copying IDs requires Discord Developer Mode: go to User Settings (bottom-left gear icon) → "Advanced" → enable "Developer Mode".
   - Right-click the bot's name in the server member list → "Copy User ID" → set it as `ANNOUNCE_BOT_USER_ID` in `settings.py`.
4. Right-click the notification channel name to copy its ID, and set it as `DISCORD_CHANNEL` in `settings.py`.

### Installing a Discord Communication Tool for Progress Monitoring (Optional)

A lightweight resident tool for reading and writing to the Discord #gokrax channel. Eliminates the need to switch to the Discord window for checking gokrax status and issuing commands.

WatcherB: <https://github.com/atakalive/WatcherB>

1. Install WatcherB following its instructions, and create a separate Discord bot from the notification bot above.
2. If you want to post gokrax commands to the Discord channel from WatcherB, set the bot's user ID as `COMMAND_BOT_USER_ID` in `settings.py`.


## Basic Usage

### 1. Create a GitLab Issue

Describe what you want implemented in the Issue body. The assigned agent reads the Issue body and elaborates a design plan.

### 2. Start a Batch

```bash
# Start with specific Issue numbers
gokrax start --pj MyProject --issue 1 2 --mode lite
```

`start` executes the following in sequence:
- Triage of specified Issues (submission to the batch)
- Transition to `DESIGN_PLAN` state
- Enabling the watchdog
- In practice, running commands each time is cumbersome, so creating a queue file and using queue execution is simpler (described below).

### 3. Everything After Is Automatic

The watchdog automatically drives the following:

1. `IDLE` → `INITIALIZE` (agent initialization, etc.)
2. `DESIGN_PLAN` → `DESIGN_REVIEW` (reviewers are notified automatically)
3. Based on review results: `DESIGN_APPROVED` or `DESIGN_REVISE`
4. `DESIGN_APPROVED` → `IMPLEMENTATION` (implementation agent starts automatically)
5. `IMPLEMENTATION` → `CODE_REVIEW` (reviewers are notified automatically)
6. Based on review results: `CODE_APPROVED` or `CODE_REVISE`
7. `CODE_APPROVED` → `MERGE_SUMMARY_SENT` (summary posted to Discord)
8. Human replies "OK" or auto-merge → `DONE` (git push + Issue close)
9. During queue execution, returns to (1).

Each agent reports completion by running commands like `gokrax plan-done ...` or `gokrax review ...`. State transitions occur when completion reports satisfy the transition conditions. Under normal circumstances, each Issue takes about 30 minutes to complete.

Context reset decisions are made at `INITIALIZE` and `IMPLEMENTATION`. Execution settings allow specifying whether to maintain context within a batch and/or across batches.


## Pipeline State Transitions

```
IDLE → INITIALIZE → DESIGN_PLAN → DESIGN_REVIEW ⇄ DESIGN_REVISE
                                        ↓
                                  DESIGN_APPROVED → ASSESSMENT → IMPLEMENTATION
                                                                          ↓
                                                                     CODE_TEST ⇄ CODE_FIX
                                                                          ↓
                                                                     CODE_REVIEW ⇄ CODE_REVISE
                                                                          ↓
                                                                     CODE_APPROVED → MERGE_SUMMARY_SENT → DONE → IDLE
```
[State Diagram (png)](docs/state-diagram.png)

For design details, see [docs/architecture.md](docs/architecture.md).

- `ASSESSMENT` is a judgment state after design approval that performs a 5-level code complexity assessment and a 3-level domain risk assessment. When `--exclude-high-risk` / `--exclude-any-risk` is specified, Issues are skipped based on the risk assessment result. (Default: `skip-assess: True`)
- `CODE_TEST` is currently experimental. It ensures tests pass before transitioning to `CODE_REVIEW`. After `CODE_REVISE`, tests must also pass before returning to review. (Default: `skip_test: True`)
- After `DONE` → `IDLE` transition, queue execution automatically proceeds to the next batch.

Each state has a timeout configured in `settings.py` (`BLOCK_TIMERS`):

| State | Default Timeout |
|-------|-----------------|
| `DESIGN_PLAN` | 30 min |
| `DESIGN_REVIEW` | 60 min |
| `DESIGN_REVISE` | 30 min |
| `ASSESSMENT` | 20 min |
| `IMPLEMENTATION` | 120 min |
| `CODE_TEST` | 10 min |
| `CODE_TEST_FIX` | 60 min |
| `CODE_REVIEW` | 60 min |
| `CODE_REVISE` | 30 min |

These defaults rarely trigger timeouts, but the `extend` command can extend deadlines (up to 2 times).

The maximum number of revision loops (REVISE → REVIEW) is limited by `MAX_REVISE_CYCLES` (default: 4).

### Transition to BLOCKED State and Recovery

The pipeline transitions to `BLOCKED` and halts in the following cases:

- **Timeout exceeded**: When completion reports are not fully collected within the `BLOCK_TIMERS` for a given state.
- **Revision loop limit reached**: When P0 or P1 issues are raised in review and revisions reach `MAX_REVISE_CYCLES` (default: 4). Applies to both design review and code review.
- **Test fix limit reached**: When `CODE_TEST_FIX` reaches `MAX_TEST_RETRY` (default: 4).

Recovery from `BLOCKED` (assuming `DESIGN_REVIEW` → `BLOCKED`):

```bash
# 1. Restore the state (can also transition to DESIGN_APPROVED if appropriate)
gokrax transition --to DESIGN_REVIEW --pj MyProject --force

# 2. Restart the watchdog
gokrax enable --pj MyProject
```

To reset all projects to `IDLE`:
```bash
gokrax reset
```

### Handling Unresponsive Reviewers

If a reviewer becomes unavailable (e.g., due to rate limits), it can be excluded using the `exclude` command:

```bash
# Exclude reviewer1 from the current batch
gokrax exclude --pj MyProject --add reviewer1
```


## Ensemble Review

gokrax employs an ensemble approach to review, running multiple LLM reviewers in parallel.

### Review Strategy

Three methods are available to increase review coverage in line with development goals:

1. Use different models to compensate for each model's biases and blind spots
   → Combine LLMs from multiple providers

2. Orthogonalize review perspectives (user-configured)
   → Per-agent and per-project skill injection, LLM agent memory tuning (backend-side)

3. Reduce oversights through iterative review
   → N-pass review feature

### Effectiveness

Regarding strategy (1) above, an experiment was conducted in which code generated from identical initial conditions (specification, Issue decomposition, and batch structure) was evaluated by multiple LLMs. The results showed a reduction in detected Critical and Major issues compared with Claude Code alone, indicating improved code quality.

Details: [260407_report](reports/260407_report_chortal.md)


### Leveraging Small and Local Models

In the reviewer role, even small models that are biased toward domain knowledge or that reference external knowledge — rather than large general-purpose models — may prove useful. gokrax presents an application of small models: integration into purpose-specific review systems.


## Spec Mode

A mode for reviewing and revising specifications when starting a new project or implementing a large feature. Ensures specification quality before proceeding to Issue decomposition and task queue creation (effort calibration).

```
Spec input → SPEC_REVIEW ⇄ SPEC_REVISE → SPEC_APPROVED
  → ISSUE_SUGGESTION → ISSUE_PLAN → QUEUE_PLAN → SPEC_DONE
```

```bash
gokrax spec start \
  --project MyProject \
  --spec docs/feature-spec.md \
  --implementer agent-name \
  --review-mode full
```

The specification undergoes iterative revision based on reviewer feedback. Once all reviewers approve, it enters the Issue decomposition phase, where Issue breakdown proposals, implementation ordering, and queue generation are performed automatically.

Use `--auto-continue` to skip human confirmation steps after approval.

Use `--auto-qrun` to automatically proceed to the development pipeline after queue generation.

For details, see [docs/spec_mode_spec.md](docs/spec_mode_spec.md).

## Directory Structure

Default pipeline directory (configurable via `PIPELINES_DIR` in `settings.py`):

```
~/.gokrax/
├── pipelines/
│   ├── MyProject.json       # Per-project pipeline state
│   ├── MyProject.lock       # File lock
│   └── gokrax-state.json    # Global state (cross-project session management)
└── gokrax-metrics.jsonl     # Metrics (review records for reviewer evaluation — local only)
```

```
/tmp/
├── gokrax-watchdog.log          # Watchdog log
├── gokrax-watchdog-loop.pid     # Watchdog PID
└── gokrax-review/               # Review data externalization directory
    └── MyProject_reviewer1.md   # File for large review requests
```

## CLI Commands

For details, see [CLI.md](CLI.md).

| Command | Description |
|---------|-------------|
| `init` | Create a new project (**required on first use**, see example below) |
| `status` | Display status of all projects |
| `start` | Start a batch (triage + design plan transition + enable watchdog) |
| `enable` / `disable` | Enable/disable the watchdog (primarily for recovery from BLOCKED) |
| `transition` | Manual state transition (`--force` to force) |
| `review-mode` | Change review mode (full, lite, etc.) |

### Project Initialization

```bash
# Basic (specify GitLab path and local repository)
gokrax init --pj myproject --gitlab user/myproject --repo-path /path/to/repo --implementer my_agent
```

`init` is run once per project. It generates a pipeline management file (`pipeline.json`), which all subsequent commands reference.

| Queue Commands (recommended after initial testing) | Description |
|---------|-------------|
| `qrun` | Start batch in queue mode (starts as soon as the signal is detected) |
| `qstatus` | Display queue contents with queue numbers [0...N] |
| `qadd ...` | Add an item to the queue file |
| `qdel N` | Delete the Nth item from the queue file (corresponds to qstatus numbers) |


## Configuration

Key configuration items in `settings.py`:

### Agent Definitions

Register openclaw agent IDs. These agent names are used throughout subsequent configuration.

```python
# Reviewer names
REVIEWERS = ["rev1", "rev2", "rev3", "rev4"]
# Implementer names
IMPLEMENTERS = ["impl1"]
```

### Path Settings

```python
GOKRAX_CLI = "/home/you/.local/bin/gokrax"      # Check with: which gokrax (symlink destination)
GITLAB_NAMESPACE = "your-username"              # gitlab.com/YOUR_NAMESPACE/...
```

### Backend Settings

```python
# Run all agents with openclaw
DEFAULT_AGENT_BACKEND = "openclaw"

# Run agents with Claude Code backend
DEFAULT_AGENT_BACKEND = "cc"

# Mix backends per agent
DEFAULT_AGENT_BACKEND = "pi"
AGENT_BACKEND_OVERRIDE = {"impl1": "openclaw"}
```

### Reviewer Tiers

Reviewers are classified into tiers based on infrastructure stability:

```python
REVIEWER_TIERS = {
    "regular":       ["rev1", "rev2"],  # Stable connection, sufficient context length
    "short-context": ["rev3"],          # Limited context length (handled by frequent session resets)
    "free":          ["rev4"],          # Daily token limits, unstable, difficult to manage
}
```

### Review Modes

Modes allow adjusting review cost according to the problem at hand.

```python
REVIEW_MODES = {
    "full":     {"members": ["rev1", "rev2", "rev3"],
                 "min_reviews": 3, "grace_period_sec": 0},  # Optional: min_reviews, grace_period_sec
    "lite":     {"members": ["rev1", "rev2"],},
    "min":      {"members": ["rev1"],},
    "skip":     {"members": [],},

    "lite3":    {"members": ["rev1", "rev2", "rev3"],
                 "min_reviews": 2, "grace_period_sec": 300},
    "lite_x2":  {"members": ["rev1", "rev2"],
                 "n_pass":  {"rev1": 2, "rev2": 2}, },
}
```

- The pipeline transitions to the next state once `min_reviews` approvals are collected (default: all members). When `min_reviews` is less than the number of `members` (e.g., `lite3` requires 2 of 3), the pipeline waits an additional `grace_period_sec` after reaching `min_reviews`. If remaining reviewers respond within the grace period, their reviews are included; if not, the pipeline proceeds with what has been collected. This allows including slow or unstable reviewers without blocking the pipeline.

- The `n_pass` setting causes the specified reviewer to perform N review passes. (Default if unspecified: 1)

- A warning is issued if non-existent reviewer names are configured — remove them or comment them out.

- **Phase overrides**: Within a mode definition, phase-specific settings can be added under `"design"` / `"code"` keys. Fields that can be overridden are `members`, `min_reviews`, `n_pass`, and `grace_period_sec`. Fields not overridden inherit the mode's default values. `min_reviews` is automatically capped at `len(members)` and cannot exceed the member count.

```python
"full_x2": {
    "members": ["rev1", "rev2", "rev3", "rev4"],
    "n_pass": {"rev1": 2, "rev2": 2, "rev3": 2, "rev4": 2},
    "code": {
        "members": ["rev1", "rev2", "rev3"],  # excluded rev4 in CODE_REVIEW
        "n_pass": {"rev1": 2, "rev2": 2},     # rev3: 1 (default value)
    },
},
```

## Prompt Customization

Prompts that gokrax sends to agents are defined in templates under `messages/{lang}/`. To override, copy only the templates you want to change into `messages_custom/`:

```bash
# Example: Customize the design review prompt
cp messages/{lang}/dev/design_review.py messages_custom/{lang}/dev/
# Edit messages_custom/{lang}/dev/design_review.py
```

`messages_custom/` is included in `.gitignore`, so it won't be overwritten by `git pull`. Templates not copied to `messages_custom/` fall back to the defaults in `messages/`.

## Uninstallation

To stop and remove gokrax:

```bash
# 1. Reset all projects to IDLE
gokrax reset

# 2. Manually remove crontab entries (gokrax registers crontab entries for the resident watchdog)
crontab -e
# Delete the line containing gokrax-watchdog-loop

# 3. Remove residual processes and files
rm -f /tmp/gokrax-watchdog-loop.pid /tmp/gokrax-watchdog-loop.lock /tmp/gokrax-cron-spawn.lock /tmp/gokrax-watchdog.log
rm -rf /tmp/gokrax-review/

# 4. Remove pipeline state files (if desired)
rm -rf ~/.gokrax/pipelines/

# 5. Remove the gokrax repository
rm -rf /path/to/gokrax
```

**Note:** Running `gokrax enable` adds a crontab entry for automatic watchdog recovery. `reset` only resets pipeline state and does not remove the crontab entry. For a complete shutdown, the manual crontab removal in step 2 is required.


## Limitations

### Limitations of Code Review

gokrax performs static code review and cannot detect bugs that only manifest on actual hardware or in runtime environments.

### Parallelization

Currently, the pipeline executes one project at a time sequentially. While the pipeline state management is designed with parallelization in mind, sequential execution is currently enforced in this early stage, primarily due to the complexity of error handling in parallel scenarios.

### Supported Platforms

Currently supports GitLab only (chosen because it offers free private repositories). GitHub support is not yet implemented.


## Future Work

### GUI for Operation and Monitoring

While agent mediation makes CLI operations straightforward, users still need to manually start pipelines and configure review modes based on the situation. To simplify this, an extension of the Discord monitoring GUI tool is under consideration (a form where users select Issues and parameters with the mouse, then press "Add to Queue" and "Run" buttons).

### Parameter Tuning for Automatic Task Queue Generation

In the spec mode workflow that drives implementation end-to-end from a specification, automatic tuning of model selection and similar parameters for each decomposed task batch is difficult and often does not match user intent. Improvements may be possible through prompt tuning when requesting queue generation proposals and through measuring model usage (not yet implemented).

### Testing (CODE_TEST State)

CODE_TEST is implemented but insufficiently validated, so it is currently treated as an experimental feature (default: `--skip-test: True`).


## Name

*gokrax* derives from *gokuraku* (極楽) — Japanese for paradise. Developer's paradise.


## License

MIT License
