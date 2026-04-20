#!/usr/bin/env python3
"""Unit tests for dispatcher git worktree helpers.

Tests: get_default_branch(), create_worktree(), merge_worktree() Pre-check 3.

All tests use tmp_path + bare git fixture — no mocking, no network, no
~/.half-bakery state touched. WORKTREE_DIR is monkeypatched per test.

Requirement: pytest, git ≥2.5 (worktrees)
Run: pytest scripts/test_dispatcher_worktree.py -v
"""

import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Make dispatcher importable from scripts/
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import dispatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _git(repo_dir, *args):
    """Run a git command in repo_dir; raise on failure."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def make_repo(path, *, branch="main", remote_head=None):
    """Create a git repo at `path` with one commit on `branch`.

    Args:
        branch: initial branch name (passed via -b to git init)
        remote_head: if set, wire refs/remotes/origin/HEAD to this branch name
    Returns: path (str)
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", branch)
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    if remote_head:
        # Simulate cloned repo with refs/remotes/origin/HEAD without a real remote
        _git(path, "symbolic-ref", "refs/remotes/origin/HEAD",
             f"refs/remotes/origin/{remote_head}")
    return str(path)


# ---------------------------------------------------------------------------
# get_default_branch() tests
# ---------------------------------------------------------------------------

class TestGetDefaultBranch:
    """Covers the three-tier fallback chain."""

    def test_uses_remote_tracking_ref(self, tmp_path):
        """Repo with refs/remotes/origin/HEAD pointing to main → 'main'."""
        repo = make_repo(tmp_path / "repo", branch="main", remote_head="main")
        assert dispatcher.get_default_branch(repo) == "main"

    def test_remote_tracking_ref_non_main_name(self, tmp_path):
        """Symbolic-ref pointing to 'develop' → 'develop'."""
        repo_dir = tmp_path / "repo"
        make_repo(repo_dir, branch="develop", remote_head="develop")
        assert dispatcher.get_default_branch(str(repo_dir)) == "develop"

    def test_fallback_to_main_when_no_remote(self, tmp_path):
        """No remote tracking ref, but main branch exists → 'main'."""
        repo = make_repo(tmp_path / "repo", branch="main")  # no remote_head
        assert dispatcher.get_default_branch(repo) == "main"

    def test_fallback_to_master(self, tmp_path):
        """No remote tracking ref, only master branch → 'master'."""
        repo = make_repo(tmp_path / "repo", branch="master")
        assert dispatcher.get_default_branch(repo) == "master"

    def test_hard_fallback_empty_repo(self, tmp_path):
        """Empty repo (no commits, no branches) → hard fallback 'main'."""
        repo_dir = tmp_path / "empty"
        repo_dir.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(repo_dir)],
                       capture_output=True, check=True)
        # An empty repo has no commits, so rev-parse fails for main/master.
        # get_default_branch should return the hard-coded "main" fallback.
        assert dispatcher.get_default_branch(str(repo_dir)) == "main"


# ---------------------------------------------------------------------------
# create_worktree() tests
# ---------------------------------------------------------------------------

class TestCreateWorktree:
    """Verify new agent branch starts from default branch, not HEAD."""

    def test_worktree_branches_from_default_not_feature(self, tmp_path, monkeypatch):
        """When project is on a feature branch, new worktree still starts from main."""
        # Patch WORKTREE_DIR so tests don't touch ~/.half-bakery
        wt_dir = tmp_path / "worktrees"
        monkeypatch.setattr(dispatcher, "WORKTREE_DIR", wt_dir)

        # Create repo on main with an initial commit
        repo_dir = tmp_path / "project"
        make_repo(repo_dir, branch="main")

        # Move project dir to a feature branch with a diverging commit
        _git(repo_dir, "checkout", "-b", "feature/my-work")
        (repo_dir / "feature.txt").write_text("feature work\n")
        _git(repo_dir, "add", "feature.txt")
        _git(repo_dir, "commit", "-m", "feature commit")

        # Confirm HEAD is now on the feature branch
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "feature/my-work"

        result = dispatcher.create_worktree(str(repo_dir), 999, repo_name="myrepo")
        assert result is not None, "create_worktree should succeed"

        wt_path, branch_name = result
        assert branch_name == "agent/myrepo-999"

        # The new worktree branch should be at the same commit as main, NOT feature/my-work
        main_sha = _git(repo_dir, "rev-parse", "main")
        feature_sha = _git(repo_dir, "rev-parse", "feature/my-work")
        agent_sha = _git(repo_dir, "rev-parse", branch_name)

        assert agent_sha == main_sha, (
            f"Agent branch should start from main ({main_sha[:8]}) "
            f"not feature ({feature_sha[:8]})"
        )
        assert agent_sha != feature_sha

    def test_worktree_log_matches_default_branch(self, tmp_path, monkeypatch):
        """git log on new worktree branch matches default branch HEAD exactly."""
        wt_dir = tmp_path / "worktrees"
        monkeypatch.setattr(dispatcher, "WORKTREE_DIR", wt_dir)

        repo_dir = tmp_path / "project"
        make_repo(repo_dir, branch="main")

        # Add a second commit to main
        (repo_dir / "extra.txt").write_text("extra\n")
        _git(repo_dir, "add", "extra.txt")
        _git(repo_dir, "commit", "-m", "second commit on main")

        result = dispatcher.create_worktree(str(repo_dir), 42, repo_name="test")
        assert result is not None
        wt_path, branch_name = result

        # Log in worktree path should have exactly 2 commits (matching main)
        log_output = _git(Path(wt_path), "log", "--oneline")
        lines = [l for l in log_output.splitlines() if l.strip()]
        assert len(lines) == 2, f"Expected 2 commits, got: {log_output}"
        assert "second commit on main" in lines[0]
        assert "initial" in lines[1]


