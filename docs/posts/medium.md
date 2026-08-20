# My Agent Undid Its Own Correct Fix. The Bug Was in My Test Setup.

### What happened when I swapped `kubectl apply` for real Argo CD, and why 33 tests never stood a chance

*[Cover: docs/media/demo.gif]*

Every observability tool on earth can tell you a pod is in `CrashLoopBackOff`.
Almost none of them are trusted to do anything about it, and the reason is not
model quality. Granting write access to production requires an answer to one
question, which is what stops this doing something catastrophic at 3am, and "the
model is usually careful" is not an answer. It is a claim about average behavior
offered in reply to a question about the worst case.

So I built kubemend on the opposite constraint. It has no cluster credentials and
issues no write to the Kubernetes API. Its only write surface is a commit to the
GitOps repository that already defines your cluster. Argo CD or Flux carries it
the rest of the way. Undoing the agent is `git revert`.

That single decision inherits an entire safety apparatus instead of
reimplementing it. An audit trail exists because git is one. Review before
rollout exists because a pull request is the native unit of review. Rollback is a
command every operator already knows at 3am.

It also, as it turns out, gave me an excellent place to hide a bug.

## The convenient stand-in

Installing Argo CD into a throwaway cluster costs a few minutes and about four
gigabytes of RAM. While iterating I wanted neither, so my demo script used
`kubectl apply` where a reconciler would be.

This felt like an honest simplification. I even wrote a comment saying so. The
agent's contract ends at the commit, the reasoning went, so whatever applies that
commit is somebody else's concern.

Last week I stood up the real thing. A k3d cluster, actual Argo CD, and a real
GitOps repository on GitHub for it to reconcile.

It found three bugs.

## The one that matters

My agent was committing its fix and never pushing it.

With `kubectl apply` reading my local working tree, a local commit **was** the
delivery. The substitute had quietly satisfied a requirement the real thing
enforces, so my code was correct for the stand-in and wrong for everything it
stood in for.

Point a real reconciler at a remote and that commit simply does not exist:

1. The agent detects a bad image tag and plans a rollback. Correct.
2. It writes the manifest change and commits. Correct.
3. The commit never leaves my machine. Nothing notices.
4. Argo CD, watching GitHub, sees nothing. The cluster keeps running the broken release.
5. The agent polls, watching for recovery. It never comes.
6. Verification correctly reports `still_failing`.
7. The agent reverts its own commit.

Step seven is the one that gets me. Given the evidence available, reverting was
the *right call*. The fix appeared not to work. The agent has a rule that says an
unverified change does not stay, and it followed the rule.

It just happened to be reasoning about a change the cluster was never sent. A
correct fix, thrown away, because delivery failed silently and nothing in the
system could tell the difference between "this fix did not work" and "this fix
never arrived."

I track those separately now. `Emission.delivered` distinguishes "no remote,"
which is fine because a local repository is the source of truth for whatever
reads it, from "the push failed," which is not fine at all. Only the second skips
verification, because watching a cluster that never received your change and then
drawing conclusions from its behavior is not verification. It is astrology with
better logging.

## Why the tests were never going to catch it

I had 33 tests on the emission path. They run against a real temporary git
repository rather than mocks, because the behavior under test is what git
actually does with history, branches, and a dirty tree. I was rather pleased with
myself about that.

Not one of them had a remote.

Every one of those tests silently encoded the same assumption the demo script
did, which is that a commit landing in the local repository is the end of the
story. The stand-in had not merely gone untested. It had defined the interface my
tests were written against.

This is the generalizable part, and I think it is worth more than the bug:

> A stand-in does not just leave a gap in your coverage. It satisfies a
> requirement the real dependency would have enforced, and hides every defect
> that depends on that requirement.

The more convenient the substitute, the more requirements it is quietly meeting
on your behalf. `kubectl apply` was extremely convenient.

## The two I found on the way out

Once a remote was in play, two more surfaced within the hour.

**Every revert hash was dangling.** `revert()` ran `git revert`, captured HEAD,
then ran `git commit --amend` to rewrite the message. Amending replaces the
commit object, so the hash I captured no longer existed. That value is what the
incident log stores and what the CLI prints as `reverted in ...`, so my audit
trail was pointing reviewers at a commit that was not in the history. The fix is
one line. The embarrassment was free.

**My test suite went from 13 seconds to 346.** The moment pushes appeared, git
started sitting at credential prompts until each 30 second timeout expired. This
looks like a test suite annoyance and is actually a production defect in
disguise: an agent that runs unattended must never be the process waiting for
somebody to type a password. Every git call now runs with no terminal prompt, no
askpass, non interactive SSH, and no inherited stdin, so a credential request
becomes a fast reportable failure instead of a hang.

## The number nobody publishes

Because the agent verifies its own work, it can measure something most tooling in
this space does not report: how often its fix failed to hold and it had to
withdraw its own commit.

I call it the revert rate. It is the only honest input to the question of whether
an agent should be allowed to act unattended rather than open a pull request for
a human. Detection rates get published constantly, and nobody is stuck on whether
a crash loop can be identified. The blocker has always been whether the thing
gets to act.

My guess is the number goes unpublished because publishing it means saying out
loud that your agent is sometimes wrong.

Two design decisions I will defend at length. Recovery requires two consecutive
clean reads, and a failed read *breaks* the streak rather than being skipped,
because two clean reads either side of a blind one are not two consecutive
observations. And an unreachable API server is not a failed fix, so it does not
trigger a revert. Undoing a change on that basis would turn a network blip into a
second unplanned production change. Silence is not success, but it is not failure
either.

## The honest part

The planner is deterministic. There is no model in the decision path, and I would
rather say that plainly than let anyone assume otherwise. That is sequencing, not
modesty: the parts that have to be trustworthy are code with tests, so that when
a model is added it correlates and explains rather than deciding what happens to
your cluster.

The demo in the repository produces a 50 percent revert rate, and that is a
property of a demo deliberately built so one of its two scenarios cannot be fixed
by a rollback. It shows the measurement working end to end. It is not a
production accuracy claim, and anyone quoting it as one is quoting a demo
parameter.

Version 0.1.0, alpha, Deployments only.

But it does read your cluster, commit a fix, watch, keep the one that worked, and
revert the one that did not. And it will tell you, in writing, how often that
second thing happens.

Code, threat model, and evaluation: [github.com/Srivatsa03/kubemend](https://github.com/Srivatsa03/kubemend)
