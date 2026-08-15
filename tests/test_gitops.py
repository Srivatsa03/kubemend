"""Tests for manifest editing and GitOps emission.

Run against a real temporary git repository rather than a mock, because the
behaviour that matters — restoring a file to its previous committed state,
refusing to edit a value that has drifted, leaving the tree clean when a plan is
only reported — is behaviour of git, and a mock would only prove the mock works.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kubemend.gitops import GitError, GitOpsRepo
from kubemend.manifest import ManifestError, find_document, read_field, set_field
from kubemend.model import Action, ActionKind, Autonomy, Finding, Plan, Severity, Target

CHECKOUT = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: checkout
  namespace: payments
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: checkout
          image: reg.internal/checkout:2.3.0   # pinned by release
          resources:
            limits:
              memory: 512Mi
              cpu: 500m
        - name: sidecar
          image: reg.internal/envoy:1.2.0
          resources:
            limits:
              memory: 64Mi
"""

API = """apiVersion: v1
kind: Service
metadata:
  name: api
  namespace: payments
spec:
  ports:
    - port: 80
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
  namespace: payments
spec:
  replicas: 4
  template:
    spec:
      containers:
        - name: api
          image: reg.internal/api:1.9.2
          resources:
            limits:
              memory: 256Mi
"""

PAY = Target("payments", "Deployment", "checkout")
API_T = Target("payments", "Deployment", "api")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A GitOps repo with two commits, so rollback has somewhere to go."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(tmp_path, "config", k, v)

    apps = tmp_path / "clusters" / "prod"
    apps.mkdir(parents=True)
    # The first release: the version we will want to get back to.
    (apps / "checkout.yaml").write_text(CHECKOUT.replace("2.3.0", "2.2.0"))
    (apps / "api.yaml").write_text(API)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "initial")

    # The release that broke it.
    (apps / "checkout.yaml").write_text(CHECKOUT)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "release 2.3.0")
    return tmp_path


def scale(target=PAY, before=3, after=5):
    return Action(ActionKind.SCALE, target, {"replicas": before}, {"replicas": after},
                  "capacity", impacted_pods=after)


def memory(target=API_T, before="256Mi", after="512Mi", container="api"):
    return Action(ActionKind.SET_RESOURCES, target, {"memory": before}, {"memory": after},
                  "OOMKilled", impacted_pods=4, container=container)


def rollback(target=PAY):
    return Action(ActionKind.ROLLBACK, target, {"revision": 2}, {"revision": 1},
                  "rollout crashed", impacted_pods=3)


def plan_of(*actions):
    return Plan(
        findings=[Finding("crashloop", Severity.CRITICAL, actions[0].target, "container crashing")],
        actions=list(actions),
        rationale="test",
    )


# --- manifest editing --------------------------------------------------------


def test_edits_one_line_and_nothing_else():
    """A commit that reformats the file is unreviewable, which defeats the point."""
    edit = set_field(CHECKOUT, ("spec", "replicas"), "5")
    before, after = CHECKOUT.splitlines(), edit.text.splitlines()
    differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(differing) == 1
    assert after[differing[0]].strip() == "replicas: 5"


def test_trailing_comments_survive_an_edit():
    path = ("spec", "template", "spec", "containers", "checkout", "image")
    edit = set_field(CHECKOUT, path, "reg.internal/checkout:2.2.0")
    assert "# pinned by release" in edit.text


def test_containers_are_addressed_by_name_not_position():
    """Editing the wrong container is a silent, dangerous mistake."""
    base = ("spec", "template", "spec", "containers")
    edit = set_field(CHECKOUT, base + ("sidecar", "resources", "limits", "memory"), "128Mi")
    assert "memory: 128Mi" in edit.text
    assert "memory: 512Mi" in edit.text  # the first container is untouched


def test_reading_a_value_strips_its_comment():
    path = ("spec", "template", "spec", "containers", "checkout", "image")
    assert read_field(CHECKOUT, path) == "reg.internal/checkout:2.3.0"


def test_absent_fields_are_refused_not_created():
    """Inventing a limit the author never wrote is a different kind of change."""
    with pytest.raises(ManifestError):
        set_field(CHECKOUT, ("spec", "template", "spec", "containers", "checkout",
                             "resources", "limits", "ephemeral-storage"), "1Gi")


