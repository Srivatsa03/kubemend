# LinkedIn

Attach `docs/media/demo.gif`. Links go in the first comment, not the body.

---

## The post

I have built a Kubernetes agent that can diagnose a broken deployment, write the
correct fix, commit it, confirm the workload did not recover, and then revert its
own change.

Three of those five steps are features.

The whole pitch is that it never touches your cluster. No credentials. It writes
a git commit, whatever reconciler you already run picks it up, and undoing it is
`git revert`.

While building it I did not feel like installing Argo CD, so I used `kubectl
apply` as a stand-in. Close enough, I decided.

Last week I installed the real thing. It found three bugs in an afternoon.

The best one: my agent was committing the fix and never pushing it. With `kubectl
apply` reading my working tree, a local commit was the delivery. So the code was
correct for the fake reconciler and wrong for every real one.

Against Argo CD that commit does not exist. The workload never recovers.
Verification correctly reports "still failing." The agent then does the
reasonable thing with the wrong information and reverts its own fix.

A fix that was correct. Undone, because nobody ever received it.

33 tests covered that code path. Not one had a git remote, so not one could have
caught it. The stand-in had not just gone untested. It had been answering the
question on my behalf.

I keep coming back to that. A stand-in does not only leave a gap in your
coverage. It satisfies a requirement the real dependency would have enforced, and
hides every bug that depends on it.

The tool also reports how often its own fixes fail to hold, which is a number I
have not seen anyone else publish.

I assume that is because publishing it means admitting your agent is sometimes
wrong.

Mine is. It says so in the log.

---

## First comment

Wrote both halves up properly.

The bug and what it taught me: [MEDIUM LINK]
How the whole thing is built, start to finish: [DEV.TO LINK]

Code: github.com/Srivatsa03/kubemend

The clip is a real run. It fixes one incident, fails to fix the next, and
withdraws its own commit. Planner is deterministic, no model in the decision path
yet, and I would rather say that than let anyone assume otherwise.

---

## If you want a second, shorter post later in the week

Nobody publishes how often their agent is wrong.

Detection rates, constantly. Nobody is stuck on whether a crash loop can be
identified. The question that actually gates autonomy is different: when it acts,
how often does it have to undo itself?

My Kubernetes agent commits a fix, watches, and reverts its own commit when the
fix does not hold. That gives me a revert rate. It is the only honest input to
"should this be allowed to act unattended rather than open a PR."

Two rules I would defend at length:

Recovery needs two consecutive clean reads, and a failed read *breaks* the streak
rather than being skipped. Two clean reads either side of a blind one are not two
consecutive observations.

An unreachable API server is not a failed fix. Reverting on that would turn a
network blip into a second unplanned production change. Silence is not success,
but it is not failure either.

github.com/Srivatsa03/kubemend
