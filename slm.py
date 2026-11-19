"""Small language model for LinkedIn posts, trained from scratch (random init, byte-level, no pretrained weights).

  python slm.py fetch                            # download the curated HF sources -> data/raw/
  python slm.py prep                             # data/raw/* -> data/posts.jsonl
  python slm.py pretrain [iters] [out] [mb] [size]  # general English first (streams mb MB of FineWeb-Edu) -> out/ (pre/)
  python slm.py train data/posts.jsonl [iters] [out] [init]  # lines of {"prompt": "...", "post": "..."}; init/ (pre/) -> out/ (ckpt/)
  python slm.py generate "Just got promoted to Senior Engineer"
  python slm.py eval [ckpt] [name]               # fixed prompts -> evals/<name>.md, scores -> evals/scores.md
  python slm.py deploy user/space [ckpt]         # app.py + the model -> a private Hugging Face Space
  python slm.py test                             # self-check
"""
import collections, datetime, itertools, json, math, pathlib, random, re, shutil, statistics, subprocess, sys, unicodedata
import torch
from transformers import LlamaConfig, LlamaForCausalLM, get_cosine_schedule_with_warmup

END, PROMPT, POST = 0, 1, 2  # special token ids, fixed first in the vocab
VOCAB, TOK_PATH = 8000, 'data/tokenizer.json'
BLOCK = 512  # BPE tokens ~ 2000 chars: nearly every post fits whole
CFG = dict(vocab_size=VOCAB, hidden_size=384, intermediate_size=1024, num_hidden_layers=6,
           num_attention_heads=6, max_position_embeddings=BLOCK, tie_word_embeddings=True,
           bos_token_id=PROMPT, eos_token_id=END, pad_token_id=END,
           attention_dropout=0.1)  # ~13M params; dropout won the size sweep
# merged over CFG. That sweep trained on ~20M tokens of posts alone; pretraining on ~600M tokens of English
# (20 per param) can feed a bigger model, which the coherence errors at 13m ("accepted a bootcamp") need
SIZES = {'13m': {}, '30m': dict(hidden_size=512, intermediate_size=1376, num_hidden_layers=8, num_attention_heads=8)}
DEV = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
# ~20M tokens of posts is too little to learn English from, so pretrain on general text first.
# Still from scratch: this is training data, not pretrained weights.
PRETRAIN = ('HuggingFaceFW/fineweb-edu', 'sample-10BT')
_tok = None
_tags = None  # the hashtags the loaded checkpoint's training posts use: see load() and coined()


def tokenizer(path=None):
    """Our own byte-level BPE, trained on our own posts -- nothing pretrained."""
    global _tok
    if _tok is None:
        from tokenizers import Tokenizer
        ck = pathlib.Path('ckpt/tokenizer.json')          # a checkpoint carries its own tokenizer
        _tok = Tokenizer.from_file(path or (str(ck) if ck.exists() else TOK_PATH))
    return _tok


def load(ckpt='ckpt'):
    """The model in ckpt/, switched to the tokenizer saved with it: a checkpoint and its tokenizer must match.
    Its tags.txt (see save_tags), if it has one, becomes _tags."""
    global _tok, _tags
    _tok = None
    tokenizer(f'{ckpt}/tokenizer.json')
    tags = pathlib.Path(ckpt, 'tags.txt')
    _tags = set(tags.read_text(encoding='utf-8').split()) if tags.exists() else None
    return LlamaForCausalLM.from_pretrained(ckpt).to(DEV).eval()


def known_tags(rows):
    """Hashtags used 3+ times in rows' posts, lowercased: the ones people actually use."""
    n = collections.Counter(t.lower() for r in rows for t in re.findall(r'#(\w+)', r['post']))
    return {t for t, k in n.items() if k >= 3}


def save_tags(rows, out):
    """known_tags(rows) -> out/tags.txt: a checkpoint carries the tags it learned from, as it does its tokenizer."""
    pathlib.Path(out, 'tags.txt').write_text('\n'.join(sorted(known_tags(rows))), encoding='utf-8')


def coined(prompt, post, tags):
    """Has a hashtag built from the prompt's words (#finishinginternship) that isn't in tags, the ones people use."""
    words = set(re.findall(r'[a-z]{5,}', prompt.lower())) - {'write', 'linkedin', 'about'}
    return any(t.lower() not in tags and any(w in t.lower() for w in words) for t in re.findall(r'#(\w+)', post))


def train_tokenizer(rows, path=TOK_PATH, vocab=VOCAB):
    from tokenizers import ByteLevelBPETokenizer
    global _tok
    t = ByteLevelBPETokenizer()
    t.train_from_iterator((s for r in rows for s in (r['post'], r.get('prompt', ''))),
                          vocab_size=vocab, min_frequency=2,
                          special_tokens=['<|end|>', '<|prompt|>', '<|post|>'])  # ids 0,1,2
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    t.save(path)
    _tok = None
    print(f'tokenizer: {t.get_vocab_size()} tokens -> {path}')
    return t.get_vocab_size()


def encode(prompt, post=None):
    t = tokenizer()
    ids = [PROMPT] + t.encode(prompt).ids + [POST]
    return ids + t.encode(post).ids + [END] if post is not None else ids


def decode(ids):
    return tokenizer().decode([i for i in ids if i > POST])


# byte models repeat; the penalty and top_p matter more than temperature here. On on-topic alone 0.7 won
# (beat 0.9 and 1.1 at every BEST_OF tried), but it flattens the voice: against it, 0.9 trades ~4 points of
# on topic (91% -> 87%, inside the noise) for distinct-2 0.42 -> 0.52 and stock phrasing 33% -> 30%.
# 1.0 gets to 0.55 / 28% at 85% on topic (evals/temp-*.md). Re-sweep for each model size.
SAMPLE = dict(do_sample=True, temperature=0.9, top_p=0.9, repetition_penalty=1.1)
BEST_OF = 16  # on topic, temperature 0.9: 1 sample 62%, best of 4 73%, 8 77%, 16 86%
GENERIC = 'Write a LinkedIn post'  # the no-particular-topic prompt pick() measures against


