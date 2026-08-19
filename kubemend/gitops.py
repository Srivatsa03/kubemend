"""Turn a plan into a commit against a GitOps repository.

This is where the project's central claim becomes true rather than described.
The agent has no cluster credentials and issues no API calls; it edits a
manifest and commits, and whatever reconciles that repository — Argo CD, Flux —
carries the change to the cluster. Everything a GitOps repository already gives
you then applies to the agent for free: an audit trail, review before rollout,
and a revert that is one command.

Two consequences shape the code.

**Rollback is a git operation, not an edit.** In a repository the manifest is
the source of truth, so returning a workload to its previous revision means
restoring the file to its previous committed state. There is nothing to
compute: the prior state is in the history, exactly as it was, comments and
all. This is the single strongest argument for routing actions through git, and
it falls out rather than being engineered.

**Not every action is expressible.** A rolling restart is a Kubernetes verb with
no manifest representation unless the workload already carries a restart
annotation, and inventing fields the author never wrote is not a change this may
make unattended. Those actions are refused with a reason rather than
approximated, which leaves them to a human — the same outcome the planner
already produces for findings it cannot safely fix.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import ManifestError, _walk, read_field, set_field
from .model import Action, ActionKind, Autonomy, Plan, Target

__all__ = ["GitError", "GitOpsRepo", "Change", "Emission", "field_path_for"]

# Files worth searching for a workload definition.
MANIFEST_SUFFIXES = (".yaml", ".yml")

# Directories that never contain hand-maintained manifests.
SKIP_DIRS = {".git", "node_modules", ".venv", "vendor", ".terraform"}


class GitError(RuntimeError):
    """A git command failed, or the repository is not in a usable state."""


def _first_line(text: str) -> str:
    """Git failures are multi-line and mostly hint text; keep the actionable bit."""
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("hint:"):
            return line[:160]
    return text.strip()[:160]


# Anything that touches a remote can ask for a credential, and an agent that
# runs unattended must never be the process sitting at a password prompt. These
# turn every such request into a fast, reportable failure instead of a hang:
# no terminal prompt, no GUI askpass, no interactive SSH, and no inherited stdin
# for git to read from.
_NON_INTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
}


def _git(repo: Path, *args: str, check: bool = True, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, **_NON_INTERACTIVE},
        )
    except subprocess.TimeoutExpired:
        # Reported rather than raised as a crash: a stalled remote is a delivery
        # failure like any other, and the caller decides what that means.
        raise GitError(f"git {' '.join(args)}: timed out after {timeout:.0f}s") from None
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def field_path_for(action: Action) -> tuple[str, ...] | None:
    """Where in a workload manifest this action's change lives.

    Returns None for actions with no manifest representation, which the caller
    must treat as "not expressible here" rather than as an error.
    """
    pod = ("spec", "template", "spec")
    if action.kind is ActionKind.SCALE:
        return ("spec", "replicas")
    if action.kind is ActionKind.SET_RESOURCES:
        key = next(iter(action.after), "")
        return pod + ("containers", action.container, "resources", "limits", key)
    if action.kind is ActionKind.SET_IMAGE:
        return pod + ("containers", action.container, "image")
    # ROLLBACK is handled through history; RESTART and SET_PROBE have no
    # dependable manifest field to edit.
    return None


@dataclass
class Change:
    """One action rendered against the repository, or the reason it could not be."""

    action: Action
    path: Path | None = None
    diff: str = ""
    detail: str = ""
    skipped_reason: str = ""

    @property
    def applied(self) -> bool:
        return self.path is not None and not self.skipped_reason


@dataclass
class Emission:
    """The result of writing a plan to the repository."""

    branch: str
    changes: list[Change] = field(default_factory=list)
    commit: str = ""
    message: str = ""
    committed: bool = False
    pushed: bool = False
    push_failed: bool = False
    push_note: str = ""
    pr_url: str = ""
    pr_note: str = ""

    @property
    def delivered(self) -> bool:
        """Whether a reconciler could actually see this change.

        "No remote" and "the push failed" are different facts and must not be
        collapsed. A repository with no remote *is* the source of truth for
        whatever reads it, which is the normal local case; a failed push means
        the commit exists only here while the reconciler still reads the old
        state. Only the second one makes verification meaningless — watching a
        cluster that was never sent the fix would time out and then revert a
        change that might have worked.
        """
        return self.committed and not self.push_failed

    @property
    def applied(self) -> list[Change]:
        return [c for c in self.changes if c.applied]

    @property
    def skipped(self) -> list[Change]:
        return [c for c in self.changes if not c.applied]

    @property
    def diff(self) -> str:
        return "\n".join(c.diff for c in self.applied if c.diff)


class GitOpsRepo:
    """A checkout of the repository that defines the cluster's desired state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        if not (self.path / ".git").exists():
            raise GitError(f"{self.path} is not a git repository")
        self._index: dict[Target, tuple[Path, tuple[int, int]]] | None = None

    # --- locating ---------------------------------------------------------

    def _manifests(self):
        for candidate in sorted(self.path.rglob("*")):
            if candidate.suffix not in MANIFEST_SUFFIXES or not candidate.is_file():
                continue
            if any(part in SKIP_DIRS for part in candidate.relative_to(self.path).parts):
                continue
            yield candidate

    def locate(self, target: Target) -> tuple[Path, tuple[int, int]] | None:
        """Find the file and document range defining ``target``.

        Matched on kind, name and namespace from the manifest's own metadata
        rather than on filename, because a repository laid out by team or by
        environment will not name files after the workloads inside them.
        """
        if self._index is None:
            self._index = {}
            for candidate in self._manifests():
                try:
                    text = candidate.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                # Index every workload document in the file, not just the one
                # being looked for, so later lookups cost nothing.
                self._index_file(candidate, text)
        return self._index.get(target)

    def _index_file(self, path: Path, text: str) -> None:
        assert self._index is not None
        lines = text.splitlines()
        bounds = [0] + [i + 1 for i, l in enumerate(lines) if l.rstrip() == "---" and i] + [len(lines)]
        for start, end in zip(bounds, bounds[1:]):
            kind = name = namespace = None
            for _, _, field_path, _, value in _walk(lines[start:end]):
                if field_path == ("kind",):
                    kind = value
                elif field_path == ("metadata", "name"):
                    name = value
                elif field_path == ("metadata", "namespace"):
                    namespace = value
            if kind in ("Deployment", "StatefulSet", "DaemonSet") and name:
                self._index[Target(namespace or "default", kind, name)] = (path, (start, end))

    # --- rendering --------------------------------------------------------

    def render(self, action: Action) -> Change:
        """Apply one action to the working tree, or explain why it cannot be."""
        located = self.locate(action.target)
        if located is None:
            return Change(action, skipped_reason=f"no manifest defines {action.target}")
        path, bounds = located

        if action.kind is ActionKind.ROLLBACK:
            return self._rollback(action, path)

        field = field_path_for(action)
        if field is None:
            return Change(
                action,
                skipped_reason=(
                    f"'{action.kind.value}' has no manifest field to edit; "
                    "it needs a human or a cluster-side verb"
                ),
            )
        if action.kind in (ActionKind.SET_RESOURCES, ActionKind.SET_IMAGE) and not action.container:
            return Change(action, skipped_reason="action does not say which container to change")

        text = path.read_text(encoding="utf-8")
        current = read_field(text, field, bounds)
        if current is None:
            return Change(action, skipped_reason=f"{'.'.join(field)} is not set in {path.name}")

        expected = str(next(iter(action.before.values()), ""))
        if current != expected:
            # The repository has moved since the cluster snapshot was taken.
            # Editing anyway would overwrite whatever changed it.
            return Change(
                action,
                skipped_reason=(
                    f"{'.'.join(field)} is {current} in the repository but the cluster "
                    f"reported {expected}; refusing to edit a stale value"
                ),
            )

        try:
            edit = set_field(text, field, str(next(iter(action.after.values()))), bounds)
        except ManifestError as exc:
            return Change(action, skipped_reason=str(exc))
        path.write_text(edit.text, encoding="utf-8")
        return Change(action, path=path, diff=self._diff(path), detail=edit.describe())

    def _rollback(self, action: Action, path: Path) -> Change:
        """Restore a manifest to its previous committed state.

        The prior revision is already in the history, byte for byte. Nothing is
        reconstructed, which is why this is the action trusted earliest.
        """
        relative = path.relative_to(self.path).as_posix()
        history = _git(self.path, "log", "--format=%H", "--", relative).split()
        if len(history) < 2:
            return Change(
                action,
                skipped_reason=(
                    f"{relative} has no earlier commit to roll back to "
                    f"({len(history)} in history)"
                ),
            )
        previous = history[1]
        restored = _git(self.path, "show", f"{previous}:{relative}")
        if restored == path.read_text(encoding="utf-8"):
            return Change(action, skipped_reason=f"{relative} already matches {previous[:8]}")
        path.write_text(restored, encoding="utf-8")
        return Change(
            action,
            path=path,
            diff=self._diff(path),
            detail=f"restored {relative} to {previous[:8]}",
        )

    def _diff(self, path: Path) -> str:
        return _git(self.path, "diff", "--", str(path.relative_to(self.path))).rstrip()

    # --- committing -------------------------------------------------------

    def emit(
        self,
        plan: Plan,
        autonomy: Autonomy,
        *,
        branch: str | None = None,
        author: str = "kubemend <kubemend@localhost>",
        push: bool = True,
    ) -> Emission:
        """Render a plan and, unless reporting only, commit it.

        ``autonomy`` decides the destination, not whether the work happens:
        REPORT renders the diff and reverts the working tree, PROPOSE commits to
        a new branch for review, APPLY commits to the current branch.

        An APPLY commit is pushed when a remote exists, because the contract of
        this agent is that a reconciler picks the commit up. A commit sitting
        unpushed on one machine is not a change to the cluster at all, and the
        failure mode is worse than doing nothing: verification would watch a
        workload that was never going to recover and revert a fix that was
        correct but undelivered. ``push=False`` is for callers that manage
        delivery themselves.
        """
        if self.dirty():
            raise GitError(
                "the repository has uncommitted changes; refusing to mix them with a plan"
            )

        target = next(iter(plan.targets), None)
        default = f"kubemend/{target.namespace}-{target.name}" if target else "kubemend/plan"
        emission = Emission(branch=branch or default)

        original = self.current_branch()
        if autonomy is Autonomy.PROPOSE:
            _git(self.path, "checkout", "-b", emission.branch)
        else:
            # APPLY and REPORT stay where they are; naming a branch that was
            # never created would misreport where the change landed.
            emission.branch = original

        try:
            for action in plan.actions:
                emission.changes.append(self.render(action))
            emission.message = self.message_for(plan, emission)

            if autonomy is Autonomy.REPORT or not emission.applied:
                # Leave the tree as we found it: a report changes nothing.
                _git(self.path, "checkout", "--", ".")
                if autonomy is Autonomy.PROPOSE:
                    _git(self.path, "checkout", original)
                    _git(self.path, "branch", "-D", emission.branch, check=False)
                    emission.branch = original
                return emission

            for change in emission.applied:
                _git(self.path, "add", str(change.path.relative_to(self.path)))
            _git(self.path, "-c", f"user.name={author.split(' <')[0]}",
                 "-c", f"user.email={author.split('<')[1].rstrip('>')}",
                 "commit", "-m", emission.message)
            emission.commit = _git(self.path, "rev-parse", "HEAD").strip()
            emission.committed = True
            if autonomy is Autonomy.APPLY and push:
                self._deliver(emission)
            if autonomy is Autonomy.PROPOSE:
                _git(self.path, "checkout", original)
        except Exception:
            _git(self.path, "checkout", "--", ".", check=False)
            if autonomy is Autonomy.PROPOSE and self.current_branch() != original:
                _git(self.path, "checkout", original, check=False)
                _git(self.path, "branch", "-D", emission.branch, check=False)
            raise
        return emission

    def message_for(self, plan: Plan, emission: Emission) -> str:
        """A commit message a reviewer can act on without opening the tool.

        Carries the evidence, not just the change: what was observed, what is
        being done about it, how to undo it, and what was deliberately left
        alone.
        """
        target = next(iter(plan.targets), None)
        subject = f"{plan.actions[0].kind.value} {target}" if target else "kubemend plan"
        lines = [subject, ""]

        lines.append("Observed:")
        for finding in plan.findings:
            lines.append(f"  [{finding.severity.value}] {finding.summary}")

        if emission.applied:
            lines += ["", "Changed:"]
            for change in emission.applied:
                lines.append(f"  {change.detail}")
                lines.append(f"    reason: {change.action.reason}")
                # The per-action inverse describes a field swap, which is only
                # what happened for an edit. A rollback restored a file from
                # history, and quoting a cluster revision number for it would
                # point a reader at the wrong thing.
                if change.action.kind is not ActionKind.ROLLBACK and change.action.reversible:
                    lines.append(f"    undo:   {change.action.inverse().describe()}")

        if emission.skipped:
            lines += ["", "Not changed:"]
            for change in emission.skipped:
                lines.append(f"  {change.action.kind.value}: {change.skipped_reason}")

        lines += [
            "",
            f"Blast radius: {plan.impacted_pods} pod(s) across {len(plan.targets)} workload(s).",
            # True of every commit here, and the reason the actions travel this
            # way at all: undoing the agent is the same operation as undoing any
            # other change to the repository.
            "Undo: git revert this commit.",
        ]
        return "\n".join(lines) + "\n"

    # --- undoing and proposing --------------------------------------------

    def revert(self, commit: str, reason: str, *, push: bool = True) -> str:
        """Undo one of our own commits with a new commit.

        ``git revert`` rather than a reset: the history of an automated system
        acting on production is worth keeping, and a reviewer looking later
        should see both that it acted and that it withdrew.
        """
        _git(self.path, "-c", "user.name=kubemend", "-c", "user.email=kubemend@localhost",
             "revert", "--no-edit", commit)
        _git(self.path, "-c", "user.name=kubemend", "-c", "user.email=kubemend@localhost",
             "commit", "--amend", "--no-edit", "-m",
             f"Revert \"{self._subject(commit)}\"\n\n"
             f"Verification did not confirm recovery: {reason}\n\n"
             f"This reverts {commit[:8]}. The cluster is back to the state a human\n"
             f"last approved, which is where an unverified automated change belongs.\n")
        # Read HEAD *after* the amend. Amending replaces the commit object, so a
        # SHA captured before it is left dangling — and this value is what the
        # journal stores and the CLI prints as "reverted in ...".
        head = _git(self.path, "rev-parse", "HEAD").strip()
        # A revert nobody can see leaves the broken change live wherever the
        # reconciler is looking, which is the one outcome worse than not
        # reverting at all.
        if push and self.has_remote():
            _git(self.path, "push", "origin", self.current_branch(), check=False)
        return head

    def _subject(self, commit: str) -> str:
        return _git(self.path, "log", "-1", "--format=%s", commit).strip()

    def has_remote(self) -> bool:
        return bool(_git(self.path, "remote", check=False).strip())

    def _deliver(self, emission: Emission) -> None:
        """Push the commit so the reconciler can see it.

        A local repository with no remote is the normal case for a demo, and is
        recorded rather than treated as an error. A push that *fails* is the
        opposite: the change was not delivered, and saying so is what stops
        verification drawing a conclusion about a change the cluster never got.
        """
        if not self.has_remote():
            emission.push_note = "no git remote; commit is local only"
            return
        try:
            _git(self.path, "push", "origin", emission.branch)
            emission.pushed = True
        except GitError as exc:
            emission.push_failed = True
            emission.push_note = f"push failed: {_first_line(str(exc))}"

    def open_pull_request(self, emission: Emission, base: str | None = None) -> str:
        """Push the branch and open a pull request, if that is possible here.

        Returns the PR URL, or an empty string with the reason recorded on the
        emission. A local repository with no remote is the normal case for a
        demo and for a dry run, so it is not an error.
        """
        if not emission.committed or not emission.branch.startswith("kubemend/"):
            emission.pr_note = "nothing to propose: no branch commit was made"
            return ""
        if not self.has_remote():
            emission.pr_note = "no git remote; branch exists locally only"
            return ""

        try:
            _git(self.path, "push", "-u", "origin", emission.branch)
        except GitError as exc:
            emission.pr_note = f"could not push the branch: {exc}"
            return ""

        base = base or self._default_base()
        result = subprocess.run(
            ["gh", "pr", "create", "--base", base, "--head", emission.branch,
             "--title", emission.message.splitlines()[0],
             "--body", emission.message],
            cwd=str(self.path), capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            emission.pr_note = (
                "branch pushed; `gh` is not installed or not authenticated"
                if "not found" in stderr or "gh auth" in stderr
                else f"branch pushed; gh pr create failed: {stderr[:160]}"
            )
            return ""
        emission.pr_url = result.stdout.strip().splitlines()[-1]
        return emission.pr_url

    def _default_base(self) -> str:
        head = _git(self.path, "symbolic-ref", "refs/remotes/origin/HEAD", check=False).strip()
        return head.rsplit("/", 1)[-1] if head else "main"

    # --- state ------------------------------------------------------------

    def current_branch(self) -> str:
        return _git(self.path, "rev-parse", "--abbrev-ref", "HEAD").strip()

    def dirty(self) -> bool:
        return bool(_git(self.path, "status", "--porcelain").strip())
