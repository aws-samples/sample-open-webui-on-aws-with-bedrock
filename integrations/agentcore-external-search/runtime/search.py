"""Normalize the managed AgentCore Web Search connector 1.2.0 contract.

Only public citation links are returned; no URLs are fetched or resolved.
AWS payloads contain results with required text and optional url/title/date.
Missing URLs are omitted; missing titles fall back to the citation URL.
"""

import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


class SearchContractError(ValueError):
    """Invalid input or malformed response, never an invented empty result."""


class SearchToolError(SearchContractError):
    """An MCP result explicitly marked isError."""


def unique_json_object(pairs) -> dict:
    """Reject duplicate JSON keys at every nesting level without exposing data."""
    result = {}
    for name, value in pairs:
        if name in result:
            raise SearchContractError("duplicate JSON key")
        result[name] = value
    return result


@dataclass(frozen=True)
class SearchRecord:
    link: str
    title: str
    snippet: str


@dataclass(frozen=True)
class SearchResponse:
    records: tuple[SearchRecord, ...]
    omitted_count: int


def validate_search_input(query: str, max_results: int = 10) -> dict:
    """Validate AWS limits without trimming or silently truncating queries."""
    if not isinstance(query, str) or not 1 <= len(query) <= 200:
        raise SearchContractError("query must contain 1 through 200 characters")
    if type(max_results) is not int or not 1 <= max_results <= 25:
        raise SearchContractError("maxResults must be an integer from 1 through 25")
    return {"query": query, "maxResults": max_results}


def _public_url(value: str) -> bool:
    if any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii")
        if parsed.scheme not in ("http", "https") or not host or parsed.username is not None:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_multicast
        except ValueError:
            labels = host.split(".")
            return (
                len(host) <= 253 and len(labels) > 1 and not labels[-1].isdigit()
                and labels[-1] not in {"localhost", "local", "internal", "lan", "home", "onion", "invalid", "test", "example"}
                and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            )
    except (ValueError, UnicodeError):
        return False


def _payload(tool: dict) -> dict:
    candidate = None
    if "structuredContent" in tool:
        structured = tool["structuredContent"]
        if not isinstance(structured, dict) or not isinstance(structured.get("results"), list):
            raise SearchContractError("malformed structured search payload")
        candidate = structured
    content = tool.get("content", [])
    if not isinstance(content, list):
        raise SearchContractError("malformed search content")
    for block in content:
        if not isinstance(block, dict):
            raise SearchContractError("malformed content block")
        if block.get("type") != "text":
            continue
        if not isinstance(block.get("text"), str):
            raise SearchContractError("malformed text content")
        try:
            parsed = json.loads(block["text"], object_pairs_hook=unique_json_object)
        except json.JSONDecodeError:
            continue
        except RecursionError:
            raise SearchContractError("malformed text search payload") from None
        if isinstance(parsed, dict) and "results" in parsed:
            if not isinstance(parsed["results"], list):
                raise SearchContractError("malformed text search payload")
            if candidate is not None and parsed != candidate:
                raise SearchContractError("ambiguous search payloads")
            candidate = parsed
    if candidate is None:
        raise SearchContractError("missing search payload")
    return candidate


def parse_search_response(envelope: dict) -> SearchResponse:
    """Project AWS text/url/title into OWUI snippet/link/title, ignoring metadata."""
    if (not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0"
            or type(envelope.get("id")) not in (str, int) or "error" in envelope):
        raise SearchContractError("invalid search envelope")
    tool = envelope.get("result")
    if not isinstance(tool, dict) or type(tool.get("isError", False)) is not bool:
        raise SearchContractError("malformed tool result")
    if tool.get("isError"):
        raise SearchToolError("search tool failed")
    payload = _payload(tool)
    records, omitted = [], 0
    for result in payload["results"]:
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise SearchContractError("search result requires text")
        if any(field in result and not isinstance(result[field], str) for field in ("url", "title", "publishedDate")):
            raise SearchContractError("malformed optional search fields")
        if not _public_url(result.get("url", "")):
            omitted += 1
            continue
        records.append(SearchRecord(result["url"], result.get("title") or result["url"], result["text"]))
    if omitted and not records:
        raise SearchContractError("search results contain no usable links")
    return SearchResponse(tuple(records), omitted)