def complete(model, prompt, max_new=BLOCK, n=1, **kw):
    """n sampled continuations of prompt as token ids; one the model finished on its own ends in END."""
    ids = torch.tensor([encode(prompt)], device=model.device)
    max_new = min(max_new, BLOCK - ids.shape[1])  # no positions past the context window
    out = model.generate(ids, max_new_tokens=max_new, eos_token_id=END, pad_token_id=END,
                         num_return_sequences=n, **(kw or SAMPLE))
    return out[:, ids.shape[1]:].tolist()


def logp(model, prompt, cands):
    """Summed log-prob of each candidate (token ids) as the continuation of prompt."""
    pre = encode(prompt)
    cands = [c[:BLOCK - len(pre)] for c in cands]
    x = torch.full((len(cands), len(pre) + max(map(len, cands))), END)
    keep = torch.zeros(x.shape, dtype=torch.bool)
    for i, c in enumerate(cands):
        x[i, :len(pre) + len(c)] = torch.tensor(pre + c)
        keep[i, len(pre):len(pre) + len(c)] = True
    x, keep = x.to(model.device), keep.to(model.device)
    with torch.no_grad():
        lp = model(input_ids=x).logits[:, :-1].log_softmax(-1).gather(-1, x[:, 1:, None])[..., 0]
    return (lp * keep[:, 1:]).sum(1)


LOOP = 0.2  # repeats() share that counts as a loop: the worst of 1600 sampled posts was 0.09


def requirements(prompt, done):
    """What a post must pass before pick() will choose it, as mellea requirements. All plain Python checks,
    no judge model. done: whether the model ended the post itself, which the text alone can't show."""
    from mellea import req, simple_validate
    must = lambda why, fn: req(why, validation_fn=simple_validate(fn))
    head = prompt[:25]
    return [must('Ends on its own, not cut off at the length limit', lambda p: done),
            # opening with the prompt is eval's echo; the paste also lands mid-sentence ("announce that Just got ...")
            must('Does not paste the prompt in', lambda p: not head or not (p.lower().startswith(head.lower()) or head in p)),
            must('Does not loop', lambda p: repeats(p) < LOOP),
            must('At least 100 characters, the shortest post prep keeps', lambda p: len(p) >= 100)] + (
        [must("Its hashtags are ones people use, not coined from the prompt's words", lambda p: not coined(prompt, p, _tags))]
        if _tags is not None else [])  # a checkpoint from before tags.txt can't tell


def validate(prompt, ids):
    """mellea's ValidationResult for each of requirements() on the post ids; it passes if all() of them do."""
    from mellea import ModelOutputThunk, SimpleContext, mfuncs
    return mfuncs.validate(requirements(prompt, END in ids), SimpleContext(), None,  # no backend: no judge to call
                           output=ModelOutputThunk(decode(ids)))


def pick(model, prompt, cands):
    """Of the candidates that pass validate() (all of them, if none do), the one the prompt explains best:
    log p(post | prompt) - log p(post | GENERIC). A fluent but off-topic post is about as likely under either
    and scores ~0. Plain log p(post | prompt) is worse than not reranking at all (picks short bland posts),
    and counting prompt words in the post games eval ("about leadership, leadership, and leadership").
    Filtering first matters: pasting the prompt in raises log p(post | prompt), so PMI favors echoes."""
    cands = [c[:c.index(END) + 1] if END in c else c for c in cands]  # drop the padding after END
    cands = [c for c in cands if all(validate(prompt, c))] or cands
    return cands[(logp(model, prompt, cands) - logp(model, GENERIC, cands)).argmax()]


def generate(model, prompt, max_new=BLOCK, best_of=BEST_OF, **kw):
    """The best (see pick) of best_of sampled posts."""
    return decode(pick(model, prompt, complete(model, prompt, max_new, n=best_of, **kw)))


EVAL = [  # (prompt, regex an on-topic post matches): phrased the way people ask, not the way prep's prompts read
    ('Write a LinkedIn post about finishing a machine learning bootcamp', r'bootcamp|machine learning'),
    ('Just got promoted to Senior Engineer', r'promot|senior engineer'),
    ('Announce that I am starting a new job at a startup', r'startup|new (role|job|position|chapter)'),
    ('Write a LinkedIn post about being laid off', r'laid off|layoff'),
    ('We are hiring a data scientist, write the post', r'hiring|data scien'),
    ('Share a leadership lesson I learned from a mistake', r'mistake|fail'),
    ('Announce the launch of our new budgeting app', r'budget|launch'),
    ('Write a recap of my talk at a tech conference', r'conference|talk|spoke|speaking|keynote'),
    ('Celebrating 5 years at my company', r'anniversary|(\d+|five)(st|nd|rd|th)? years?\b'),
    ('Write a LinkedIn post about getting my AWS certification', r'aws|certif'),
    ('Thank my team for shipping a big release', r'release|ship|launch'),
    ('Write a post about finishing my summer internship', r'internship|interns?\b'),
    ('Write a LinkedIn post about graduating with a computer science degree', r'graduat|degree|computer science'),
    ('Our startup just closed a seed round', r'seed|funding|raised|investor'),
    ('Write a LinkedIn post about remote work', r'remote|work from home|hybrid'),
    ('Write a post about how AI is changing my job as a designer', r'ai\b|artificial intelligence'),
    ('Write a thank-you post for my mentor', r'mentor'),
    ('Write about switching careers from teaching to software engineering', r'teach|career (change|switch|pivot)'),
    ('Write a LinkedIn post about dealing with burnout', r'burn.?out|exhaust|mental health'),
    ('Share tips for public speaking', r'speak|presentation|audience'),
    ('Announce that we open-sourced our internal tool', r'open.?sourc|github'),
    ('Write a post about volunteering at a food bank', r'volunteer|food'),
    ('Recommend a book that changed how I think about leadership', r'book|author'),
    ('Write a customer success story about a client who doubled their revenue', r'client|customer|revenue'),
    ('Write my farewell post, I am leaving the company after 10 years', r'farewell|goodbye|leaving|last day|moving on'),
]