# ---------------------------------------------------------------------------
# merge_worktree() Pre-check 3 tests
# ---------------------------------------------------------------------------

class TestMergeWorktreePrecheck3:
    """Pre-check 3 ensures HEAD is on default branch before merging."""

    def _setup_feature_branch_repo(self, tmp_path):
        """Helper: repo on feature branch with an agent branch ready to merge."""
        repo_dir = tmp_path / "project"
        make_repo(repo_dir, branch="main")

        # Create an agent branch (what we'll be "merging")
        _git(repo_dir, "checkout", "-b", "agent/test-42")
        (repo_dir / "agent_change.txt").write_text("agent output\n")
        _git(repo_dir, "add", "agent_change.txt")
        _git(repo_dir, "commit", "-m", "agent work")

        # Return to main then create a feature branch there (simulating project state)
        _git(repo_dir, "checkout", "main")
        _git(repo_dir, "checkout", "-b", "feature/ongoing")

        return repo_dir

    def test_on_feature_with_dirty_files_stash_created(self, tmp_path, monkeypatch):
        """Feature branch + dirty files → stash created, checkout to main, merge proceeds."""
        repo_dir = self._setup_feature_branch_repo(tmp_path)

        # Create a dirty tracked file on the feature branch
        # Must be a tracked file (not untracked) to trigger the stash path
        (repo_dir / "README.md").write_text("dirty content\n")

        # Confirm we're on feature branch with dirty state
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "feature/ongoing"
        status = _git(repo_dir, "status", "--porcelain")
        assert status.strip() != "", "Repo should be dirty"

        # Verify stash list is empty before
        stash_before = _git(repo_dir, "stash", "list")
        assert stash_before == ""

        # Call merge_worktree — pre-check 3 should stash + checkout main
        # Full merge may fail (main may not exist as tracked) but we only care
        # that pre-check 3 ran: stash was created and we ended up on main
        dispatcher.merge_worktree(str(repo_dir), "agent/test-42")

        # Stash should have been created
        stash_after = _git(repo_dir, "stash", "list")
        assert "dispatcher: stashed feature/ongoing" in stash_after, (
            f"Expected stash entry, got: {stash_after!r}"
        )

        # Should be on main now (not feature/ongoing)
        current = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main", f"Expected 'main', got '{current}'"

    def test_on_feature_clean_no_stash_checkout_to_main(self, tmp_path, monkeypatch):
        """Feature branch + clean state → no stash, checkout to main, merge proceeds."""
        repo_dir = self._setup_feature_branch_repo(tmp_path)

        # feature/ongoing is clean — no dirty files
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "feature/ongoing"
        status = _git(repo_dir, "status", "--porcelain")
        assert status.strip() == "", "Repo should be clean"

        stash_before = _git(repo_dir, "stash", "list")

        dispatcher.merge_worktree(str(repo_dir), "agent/test-42")

        # No stash should have been created (clean branch)
        stash_after = _git(repo_dir, "stash", "list")
        assert stash_after == stash_before, "Clean branch should not create a stash"

        # Should still be on main after checkout
        current = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main", f"Expected 'main', got '{current}'"

    def test_on_default_branch_precheck3_skipped(self, tmp_path, monkeypatch):
        """Already on default branch → pre-check 3 skipped (no stash, stays on main)."""
        repo_dir = tmp_path / "project"
        make_repo(repo_dir, branch="main")

        # Create agent branch to merge
        _git(repo_dir, "checkout", "-b", "agent/test-99")
        (repo_dir / "agent_out.txt").write_text("done\n")
        _git(repo_dir, "add", "agent_out.txt")
        _git(repo_dir, "commit", "-m", "agent done")
        _git(repo_dir, "checkout", "main")

        # Confirm we're on main (default branch)
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") == "main"

        stash_before = _git(repo_dir, "stash", "list")

        # merge_worktree should skip pre-check 3 entirely and proceed to merge
        success, err = dispatcher.merge_worktree(str(repo_dir), "agent/test-99")

        # Pre-check 3 was skipped (no stash created, still on main)
        stash_after = _git(repo_dir, "stash", "list")
        assert stash_after == stash_before, "Pre-check 3 should not create stash on default branch"

        current = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        assert current == "main"

        # Merge should succeed since we were already on main
        assert success is True, f"Merge should succeed; error: {err}"
