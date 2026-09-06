"""
services.rule_evolution
─────────────────────────
The Rule Evolution Engine — portfolio UI update plan.

A Golden Setup / Toxic Pattern rule is not a one-shot artifact: every trade
review is evidence for or against it, and a review's qualitative feedback
("this needs a volume-confirmation condition", "tighten the stop after this
FOMO chase") is a candidate improvement the rule itself should absorb. This
module is the "compute in Python, propose with the LLM, apply only on user
approval" engine behind that:

  - :func:`sync_rule_adherence_counts` — cheap, deterministic, network-free.
    Recomputes (never increments) every active rule's adherence/violation
    counts from the CURRENT set of closed round trips, so calling it after
    every review is safe and idempotent — re-reviewing the same trades never
    inflates a count.
  - :func:`generate_evolution_proposals` — the expensive half: one LLM call
    that reads the active rules' stats, recent reviews' qualitative feedback,
    and the empirically-synthesized candidates already computed by
    ``journal_analysis.synthesize_rules``, and proposes SPECIFIC evolutions.
    Never auto-applied — every proposal is persisted ``status='pending'``
    until the user reviews it.
  - :func:`apply_proposal` — promotes a rule to its next version (or creates a
    brand-new one), and writes the matching ``rule_evolution_history`` entry
    in the same operation, so the version number and the audit trail can
    never drift apart.

Fabrication is the real risk here, same as everywhere else in this app's
coach: a proposal that cites a trade that doesn't exist, or invents a win
rate, is worse than no proposal. So every number the LLM is allowed to use
is handed to it as already-computed structure (never estimated by the
model), and every ``evidence_trade_ids`` entry is verified against the real
journal before the proposal is persisted.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from agents import llm_utils
from services import db, journal_analysis, portfolio_service, trading_rules

logger = logging.getLogger(__name__)

# A batch of proposals from one synthesis call — enough to be useful, bounded
# so a single review can't flood the user with more evolutions than they can
# meaningfully review at once.
_MAX_PROPOSALS = 5


# =============================================================================
# Adherence / violation — cheap, deterministic, no LLM
# =============================================================================

def evaluate_trade_against_rules(trade: dict) -> dict:
    """
    Every active rule ``trade`` matches, classified into golden vs. toxic.

    ``trade`` needs ``entry_rationale`` and ``emotion_tag`` (a raw journal
    row is enough — this classifies it the same way
    ``journal_analysis``/the pre-trade coach already do). Used to inject
    "here is what your own rules say about this decision" into a
    retrospective review's prompt; the counts themselves are recomputed
    separately by :func:`sync_rule_adherence_counts`, not incremented here.
    """
    segment = {
        "rationale_type": journal_analysis.classify_rationale(trade.get("entry_rationale")),
        "strategy_type": journal_analysis.classify_strategy(trade.get("entry_rationale")),
        "emotion_tag": (trade.get("emotion_tag") or "untagged"),
    }
    return {
        "golden_matches": trading_rules.match_active_rules("golden", segment),
        "toxic_matches": trading_rules.match_active_rules("toxic", segment),
        "custom_matches": trading_rules.match_active_rules("custom", segment),
    }


def _compact(rule: dict) -> dict:
    """Trim a full rule row to what a journal-row badge needs to render."""
    return {"id": rule["id"], "rule_type": rule["rule_type"], "title": rule["title"], "version": rule["version"]}


def match_trades_bulk(trades: list[dict]) -> dict[str, dict]:
    """
    ``evaluate_trade_against_rules`` for many trades at once, trimmed to the
    compact ``{id, rule_type, title, version}`` shape a badge needs (not the
    full rule row) — the journal-row rule-match badges (§B.3 of the portfolio
    UI update plan) need this for every trade in one round trip rather than
    one request per row.
    """
    results: dict[str, dict] = {}
    for trade in trades:
        matches = evaluate_trade_against_rules(trade)
        golden = [_compact(r) for r in matches["golden_matches"]]
        toxic = [_compact(r) for r in matches["toxic_matches"]]
        custom = [_compact(r) for r in matches["custom_matches"]]
        if golden or toxic or custom:
            results[str(trade["id"])] = {"golden": golden, "toxic": toxic, "custom": custom}
    return results


def sync_rule_adherence_counts() -> list[dict]:
    """
    Recompute every active rule's ``adherence_count``/``violation_count``
    from the journal's CURRENT closed round trips.

    Matching a Golden Setup or a user's Custom rule counts as adherence (the
    trade followed a pattern the user endorsed); matching a Toxic Pattern
    counts as a violation (the trade repeated a pattern the user flagged as
    bad). Safe to call after every review — it SETS the totals from scratch
    each time (see ``trading_rules.set_adherence_counts``), so reviewing the
    same trades twice never double-counts them.
    """
    trips = journal_analysis.closed_round_trips()

    tallies: dict[int, dict[str, int]] = {}
    for trip in trips:
        segment = {
            "rationale_type": trip["rationale_type"],
            "strategy_type": trip["strategy_type"],
            "emotion_tag": trip["emotion_tag"],
        }
        for rule_type in ("golden", "custom"):
            for m in trading_rules.match_active_rules(rule_type, segment):
                tallies.setdefault(m["id"], {"adherence": 0, "violation": 0})["adherence"] += 1
        for m in trading_rules.match_active_rules("toxic", segment):
            tallies.setdefault(m["id"], {"adherence": 0, "violation": 0})["violation"] += 1

    updated = []
    for rule in trading_rules.list_rules(active_only=True):
        counts = tallies.get(rule["id"], {"adherence": 0, "violation": 0})
        updated.append(
            trading_rules.set_adherence_counts(
                rule["id"], counts["adherence"], counts["violation"]
            )
        )
    return updated


# =============================================================================
# Evolution proposal synthesis — the LLM half
# =============================================================================

class _ProposalDraft(BaseModel):
    proposal_type: str = Field(
        ..., description="'refine_existing', 'new_rule', 'tighten_risk', or 'deprecate'"
    )
    rule_id: int | None = Field(
        None, description="Existing rule id being changed; null ONLY for 'new_rule'"
    )
    rule_type: str = Field(..., description="'golden', 'toxic', or 'custom'")
    title: str = Field(..., min_length=1)
    conditions: dict[str, str] = Field(default_factory=dict)
    description: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)
    evidence_trade_ids: list[int] = Field(default_factory=list)


class _ProposalBatch(BaseModel):
    proposals: list[_ProposalDraft] = Field(default_factory=list)


_SYSTEM_PROMPT = """\
You are a trading-rules evolution engine. You are given this user's ACTIVE
rules (with real empirical adherence/violation counts), their recent coach
reviews (qualitative feedback on real decisions), and rule candidates already
synthesized from their closed trades. Propose SPECIFIC, evidence-backed
evolutions — tightening a condition, adding a risk parameter, deprecating a
rule that no longer holds, or formalizing a genuinely new pattern the reviews
surfaced.