def repeats(text):
    """Share of word trigrams that already appeared earlier in text: ~0 for a sane post, high for a loop."""
    w = re.findall(r'\w+', text.lower())
    tri = list(zip(w, w[1:], w[2:]))
    return 1 - len(set(tri)) / len(tri) if tri else 0.0


def bigrams(text):
    w = re.findall(r'\w+', text.lower())
    return list(zip(w, w[1:]))


# the phrases the model leans on: in 34% of its eval posts (baseline-n12), ~9% of the training posts
STOCK = ('thrilled to', 'excited to', 'incredibly proud', 'testament to', 'driving innovation')


def judge(prompt, topic, post, tags):
    """eval's checks on one post; tags: the hashtags people use (see known_tags)."""
    return dict(topic=bool(re.search(rf'\b({topic})', post, re.I)),
                echo=post.lower().startswith(prompt.lower()[:25]),  # opens by repeating the prompt
                coined=coined(prompt, post, tags),
                stock=any(s in post.lower() for s in STOCK),
                rep=repeats(post), bi=bigrams(post), words=len(post.split()))


def summarize(rows):
    share = lambda k: f'{sum(r[k] for r in rows) / len(rows):.0%}'
    bi = [b for r in rows for b in r['bi']]
    return {'on topic': share('topic'), 'finished': share('done'), 'echoes prompt': share('echo'),
            'coined tags': share('coined'), 'stock phrasing': share('stock'),
            'distinct-2': f'{len(set(bi)) / len(bi):.2f}',  # across the run: low when every post says the same things
            'repeated trigrams': f"{statistics.mean(r['rep'] for r in rows):.1%}",
            'median words': f"{statistics.median(r['words'] for r in rows):.0f}"}


def evaluate(ckpt='ckpt', name=None, n=12, best_of=BEST_OF):
    """n posts per EVAL prompt, each picked from best_of samples the way generate() does, with fixed seeds;
    scored on staying on topic, finishing on their own, not looping and how stock the phrasing is. Val loss
    can't see any of that. Posts -> evals/<name>.md, one score row -> evals/scores.md."""
    model = load(ckpt)  # with its own tokenizer: ckpt-scratch/ speaks a different one
    name =name or f'{pathlib.Path(ckpt).name}-{datetime.date.today()}'
    tags = _tags if _tags is not None else known_tags(load_rows('data/posts.jsonl'))  # a checkpoint without tags.txt
    rows, body = [], []
    for i, (prompt, topic) in enumerate(EVAL):
        torch.manual_seed(i)  # per prompt: re-running one checkpoint reproduces its samples exactly
        body.append(f'## {i + 1}. {prompt}\n')
        for j in range(n):  # one best_of batch at a time: n * best_of sequences at once is GBs of KV cache
            ids = pick(model, prompt, complete(model, prompt, n=best_of))
            post = decode(ids)
            r = judge(prompt, topic, post, tags) | dict(done=ids[-1] == END)
            rows.append(r)
            body.append(', '.join(['on topic' if r['topic'] else '**off topic**'] + ['**cut off**'] * (not r['done'])
                                  + ['**echoes prompt**'] * r['echo'] + ['**coined tag**'] * r['coined'])
                        + f'\n```text\n{post}\n```\n')
    score = summarize(rows)
    row = lambda cells: '| ' + ' | '.join(cells) + ' |\n'
    d = pathlib.Path('evals')
    d.mkdir(exist_ok=True)
    (d / f'{name}.md').write_text(
        f'# {name}\n\n`{ckpt}/` on {DEV}: {len(EVAL)} prompts x {n} posts, each the best of {best_of}, '
        f'seeds 0-{len(EVAL) - 1}, sampling `{SAMPLE}`\n\n' + row(score) + row(['---'] * len(score)) + row(score.values()) + '\n'
        + '\n'.join(body), encoding='utf-8')
    log = d / 'scores.md'  # with 300 posts a run, gaps under ~6 points are sampling noise (~10 at the old 100)
    old = log.read_text(encoding='utf-8') if log.exists() else ''
    head = row(['run', *score]) + row(['---'] * (len(score) + 1))
    with open(log, 'a', encoding='utf-8') as fh:  # a changed set of metrics starts a new table below the old one
        fh.write(('' if head in old else '\n' * bool(old) + head) + row([f'[{name}]({name}.md)', *score.values()]))
    print(f'{name}: ' + ', '.join(f'{k} {v}' for k, v in score.items()) + f' -> {d / name}.md')
    return score


def deploy(space, ckpt='ckpt'):
    """Upload app.py, this file and the model in ckpt/ to the Hugging Face Space `space` (user/name) in one
    commit, creating it private if it is new; the Space then rebuilds and serves app.py. Needs `hf auth login`."""
    from huggingface_hub import CommitOperationAdd, HfApi
    api = HfApi()
    api.create_repo(space, repo_type='space', space_sdk='gradio', private=True, exist_ok=True)
    files = {'app.py': 'app.py', 'slm.py': 'slm.py', 'README.md': 'space/README.md',  # the Space's own card
             'requirements.txt': 'space/requirements.txt'} | {f'ckpt/{p.name}': str(p) for p in pathlib.Path(ckpt).iterdir()}
    api.create_commit(space, [CommitOperationAdd(k, v) for k, v in files.items()], repo_type='space',
                      commit_message=f'deploy {ckpt}/')
    print(f'https://huggingface.co/spaces/{space} (building: a minute or two)')


