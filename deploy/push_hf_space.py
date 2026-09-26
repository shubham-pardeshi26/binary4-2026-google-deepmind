#!/usr/bin/env python3
"""Create/update the AdLoop Hugging Face Space (Docker SDK) in one command.

Uploads the repo (minus secrets, venv and runtime data), swaps in
deploy/hf_space_README.md as the Space README (it carries the Docker
frontmatter), and stores GEMINI_API_KEY as a Space *secret* read from your
local environment -- the key never touches the uploaded files.

    pip install huggingface_hub            # one-off, not an app dependency
    huggingface-cli login                  # or export HF_TOKEN=...
    GEMINI_API_KEY=... python deploy/push_hf_space.py <user-or-org>/adloop

Optional env passed through as Space variables: ADLOOP_MAX_CONCURRENT_RUNS,
ADLOOP_RUNS_PER_IP_PER_HOUR, ADLOOP_SHOWCASE_RUN, ADLOOP_MOCK.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORE = [".env", ".env.*", ".venv/**", "venv/**", "data/**", "**/__pycache__/**", "*.pyc",
          ".git/**", ".DS_Store", "README.md",
          # local-only files: IDE config, stray photos at the repo root, internal pitch/notes
          ".idea/**", "/*.jpg", "/*.jpeg", "/*.png", "PITCH.md", "HOW_IT_WORKS.md", "CLAUDE.md"]
PASSTHROUGH_VARS = ("ADLOOP_MAX_CONCURRENT_RUNS", "ADLOOP_RUNS_PER_IP_PER_HOUR", "ADLOOP_SHOWCASE_RUN", "ADLOOP_MOCK")


def main() -> int:
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        print(__doc__)
        return 2
    repo_id = sys.argv[1]
    try:
        from huggingface_hub import HfApi
    except ImportError:
        print("huggingface_hub is not installed: pip install huggingface_hub")
        return 2

    api = HfApi()
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)

    key = os.getenv("GEMINI_API_KEY")
    if key:
        api.add_space_secret(repo_id, "GEMINI_API_KEY", key)
        print("GEMINI_API_KEY stored as a Space secret")
    else:
        print("GEMINI_API_KEY not set locally: the Space will start in mock mode until you add the secret")
    for name in PASSTHROUGH_VARS:
        if os.getenv(name):
            api.add_space_variable(repo_id, name, os.environ[name])

    api.upload_folder(folder_path=str(ROOT), repo_id=repo_id, repo_type="space",
                      ignore_patterns=IGNORE, commit_message="Deploy AdLoop")
    api.upload_file(path_or_fileobj=str(ROOT / "deploy" / "hf_space_README.md"), path_in_repo="README.md",
                    repo_id=repo_id, repo_type="space", commit_message="Space README (Docker frontmatter)")
    print(f"Pushed. Build logs: https://huggingface.co/spaces/{repo_id}?logs=build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
