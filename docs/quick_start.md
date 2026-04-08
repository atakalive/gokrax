# Quick Start

[English](quick_start.md) | [日本語](quick_start_ja.md)

Get gokrax running as fast as possible. Estimated time: 30–60 minutes.

## 1. Prerequisites

- Linux or macOS (including WSL2)
- Python 3.11+
- A [GitLab](https://gitlab.com/) account (free tier supports private repositories)
- An SSH key registered with GitLab (see below)
- An account with any LLM provider [supported by pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/providers.md): Anthropic, GitHub Copilot, Google Gemini CLI, OpenAI Codex, Antigravity, etc.

### Registering an SSH Key (GitLab)

gokrax automatically runs `git push` during the pipeline, so an SSH key is required.

```bash
# Check for an existing key
ls ~/.ssh/id_ed25519.pub
# If "No such file or directory", generate one:
ssh-keygen -t ed25519 -C "you@example.com"
# Press Enter for all defaults (passphrase can be empty)

# Display the public key and copy it
cat ~/.ssh/id_ed25519.pub
```

Paste the output into the [GitLab SSH Keys settings page](https://gitlab.com/-/user_settings/ssh_keys).

```bash
# Verify the connection
ssh -T git@gitlab.com
# Expected: "Welcome to GitLab, @your-username!"
```

## 2. Install Dependencies

If you get permission errors, prefix commands with `sudo`.

```bash
# gokrax
git clone https://github.com/atakalive/gokrax.git
cd gokrax
pip install -r requirements.txt
# If you get an "externally managed" error:
# pip install -r requirements.txt --break-system-packages
# If python3/pip are missing: sudo apt update && sudo apt install -y python3.12 python3-pip

# Homebrew (https://brew.sh/)
# Install if not present, then follow the "Next steps" to add it to PATH
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# glab (GitLab CLI — https://gitlab.com/gitlab-org/cli/-/releases)
brew install glab
glab auth login  # Host: gitlab.com, default Git protocol: SSH

# Node.js (WSL: install via nvm)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
source ~/.bashrc
nvm install --lts

# pi (agent framework — https://github.com/badlogic/pi-mono/tree/main/packages/agent)
npm install -g @mariozechner/pi-coding-agent
# Verify pi is on PATH
which pi
# If not found: echo 'export PATH="$(npm -g prefix)/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
pi    # After launching, run /login to authenticate with your provider and confirm the model responds
```

## 3. Set Up the gokrax Command (Required)

Agents invoke the `gokrax` command internally, so a symlink must be on PATH:

```bash
# Run this inside the gokrax directory
chmod +x gokrax.py
mkdir -p ~/.local/bin
ln -s "$(realpath gokrax.py)" ~/.local/bin/gokrax

# If ~/.local/bin is not on PATH:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# Verify
which gokrax
```

## 4. Configuration

```bash
python3 update_settings.py    # Generates settings.py from settings.example.py
```

Edit `settings.py`:

```bash
# Edit (Save: Ctrl+O, Exit: Ctrl+X)
nano settings.py
```

```python
# --- Required ---
GOKRAX_CLI = "/home/you/.local/bin/gokrax"  # Run: which gokrax (symlink created in step 3)
GITLAB_NAMESPACE = "your-username"      # gitlab.com/YOUR_NAMESPACE/...

DEFAULT_AGENT_BACKEND = "pi"

DEFAULT_QUEUE_OPTIONS = {
    "automerge": True,          # Defaults are fine except no_cc
    "skip_cc_plan": True,
    "no_cc": True,              # <- Run without Claude Code CLI
    "keep_ctx_intra": True,
    "skip_test": True,
    "skip_assess": True,
}
```

Minimum setup: 1 reviewer + 1 implementer.

## 5. Prepare Agents

```bash
# Copy prompt templates
mkdir -p agents/reviewer1
cp agents/example/INSTRUCTION.md.reviewer agents/reviewer1/INSTRUCTION.md

mkdir -p agents/impl1
cp agents/example/INSTRUCTION.md.implementer agents/impl1/INSTRUCTION.md
```

Configure models in `agents/config_pi.json`. Use provider and model names from `pi --list-models`:

```bash
# List available providers and models
pi --list-models

# Edit (Save: Ctrl+O, Exit: Ctrl+X)
nano agents/config_pi.json
```

```json
{
  "reviewer1": {
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

## 6. Register a Project and Create a Sample Issue

If you don't have a GitLab repository yet:

```bash
# Move outside the gokrax directory before creating a project
cd ~

# Create a GitLab repository
glab repo create myproject --private

mkdir myproject  # Skip if already created by glab
git init         # Skip if already created by glab

cd myproject
git config user.email "you@example.com"
git config user.name "Your Name"
git remote add origin git@gitlab.com:your-username/myproject.git
# To fix the URL: git remote set-url origin git@gitlab.com:correct-username/myproject.git

# Initial commit and push
echo "# myproject" > README.md
git add README.md
git commit -m "init"
git push -u origin HEAD
```

After the GitLab repository is ready:

```bash
# Register the project with gokrax (specify the GitLab repo and local path)
gokrax init --pj myproject --gitlab your-username/myproject --repo-path /fullpath/to/your/project --implementer impl1

# Create a sample Issue
glab issue create \
  --title "Add hello.py" \
  --description "Create hello.py that prints 'Hello, gokrax.' to stdout."
```

## 7. Run

Open the GitLab Issue #1 page in your browser. The issue description, design plan, and review comments will update in real time.

```bash
gokrax start --project myproject --issue 1 --mode min

# Watch progress in real time
tail -f /tmp/gokrax-watchdog.log
# The pipeline progresses automatically: DESIGN_PLAN → DESIGN_REVIEW → ... → DONE

# Once complete, check the result
cat hello.py
```

## Next Steps

- **Add Discord notifications** — See [README: Discord Notification Setup](../README.md#discord-notification-setup) for bot creation steps
- **Add more reviewers** — Ensemble review improves quality ([README: Ensemble Review](../README.md#ensemble-review))
- **Batch execution** — Process multiple Issues in sequence with queue files (`gokrax qrun`)
- **Spec Mode** — Automatically split a spec document into Issues ([README: Spec Mode](../README.md#spec-mode))
- **Domain risk assessment** — Define project-specific risks in `DOMAIN_RISK.md`

## Cleanup

To remove a test project:

```bash
python3 gokrax.py reset --pj myproject           # Reset pipeline state
glab repo delete your-username/myproject --yes   # Delete the GitLab repository
rm -rf /path/to/your/project                     # Delete the local directory
```