SOURCES = [  # HF datasets that really do hold English LinkedIn post text (surveyed 2026-09)
    'sanjayram-a/linkedin_posts',                    # 2560 templated certificate/internship posts
    'BrianClone/linkedin_posts',                     # 1035 real posts w/ engagement metrics
    'ShayanShamsi/prompt_to_linkedin_post',          # 447 prompt -> post pairs
    'ro-anderson/linkedin-top-voices-market-alpaca',  # 300 LinkedIn Top Voices posts
    'Ayushfj/LinkedInPostDataset',                   # 190 real posts
    'NeuML/neuml-linkedin-202501',                   # 258 company posts
    'logiover/linkedin-top-content-scraper-sample-data',  # 20 high-quality scraped posts
    # raw URLs work too:
    'https://raw.githubusercontent.com/harsh-jos/gemma-SFT-linkedin/main/data/final_dataset.jsonl',  # ~150 posts
]

KAGGLE_SOURCES = [  # needs ~/.kaggle/kaggle.json (kaggle.com -> Settings -> API); this is most of the corpus
    'shreyasajal/linkedin-influencers-data',           # ~24k real influencer posts, 91MB
    'mozharovartem/english-linkedin-posts',            # ~170k english<->linkedin-speak pairs, 82MB
    'moeinaminifard/1600-posts-on-linkedin',
    'themeghnasahu/linkedin-company-posts',
    'logiover/linkedin-top-content-scraper-data',
    'sreevaatsavbavana/layoffs-linkedin-posts',
]


def fetch(dst='data/raw'):
    """Pull SOURCES + KAGGLE_SOURCES into data/raw/; prep turns them into training data."""
    from datasets import load_dataset
    pathlib.Path(dst).mkdir(parents=True, exist_ok=True)
    for name in SOURCES:
        f = pathlib.Path(dst, re.sub(r'\W+', '_', name.split('://')[-1])[:80] + '.jsonl')
        if f.exists():
            print(f'have {f.name}'); continue
        try:
            if name.startswith('http'):
                import requests
                f.write_bytes(requests.get(name, timeout=60).content)
                print(f'{f.name}: {f.stat().st_size / 1e3:.0f} KB')
                continue
            df = load_dataset(name)['train'].to_pandas()
            df.to_json(f, orient='records', lines=True)
            print(f'{f.name}: {len(df)} rows')
        except Exception as e:
            print(f'skip {name}: {type(e).__name__} {e}')

    if not pathlib.Path('~/.kaggle/kaggle.json').expanduser().exists():
        print('no ~/.kaggle/kaggle.json: skipping KAGGLE_SOURCES (kaggle.com -> Settings -> API -> Create New Token)')
        return
    for ref in KAGGLE_SOURCES:
        marker = pathlib.Path(dst, '.kg_' + ref.replace('/', '_'))
        if marker.exists():
            print(f'have {ref}'); continue
        kaggle_bin = pathlib.Path(sys.executable).with_name('kaggle')  # not 'kaggle': PATH may not have .venv/bin
        r = subprocess.run([str(kaggle_bin) if kaggle_bin.exists() else 'kaggle',
                            'datasets', 'download', '-d', ref, '-p', dst, '--unzip', '-q'],
                           capture_output=True, text=True, timeout=900)
        if r.returncode == 0:
            marker.touch()
            print(f'{ref}: downloaded')
        else:
            print(f'skip {ref}: {(r.stderr or r.stdout).strip()[:150]}')


def clean(t):
    t = unicodedata.normalize('NFKC', str(t))                 # 𝗳𝗮𝗻𝗰𝘆 LinkedIn text -> plain ascii
    t = re.sub(r'https?://\S+|www\.\S+', '', t)               # links teach nothing
    t = re.sub(r'\s*[…\.]{3}\s*see more\s*$', '', t, flags=re.I)
    t = re.sub(r'[​-‏﻿]', '', t)   # zero-width junk from scrapers
    t = re.sub(r'(?<=[a-z])([.!?])(?=[A-Z])', r'\1 ', t)      # scraper joins: "Mode.I tried" -> "Mode. I tried"
    t = re.sub(r'\n{3,}', '\n\n', t.replace('\r\n', '\n'))
    return re.sub(r'[ \t]+', ' ', t).strip()


def english(t):
    letters = [c for c in t if c.isalpha()]
    return len(letters) > 20 and sum(c.isascii() for c in letters) / len(letters) > 0.9


COLS = {  # files whose real (post, prompt) columns the name heuristic cannot guess
    'english-linkedin-posts-v2.csv': ('linkedin', 'english'),  # plain english <-> linkedin-speak pairs
}


ABOUT = ('Write a LinkedIn post about ', 'Write a post about ')
# its 'linkedin' column is LLM-rewritten corporate-speak (the real post is the truncated 'preview'), and it is
# 86% of the corpus: the model's stock phrasing (30% of posts vs 9% of the human ones) is its voice
MACHINE = {'english-linkedin-posts-v2.csv'}