def test_the_right_document_is_found_in_a_multi_document_file():
    bounds = find_document(API, "Deployment", "api", "payments")
    assert bounds is not None
    assert read_field(API, ("spec", "replicas"), bounds) == "4"
    # The Service in the same file also has a spec; it must not be matched.
    assert find_document(API, "Deployment", "api", "other") is None


# --- locating ----------------------------------------------------------------


def test_workloads_are_found_by_metadata_not_filename(repo):
    located = GitOpsRepo(repo).locate(PAY)
    assert located is not None
    assert located[0].name == "checkout.yaml"


def test_a_workload_in_a_multi_document_file_is_found(repo):
    path, bounds = GitOpsRepo(repo).locate(API_T)
    assert path.name == "api.yaml"
    assert read_field(path.read_text(), ("spec", "replicas"), bounds) == "4"


def test_an_unknown_workload_is_reported_not_guessed(repo):
    change = GitOpsRepo(repo).render(scale(Target("payments", "Deployment", "ghost")))
    assert not change.applied
    assert "no manifest defines" in change.skipped_reason


# --- rendering ---------------------------------------------------------------


def test_scale_edits_the_replica_count(repo):
    change = GitOpsRepo(repo).render(scale())
    assert change.applied
    assert "replicas: 5" in change.path.read_text()
    assert "-  replicas: 3" in change.diff and "+  replicas: 5" in change.diff


def test_resources_edit_targets_the_named_container(repo):
    change = GitOpsRepo(repo).render(memory())
    assert change.applied
    assert "memory: 512Mi" in change.path.read_text()


def test_a_resource_action_without_a_container_is_refused(repo):
    change = GitOpsRepo(repo).render(memory(container=""))
    assert not change.applied
    assert "which container" in change.skipped_reason


def test_a_value_that_drifted_in_the_repository_is_not_overwritten(repo):
    """The snapshot said 3 replicas; if the repo says otherwise, someone changed
    it and their change is not ours to discard."""
    change = GitOpsRepo(repo).render(scale(before=99))
    assert not change.applied
    assert "stale value" in change.skipped_reason


def test_restart_is_refused_because_it_has_no_manifest_field(repo):
    action = Action(ActionKind.RESTART, PAY, {"restartedAt": "t0"}, {"restartedAt": "t1"}, "x")
    change = GitOpsRepo(repo).render(action)
    assert not change.applied
    assert "no manifest field" in change.skipped_reason


# --- rollback through history ------------------------------------------------


def test_rollback_restores_the_previous_committed_state(repo):
    """The prior revision is in the history byte for byte; nothing is rebuilt."""
    change = GitOpsRepo(repo).render(rollback())
    assert change.applied
    text = change.path.read_text()
    assert "reg.internal/checkout:2.2.0" in text
    assert "# pinned by release" in text        # comments come back too
    assert "restored" in change.detail


def test_rollback_needs_an_earlier_commit(repo):
    """api.yaml has only ever been committed once."""
    change = GitOpsRepo(repo).render(rollback(API_T))
    assert not change.applied
    assert "no earlier commit" in change.skipped_reason


# --- emission ----------------------------------------------------------------


def test_report_leaves_the_tree_untouched(repo):
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.REPORT)
    assert emission.applied and emission.diff          # the diff was computed
    assert not emission.committed
    assert not r.dirty()                               # and then reverted


def test_propose_commits_to_a_new_branch_and_returns(repo):
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.PROPOSE)
    assert emission.committed
    assert emission.branch.startswith("kubemend/")
    assert r.current_branch() == "main"                # back where we started
    assert "replicas: 3" in (repo / "clusters/prod/checkout.yaml").read_text()
    on_branch = git(repo, "show", f"{emission.branch}:clusters/prod/checkout.yaml")
    assert "replicas: 5" in on_branch


def test_apply_commits_to_the_current_branch(repo):
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.APPLY)
    assert emission.committed
    assert r.current_branch() == "main"
    assert "replicas: 5" in (repo / "clusters/prod/checkout.yaml").read_text()
    assert not r.dirty()


def test_a_plan_with_nothing_appliable_commits_nothing(repo):
    action = Action(ActionKind.RESTART, PAY, {"a": "1"}, {"a": "2"}, "x")
    emission = GitOpsRepo(repo).emit(plan_of(action), Autonomy.APPLY)
    assert not emission.committed
    assert emission.skipped


def test_uncommitted_work_blocks_emission(repo):
    """Mixing someone's in-progress edit into an automated commit is not ours to do."""
    (repo / "clusters/prod/checkout.yaml").write_text(CHECKOUT + "\n# someone was here\n")
    with pytest.raises(GitError, match="uncommitted"):
        GitOpsRepo(repo).emit(plan_of(scale()), Autonomy.APPLY)


