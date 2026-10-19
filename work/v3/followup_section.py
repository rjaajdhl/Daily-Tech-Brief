"""Section 12 ("Follow-up & Deep Dives") of a Daily Brief.

The 02:30 publisher only guarantees the page EXISTS (a row in Daily Brief Sections plus one placeholder paragraph).  The 11:00
follow-up job is the only writer of its content.  Once the page has any content the publisher never touches it again, so a
same-day re-publish cannot wipe a Deep Dive report.  Section 12 is not an article section: it is excluded from issue-count,
inventory and Overview checks, which stay at the original eleven sections.
"""
from __future__ import annotations

KEY = "followup"
NAME = "12. Follow-up & Deep Dives"
ORDER = 12
PLACEHOLDER = ("Answers to your follow-up requests and Deep Dive reports for this brief are added here by the 11:00 job, "
               "based on what you write under \"My Feedback / Follow-up\" on the Index page.")
FEEDBACK_HEADINGS = ("My Feedback / Follow-up", "내 피드백 / 후속 요청")
FEEDBACK_PLACEHOLDERS = {"Leave feedback here."}


def ensure_followup_section(request, headers: dict, *, day: str, parent_brief_id: str, stage: str = "DRAFT",
                            brief_status: str = "Draft", review_needed: bool = True) -> dict:
    """Create the day's page 12 if missing, set its status, and seed the placeholder only when the page is empty."""
    from work import notion_block_writer as NB
    from work.daily_brief_sections import resolve_section_page
    page_id, meta, created = resolve_section_page(
        request, headers, day=day, section_key=KEY, section_name=NAME, parent_brief_id=parent_brief_id, section_order=ORDER,
        issue_count=0, stage=stage, brief_status=brief_status, coverage_status="PASS", review_needed=review_needed,
        quality_note="Follow-up section; content is written by the 11:00 job and is never rewritten by the 02:30 publisher.")
    seeded = False
    if not NB._children(request, page_id, headers):
        NB.append_blocks(request, page_id, headers, NB.markdown_blocks(PLACEHOLDER))
        seeded = True
    if not NB._children(request, page_id, headers):
        raise RuntimeError("followup_section_empty_after_seed")
    return {"page_id": page_id, "url": meta.get("url") or "https://www.notion.so/" + page_id.replace("-", ""), "created": created, "seeded": seeded}


def feedback_tail(tree: list[dict], plain) -> list[dict]:
    """Blocks after the feedback heading that carry user-written content (placeholders and blank paragraphs dropped)."""
    start = None
    for i, b in enumerate(tree):
        if b.get("type") in {"heading_1", "heading_2", "heading_3"} and plain(b).strip() in FEEDBACK_HEADINGS:
            start = i + 1
            break
    if start is None:
        return []
    tail = [b for b in tree[start:] if not (b.get("type") == "paragraph" and (not plain(b).strip() or plain(b).strip() in FEEDBACK_PLACEHOLDERS))]
    return tail


def with_preserved_feedback(index_tree: list[dict], existing_tree: list[dict], plain, native_payload) -> tuple[list[dict], int]:
    """New Index tree whose feedback area keeps the user's existing blocks.  Returns (tree, preserved_block_count)."""
    user_blocks = feedback_tail(existing_tree, plain)
    if not user_blocks:
        return index_tree, 0
    head = next((i for i, b in enumerate(index_tree) if b.get("type") in {"heading_1", "heading_2", "heading_3"}
                 and plain(b).strip() in FEEDBACK_HEADINGS), None)
    if head is None:
        return index_tree, 0
    return [*index_tree[:head + 1], *native_payload(user_blocks)], len(user_blocks)