ABSOLUTE RULES — violating these produces a harmful proposal:
- Every `evidence_trade_ids` entry MUST be a real trade id from THE USER'S
  JOURNAL or RECENT REVIEWS below. Never invent one.
- Never invent a win rate, expectancy, or count. Cite only figures present in
  ACTIVE RULES or RULE CANDIDATES.
- `rule_id` MUST be an id from ACTIVE RULES for 'refine_existing',
  'tighten_risk', or 'deprecate'. It MUST be null for 'new_rule'.
- `conditions` uses ONLY the keys 'rationale_type', 'strategy_type',
  'emotion_tag' — the same three dimensions the rules already use — with
  values drawn from what appears in the journal data (never invent a new
  category name).
- Propose AT MOST 5 proposals, most important first. Do not propose a change
  with no supporting evidence — an empty list is a valid, honest answer when
  the reviews don't actually suggest anything.
- Every `rationale` must name the SPECIFIC review or trade that motivated it
  (e.g. "Trade #14's retrospective review found the 200 EMA support was never
  confirmed before entry") — a generic rationale is not acceptable.

Output ONLY a single JSON object:
{
  "proposals": [
    {
      "proposal_type": "refine_existing|new_rule|tighten_risk|deprecate",
      "rule_id": <int or null>,
      "rule_type": "golden|toxic|custom",
      "title": "<short title>",
      "conditions": {"rationale_type": "...", "strategy_type": "...", "emotion_tag": "..."},
      "description": "<the rule as it would read after this change>",
      "rationale": "<why, citing a specific review/trade>",
      "evidence_trade_ids": [<int>, ...]
    }
  ]
}
"""

_USER_TEMPLATE = """\
=== ACTIVE RULES (id, version, stats) ===
{active_rules}
=== END ACTIVE RULES ===

=== RECENT COACH REVIEWS (qualitative feedback on real decisions) ===
{reviews}
=== END REVIEWS ===

=== EMPIRICALLY-SYNTHESIZED RULE CANDIDATES (computed from closed round trips) ===
{candidates}
=== END CANDIDATES ===

Propose evolutions. An empty list is correct if nothing here actually
supports a change.
"""


def _real_trade_ids(reviews: list[dict]) -> set[int]:
    ids: set[int] = set()
    for r in reviews:
        if r.get("trade_id") is not None:
            ids.add(int(r["trade_id"]))
    for t in portfolio_service.list_trades():
        ids.add(int(t["id"]))
    return ids


async def generate_evolution_proposals(
    *, ticker: str | None = None, review_limit: int = 20,
) -> list[dict]:
    """
    Synthesize and PERSIST (status='pending') up to :data:`_MAX_PROPOSALS`
    evolution proposals from the active rules, recent reviews, and the
    already-computed candidate rules. Never auto-applies anything.

    Returns the newly created proposal rows. An empty list is a legitimate
    outcome — the reviews may simply not support any change right now.
    """
    from services import review_store

    active_rules = trading_rules.list_rules(active_only=True)
    reviews = review_store.list_reviews(ticker=ticker, limit=review_limit)
    edge = await journal_analysis.edge_analytics(ticker=ticker)
    candidates = edge.get("rule_candidates", {"golden_candidates": [], "toxic_candidates": []})

    if not active_rules and not candidates.get("golden_candidates") and not candidates.get("toxic_candidates"):
        logger.info("[rule_evolution] nothing to evolve from yet — no rules or candidates.")
        return []

    reviews_compact = [
        {
            "review_id": r.get("id"),
            "reviewed_at": r.get("created_at"),
            "review_type": r.get("review_type"),
            "ticker": r.get("ticker"),
            "trade_id": r.get("trade_id"),
            "rationale_at_the_time": r.get("rationale_snapshot"),
            "coaching_feedback": (r.get("report") or {}).get("coaching_feedback"),
            "detected_biases": (r.get("report") or {}).get("detected_biases"),
            "priorities": (r.get("report") or {}).get("priorities"),
            "luck_vs_skill": (r.get("report") or {}).get("luck_vs_skill"),
        }
        for r in reviews
    ]

    import json

    user_prompt = _USER_TEMPLATE.format(
        active_rules=json.dumps(active_rules, ensure_ascii=False, indent=2, default=str)
        if active_rules else "(No active rules yet.)",
        reviews=json.dumps(reviews_compact, ensure_ascii=False, indent=2, default=str)
        if reviews_compact else "(No reviews yet — do not propose a review-driven change.)",
        candidates=json.dumps(candidates, ensure_ascii=False, indent=2, default=str),
    )

    batch = await llm_utils.generate_structured(
        _SYSTEM_PROMPT, user_prompt, _ProposalBatch, max_output_tokens=4096,
    )

    allowed_trade_ids = _real_trade_ids(reviews)
    allowed_rule_ids = {r["id"] for r in active_rules}
    created: list[dict] = []
    for draft in batch.proposals[:_MAX_PROPOSALS]:
        # Enforce what the prompt only asked for — never trust the model's
        # own compliance with an evidence rule that matters this much.
        verified_trade_ids = [
            tid for tid in draft.evidence_trade_ids if tid in allowed_trade_ids
        ]
        if draft.proposal_type != "new_rule" and draft.rule_id not in allowed_rule_ids:
            logger.warning(
                f"[rule_evolution] dropped proposal {draft.title!r}: "
                f"rule_id {draft.rule_id} is not an active rule."
            )
            continue
        if draft.proposal_type == "new_rule":
            draft.rule_id = None

        proposal = trading_rules.create_proposal(
            proposal_type=draft.proposal_type,
            rule_type=draft.rule_type,
            title=draft.title,
            conditions=draft.conditions,
            description=draft.description,
            rationale=draft.rationale,
            rule_id=draft.rule_id,
            evidence_trade_ids=verified_trade_ids,
        )
        created.append(proposal)

    logger.info(f"[rule_evolution] generated {len(created)} evolution proposal(s).")
    return created


# =============================================================================
# Applying / dismissing a proposal
# =============================================================================

def apply_proposal(proposal_id: int, trigger_source: str = "manual") -> dict:
    """
    Approve and apply one pending proposal: promotes the target rule to its
    next version (or creates a brand-new one), writes the matching
    ``rule_evolution_history`` entry, and marks the proposal 'applied'.

    Raises ``trading_rules.RuleError`` if the proposal doesn't exist or isn't
    pending — applying an already-applied or dismissed proposal would create
    a history entry for a decision that was never actually made now.
    """
    proposal = trading_rules.get_proposal(proposal_id)
    if proposal is None:
        raise trading_rules.RuleError(f"No proposal with id {proposal_id}.")
    if proposal["status"] != "pending":
        raise trading_rules.RuleError(
            f"Proposal {proposal_id} is already '{proposal['status']}' — only a "
            f"pending proposal can be applied."
        )

    details = {
        "rationale": proposal["rationale"],
        "evidence_trade_ids": proposal["evidence_trade_ids"],
        "proposal_id": proposal_id,
    }

    if proposal["rule_id"] is None:
        # A brand-new emergent rule the reviews surfaced.
        rule = trading_rules.create_rule(
            rule_type=proposal["rule_type"],
            title=proposal["title"],
            conditions=proposal["conditions"],
            description=proposal["description"],
            trigger_source=trigger_source,
            evolution_summary=proposal["rationale"],
            trigger_id=proposal_id,
        )
    else:
        change_type = {
            "tighten_risk": "risk_tightened",
            "deprecate": "deprecated",
        }.get(proposal["proposal_type"], "condition_refined")

        rule = trading_rules.apply_evolution_to_rule(
            proposal["rule_id"],
            title=proposal["title"],
            conditions=proposal["conditions"],
            description=proposal["description"],
            notes=proposal["rationale"],
        )
        if proposal["proposal_type"] == "deprecate":
            trading_rules.set_active(rule["id"], False)
            rule = trading_rules.get_rule(rule["id"])

        trading_rules.log_evolution(
            rule["id"], rule["version"], change_type, trigger_source,
            summary=proposal["rationale"], trigger_id=proposal_id, details=details,
        )

    trading_rules.mark_proposal_applied(proposal_id)
    logger.info(
        f"[rule_evolution] applied proposal #{proposal_id} -> rule #{rule['id']} "
        f"v{rule['version']}"
    )
    return rule