def keywords(posts, k=3):
    """Each post's k topic words: its hashtags that are plain words (#leadership, not #companyculture),
    then its most distinctive body words by tf-idf across posts."""
    words = [re.findall(r"(?<![#@\w'’])[a-z]{4,}(?![\w'’])", p.lower()) for p in posts]  # no tags, no "couldn’t"
    df = collections.Counter(w for ws in words for w in set(ws))
    rare = max(2, len(posts) // 10000)  # typos and one-off names are distinctive, but no topic
    out = []
    for p, ws in zip(posts, words):
        tags = [t for t in dict.fromkeys(re.findall(r'#([a-z]+)\b', p.lower())) if df[t] >= rare]
        tf = collections.Counter(w for w in ws if df[w] >= rare and w not in tags)
        out.append((tags + sorted(tf, key=lambda w: -tf[w] * math.log(len(posts) / df[w])))[:k])
    return out


def related(prompt, post):
    """True if the prompt shares a content word (5-char prefix match) with the post."""
    stem = lambda t: {w[:5] for w in re.findall(r'[a-z]{4,}', t.lower())}
    return bool(stem(prompt) & stem(post))


def prep(src='data/raw', out='data/posts.jsonl', min_chars=100, max_chars=6000, max_per_prompt=5, machine_share=None):
    """Fold every csv/json/jsonl/parquet under src into one deduped posts.jsonl. machine_share: downsample
    the MACHINE files' posts to at most that share of it (None keeps them all)."""
    import pandas as pd
    per_prompt = collections.Counter()
    reader = {'.csv': pd.read_csv, '.tsv': lambda p: pd.read_csv(p, sep='\t'),
              '.json': pd.read_json, '.jsonl': lambda p: pd.read_json(p, lines=True),
              '.parquet': pd.read_parquet}
    seen, kept = set(), []
    for f in sorted(p for p in pathlib.Path(src).rglob('*') if p.suffix.lower() in reader):
        try:
            df = reader[f.suffix.lower()](f)
        except Exception as e:
            print(f'skip {f.name}: {type(e).__name__} {e}'); continue
        # the post column is the free-text one: longest average string, but skip JSON/URL
        # columns and prefer a post-shaped name -- a profile 'about' blurb is longer than the post
        text = {}
        for c in df.columns:
            if pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_datetime64_any_dtype(df[c]):
                continue
            s = df[c].dropna().astype(str)
            if not len(s) or s.head(200).str.match(r'\s*[\[{]|https?://').mean() > 0.5:
                continue
            text[c] = s.str.len().mean()
        named = {c: v for c, v in text.items() if re.search(r'post|content|text|body|message|caption', c, re.I)}
        col = max(named or text, key=(named or text).get) if text else None
        if f.name in COLS:
            col = COLS[f.name][0]
        if not col or text.get(col, 0) < min_chars:
            print(f'skip {f.name}: no free-text column (best {col!r} avg {text.get(col, 0):.0f} chars)'); continue
        # use a real prompt column when the file has one, else the post's own hook
        cand = {c: v for c, v in text.items()
                if c != col and v < text[col] and re.search(r'prompt|instruction|input|topic', c, re.I)}
        pcol = COLS[f.name][1] if f.name in COLS else (max(cand, key=cand.get) if cand else None)
        dupes = collections.Counter(df[pcol].astype(str)) if pcol else {}
        n = 0
        for raw, praw in zip(df[col], df[pcol] if pcol else df[col]):
            post = clean(raw)
            norm = re.sub(r'\W+', '', post.lower())
            key, tail = norm[:300], norm[-120:]                   # same opening or same ending = near-duplicate
            if min_chars <= len(post) <= max_chars and english(post) and key not in seen and tail not in seen:
                p = clean(praw) if pcol else ''
                # boilerplate, JSON config blobs and "unknown" placeholders teach nothing, and a prompt sharing
                # no content word with its post is a mismatched pair: these get a keyword prompt below instead
                bad = not p or p.startswith('{') or 'unknown' in p.lower() or dupes[str(praw)] > 3 or not related(p, post)
                # one account's templated posts (same hashtags, else same first line) must not dominate
                cap = (', '.join(t.lower() for t in re.findall(r'#(\w+)', post)[:3]) or post.split('\n')[0][:80]
                       if bad else p[:120])
                if per_prompt[cap] >= max_per_prompt:
                    continue
                per_prompt[cap] += 1
                seen.update((key, tail))
                kept.append({'prompt': None if bad else p[:300], 'post': post, 'machine': f.name in MACHINE})
                n += 1
        print(f'{f.name}: {n} posts from {col!r}' + (f', prompts from {pcol!r}' if pcol else ''))
    if machine_share is not None:
        mach = [i for i, r in enumerate(kept) if r['machine']]
        k = int((len(kept) - len(mach)) * machine_share / (1 - machine_share))
        drop = set(mach) - set(random.Random(0).sample(mach, min(k, len(mach))))
        kept = [r for i, r in enumerate(kept) if i not in drop]
        print(f'machine_share {machine_share}: kept {len(mach) - len(drop)} of {len(mach)} {"/".join(MACHINE)} posts')
    for r in kept:
        del r['machine']
    # Prompts in the shape people type them. A post without a real prompt gets its most distinctive words:
    # its hashtags used to fill in, and taught the model that what follows "Write a LinkedIn post about" is a
    # run-together tag list to copy (#finishinginternship); its first line taught it to echo the prompt.
    # Half the real descriptive prompts get the same "Write a post about" opening, so it leads into real topics.
    rng = random.Random(0)
    for r, kw in zip(kept, keywords([r['post'] for r in kept])):
        p = r['prompt']
        if p is None:
            p = rng.choice(ABOUT) + ', '.join(kw) if kw else r['post'].split('\n')[0][:80]
        elif not re.match(r'(write|generate|create|draft|craft|compose)\b', p, re.I) and rng.random() < 0.5:
            # "The author discusses X" -> "Write a post about X"
            p = rng.choice(ABOUT) + re.sub(r'^The (author|user|writer|poster) \w{4,}s (on |about |that )?', '', p).rstrip('.')
        r['prompt'] = p
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, 'w') as fh:
        for r in kept:
            fh.write(json.dumps(r) + '\n')
    print(f'wrote {len(kept)} posts, {sum(len(r["post"].encode()) for r in kept) / 1e6:.1f} MB to {out}')


