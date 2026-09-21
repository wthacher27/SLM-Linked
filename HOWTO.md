# How to recreate this from scratch

Step-by-step rebuild of the whole pipeline, in order. Each step says what to
run and what success looks like, so you know when to move on.

## 1. Environment

```bash
git clone https://github.com/wthacher27/SLM-Linked.git && cd SLM-Linked
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check: `.venv/bin/python slm.py test` prints `ok`. This runs without any
data or Kaggle setup — it's a self-contained sanity check on the code itself
(tokenizer round-trip, batch masking, a tiny model overfitting one post).

## 2. Kaggle credentials (most of the corpus comes from here)

1. kaggle.com → your profile → Settings → API → "Create New Token" — downloads `kaggle.json`.
2. `mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json`

Skip this and `fetch` still works — it just won't pull `KAGGLE_SOURCES`
(roughly 170k of the ~199k posts), only the smaller HuggingFace/GitHub sources.

## 3. Download the raw sources

```bash
.venv/bin/python slm.py fetch
```

Pulls everything in `SOURCES` (HuggingFace datasets + a couple of raw URLs)
and, if step 2 is done, everything in `KAGGLE_SOURCES` — into `data/raw/`.
Re-running is safe: already-downloaded files are skipped.

Check: `data/raw/` has ~14 files, biggest two are `english-linkedin-posts-v2.csv`
(~103MB) and `influencers_data.csv` (~91MB). If those are missing, `kaggle.json`
isn't set up right — rerun `fetch` and read its `skip <ref>: ...` lines.

## 4. Build the training corpus

```bash
.venv/bin/python slm.py prep
```

Folds every file under `data/raw/` into `data/posts.jsonl`: picks each file's
real post-text column (see `COLS` in `slm.py` for the couple of files where
that can't be guessed by name), normalizes Unicode, strips links, drops
short/long/non-English/duplicate posts, and builds a prompt for each post
(real prompt column → hashtags as a topic → the post's own first line).

Check: prints `wrote 198734 posts, 75.7 MB to data/posts.jsonl` (or close to
it — a source dataset changing upstream will shift this a little).

## 5. Train

```bash
.venv/bin/python slm.py train data/posts.jsonl
```

Trains its own byte-level BPE tokenizer on this corpus first (8k vocab,
saved to `data/tokenizer.json`), then a random-init ~13M-param Llama from
scratch. Cosine LR schedule, early stops after 3 evals (750 iters) with no
validation improvement. Best-validation checkpoint is saved to `ckpt/`,
with `ckpt/tokenizer.json` copied alongside it.

**Before running:** make sure there's actually free RAM. Check with
`vm_stat | awk '/Pages free/{print $3}'` — each page is 16KB, so you want a
few thousand free pages at minimum. On a memory-constrained machine, close
whatever isn't needed (Docker Desktop, extra browser tabs) first; a starved
system pushes training into swap and it slows to a crawl. Training itself
pads each batch to a bucketed width rather than its exact size, so it
shouldn't leak memory over a long run (see README's Notes section).

Check: `iter 0` prints a val loss near `ln(8000) ≈ 8.99` (the model is
correctly at random-guess entropy before it's learned anything); loss drops
sharply over the first ~1000 iterations.

## 6. Generate

```bash
.venv/bin/python slm.py generate "Write a short LinkedIn post about finishing a machine learning bootcamp"
```

Loads `ckpt/` (model + its matching tokenizer) and samples a post.

## Rebuild order if something's out of sync

`fetch` → `prep` → `train` → `generate`, always in that order — each step's
output is the next step's input. If you edit `slm.py`'s cleaning/column logic,
rerun from `prep`. If you add a new source, rerun from `fetch`.

## What's intentionally not in git

`.venv/`, `data/`, `ckpt/` — all rebuilt by the steps above, not committed.
`data/posts.jsonl` alone is ~90MB, well past what belongs in a git repo for
something this reproducible.
