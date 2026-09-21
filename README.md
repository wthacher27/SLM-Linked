# SLM-Linked

A small language model that writes LinkedIn posts from a prompt, trained from scratch.

No pretrained weights: a randomly initialized Llama (~13M params, 6 layers) with its
own byte-level BPE tokenizer (8k vocab, trained on its own posts) — it learns
everything, spelling included, from the data you give it.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

For the Kaggle-sourced data (most of the corpus): kaggle.com → Settings → API →
Create New Token, then `mkdir -p ~/.kaggle && mv ~/Downloads/kaggle.json ~/.kaggle/
&& chmod 600 ~/.kaggle/kaggle.json`. Without it, `fetch` still pulls the smaller
HuggingFace/GitHub sources.

## Use

```bash
.venv/bin/python slm.py fetch                           # download sources -> data/raw/
.venv/bin/python slm.py prep                             # data/raw/* -> data/posts.jsonl
.venv/bin/python slm.py train data/posts.jsonl 5000      # trains its own tokenizer first
.venv/bin/python slm.py generate "Write a short post about finishing a bootcamp"
.venv/bin/python slm.py test                             # self-check
```

Best-validation checkpoint lands in `ckpt/`, with its matching `tokenizer.json`
copied alongside it (a checkpoint and its tokenizer are useless apart).

`data/`, `ckpt/` and `.venv/` are gitignored — rebuild them with the commands
above rather than committing them; `data/posts.jsonl` alone is ~90MB.

## Data

`slm.py fetch` downloads every dataset in `SOURCES` (HuggingFace + a couple of raw
URLs) and `KAGGLE_SOURCES` (needs the token above) into `data/raw/`.

`slm.py prep` then folds every `.csv`/`.json`/`.jsonl`/`.tsv`/`.parquet` under
`data/raw/` into one `data/posts.jsonl`. It picks each file's post-text column
automatically (preferring a post-shaped name, falling back to the longest
average string; skips JSON/URL columns), NFKC-normalizes fancy Unicode,
strips links and "…see more", drops posts under 100 or over 6000 characters,
drops non-English and near-duplicate posts. For the prompt it uses a real
prompt/instruction column when the file has one, else the post's hashtags as
a topic ("Write a LinkedIn post about ai, hiring"), else the post's own hook.
A few files whose real columns can't be guessed by name are listed explicitly
in `COLS`.

Training data format, one JSON object per line:

```json
{"prompt": "finishing a bootcamp", "post": "Some personal news 🎉\n\nAfter 6 months..."}
```

Current corpus: **~199k posts, 76MB**, roughly half topic-style prompts and
half real/hook prompts. Kaggle's `mozharovartem/english-linkedin-posts`
(plain-English ↔ LinkedIn-speak pairs) is 85% of it, so the corpus currently
skews toward that dataset's machine-generated corporate voice; the influencer
and scraped sources are the more authentic human-voice slice.

Kaggle and HF have both been swept fairly exhaustively for English LinkedIn
*post* text (not job listings, not profile data) — see `SOURCES` /
`KAGGLE_SOURCES` for what survived. Beyond this, more data means a licensed
scraping API (Bright Data, Apify, ...) or personal/team LinkedIn exports.
Do not scrape LinkedIn directly — it breaks their ToS.

## Notes

- 76MB of data is enough for the model to produce real English; below ~20MB
  expect LinkedIn-flavored word salad instead of coherent posts.
- Training pads each batch to a bucketed width (64/128/256/384/512 tokens),
  not to a fixed max — padding every batch to its own exact width thrashes
  MPS's memory allocator over a long run (fills swap, brings the run to a
  crawl). Bucketing keeps the shape count small enough to cache.
- Early stopping: training halts after 3 evals (750 iters) with no
  validation improvement.