def load_rows(path):
    """Local .jsonl of {"prompt", "post"}, or a HuggingFace dataset name."""
    if path.endswith('.jsonl'):
        return [json.loads(l) for l in open(path) if l.strip()]
    from datasets import load_dataset
    d = load_dataset(path)['train']
    return [{'prompt': r['input'], 'post': r['output']} for r in d]


def batch_of(exs, batch):
    """One padded batch; labels are -100 everywhere but the post bytes, so the model is
    scored on writing the post, not on echoing the prompt."""
    picks = [exs[k] for k in torch.randint(len(exs), (batch,)).tolist()]
    need = max(len(ids) for ids, _ in picks)
    # bucket to a handful of fixed widths (not the exact max): a fresh shape almost every step
    # starves MPS's allocator of any chance to reuse buffers, which fills swap over a long run
    width = next(b for b in (64, 128, 256, 384, BLOCK) if b >= need)
    x = torch.full((batch, width), END)
    y = torch.full((batch, width), -100)
    for row, (ids, plen) in enumerate(picks):
        x[row, :len(ids)] = torch.tensor(ids)
        y[row, plen:len(ids)] = torch.tensor(ids[plen:])
    return x.to(DEV), y.to(DEV)


def train(path, iters=5000, cfg=CFG, batch=16, lr=3e-4, out='ckpt', init='pre'):
    """Fine-tune the pretrained model in init/ (see pretrain) on prompt -> post pairs. Without one,
    start from random init with a fresh tokenizer fit to these posts alone."""
    rows = load_rows(path)
    random.Random(0).shuffle(rows)  # else the val split is whichever source landed last
    pre = init and pathlib.Path(init, 'config.json').exists()
    if pre:
        tok = f'{init}/tokenizer.json'  # the pretrained model only speaks the tokenizer it learned with
    else:
        cfg = cfg | dict(vocab_size=train_tokenizer(rows))  # fresh tokenizer for this corpus
        tok = TOK_PATH
    global _tok
    _tok = None
    tokenizer(tok)  # explicitly: tokenizer() would otherwise pick up an older ckpt/'s
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    shutil.copy(tok, f'{out}/tokenizer.json')  # keep them together, they must match
    save_tags(rows, out)
    exs = [(ids, len(encode(r.get('prompt', ''))))
           for r in rows for ids in [encode(r.get('prompt', ''), r['post'])[:BLOCK]]]
    exs = [e for e in exs if e[1] < len(e[0])]  # keep only examples with post bytes left after truncation
    n = int(len(exs) * 0.9)
    tr, va = exs[:n], exs[n:]
    assert len(va) > batch, f'need more data: val split has {len(va)} examples'

    model = (LlamaForCausalLM.from_pretrained(init) if pre else LlamaForCausalLM(LlamaConfig(**cfg))).to(DEV)
    print(f'{len(exs)} examples ({sum(len(i) for i, _ in exs):,} tokens), '
          f'{model.num_parameters() / 1e6:.1f}M params on {DEV}, ' + (f'from {init}/' if pre else 'random init'))
    # the same ~1000 val posts every eval: fresh random ones each time made val loss noisy
    # enough to trip early stopping while the model was still improving
    vb = [batch_of(va, batch) for _ in range(64)]
    return fit(model, lambda: batch_of(tr, batch), vb, iters, lr, out)


def fit(model, get, vb, iters, lr, out, patience=6):
    """AdamW on a cosine schedule; every 250 iters, eval on the fixed val batches vb and keep the best in out/.
    Stop after patience evals without improvement (None: never)."""
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.1)
    sched = get_cosine_schedule_with_warmup(opt, 100, iters)
    best, stale = float('inf'), 0
    for it in range(iters + 1):
        if it % 250 == 0:
            model.eval()
            with torch.no_grad():
                val = sum(model(input_ids=x, labels=y).loss.item() for x, y in vb) / len(vb)
            model.train()
            print(f'iter {it}  val loss {val:.3f}')
            if val < best:  # ponytail: best-val weights only, no optimizer state; pretrain resumes from weights alone
                best, stale = val, 0
                model.save_pretrained(out)
            else:
                stale += 1
                if stale == patience:  # 6 evals (1500 iters) without improvement: it is overfitting, stop
                    print(f'early stop at {it}, best val {best:.3f}')
                    break
        x, y = get()
        loss = model(input_ids=x, labels=y).loss  # HF shifts labels internally
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
    return best


def fetch_pretrain(dst='data/pretrain.jsonl', mb=1000):
    """Stream the first ~mb MB of PRETRAIN into dst. 1GB is ~250M tokens, about 20 per param: the
    compute-optimal amount for a model this size."""
    from datasets import load_dataset
    n = 0
    with open(dst + '.part', 'w') as fh:  # renamed at the end: a cut-off download must not pass for a whole one
        for r in load_dataset(*PRETRAIN, split='train', streaming=True):
            fh.write(json.dumps({'text': r['text']}) + '\n')
            n += len(r['text'].encode())
            if n >= mb * 1e6:
                break
    pathlib.Path(dst + '.part').replace(dst)
    print(f'{n / 1e6:.0f} MB of {PRETRAIN[0]} -> {dst}')