def test_the_commit_message_carries_evidence_and_the_undo(repo):
    emission = GitOpsRepo(repo).emit(plan_of(scale()), Autonomy.APPLY)
    message = git(repo, "log", "-1", "--format=%B")
    assert "Observed:" in message
    assert "container crashing" in message
    assert "undo:" in message
    assert "Blast radius" in message


def test_the_commit_message_records_what_was_left_alone(repo):
    plan = plan_of(scale(), Action(ActionKind.RESTART, PAY, {"a": "1"}, {"a": "2"}, "x"))
    GitOpsRepo(repo).emit(plan, Autonomy.APPLY)
    assert "Not changed:" in git(repo, "log", "-1", "--format=%B")


def test_a_directory_without_git_is_refused(tmp_path):
    with pytest.raises(GitError, match="not a git repository"):
        GitOpsRepo(tmp_path)


def test_apply_reports_the_branch_it_actually_committed_to(repo):
    """Naming a branch that was never created misreports where a change landed."""
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.APPLY)
    assert emission.branch == "main"
    assert git(repo, "rev-parse", "HEAD").strip() == emission.commit


def test_report_does_not_claim_a_branch_either(repo):
    emission = GitOpsRepo(repo).emit(plan_of(scale()), Autonomy.REPORT)
    assert emission.branch == "main"


def test_comment_spacing_is_preserved_so_the_diff_shows_one_change(repo):
    """Shifting a trailing comment left is churn in a diff meant to be minimal."""
    manifest = repo / "clusters/prod/checkout.yaml"
    before = manifest.read_text()
    GitOpsRepo(repo).render(
        Action(ActionKind.SET_IMAGE, PAY,
               {"image": "reg.internal/checkout:2.3.0"},
               {"image": "reg.internal/checkout:2.4.0"},
               "pin", container="checkout")
    )
    after = manifest.read_text()
    # The run of spaces ahead of the comment is identical on the changed line.
    b = next(l for l in before.splitlines() if "pinned by release" in l)
    a = next(l for l in after.splitlines() if "pinned by release" in l)
    assert b.index("#") == a.index("#")


def test_every_commit_states_the_universal_undo(repo):
    """Reverting the agent is the same operation as reverting anything else,
    which is the reason its actions travel through git at all."""
    GitOpsRepo(repo).emit(plan_of(scale()), Autonomy.APPLY)
    assert "git revert this commit" in git(repo, "log", "-1", "--format=%B")


def test_a_rollback_does_not_quote_a_cluster_revision_as_its_undo(repo):
    """It restored a file from history; a revision number points elsewhere."""
    GitOpsRepo(repo).emit(plan_of(rollback()), Autonomy.APPLY)
    message = git(repo, "log", "-1", "--format=%B")
    assert "restored" in message
    assert "undo:   rollback" not in message


# --- undoing and proposing ---------------------------------------------------


def test_revert_undoes_our_commit_with_another_commit(repo):
    """History of an automated system acting on production is worth keeping, so
    it withdraws with a revert rather than a reset."""
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.APPLY)
    manifest = repo / "clusters/prod/checkout.yaml"
    assert "replicas: 5" in manifest.read_text()

    r.revert(emission.commit, "still failing after 180s")
    assert "replicas: 3" in manifest.read_text()
    log = git(repo, "log", "--format=%s")
    assert log.splitlines()[0].startswith("Revert")
    assert emission.commit[:8] in git(repo, "log", "-1", "--format=%B")


def test_the_revert_message_says_why(repo):
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.APPLY)
    r.revert(emission.commit, "still failing after 180s")
    body = git(repo, "log", "-1", "--format=%B")
    assert "did not confirm recovery" in body
    assert "still failing after 180s" in body


def test_a_local_repo_reports_that_it_cannot_open_a_pull_request(repo):
    """No remote is the normal case for a demo, not an error."""
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.PROPOSE)
    assert r.open_pull_request(emission) == ""
    assert "no git remote" in emission.pr_note


def test_nothing_to_propose_when_no_branch_commit_was_made(repo):
    r = GitOpsRepo(repo)
    emission = r.emit(plan_of(scale()), Autonomy.APPLY)   # lands on main
    assert r.open_pull_request(emission) == ""
    assert "nothing to propose" in emission.pr_note
