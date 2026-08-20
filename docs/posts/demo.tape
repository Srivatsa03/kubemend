# vhs tape for the kubemend demo.
#   brew install vhs && vhs docs/posts/demo.tape
#
# Sleep values are tuned for a run that pulls images fresh. If your cluster is
# warm the waits are shorter and you can cut them down.

Output docs/media/demo.gif

Set FontFamily "Menlo"
Set FontSize 15
Set Width 1000
Set Height 620
Set Theme "Github Dark"
Set Padding 20
Set TypingSpeed 55ms

Type "demo/run.sh"
Sleep 500ms
Enter

# Cluster creation and the first apply. Nothing to see, so this is the one
# place worth trimming afterwards if you want a shorter clip.
Sleep 90s

# The bad release, and the agent reading the cluster.
Sleep 60s

# It commits the fix and watches. This is beat one.
Sleep 75s

# Two bad releases, the rollback that does not help, and the revert.
Sleep 150s

# The log. Hold here, it is the payoff.
Sleep 12s
