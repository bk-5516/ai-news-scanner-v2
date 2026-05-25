"""Claude API integration for relevance scoring, summarization, and dedup."""
from __future__ import annotations
import json
import logging
from dataclasses import dataclass
from typing import Optional

import anthropic

from .settings import settings

log = logging.getLogger(__name__)

_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


# ── Scoring rubric (cached system prompt) ────────────────────────────────────

_SCORE_SYSTEM_GLOBAL_AI = """\
You are a relevance scorer for an AI intelligence service tracking global AI news in English.
The audience is analysts and researchers who need to understand major developments in:
- Foundation models and AI research (new models, capabilities, benchmarks, papers)
- AI policy and regulation (government actions, standards, laws)
- Hardware and semiconductors (GPUs, chips, inference infrastructure)
- Corporate moves (acquisitions, funding, partnerships, layoffs)
- US AI labs (OpenAI, Anthropic, Google DeepMind, Meta AI, xAI, Mistral, etc.)
- Chinese AI companies competing or collaborating globally (DeepSeek, Baidu, Alibaba, ByteDance, etc.)
- Applications of AI (robotics, coding, healthcare, finance, defense)
- AI safety, alignment, and governance

Score each article 0-10:
10 = Major development (major model release, significant law/regulation, large funding, lab breakthrough)
7-9 = Noteworthy (meaningful product launch, regulatory update, significant research paper, notable corporate move)
4-6 = Marginally relevant (general tech news with AI angle, incremental updates)
1-3 = Barely relevant (passing mention of AI, consumer product fluff, listicle)
0 = Not relevant (completely unrelated to AI)

Keep scores at 1-3 for:
- Consumer product reviews where AI is a minor feature (e.g. "phone's AI camera")
- Generic "AI will change X industry" think-pieces with no concrete news peg
- Earnings reports or stock prices unless they include a major AI strategic announcement
- Event announcements, conference previews, or job postings
- Listicles and roundups without a specific new development

Return ONLY valid JSON array, no other text:
[{"id": "<id>", "score": <0-10>, "reason": "<one concise sentence>"}]"""

_SCORE_SYSTEM_CHINA_AI = """\
You are a relevance scorer for an AI intelligence service tracking Chinese-language AI news.
The audience is analysts tracking China's AI ecosystem: policy, companies, research, and competition.
Sources include Chinese tech media, business papers, and AI-focused outlets.

Score each article 0-10 for relevance to Chinese AI:
10 = Major development (major Chinese AI model/product, significant government policy, large funding round)
7-9 = Noteworthy (Chinese AI company moves, regulatory guidance, research milestones, industry dynamics)
4-6 = Marginally relevant (general tech news in China with some AI angle, global AI news translated)
1-3 = Barely relevant (tangential AI mention, non-AI China tech)
0 = Not relevant (no AI angle, purely financial/market data, unrelated)

Keep scores at 1-3 for:
- General Chinese business or financial news with no AI angle
- Routine translated summaries of global AI news already widely reported
- Government notices that are administrative or non-AI-specific
- Consumer gadget reviews where AI is a minor feature
- Event announcements, conference previews, or job postings

Return ONLY valid JSON array, no other text:
[{"id": "<id>", "score": <0-10>, "reason": "<one concise sentence in English>"}]"""

_SUMMARIZE_SYSTEM = """\
You are an AI news analyst producing concise intelligence summaries.

For each article provided (title + snippet):
1. Write a 2-sentence summary:
   - Sentence 1: What happened (the concrete fact/development)
   - Sentence 2: Why it matters for AI development, policy, or competition
2. Assign 1-3 theme tags from this fixed list only:
   Foundation Models, Policy & Regulation, Hardware & Chips, Corporate Moves,
   Research, Applications, Geopolitics, Funding, Robotics, Safety & Alignment, Cybersecurity
3. Flag duplicates: if two articles clearly cover the same specific event, mark the weaker one
   as duplicate_of: "<id of better article>". Only flag exact same-event duplicates.

Return ONLY valid JSON array, no other text:
[{"id": "<id>", "summary": "<2 sentences>", "themes": ["<tag>", ...], "duplicate_of": null}]

If snippet is empty or too short, write the best summary you can from the title alone.
Write summaries in English regardless of the source article language."""


@dataclass
class ScoreResult:
    article_id: str
    score: int
    reason: str


@dataclass
class SummarizeResult:
    article_id: str
    summary: str
    themes: list[str]
    duplicate_of: Optional[str]


def _score_system(category: str) -> str:
    return _SCORE_SYSTEM_CHINA_AI if category == "china_ai" else _SCORE_SYSTEM_GLOBAL_AI


def _strip_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM responses."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]  # drop opening fence line
        text = text.rsplit("```", 1)[0]  # drop closing fence
    return text.strip()


def score_batch(articles: list[dict], category: str) -> tuple[list[ScoreResult], bool]:
    """Score a batch of articles for relevance. Returns (results, cache_hit)."""
    if not articles:
        return [], False

    article_lines = "\n".join(
        f'[{a["id"]}] Title: {a["title"]}\nSnippet: {a["snippet"] or "(no snippet)"}'
        for a in articles
    )
    user_content = f"Score these {len(articles)} articles:\n\n{article_lines}"

    client = get_client()
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=3072,
        system=[
            {
                "type": "text",
                "text": _score_system(category),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    cache_hit = (response.usage.cache_read_input_tokens or 0) > 0
    raw = _strip_fences(response.content[0].text)

    try:
        data = json.loads(raw)
        results = [
            ScoreResult(
                article_id=str(item["id"]),
                score=int(item.get("score", 0)),
                reason=item.get("reason", ""),
            )
            for item in data
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("score_batch parse error: %s — raw: %.200s", e, raw)
        results = [ScoreResult(article_id=str(a["id"]), score=0, reason="parse error") for a in articles]

    return results, cache_hit


def summarize_batch(articles: list[dict]) -> list[SummarizeResult]:
    """Summarize, theme-tag, and dedup a batch of passing articles."""
    if not articles:
        return []

    article_lines = "\n".join(
        f'[{a["id"]}] Title: {a["title"]}\nSnippet: {a["snippet"] or "(no snippet)"}'
        for a in articles
    )
    user_content = f"Summarize and tag these {len(articles)} articles:\n\n{article_lines}"

    client = get_client()
    response = client.messages.create(
        model=settings.llm_model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": _SUMMARIZE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_content}],
    )

    raw = _strip_fences(response.content[0].text)

    try:
        data = json.loads(raw)
        results = [
            SummarizeResult(
                article_id=str(item["id"]),
                summary=item.get("summary", ""),
                themes=item.get("themes", []),
                duplicate_of=item.get("duplicate_of") or None,
            )
            for item in data
        ]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        log.warning("summarize_batch parse error: %s — raw: %.200s", e, raw)
        results = [
            SummarizeResult(article_id=str(a["id"]), summary="", themes=[], duplicate_of=None)
            for a in articles
        ]

    return results


SCORE_BATCH_SIZE = 20
SUMMARIZE_BATCH_SIZE = 25
