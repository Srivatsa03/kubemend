# site

The published page: **[srivatsa03.github.io/kubemend](https://srivatsa03.github.io/kubemend/)**

| File | What it is |
|---|---|
| `index.html` | The whole page. No framework, no build step, no external requests. |
| `export.py` | Regenerates `data.json` from the code, the fixture and the demo transcript. |
| `data.json` | Every number the page displays. Generated — do not edit by hand. |

## Why the data is exported rather than written

Nothing on the page is typed by a human. The gate verdicts come from calling the
real gate, the findings from the real detector, and the live-cluster timings are
parsed out of `demo/transcript.txt`. A landing page that quotes numbers is a
landing page that will eventually quote stale ones, so the numbers are derived:

```bash
python site/export.py
```

```
wrote site/data.json
  13 findings -> 4 plans, 5 produce no action
  live: recovered 26s, failed 76s, revert rate 50%
  157 tests, 3248 LOC
```

CI regenerates it on every deploy and **fails the build if the committed
`data.json` disagrees with what the code produces**. If behaviour changes and
nobody re-exports, the deploy stops rather than shipping a number the code no
longer supports.

## Working on it locally

```bash
python site/export.py          # after any change to detection, planning or policy
python -m http.server 8899 -d site
open http://127.0.0.1:8899/
```

`data.json` is fetched at runtime, so opening `index.html` directly off the
filesystem will not work — serve the directory.

## Deployment

`.github/workflows/pages.yml` builds and deploys on any push to `main` that
touches `site/`, `kubemend/`, `fixtures/` or the demo transcript. The repository
must have Pages set to **GitHub Actions** as its source.
