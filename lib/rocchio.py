"""Rank notices by similarity to the ones you kept, away from the ones you hid.

Rocchio relevance feedback. Every notice becomes a TF-IDF vector over the words
in its title and description. So does a query, built from three parts:

    q = alpha * seed  +  beta * mean(kept)  -  gamma * mean(hidden)

The seed comes from profile.json, so the ranking is sensible before a single
button has been clicked; the other two terms pull it toward what you actually
kept and away from what you actually hid. Notices are then ordered by cosine
similarity to q.

Why this rather than a classifier: it works at twenty examples. There is no
model to fit and nothing to overfit -- adding a label moves a centroid, and the
next run reflects it. It also stays explainable, because a cosine is a sum of
per-term contributions, so a score can name the words that produced it exactly
the way the rule scorer names its rules.

Standard library only, so the vectors live in flat arrays rather than dicts:
48,000 notices is enough that per-document dictionaries cost real memory.
"""

from __future__ import annotations

import math
import re
from array import array
from collections import Counter
from typing import Iterable, Iterator, Sequence

# Descriptions are plain text with the occasional stray tag.
TAG = re.compile(r"<[^>]{1,200}>")
WORD = re.compile(r"[a-z0-9][a-z0-9'&/.-]*")

# Words too common in a federal notice to separate anything from anything. The
# short function words are handled by document frequency; these survive it,
# because they genuinely appear in nearly every notice.
STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from by with
without within into over under about as is are was were be been being it its they them
their there here which who whom whose what when where why how all any both each few more
most other some such no nor not only own same so too very can will just should now
shall may must upon per via any-and-all
submit submitted submission submissions requirement requirements required require
provide provided providing include including included work services service performance
period date dates time page pages number numbers information please see below above
following section
""".split())

# The words above are generic English. Every corpus also has its own boilerplate
# -- the terms that appear in nearly every notice and so carry no signal -- and
# that vocabulary says a lot about what you are tracking. It lives in
# profile.json under "stopwords" rather than here, and folds in at load time.
def extend_stopwords(words: Iterable[str]) -> None:
    global STOPWORDS
    STOPWORDS = STOPWORDS | frozenset(w.lower() for w in words if w)

# A term must appear in at least this many notices to enter the vocabulary, and
# in no more than this share of them.
MIN_DF = 3
MAX_DF_RATIO = 0.35

# A title is short and deliberate; a description is long and full of boilerplate.
# Counting a title word for more keeps the description from drowning it.
TITLE_WEIGHT = 3.0

# The tail of a long description is procedural: clauses, submission instructions,
# addresses. The subject matter is stated near the top.
MAX_DESC_CHARS = 20_000

# Plain cosine divides by the document's own length, which flatters very short
# ones: "39--TRAILER,PLATFORM,WAREH" is four words, so matching "platform" alone
# makes it look like a hit. Pivoted normalisation divides by a blend of the
# document's length and the corpus average instead, so a short title has to earn
# its score on more than one word. 1.0 restores plain cosine.
SLOPE = 0.7

# Classic Rocchio weights lean on the query; these lean harder on the feedback,
# because the seed here is a hand-written profile rather than a typed search.
ALPHA, BETA, GAMMA = 1.0, 1.0, 0.6

# Feedback is trusted in proportion to how much of it there is: a centroid of
# two notices is mostly those two notices' boilerplate. Star two things that
# both happen to say "notice of intent to sole source" and, at full weight,
# every sole-source notice in the database climbs. At PRIOR examples a centroid
# gets half its nominal weight, and it approaches full weight from there.
PRIOR = 8.0

# A centroid keeps only its heaviest terms. The tail is the language every
# federal notice shares, and averaging a handful of documents does not cancel it.
CENTROID_TERMS = 120


def tokenize(text: str) -> list[str]:
    """Lowercase words plus adjacent bigrams, so phrases survive as phrases.

    "data analytics" has to mean something the two words separately do not --
    "data" alone is in half the corpus.
    """
    if not text:
        return []
    # Trailing punctuation is part of the token pattern so "web-based" and
    # "e-commerce" survive; it just has to come off the end.
    words = [w.rstrip("-./'&") for w in WORD.findall(TAG.sub(" ", text).lower())]
    words = [w for w in words if len(w) > 1]
    kept = [w for w in words if w not in STOPWORDS]
    # Bigrams span the stopwords rather than being broken by them, or
    # "analysis of data" would yield nothing.
    bigrams = [f"{a} {b}" for a, b in zip(words, words[1:])
               if a not in STOPWORDS or b not in STOPWORDS]
    return kept + bigrams


class Corpus:
    """TF-IDF vectors for a set of notices, held in three flat arrays.

    `starts[i]:starts[i+1]` is the slice of `terms`/`weights` belonging to
    document `i`. Vectors are length-normalised on a pivot rather than on their
    own norm (see SLOPE), so a dot product is a cosine only when slope is 1 --
    near enough to one for ranking, which is all it is used for.
    """

    def __init__(self, ids: list[str], vocab: dict[str, int],
                 starts: array, terms: array, weights: array, idf: list[float]) -> None:
        self.ids = ids
        self.vocab = vocab
        self.starts = starts
        self.terms = terms
        self.weights = weights
        self.idf = idf
        self.term_names = [""] * len(vocab)
        for term, tid in vocab.items():
            self.term_names[tid] = term

    def __len__(self) -> int:
        return len(self.ids)

    @classmethod
    def build(
        cls,
        docs: Iterable[tuple[str, str, str | None]],
        *,
        title_weight: float = TITLE_WEIGHT,
        min_df: int = MIN_DF,
        max_df_ratio: float = MAX_DF_RATIO,
        max_desc_chars: int = MAX_DESC_CHARS,
        slope: float = SLOPE,
    ) -> "Corpus":
        """`docs` yields (notice_id, title, description or None)."""
        ids: list[str] = []
        vocab: dict[str, int] = {}
        raw_starts = array("l", [0])
        raw_terms = array("l")
        raw_counts = array("f")
        df: list[int] = []

        for notice_id, title, description in docs:
            counts: Counter[str] = Counter()
            for token in tokenize(title):
                counts[token] += title_weight
            if description:
                for token in tokenize(description[:max_desc_chars]):
                    counts[token] += 1.0

            ids.append(notice_id)
            for token, count in counts.items():
                tid = vocab.get(token)
                if tid is None:
                    tid = vocab[token] = len(vocab)
                    df.append(0)
                df[tid] += 1
                raw_terms.append(tid)
                raw_counts.append(count)
            raw_starts.append(len(raw_terms))

        n = len(ids)
        ceiling = max(min_df, int(n * max_df_ratio))
        # Renumber so the surviving vocabulary is dense; a term that appears
        # twice in 48,000 notices is a typo, not a signal.
        remap: dict[int, int] = {}
        kept_vocab: dict[str, int] = {}
        idf: list[float] = []
        for term, tid in vocab.items():
            if min_df <= df[tid] <= ceiling:
                new = len(idf)
                remap[tid] = new
                kept_vocab[term] = new
                idf.append(math.log((n + 1) / (df[tid] + 1)) + 1.0)

        # Sublinear tf: the tenth mention of a word says little more than the
        # second, and long descriptions repeat themselves.
        def weigh(j: int, tid: int) -> float:
            return (1.0 + math.log(raw_counts[j])) * idf[tid]

        # Lengths first: the pivot is the average, so it can't be known until
        # every document has been measured.
        norms = [0.0] * n
        for i in range(n):
            total = 0.0
            for j in range(raw_starts[i], raw_starts[i + 1]):
                new = remap.get(raw_terms[j])
                if new is not None:
                    total += weigh(j, new) ** 2
            norms[i] = math.sqrt(total)
        live = [norm for norm in norms if norm]
        pivot = sum(live) / len(live) if live else 1.0

        starts = array("l", [0])
        terms = array("l")
        weights = array("f")
        for i in range(n):
            denom = (1.0 - slope) * pivot + slope * norms[i] or 1.0
            for j in range(raw_starts[i], raw_starts[i + 1]):
                new = remap.get(raw_terms[j])
                if new is None:
                    continue
                terms.append(new)
                weights.append(weigh(j, new) / denom)
            starts.append(len(terms))

        return cls(ids, kept_vocab, starts, terms, weights, idf)

    # -- vectors ------------------------------------------------------------

    def vector(self, i: int) -> dict[int, float]:
        return {self.terms[j]: self.weights[j]
                for j in range(self.starts[i], self.starts[i + 1])}

    def centroid(self, indices: Sequence[int], keep: int = CENTROID_TERMS) -> dict[int, float]:
        """Mean of several documents: their heaviest terms, renormalised.

        Renormalising matters: without it the pull of the feedback would grow
        with the number of examples relative to the seed, and a hundred hides
        would swamp the profile rather than refine it.
        """
        total: dict[int, float] = {}
        if not indices:
            return total
        for i in indices:
            for j in range(self.starts[i], self.starts[i + 1]):
                tid = self.terms[j]
                total[tid] = total.get(tid, 0.0) + self.weights[j]
        if keep and len(total) > keep:
            heaviest = sorted(total, key=total.get, reverse=True)[:keep]
            total = {tid: total[tid] for tid in heaviest}
        return normalize({tid: w / len(indices) for tid, w in total.items()})

    def seed(self, profile: dict) -> dict[int, float]:
        """A query vector from profile.json, for when there is no feedback yet.

        Each keyword tier contributes its terms at the tier's own points, sign
        included -- the resale and negative tiers push away exactly as they do
        in the rule scorer.
        """
        query: dict[int, float] = {}
        for tier, cfg in profile.get("keywords", {}).items():
            if tier.startswith("_") or not isinstance(cfg, dict):
                continue
            points = float(cfg.get("points", 0))
            if not points:
                continue
            for term in cfg.get("terms", []):
                for token in profile_tokens(term):
                    tid = self.vocab.get(token)
                    if tid is None:
                        continue
                    # Scale by idf like any other vector, so a rare phrase
                    # counts for more than a term that is everywhere.
                    query[tid] = query.get(tid, 0.0) + points * self.idf[tid]
        return normalize(query)

    # -- ranking ------------------------------------------------------------

    def cosines(self, query: dict[int, float]) -> list[float]:
        """Similarity of every document to `query`, in document order."""
        get = query.get
        terms, weights, starts = self.terms, self.weights, self.starts
        out = [0.0] * len(self.ids)
        for i in range(len(self.ids)):
            total = 0.0
            for j in range(starts[i], starts[i + 1]):
                qw = get(terms[j])
                if qw:
                    total += qw * weights[j]
            out[i] = total
        return out

    def contributions(self, i: int, query: dict[int, float]) -> list[tuple[str, float]]:
        """The per-term products behind one cosine, largest magnitude first.

        They sum to the cosine exactly, which is the whole point: a score that
        surprises you can be read term by term.
        """
        get = query.get
        parts = []
        for j in range(self.starts[i], self.starts[i + 1]):
            qw = get(self.terms[j])
            if qw:
                parts.append((self.term_names[self.terms[j]], qw * self.weights[j]))
        parts.sort(key=lambda p: -abs(p[1]))
        return parts


def profile_tokens(term: str) -> list[str]:
    """How a profile term enters the vector space.

    A phrase enters as its bigrams only. Its words are already in the profile
    separately where they earn their place, and "data" pulled out of "data
    analytics" would match half the corpus on its own.
    """
    words = [w for w in WORD.findall(term.lower()) if len(w) > 1]
    if len(words) == 1:
        return [] if words[0] in STOPWORDS else words
    return [f"{a} {b}" for a, b in zip(words, words[1:])]


def normalize(vector: dict[int, float]) -> dict[int, float]:
    norm = math.sqrt(sum(w * w for w in vector.values()))
    if not norm:
        return {}
    return {tid: w / norm for tid, w in vector.items()}


def confidence(n: int, prior: float = PRIOR) -> float:
    """How far to trust a centroid built from `n` examples: 0 at none, 1/2 at
    `prior`, approaching 1 from there."""
    return n / (n + prior) if n > 0 else 0.0


def build_query(
    corpus: Corpus,
    seed: dict[int, float],
    kept: Sequence[int],
    hidden: Sequence[int],
    *,
    alpha: float = ALPHA,
    beta: float = BETA,
    gamma: float = GAMMA,
    prior: float = PRIOR,
) -> dict[int, float]:
    """q = alpha*seed + beta*mean(kept) - gamma*mean(hidden), renormalised.

    Each feedback term is scaled by `confidence` in its own centroid, so the
    profile carries the ranking early and hands over as the labels arrive. The
    two sides ramp independently -- hides usually outnumber keeps by a lot, and
    there is no reason to hold back what the hides already know.

    Negative components are kept rather than clipped to zero, as the textbook
    often does. Clipping throws away the only thing hides tell us, and here they
    are the signal that exists in quantity.
    """
    query: dict[int, float] = {tid: alpha * w for tid, w in seed.items()}
    weights = (
        (beta * confidence(len(kept), prior), kept),
        (-gamma * confidence(len(hidden), prior), hidden),
    )
    for weight, group in weights:
        if not group or not weight:
            continue
        for tid, w in corpus.centroid(group).items():
            query[tid] = query.get(tid, 0.0) + weight * w
    return normalize(query)


def calibrate(cosines: Sequence[float], reference: Sequence[float]) -> list[float]:
    """Map cosines onto the distribution of `reference`, preserving order.

    A cosine is roughly 0 to 0.4 and means nothing to a reader. The dashboard's
    bands -- 60 worth reading, 40 worth a skim -- were set against the rule
    scores, so the i-th best notice here takes the i-th best rule score. The
    ranking is entirely Rocchio's; only the numbers are borrowed, and the two
    rankers stay comparable band for band.

    Ties take the same score, and a notice with nothing in common with the query
    scores zero rather than borrowing a number for being least bad.
    """
    n = len(cosines)
    if not n:
        return []
    ladder = sorted(reference) or [0.0]
    # Stretch or squeeze the reference to this many notices.
    step = (len(ladder) - 1) / (n - 1) if n > 1 else 0.0

    order = sorted(range(n), key=lambda i: cosines[i])
    out = [0.0] * n
    rank = 0
    while rank < n:
        # Everything tied at one cosine shares the score at the group's start.
        stop = rank + 1
        while stop < n and cosines[order[stop]] == cosines[order[rank]]:
            stop += 1
        value = 0.0 if cosines[order[rank]] <= 0 else ladder[round(rank * step)]
        for k in range(rank, stop):
            out[order[k]] = value
        rank = stop
    return out


def iter_docs(conn) -> Iterator[tuple[str, str, str | None]]:
    """Every stored notice, with its description when we have one."""
    for row in conn.execute(
        "SELECT o.notice_id, o.title, d.text FROM opportunities o "
        "LEFT JOIN descriptions d USING (notice_id)"
    ):
        yield row[0], row[1] or "", row[2]
