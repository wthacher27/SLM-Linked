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
(real prompt column, half of them as "Write a post about ..." → the post's
own topic words, never its run-together hashtags).

Check: prints `wrote 196296 posts, 74.4 MB to data/posts.jsonl` (or close to
it — a source dataset changing upstream will shift this a little).

## 5. Pretrain on general English

```bash
.venv/bin/python slm.py pretrain 30000
```

On first run, streams ~1GB of FineWeb-Edu (educational web text) into
`data/pretrain.jsonl`. Then trains the byte-level BPE tokenizer (8k vocab,
saved to `data/tokenizer.json`) on the posts plus a tenth of that text, and
trains a random-init ~13M-param Llama on random 512-token windows of it
(~250M tokens, about one pass). Best-validation checkpoint is saved to `pre/`.
About 2-3 hours on a GTX 1660 Ti.

Check: `iter 0` prints a val loss near `ln(8000) ≈ 8.99`, falling to ~5.4 by
iter 500.

If the run gets cut off (closed terminal, PC sleep), rerun it with the
iterations that were left, e.g. `pretrain 25000` after dying at iter 5000: it
sees `pre/` already has a model and continues from those weights and that
tokenizer (iter 0 then prints the val loss it stopped at, not ~8.99). Only the
weights carry over; the optimizer and LR schedule start fresh. Delete `pre/` to
start over from random init.

## 6. Fine-tune on the posts

```bash
.venv/bin/python slm.py train data/posts.jsonl 20000
```

(A third argument picks the output dir, e.g. `train data/posts.jsonl 20000
ckpt-v2`, to keep the current `ckpt/` around to compare against with `eval`.)

Starts from `pre/` and its tokenizer (without `pre/`, it trains a fresh
tokenizer on the posts and starts from random init instead). Cosine LR
schedule, early stops after 6 evals (1500 iters) with no validation
improvement. Best-validation checkpoint is saved to `ckpt/`, with
`ckpt/tokenizer.json` copied alongside it.

**Before running:** make sure there's actually free RAM. Check with
`vm_stat | awk '/Pages free/{print $3}'` — each page is 16KB, so you want a
few thousand free pages at minimum. On a memory-constrained machine, close
whatever isn't needed (Docker Desktop, extra browser tabs) first; a starved
system pushes training into swap and it slows to a crawl. Training itself
pads each batch to a bucketed width rather than its exact size, so it
shouldn't leak memory over a long run (see README's Notes section).

Check: `iter 0` prints a val loss well below `ln(8000) ≈ 8.99` (random-guess
entropy): the pretrained model already knows English before it has seen a
single post. From random init it starts right at ~8.99.

## 7. Generate

```bash
.venv/bin/python slm.py generate "Write a short LinkedIn post about finishing a machine learning bootcamp"
```

Loads `ckpt/` (model + its matching tokenizer) and samples a post.

## 8. Evaluate

```bash
.venv/bin/python slm.py eval ckpt my-change
```

Samples 100 posts (25 fixed prompts x 4) and scores them: on topic, finished,
repeated trigrams, median words (see README's Evaluating section). Takes under
a minute on a GPU.

Check: prints one score line and appends a row to `evals/scores.md`. Compare
it with the `baseline` row there before keeping a change.

## 9. Serve it

```bash
.venv/bin/python app.py
```

Check: http://127.0.0.1:7860 shows a prompt box, and a post comes back in a
few seconds.

To use it away from this machine, put it on a Hugging Face Space:

1. huggingface.co → Settings → Access Tokens → create one with write access.
2. `.venv/bin/hf auth login` and paste it.
3. `.venv/bin/python slm.py deploy <your-username>/slm-linked`

Check: it prints the Space's URL; the page there shows "Building", then the
same prompt box as locally after a few minutes. The Space is private: open it
while logged in, or pass the token from step 1 to the API (README's Querying
remotely section). After retraining, run step 3 again.

## Rebuild order if something's out of sync

`fetch` → `prep` → `pretrain` → `train` → `generate` (or `deploy`), always in that order —
each step's output is the next step's input. If you edit `slm.py`'s
cleaning/column logic, rerun from `prep`. If you add a new source, rerun from
`fetch`. To only retune on the posts, rerun `train`: `pre/` stays valid.

## What's intentionally not in git

`.venv/`, `data/`, `pre/`, `ckpt/` — all rebuilt by the steps above, not committed.
`data/posts.jsonl` alone is ~90MB, well past what belongs in a git repo for
something this reproducible.
