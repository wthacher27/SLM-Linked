# SLM-Linked

A small language model that writes LinkedIn posts from a prompt, trained from scratch.

No pretrained weights: a randomly initialized Llama (~13M params, 6 layers) with its
own byte-level BPE tokenizer (8k vocab, trained on its own data) — it learns
everything, spelling included, from the data you give it. It learns in two stages:
general English first (~1GB of FineWeb-Edu, since ~20M tokens of posts is too
little to learn a language from), then LinkedIn posts.

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
.venv/bin/python slm.py pretrain 30000                   # general English -> pre/ (fits the tokenizer)
.venv/bin/python slm.py train data/posts.jsonl 20000     # fine-tunes pre/ on the posts -> ckpt/
.venv/bin/python slm.py pretrain 73000 pre-30m 2500 30m  # or a 29M model on 2.5GB -> pre-30m/ (~14h)
.venv/bin/python slm.py train data/posts.jsonl 20000 ckpt-30m pre-30m  # ... fine-tuned -> ckpt-30m/ (~6.5h)
.venv/bin/python slm.py generate "Write a short post about finishing a bootcamp"
.venv/bin/python slm.py eval ckpt my-change              # 100 samples, scored -> evals/my-change.md
.venv/bin/python app.py                                  # web page + HTTP API on localhost:7860
.venv/bin/python slm.py deploy you/slm-linked            # the same, on a private Hugging Face Space
.venv/bin/python slm.py test                             # self-check
```

(On Windows: `.venv\Scripts\python.exe` instead of `.venv/bin/python`, and set
`$env:PYTHONUTF8=1` first or printing an emoji crashes `generate`.)

`pretrain` streams ~1GB of FineWeb-Edu into `data/pretrain.jsonl` on its first
run. Without `pre/`, `train` falls back to the old behavior: random init and a
tokenizer fit to the posts alone.

Best-validation checkpoints land in `pre/` and `ckpt/`, each with its matching
`tokenizer.json` copied alongside it (a checkpoint and its tokenizer are useless apart).
`ckpt/` also gets `tags.txt`: the hashtags its training posts use 3+ times,
which the coined-tag check needs (see Validating). For a checkpoint trained
before this existed: `python -c "import slm; slm.save_tags(slm.load_rows('data/posts.jsonl'), 'ckpt')"`.

`data/`, `pre/`, `ckpt/` and `.venv/` are gitignored — rebuild them with the commands
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
prompt/instruction column when the file has one, else the post's topic words
("Write a post about leadership, mentoring, roundtable"): its plain-word
hashtags, then its most distinctive words by tf-idf. Half the real descriptive
prompts get the same "Write a (LinkedIn) post about ..." opening, so the
phrase people actually type leads into real topics. (Hashtag-list prompts
like "about companyculture, wearehiring" taught the model to copy prompt
words into run-together tags; first-line prompts taught it to echo the
prompt.) A few files whose real columns can't be guessed by name are listed
explicitly in `COLS`.

Training data format, one JSON object per line:

```json
{"prompt": "finishing a bootcamp", "post": "Some personal news 🎉\n\nAfter 6 months..."}
```

Current corpus: **~196k posts, 74MB**: about a third bare real prompts, a
third real prompts as "Write a post about ...", a third topic-word prompts. Kaggle's `mozharovartem/english-linkedin-posts`
(plain-English ↔ LinkedIn-speak pairs) is 85% of it, so the corpus currently
skews toward that dataset's machine-generated corporate voice; the influencer
and scraped sources are the more authentic human-voice slice.

Kaggle and HF have both been swept fairly exhaustively for English LinkedIn
*post* text (not job listings, not profile data) — see `SOURCES` /
`KAGGLE_SOURCES` for what survived. Beyond this, more data means a licensed
scraping API (Bright Data, Apify, ...) or personal/team LinkedIn exports.
Do not scrape LinkedIn directly — it breaks their ToS.

## Evaluating

Val loss says how well the model predicts posts, not whether it writes about
what you asked. `slm.py eval [ckpt] [name]` samples 12 posts for each of 25
fixed prompts in `EVAL` (phrased the way people actually ask), with fixed
seeds, so re-running one checkpoint reproduces its samples exactly (~6 min on a
GTX 1660 Ti). Each post is scored on:

- **on topic**: matches that prompt's topic regex (e.g. `laid off|layoff`)
- **finished**: ended on its own instead of running into the context limit
- **echoes prompt**: opens by repeating the prompt
- **coined tags**: has a hashtag built from the prompt's words that the
  training posts (`data/posts.jsonl`) almost never use, like `#finishinginternship`
