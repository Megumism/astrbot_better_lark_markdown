from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

_original_send_message_chain: Callable[..., Any] | None = None
_patch_token = object()


def _is_markdown_table_separator(line: str) -> bool:
    """Return True when a line looks like a markdown table separator row."""

    stripped = line.strip()
    if "|" not in stripped:
        return False

    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if len(cells) < 2:
        return False

    return all(re.fullmatch(r":?-{3,}:?", cell) is not None for cell in cells)


def _is_markdown_table_segment(text: str) -> bool:
    """Check if a segment is purely a markdown table."""

    lines = text.strip().split("\n")
    if len(lines) < 3:
        return False

    # First line and last line should contain pipes
    if "|" not in lines[0] or "|" not in lines[-1]:
        return False

    # Second line should be separator
    if not _is_markdown_table_separator(lines[1]):
        return False

    # All lines should have pipes (table structure)
    return all("|" in line for line in lines)


def _build_table_card(table_markdown: str) -> dict:
    """Build a Lark card JSON 2.0 with markdown table as the only content.

    Args:
        table_markdown: Markdown table text

    Returns:
        Card JSON structure
    """

    logger.debug("[table_card] Building card for markdown table")

    card_json = {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": table_markdown,
                    "text_align": "left",
                }
            ],
        },
    }

    logger.debug(f"[table_card] Card structure built: {len(card_json)} keys")
    return card_json


async def _send_table_card(
    table_markdown: str,
    lark_client: Any,
    reply_message_id: str | None = None,
    receive_id: str | None = None,
    receive_id_type: str | None = None,
) -> bool:
    """Send a markdown table as a Lark interactive card.

    Args:
        table_markdown: Markdown table text
        lark_client: Lark client instance
        reply_message_id: Reply message ID (optional)
        receive_id: Receiver ID (optional)
        receive_id_type: Receiver ID type (optional)

    Returns:
        True if card sent successfully, False otherwise
    """

    try:
        from astrbot.core.platform.sources.lark.lark_event import (
            LarkMessageEvent,
        )
    except ImportError:
        logger.warning("[table_card] Failed to import LarkMessageEvent")
        return False

    card_json = _build_table_card(table_markdown)

    logger.debug(
        f"[table_card] Sending table card to receive_id={receive_id}, type={receive_id_type}"
    )

    return await LarkMessageEvent._send_interactive_card(
        card_json,
        lark_client,
        reply_message_id=reply_message_id,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
    )


def _split_text_by_markdown_table(text: str) -> list[str]:
    """Split text by ALL markdown tables, returning alternating prefix/table/suffix segments."""

    lines = text.splitlines()
    tables = []  # List of (start_line_index, end_line_index)

    logger.debug(f"[split_text] Processing {len(lines)} lines for markdown tables")

    # Find all markdown tables in the text
    for index in range(len(lines) - 1):
        if "|" not in lines[index]:
            continue
        if not _is_markdown_table_separator(lines[index + 1]):
            continue

        table_start = index
        table_end = index + 2

        while table_end < len(lines):
            current_line = lines[table_end]
            if not current_line.strip() or "|" not in current_line:
                break
            table_end += 1

        logger.debug(f"[split_text] Found table at lines {table_start}-{table_end - 1}")
        tables.append((table_start, table_end))

    if not tables:
        logger.debug("[split_text] No markdown tables found")
        return [text]

    logger.debug(f"[split_text] Found {len(tables)} table(s) total")

    # Build segments by walking through tables
    segments = []
    current_pos = 0

    for table_idx, (table_start, table_end) in enumerate(tables):
        # Add prefix segment (text before this table)
        if current_pos < table_start:
            prefix = "\n".join(lines[current_pos:table_start]).strip("\n")
            if prefix:
                logger.debug(
                    f"[split_text] Adding prefix before table {table_idx} (lines {current_pos}-{table_start - 1})"
                )
                segments.append(prefix)

        # Add table segment
        table = "\n".join(lines[table_start:table_end]).strip("\n")
        logger.debug(
            f"[split_text] Adding table {table_idx} (lines {table_start}-{table_end - 1})"
        )
        segments.append(table)

        current_pos = table_end

    # Add remaining text after last table (if any)
    if current_pos < len(lines):
        suffix = "\n".join(lines[current_pos:]).strip("\n")
        if suffix:
            logger.debug(
                f"[split_text] Adding suffix after last table (lines {current_pos}-{len(lines) - 1})"
            )
            segments.append(suffix)

    logger.debug(f"[split_text] Final result: {len(segments)} segments")
    return segments or [text]


