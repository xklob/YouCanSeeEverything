"""Adapters for read-only history collector snapshots, never acquisition logs."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, localcontext

from .common import clean_text, event, parse_date, pick


def canonical_person(person) -> dict:
    if not isinstance(person, dict):
        return {"name": "", "email": "", "date": None}
    normalized_date, _, _ = parse_date(person.get("date"))
    return {
        "name": clean_text(person.get("name", ""), 500),
        "email": clean_text(person.get("email", ""), 500),
        "date": normalized_date,
    }


def git_commit(record: dict, locator: str, limit: int) -> dict:
    author = canonical_person(record.get("author"))
    committer = canonical_person(record.get("committer"))
    personal = record.get("personal_attribution") is True
    category = "git_commit" if personal else "project_context"
    subject = record.get("subject") or "Git commit"
    title = subject if personal else f"Project context · {author['name'] or 'Other author'}: {subject}"
    commit_hash = str(record.get("commit_hash") or "").casefold() or None
    native_id = record.get("stable_id") or ("git:" + commit_hash if commit_hash else None)
    # Provider paths, refs, attribution reasons, and repository membership can
    # differ for the same Git object. They remain in the immutable evidence file;
    # event identity uses only commit facts shared by both collectors.
    metadata = {
        "kind": "git_commit",
        "commit_hash": commit_hash,
        "author": author,
        "committer": committer,
        "personal_attribution": personal,
        "event_type": category,
        "attribution": "personal author attribution supplied by collector" if personal else "repository context; not attributed to the profile owner",
    }
    return event(title, str(record.get("body") or subject).rstrip("\r\n"), locator, native_id=native_id, when=(record.get("author") or {}).get("date"), category=category, metadata=metadata, limit=limit)


def address_hash(value) -> str | None:
    raw = value.get("hash") or value.get("address_hash") or value.get("address") if isinstance(value, dict) else value
    return str(raw).casefold() if raw else None


def unit_amount(raw, decimals) -> str | None:
    """Convert integer base units exactly; floats never enter the conversion."""
    if isinstance(raw, bool) or raw is None or isinstance(raw, float):
        return None
    text = str(raw)
    if not re.fullmatch(r"-?\d+", text):
        return None
    try:
        places = int(decimals)
        if places < 0 or places > 255:
            return None
        with localcontext() as context:
            context.prec = max(80, len(text) + places + 5)
            result = format(Decimal(text).scaleb(-places), "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except (ValueError, TypeError, InvalidOperation):
        return None


def receipt_status(record: dict) -> str:
    # An explicit failure wins over a stale or conflicting success field.
    failures = {"0", "0x0", "error", "failed", "failure", "reverted"}
    successes = {"1", "0x1", "ok", "success", "successful", "confirmed"}
    is_error = record.get("is_error")
    if is_error is True or (isinstance(is_error, (str, int)) and str(is_error).casefold() in {"true", "1"}):
        return "failed"
    if record.get("error") not in (None, "", False, 0) or record.get("revert_reason"):
        return "failed"
    observed = []
    for key in ("status", "receipt_status", "txreceipt_status"):
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("status")
        if value is not None:
            observed.append(str(int(value)) if isinstance(value, bool) else str(value).casefold())
    if record.get("success") is False or any(value in failures for value in observed):
        return "failed"
    if record.get("success") is True or any(value in successes for value in observed):
        return "successful"
    return "unknown"


def ethereum_record(record: dict, locator: str, limit: int) -> dict:
    kind = record["kind"]
    token_transfer = kind == "ethereum_token_transfer"
    internal = kind == "ethereum_internal_transaction"
    account = address_hash(record.get("address"))
    sender = address_hash(record.get("from"))
    recipient = address_hash(record.get("to"))
    if account and sender == account and recipient == account:
        direction = "self_transfer"
    elif account and sender == account:
        direction = "sent"
    elif account and recipient == account:
        direction = "received"
    else:
        direction = "related"
    transaction_hash = pick(record, "transaction_hash", "hash")
    if transaction_hash:
        transaction_hash = str(transaction_hash).casefold()
    chain_id = record.get("chain_id", 1)
    log_index = record.get("log_index")
    trace_ordinal = pick(record, "trace_index", "trace_address", "index", "trace_id", "internal_index")
    native_id = record.get("stable_id")
    if not native_id and transaction_hash:
        if token_transfer and log_index is not None:
            native_id = f"{chain_id}:log:{transaction_hash}:{log_index}"
        elif internal and trace_ordinal is not None:
            trace_id = ".".join(map(str, trace_ordinal)) if isinstance(trace_ordinal, list) else str(trace_ordinal)
            native_id = f"{chain_id}:trace:{transaction_hash}:{trace_id}"
        elif not token_transfer and not internal:
            native_id = f"{chain_id}:tx:{transaction_hash}"
    metadata = {
        "kind": kind,
        "chain_id": chain_id,
        "account": account,
        "from": sender,
        "to": recipient,
        "transaction_hash": transaction_hash,
        "direction": direction,
        "receipt_status": receipt_status(record),
        "block_number": record.get("block_number"),
    }
    symbol = "ETH"
    if token_transfer:
        total = record.get("total") if isinstance(record.get("total"), dict) else {}
        token = record.get("token") if isinstance(record.get("token"), dict) else {}
        raw = pick(total, "value", default=record.get("value"))
        decimals = pick(total, "decimals", default=token.get("decimals"))
        token_type = pick(record, "token_type", default=token.get("type"))
        if decimals is None and str(token_type).upper().replace("-", "") in {"ERC721", "ERC1155"}:
            decimals = 0
        symbol = token.get("symbol") or token.get("name") or "token"
        metadata.update({
            "log_index": log_index,
            "token": {"address": address_hash(token.get("address_hash")), "name": token.get("name"), "symbol": token.get("symbol"), "type": token_type, "decimals": decimals},
            "token_id": pick(total, "token_id", default=record.get("token_id")),
        })
    else:
        raw = record.get("value")
        decimals = 18
        metadata["created_contract"] = address_hash(record.get("created_contract"))
    amount = unit_amount(raw, decimals)
    metadata.update({"value_base_units": str(raw) if raw is not None else None, "amount": amount, "amount_decimals": decimals})
    if internal:
        metadata["trace_ordinal"] = trace_ordinal
        metadata["trace_type"] = record.get("type")
    fee = record.get("fee")
    if isinstance(fee, dict):
        metadata["fee"] = {"type": fee.get("type"), "wei": fee.get("value"), "eth": unit_amount(fee.get("value"), 18)}
    if record.get("method"):
        metadata["method"] = record["method"]
    direction_label = {"self_transfer": "Self transfer", "sent": "Sent", "received": "Received", "related": "Related transfer"}[direction]
    value_label = f"{amount} {symbol}" if amount is not None else f"{symbol} (amount unknown)"
    title = f"{direction_label} {value_label}"
    if metadata["receipt_status"] == "failed":
        title = "Failed attempt · " + title
    body = [f"{direction_label}: {value_label}", f"From: {sender or 'unknown'}", f"To: {recipient or 'contract creation/unknown'}", f"Transaction: {transaction_hash or 'unknown'}", f"Receipt status: {metadata['receipt_status']}"]
    if amount is None and raw is not None:
        body.append(f"Raw source units: {raw}; decimals unavailable or invalid")
    if metadata["receipt_status"] == "failed":
        body.append("This failed attempt does not establish a completed value transfer.")
    if internal:
        body.append(f"Internal trace: {trace_ordinal if trace_ordinal is not None else 'unknown'}")
    # captured_at is acquisition provenance and must never become event time.
    return event(title, "\n".join(body), locator, native_id=native_id, when=record.get("timestamp"), category=kind, metadata=metadata, limit=limit)
