# Recording the demo yourself

Two ways. Pick based on whether you want the *real run* or a *clean take*.

Before either: the demo needs Docker. Argo CD is not involved here, so 4 GB is
plenty. Last time I ran colima at 8 GB it nearly took the machine down.

```bash
colima start --cpu 4 --memory 4
```

---

## Option A: record the real run (asciinema + agg)

Records exactly what happens, warts and timing included. The run takes about six
minutes because it waits on real image pulls and two verification windows.

```bash
brew install asciinema agg

cd ~/dev/kubemend
asciinema rec demo.cast --cols 100 --rows 28 -c "demo/run.sh"
```

It drops you into the recording, runs the demo, and stops when the script exits.

Then convert to a GIF:

```bash
agg demo.cast docs/media/demo.gif \
  --font-family "Menlo" \
  --font-size 15 \
  --theme github-dark \
  --speed 2.5 \
  --idle-time-limit 2
```

`--speed 2.5` matters. The raw run has long dead stretches while it waits for a
rollout, and nobody is watching 90 seconds of a blinking cursor.
`--idle-time-limit 2` caps any single pause at two seconds, which does most of
the work on its own.

**Keep the `.cast` file.** It is a text format, it is tiny, and it is the actual
recording. You can regenerate the GIF at any size or speed later without
re-running anything.

---

## Option B: a clean scripted take (vhs)

vhs types the commands for you at a controlled speed and never fat-fingers
anything at minute five of a six minute recording. Better for something you are
putting at the top of a README.

```bash
brew install vhs
cd ~/dev/kubemend
vhs docs/posts/demo.tape
```

The tape file is committed next to this one. Edit the `Sleep` values if the run
is slower on your machine.

---

## What to actually record

The full run is ~108 lines and most of it is cluster setup nobody needs to watch.
The arc worth showing is four beats:

1. A release goes out and breaks. Pods in `ImagePullBackOff`.
2. The agent reads the cluster, commits a rollback, one line diff, `recovered
   after 26s`, commit kept.
3. Two bad releases in a row, so the rollback lands on another broken revision.
   `still failing after 76s`, `reverted in ...`.
4. `kubemend log`. Two anecdotes become a revert rate.

If you record the whole thing, trim to start at step 3 of the script ("A release
goes out, and it is wrong") and end on "and reverted the one that did not."

---

## Sizing for each platform

| Where | Target | Notes |
|---|---|---|
| GitHub README | any width, under ~10 MB | renders inline, autoplays |
| LinkedIn | under 8 MB, 1200px wide | GIFs over ~5 MB sometimes convert badly, MP4 is safer |
| dev.to | under 25 MB | GIF is fine |
| Medium | any | upload as image, it will loop |

If LinkedIn mangles the GIF, convert to MP4 and upload that instead:

```bash
brew install ffmpeg
ffmpeg -i docs/media/demo.gif -movflags faststart -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" docs/media/demo.mp4
```

LinkedIn treats native video better than GIFs anyway.

---

## The one I already made

`docs/media/demo.gif` was rendered from a real run with
`docs/media/render.py`, which reads the saved terminal output and draws the
frames directly. No recording tools needed, which is why it exists.

It is honest about what it is: the text is verbatim from a real k3d run, and
`demo/transcript.txt` in the repo is the same run so anyone can check it. The
colors are kubemend's own from `cli.py`, repainted because the demo pipes through
`sed` and runs `--no-color`, which strips them from the saved file.

If you record your own, that one becomes redundant. Delete it or keep it as the
fallback.