def _should_split_message_chain(message_chain: MessageChain) -> bool:
    """Only split plain-text message chains that contain a markdown table."""

    if not message_chain.chain:
        logger.debug("[should_split] Empty message chain")
        return False

    if not all(isinstance(comp, Plain) for comp in message_chain.chain):
        logger.debug("[should_split] Message chain contains non-Plain components, skip")
        return False

    plain_text = "".join(
        comp.text for comp in message_chain.chain if isinstance(comp, Plain)
    )
    segments = _split_text_by_markdown_table(plain_text)
    should_split = len(segments) > 1
    logger.debug(
        f"[should_split] Text length={len(plain_text)}, segments={len(segments)}, should_split={should_split}"
    )
    return should_split


def _patch_send_message_chain(
    original_send_message_chain: Callable[..., Any],
):
    async def patched_send_message_chain(
        message_chain: MessageChain,
        lark_client: Any,
        reply_message_id: str | None = None,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
    ):
        logger.debug("[send_patch] Intercepted send_message_chain call")

        if not _should_split_message_chain(message_chain):
            logger.debug("[send_patch] No table splitting needed, pass through")
            return await original_send_message_chain(
                message_chain,
                lark_client,
                reply_message_id=reply_message_id,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
            )

        plain_text = "".join(
            comp.text for comp in message_chain.chain if isinstance(comp, Plain)
        )
        segments = _split_text_by_markdown_table(plain_text)

        logger.info(
            "[send_patch] Detected markdown table(s) in outgoing message, splitting into %d segments",
            len(segments),
        )

        for idx, segment in enumerate(segments, 1):
            is_table = _is_markdown_table_segment(segment)
            segment_type = "table" if is_table else "text"
            logger.debug(
                f"[send_patch] Sending segment {idx}/{len(segments)}: {len(segment)} chars ({segment_type})"
            )

            if is_table:
                logger.debug(f"[send_patch] Segment {idx} is a table, sending as card")
                await _send_table_card(
                    segment,
                    lark_client,
                    reply_message_id=reply_message_id,
                    receive_id=receive_id,
                    receive_id_type=receive_id_type,
                )
            else:
                logger.debug(
                    f"[send_patch] Segment {idx} is text, sending as plain message"
                )
                await original_send_message_chain(
                    MessageChain(chain=[Plain(segment)]),
                    lark_client,
                    reply_message_id=reply_message_id,
                    receive_id=receive_id,
                    receive_id_type=receive_id_type,
                )

    return patched_send_message_chain


def _install_patch() -> None:
    """Patch Lark send_message_chain so markdown tables are sent in segments."""

    global _original_send_message_chain

    try:
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    except ImportError as exc:  # noqa: BLE001
        logger.warning("Failed to import LarkMessageEvent, skip patch: %s", exc)
        return

    current_id = getattr(LarkMessageEvent, "_markdown_table_patch_id", None)
    if current_id is _patch_token:
        logger.debug("Markdown table patch already installed.")
        return
    if current_id is not None and current_id is not _patch_token:
        logger.warning(
            "Another plugin seems to have patched LarkMessageEvent.send_message_chain; skip.",
        )
        return

    if _original_send_message_chain is None:
        _original_send_message_chain = LarkMessageEvent.send_message_chain

    setattr(LarkMessageEvent, "_markdown_table_patch_id", _patch_token)
    LarkMessageEvent.send_message_chain = staticmethod(
        _patch_send_message_chain(_original_send_message_chain)
    )
    logger.info("Markdown table split patch installed.")


def _remove_patch() -> None:
    """Restore the original send_message_chain implementation."""

    global _original_send_message_chain

    if _original_send_message_chain is None:
        return

    try:
        from astrbot.core.platform.sources.lark.lark_event import LarkMessageEvent
    except ImportError:
        _original_send_message_chain = None
        return

    current_id = getattr(LarkMessageEvent, "_markdown_table_patch_id", None)
    if current_id is _patch_token:
        LarkMessageEvent.send_message_chain = staticmethod(_original_send_message_chain)
        delattr(LarkMessageEvent, "_markdown_table_patch_id")
        logger.info("Markdown table split patch removed.")

    _original_send_message_chain = None


@register(
    "astrbot_better_lark_markdown",
    "megumism",
    "Split markdown table messages into separate segments and render as cards.",
    "1.1.0",
)
class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        _install_patch()

    async def terminate(self):
        _remove_patch()
