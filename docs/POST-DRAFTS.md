# Post drafts — kubemend

Attach `docs/media/demo.gif` to any of these. All numbers are real and checkable
in the repo.

---

## A. LinkedIn — the bug story (recommended)

I'd been testing my Kubernetes remediation agent with `kubectl apply` standing in
for Argo CD. Close enough, I figured. The agent's job ends at the commit either way.

Swapped in the real reconciler this week. It found three bugs in about an hour.

The worst one: the agent committed its fix and never pushed it. With `kubectl apply`
reading my working tree, a local commit *was* the delivery — so the code was correct
for the stand-in and wrong for everything it stood in for.

Against a real reconciler watching a remote, that commit is invisible. The workload
never recovers. Verification correctly reports "still failing." And then the agent
does the reasonable thing with the wrong information: it reverts its own fix.

A fix that was correct. Undone, because it was never delivered.

33 tests covered that code path. None of them had a remote, so none of them could
have caught it. The stand-in hadn't just failed to test delivery — it had been
quietly satisfying the requirement on my behalf.

The lesson I'm keeping: a stand-in doesn't only leave a gap in coverage. It answers
a question the real dependency would have asked, and hides every defect that
depends on the answer.

The other two, for completeness: every revert SHA the agent reported was dangling
(read HEAD before `commit --amend`, which replaces the commit object), and git was
sitting at credential prompts until each 30s timeout — which is a slow test suite
and a production hang wearing the same clothes. An unattended agent must never be
the process waiting at a password prompt.

kubemend is a Kubernetes SRE agent whose only write surface is a git commit. It has
no cluster credentials. Undoing it is `git revert`. The GIF is a real run: it fixes
one incident, fails to fix a second, and withdraws its own commit.

Code and write-up: github.com/Srivatsa03/kubemend

---

## B. LinkedIn — the measurement angle

Every AI SRE demo shows you the fix that worked.

Mine shows both, because I think the second one is the product.

The agent finds a broken deploy, commits a rollback to the GitOps repo, then
watches. First incident: recovered in 26 seconds, commit kept. Second incident,
two bad releases in a row so the rollback lands on another broken revision: still
failing after 76 seconds, and it reverts its own commit.

That gives you a number almost nobody in this space reports — the revert rate. How
often the agent's own fix failed to hold and it had to withdraw it.

It's the only honest input to "should this be allowed to act unattended rather than
open a PR?" And I suspect it goes unreported because reporting it means saying out
loud that your agent is sometimes wrong.

Two design decisions I'd defend at length:

Recovery requires two consecutive clean reads, and a failed read *breaks* the
streak rather than being skipped. Two clean reads either side of a blind one are
not two consecutive observations.

An unreachable API server is not a failed fix. Reverting on that would turn a
network blip into a second unplanned production change — so an unverifiable
outcome stops and asks for a human. Silence is not success, but it isn't failure
either.

Honest caveats, since they belong next to the number: the planner is deterministic,
there's no model in the decision path yet, and the 50% revert rate in the GIF is a
property of a demo built so one of two scenarios is unfixable by rollback. It shows
the measurement works. It is not a production accuracy claim.

github.com/Srivatsa03/kubemend

---

## C. X / short

Swapped `kubectl apply` for real Argo CD in my k8s agent's test setup.

Found in an hour: the agent committed its fix but never pushed. Reconciler never
saw it → workload never recovered → agent reverted a fix that was *correct*.

33 tests on that path. None had a remote.

A stand-in doesn't just fail to test things. It satisfies the requirement for you.

---

## D. X / demo-first

my k8s agent fixing a broken deploy, then failing to fix the next one and
reverting its own commit

26s: recovered, kept
76s: still failing, withdrawn

revert rate is the number that should decide whether an agent gets to act
unattended, and ~nobody reports it

[gif]

---

## Notes

- Lead with A. It's a craft post — no product claims to defend, useful to anyone
  who writes tests, and it demonstrates judgment rather than output.
- Don't call it "AI-powered." The planner is deterministic and the first reply
  will be someone asking where the model is.
- Never quote the 50% revert rate without its caveat.
- The GIF is verbatim output of a real k3d run; `demo/transcript.txt` in the repo
  is the same run in text, so anyone can check it.