- **stock phrasing**: uses one of the phrases in `STOCK` ("thrilled to",
  "testament to", "driving innovation", ...)
- **distinct-2**: unique word pairs / all word pairs, across the whole run.
  It's low when every post says the same things
- **repeated trigrams**: share of word trigrams seen earlier in the post (loops)
- **median words**

For reference, 300 random training posts (the same count and length as a run)
score ~9% stock phrasing and ~0.74 distinct-2. The model at temperature 0.7
scores 34% and 0.42: it flattens the corpus into one corporate voice. Sampling
now runs at 0.9 (0.52 distinct-2, 30% stock phrasing, ~4 points less on topic;
see `evals/temp-*.md`). Temperature barely moves the stock phrasing, though:
that gap is the model's, not the sampler's.

All samples go to `evals/<name>.md`; one score row per run is appended to
`evals/scores.md`, the table to compare changes against. When the set of
metrics changes, a new table starts below the old one. With 300 samples per
run, on-topic gaps under ~6 points are sampling noise. (Runs before
`baseline-n12` had 100 samples, where the threshold was ~10 points.)

## Validating

`generate` checks each of its samples against `requirements()` with
[Mellea](https://pypi.org/project/mellea/) before reranking: it finished on its
own, doesn't paste the prompt in (at the start or mid-sentence, "announce that
Just got promoted..."), doesn't loop, is at least 100 characters, and coins no
hashtags from the prompt's words (any tag outside the checkpoint's `tags.txt`
that's built from them, like `#finishinginternship`). These are plain Python
checks; no judge model is involved. The tag check took **coined tags** from 8%
to 0% with on-topic unchanged (`evals/coined-req.md` vs `baseline-n12.md`). The PMI rerank then chooses
among the samples that pass (or among all of them, if none do). Filtering first
matters, because pasting the prompt in raises the PMI score. It took **echoes
prompt** from 5% to 0% without moving on-topic (`evals/mellea.md` vs
`prompts-v2.md`). Exact pastes are all it catches: near-pastes like "Share a
Leadership lesson" still get through.

## Querying remotely

`app.py` wraps `generate` in a Gradio page with an HTTP API (one endpoint,
`/write`: a prompt and an optional `best_of`, returns the post). `slm.py
deploy you/slm-linked` uploads it, `slm.py` and `ckpt/` to a Hugging Face Space
in one commit: free CPU hardware, created private, so only your account can
use it. First make a token with write access (huggingface.co → Settings →
Access Tokens) and run `.venv/bin/hf auth login`. Re-run `deploy` after
retraining to ship the new `ckpt/`; the Space rebuilds in a few minutes.

Then, from anywhere:

- **browser**: `https://huggingface.co/spaces/you/slm-linked`, logged in to
  Hugging Face (works on a phone)
- **Python** (`pip install gradio_client`):
  ```python
  from gradio_client import Client
  post = Client('you/slm-linked', token='hf_...').predict(prompt='Just got promoted', api_name='/write')
  ```
- **curl**: one call to start, one to read the result:
  ```bash
  URL=https://you-slm-linked.hf.space/gradio_api/call
  ID=$(curl -s -H "Authorization: Bearer $HF_TOKEN" -H 'Content-Type: application/json' \
       -d '{"prompt": "Just got promoted"}' $URL/v2/write | sed 's/.*"event_id": *"\([^"]*\)".*/\1/')
  curl -sN -H "Authorization: Bearer $HF_TOKEN" $URL/write/$ID
  ```

A post takes 1-3 s on two CPU threads at `best_of=16` (my desktop; the
Space's CPUs may be slower). A free Space sleeps after 48 hours without use;
opening its page wakes it, which takes a minute or so.

## Notes

- `generate` samples 16 posts and returns the one the prompt explains best:
  highest log p(post | prompt) − log p(post | "Write a LinkedIn post"), so a
  fluent post that ignores the prompt scores ~0. On eval this took on topic
  from 57% (one sample) to 90%. Pass `best_of=1` to `generate()` for a single
  raw sample.

- 76MB of data is enough for the model to produce real English; below ~20MB
  expect LinkedIn-flavored word salad instead of coherent posts.
- Training pads each batch to a bucketed width (64/128/256/384/512 tokens),
  not to a fixed max — padding every batch to its own exact width thrashes
  MPS's memory allocator over a long run (fills swap, brings the run to a
  crawl). Bucketing keeps the shape count small enough to cache.
- Early stopping: training halts after 6 evals (1500 iters) with no
  validation improvement.
