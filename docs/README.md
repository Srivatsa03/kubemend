# docs

| File | What it is |
|---|---|
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | The adversary model and fourteen failure modes, each marked prevented, mitigated or out of scope |
| [`EVALUATION.md`](EVALUATION.md) | Measured results, method, and the gaps |
| [`FINDINGS.md`](FINDINGS.md) | Seven engineering findings from building it |
| `kubemend-report.tex` | The technical report. Single self-contained LaTeX source. |
| `kubemend-report.pdf` | Compiled output, 13 pages. |
| `console.jpg` | Screenshot of the console, embedded by the report. |
| `media/demo.gif` | The demo, recorded against a live cluster |
| `media/demo.cast` | The asciinema recording the GIF was made from |

The three markdown documents are the primary reference and are meant to be read
in that order. The PDF covers the same ground in one continuous narrative, for
anyone who would rather have a paper than a folder.

## Using it in Overleaf

1. **New Project → Upload Project**, or drag both files into an existing one:
   - `kubemend-report.tex`
   - `console.jpg` (must sit next to the `.tex`, not in a subfolder)
2. Set `kubemend-report.tex` as the main document.
3. Compiler: **pdfLaTeX** (Menu → Compiler). This is Overleaf's default, so there is usually nothing to change.

The screenshot is optional. If `console.jpg` is missing the document still compiles and prints a one-line placeholder where the figure would be, so a `.tex`-only upload works.

## Compiling locally

```bash
cd docs
tectonic kubemend-report.tex          # what this was built with
# or
latexmk -pdf kubemend-report.tex
```

Builds clean — no errors, no overfull boxes, no missing glyphs.

## Notes on the source

- Times text and math (`mathptmx`), Helvetica headings, Courier for code.
- `newunicodechar` maps the `✓`, `✗`, `→`, `↳` and `·` glyphs that appear in the
  demo transcripts to maths equivalents. This is deliberate rather than
  incidental: `lstlisting`'s own `literate` option does not fire for multi-byte
  characters under pdfLaTeX, so the transcripts would otherwise typeset with
  missing-character warnings and silent gaps.
- The architecture diagram is TikZ, so there is no image dependency for it.

## Keeping the numbers honest

Every figure in the report is taken from the repository rather than from
recollection, and can be re-derived:

```bash
kubemend diagnose --snapshot fixtures/broken-cluster.json   # 13 findings, 4 incidents
pytest -q                                                   # 157 tests
cat demo/transcript.txt                                     # 26s recovered, 76s reverted
```

If any of those change, update the report to match. A report that drifts from
the code is worse than no report, because it reads as authoritative.
