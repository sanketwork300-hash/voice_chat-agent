"""Deterministic extraction of details from a customer message.

Identifiers are matched by pattern rather than left to the model, and anything
the model extracts is kept only if it literally appears in the message.
"""

import ipaddress
import re

INSTANCE_ID = re.compile(r"\bi-[0-9a-f]{8,17}\b")
IPV4_OR_CIDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
_ANYWHERE = re.compile(r"\b(anywhere|everyone|everywhere|anyone|any (ip|address)|all (ips|addresses))\b", re.I)
_HUMAN = re.compile(
    r"\b(human|real person|live agent|support agent|representative|supervisor|escalate|speak to (someone|somebody|a person))\b",
    re.I)
_SSH = re.compile(r"\b(ssh|port 22|putty)\b", re.I)
_RDP = re.compile(r"\b(rdp|remote desktop|port 3389)\b", re.I)


def instance_id(text: str) -> str | None:
    match = INSTANCE_ID.search(text.lower())
    return match.group(0) if match else None


def protocol(text: str) -> str | None:
    ssh, rdp = bool(_SSH.search(text)), bool(_RDP.search(text))
    if ssh == rdp:  # neither, or both mentioned
        return None
    return "ssh" if ssh else "rdp"


def source_cidr(text: str) -> str | None:
    """An IPv4 address or range in the message, as a CIDR. "Anywhere" means 0.0.0.0/0."""
    match = IPV4_OR_CIDR.search(text)
    if match:
        try:
            network = ipaddress.ip_network(match.group(0), strict=False)
        except ValueError:
            return None
        return str(network)
    if _ANYWHERE.search(text):
        return "0.0.0.0/0"
    return None


_IAM_ACTION = re.compile(r"(?<![\w:/.])([a-z][a-z0-9-]{1,30}):([A-Z][A-Za-z0-9]+\*?|\*)(?![\w:])")
_NOT_SERVICES = {"arn", "aws", "http", "https"}
_PRINCIPAL_ARN = re.compile(r"arn:aws:iam::\d*:(user|role)/([\w+=,.@-]+)")
_PRINCIPAL_WORDS = re.compile(r"^\s*(?:the\s+)?(?:iam\s+)?(user|role)\s+([\w+=,.@-]{1,64})\s*$", re.I)
_DURATION = re.compile(r"\b(\d{1,4})\s*(seconds?|secs?|s|minutes?|mins?|m)?\b", re.I)


def iam_action(text: str) -> str | None:
    """An IAM action such as ec2:StartInstances, as it appears in an access-denied error."""
    for match in _IAM_ACTION.finditer(text):
        if match.group(1) not in _NOT_SERVICES:
            return match.group(0)
    return None


def principal_arn(text: str) -> tuple[str, str] | None:
    """(kind, name) from an IAM user or role ARN in the message."""
    match = _PRINCIPAL_ARN.search(text)
    return (match.group(1), match.group(2)) if match else None


def principal_answer(text: str) -> tuple[str | None, str] | None:
    """A reply to "which user or role?": (kind, name), with kind None when only a name was given."""
    arn = principal_arn(text)
    if arn:
        return arn
    match = _PRINCIPAL_WORDS.match(text.strip().rstrip("."))
    if match:
        return match.group(1).lower(), match.group(2)
    token = single_token(text)
    return (None, token) if token else None


def seconds(text: str) -> int | None:
    """A duration in the message, in seconds. A bare number is taken as seconds."""
    match = _DURATION.search(text)
    if not match:
        return None
    value = int(match.group(1))
    return value * 60 if (match.group(2) or "").lower().startswith("m") else value


def wants_human(text: str) -> bool:
    return bool(_HUMAN.search(text))


def appears_in(value: str, text: str) -> bool:
    """Whether a model-extracted value was really said by the customer."""
    return value.strip().lower() in text.lower()


# Words a model sometimes returns as a "name" when the customer gave none ("my bucket", "the server").
_GENERIC = {
    "bucket", "s3", "s3 bucket", "instance", "ec2", "ec2 instance", "server", "ec2 server", "machine", "vm",
    "box", "host", "user", "iam user", "role", "iam role", "object", "file", "key", "aws", "account",
    "alarm", "cloudwatch alarm", "function", "lambda", "lambda function", "app", "application", "policy",
    # States and error names that customers quote; a model sometimes mistakes them for the resource's name.
    "ok", "insufficient_data", "insufficient data", "accessdenied", "access denied", "nosuchbucket", "nosuchkey",
    "running", "stopped", "timeout", "403", "404",
}
_DETERMINERS = re.compile(r"^(my|our|the|a|an|this|that|your)\s+", re.I)


def is_generic(value: str) -> bool:
    """Whether a value is a category word rather than the name of a specific resource.

    A single character counts too: no resource is called "H"; it is a fragment of a mis-heard word.
    """
    bare = _DETERMINERS.sub("", value.strip().lower())
    return bare in _GENERIC or len(bare) < 2


_SPOKEN_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10",
}


def spoken_key(name: str) -> str:
    """A name reduced to what survives being said aloud: "web-server-1", "Web Server One" and
    "webserver1" all give "webserver1"."""
    words = re.split(r"[^a-z0-9]+", name.lower())
    return "".join(_SPOKEN_NUMBERS.get(w, w) for w in words)


def single_token(text: str) -> str | None:
    """The message itself, if it is one bare token such as a name or an ID."""
    stripped = text.strip().strip("\"'`.,")
    return stripped if stripped and not re.search(r"\s", stripped) else None