def stream_ids(src, t, n=1000):
    """The docs in src as one long token stream, END after each; uint16 halves the RAM and the vocab fits.
    n docs at a time: all 2.5GB of text and its Encodings at once ran a 16GB machine out of memory."""
    import numpy as np
    with open(src) as fh:
        return np.concatenate([np.array([i for e in t.encode_batch([json.loads(l)['text'] for l in docs]) for i in e.ids + [END]],
                                        dtype=np.uint16) for docs in iter(lambda: list(itertools.islice(fh, n)), [])])


def pretrain(iters=30000, src=None, posts='data/posts.jsonl', out='pre', batch=16, lr=6e-4, mb=1000, size='13m'):
    """Random init (a model of SIZES[size]) -> next-token prediction on mb MB of general English, saved to out/;
    train() starts from it. Run prep first: the tokenizer is fit here, to posts and plain English alike, and
    both stages share it. If out/ already has a model (a cut-off run), continue from its weights and size;
    delete out/ to start over."""
    import numpy as np
    src = src or ('data/pretrain.jsonl' if mb == 1000 else f'data/pretrain-{mb}.jsonl')
    if not pathlib.Path(src).exists():
        fetch_pretrain(src, mb)
    resume = pathlib.Path(out, 'config.json').exists()  # weights only: optimizer and LR schedule start fresh
    if not resume:
        with open(src) as fh:
            n = sum(1 for _ in fh)
        with open(src) as fh:  # the first tenth of the docs, alongside the posts
            vocab = train_tokenizer(load_rows(posts) + [{'post': json.loads(l)['text']} for l in itertools.islice(fh, n // 10)])
    # explicitly: tokenizer() would otherwise pick up an older ckpt/'s; resumed weights only speak out/'s
    ids = stream_ids(src, tokenizer(f'{out}/tokenizer.json' if resume else TOK_PATH))
    n = int(len(ids) * 0.99)
    tr, va = ids[:n], ids[n:]

    def window(split):  # random full-length windows, cut across doc boundaries: no padding at all
        ix = torch.randint(len(split) - BLOCK, (batch,)).tolist()
        x = torch.from_numpy(np.stack([split[i:i + BLOCK] for i in ix]).astype(np.int64)).to(DEV)
        return x, x  # every token is a target

    model = (LlamaForCausalLM.from_pretrained(out) if resume
             else LlamaForCausalLM(LlamaConfig(**CFG | SIZES[size] | dict(vocab_size=vocab)))).to(DEV)
    print(f'{len(ids):,} tokens, {model.num_parameters() / 1e6:.1f}M params on {DEV}, '
          + (f'resuming {out}/' if resume else 'random init'))
    pathlib.Path(out).mkdir(parents=True, exist_ok=True)
    if not resume:
        shutil.copy(TOK_PATH, f'{out}/tokenizer.json')
    # no early stop: under one pass over the text it can't overfit, and a resumed run's LR warmup bumps val
    # loss up (3.501 -> 3.518 resuming pre-30m/), and 1500 iters may not be enough to recover from that
    return fit(model, lambda: window(tr), [window(va) for _ in range(64)], iters, lr, out, patience=None)


def test():
    # prep: picks the text column, cleans, drops dupes / shorts / non-English
    import tempfile, pandas as pd
    long = 'Thrilled to share that I completed my certification in cloud engineering ' * 2
    with tempfile.TemporaryDirectory() as d:
        pd.DataFrame({'author': ['a', 'b', 'c', 'd', 'e'],
                      'about': [long * 3] * 5,          # longer than the post, but not a post
                      'links': ['["http://x.com"]'] * 5,  # JSON blob column
                      'prompt': ['Topic: cloud certification', 'x', 'y', 'z', '{"tone": "proud"}'],
                      'content': [long + ' https://lnkd.in/xyz ...see more', long, 'hi',
                                  'Профессиональный рост и новые возможности в этом году ' * 3,
                                  long.replace('cloud', 'data') + ' #cloud #data']}).to_csv(f'{d}/raw.csv', index=False)
        prep(d, f'{d}/posts.jsonl')
        rows = load_rows(f'{d}/posts.jsonl')
        # machine_share: 6 MACHINE posts next to 2 human ones, cut to half the corpus -> 2 of them stay
        pathlib.Path(f'{d}/posts.jsonl').unlink()  # else the next prep reads it back in as a source
        fake = [f'Machine post {i}: leveraging synergy and cross-functional alignment for every single stakeholder in the org, item {i}.' for i in range(6)]
        pd.DataFrame({'english': [f'synergy note {i}' for i in range(6)], 'linkedin': fake}).to_csv(f'{d}/{min(MACHINE)}', index=False)
        prep(d, f'{d}/bal.jsonl', machine_share=0.5)
        bal = load_rows(f'{d}/bal.jsonl')
        assert len(bal) == 4 and sum(r['post'] in fake for r in bal) == 2 and set(bal[0]) == {'prompt', 'post'}, bal
    assert len(rows) == 2, rows                      # dupe, short and Russian dropped
    assert 'http' not in rows[0]['post'] and 'see more' not in rows[0]['post'], rows[0]
    assert rows[0]['prompt'].endswith('opic: cloud certification'), rows[0]  # real prompt, maybe "Write a post about"
    p = rows[1]['prompt']  # JSON blob -> the post's own words, never its hashtags ('cloud' is only a tag there)
    assert p.startswith(ABOUT) and 'cloud' not in p and all(w in rows[1]['post'].lower() for w in p.split('about ')[1].split(', ')), p

    with tempfile.TemporaryDirectory() as d:
        # a tokenizer trained here, on these posts: round-trips text and keeps the special ids
        vocab = train_tokenizer(rows * 20, f'{d}/tok.json', vocab=400)
        global _tok, _tags
        _tok = None
        tokenizer(f'{d}/tok.json')
        txt = 'I am thrilled to announce my cloud certification!'
        ids = encode('promo', txt)
        assert ids[0] == PROMPT and ids[-1] == END and POST in ids, ids
        assert decode(ids[ids.index(POST) + 1:]) == txt, decode(ids[ids.index(POST) + 1:])
        assert len(ids) < len(txt.encode()), 'BPE must beat raw bytes'
        # stream_ids: chunked encoding (here 4 docs, then 2) gives the same stream as encoding it all at once
        docs = ['one doc', 'and another'] * 3
        with open(f'{d}/docs.jsonl', 'w') as fh:
            fh.writelines(json.dumps({'text': s}) + '\n' for s in docs)
        assert stream_ids(f'{d}/docs.jsonl', tokenizer(), n=4).tolist() == [i for s in docs for i in tokenizer().encode(s).ids + [END]]

        # batches: loss is scored on post tokens only, never on the prompt or the padding
        ids, plen = encode('promo', 'hello'), len(encode('promo'))
        x, y = batch_of([(ids, plen)], 1)
        assert (y[0, :plen] == -100).all() and (y[0, len(ids):] == -100).all(), y
        assert y[0, plen:len(ids)].tolist() == ids[plen:], y
        assert x[0, :len(ids)].tolist() == ids, x

        # overfit a tiny model on one post; it must reproduce it from the prompt
        torch.manual_seed(0)
        cfg = CFG | dict(vocab_size=vocab, hidden_size=64, intermediate_size=128,
                         num_hidden_layers=2, num_attention_heads=2)
        model = LlamaForCausalLM(LlamaConfig(**cfg))
        x = torch.tensor([encode('promo', 'I am thrilled to announce...') * 3])
        opt = torch.optim.AdamW(model.parameters(), lr=3e-3)
        for _ in range(300):
            loss = model(input_ids=x, labels=x).loss
            opt.zero_grad(); loss.backward(); opt.step()
        out = generate(model, 'promo', max_new=60, best_of=1, do_sample=False)
        assert out == 'I am thrilled to announce...', repr(out)
        assert complete(model, 'promo', max_new=60, do_sample=False)[0][-1] == END  # stopped on its own
        # pick: the post this prompt explains wins over one any prompt would produce
        good = encode('promo', 'I am thrilled to announce...')[len(encode('promo')):]
        junk = tokenizer().encode('hi').ids + [END]
        assert pick(model, 'promo', [junk, good + [END, END]]) == good, 'pick must prefer the trained post'
        # validate: a real post passes mellea's requirements; one cut off, or with the prompt pasted in, fails
        say = lambda t, end=(END,): tokenizer().encode(t).ids + list(end)
        ok = 'So proud to share that I finished my internship at a robotics startup this summer. Grateful to the team!'
        assert all(validate('Write a post about my internship', say(ok)))
        assert not all(validate('Write a post about my internship', say(ok, ())))
        assert not all(validate('Just got promoted to Senior Engineer', say('Excited to say that Just got promoted to Senior Engineer! ' + ok)))
        # load: a saved checkpoint comes back with its own tokenizer and tags, and writes the same post
        model.save_pretrained(f'{d}/ck')
        shutil.copy(f'{d}/tok.json', f'{d}/ck/tokenizer.json')
        save_tags([{'post': '#Internship #internship'}, {'post': '#internship #rare'}], f'{d}/ck')
        assert generate(load(f'{d}/ck'), 'promo', max_new=60, best_of=1, do_sample=False) == out
        assert _tags == {'internship'}, _tags
        # with tags loaded, a tag coined from the prompt's words fails validation; a real one passes
        p = 'Write a post about finishing my internship'
        assert all(validate(p, say(ok + ' #Internship'))) and not all(validate(p, say(ok + ' #FinishingInternship')))

    # eval: a looping post scores high on repeats, a sane one zero
    assert repeats('a b c a b c a b c') > 0.5 and repeats('one two three four') == 0
    tags = {'internship'}
    r = judge('Write a post about finishing my internship', 'internship', 'Write a post about finishing my internship. #finishinginternship', tags)
    assert r['topic'] and r['echo'] and r['coined'], r
    r = judge('Write a post about finishing my internship', 'internship', 'Done with my internship! #Internship', tags)
    assert r['topic'] and not r['echo'] and not r['coined'] and not r['stock'], r
    assert judge('Write a post about finishing my internship', 'internship', 'Thrilled to wrap up my internship', tags)['stock']
    n = LlamaForCausalLM(LlamaConfig(**CFG | SIZES['30m'])).num_parameters()  # the size its name promises
    assert 28e6 < n < 31e6, n
    _tok = _tags = None
    print('ok')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'train':
        train(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5000, out=sys.argv[4] if len(sys.argv) > 4 else 'ckpt',
              init=sys.argv[5] if len(sys.argv) > 5 else 'pre')
    elif cmd == 'pretrain':
        pretrain(int(sys.argv[2]) if len(sys.argv) > 2 else 30000, out=sys.argv[3] if len(sys.argv) > 3 else 'pre',
                 mb=int(sys.argv[4]) if len(sys.argv) > 4 else 1000, size=sys.argv[5] if len(sys.argv) > 5 else '13m')
    elif cmd == 'generate':
        print(generate(load(), ' '.join(sys.argv[2:])))
    elif cmd == 'fetch':
        fetch(*sys.argv[2:])
    elif cmd == 'prep':
        prep(*sys.argv[2:4], machine_share=float(sys.argv[4]) if len(sys.argv) > 4 else None)
    elif cmd == 'eval':
        evaluate(*sys.argv[2:])
    elif cmd == 'deploy':
        deploy(*sys.argv[2:])
    elif cmd == 'test':
        test()
    else:
        print(__doc__)
