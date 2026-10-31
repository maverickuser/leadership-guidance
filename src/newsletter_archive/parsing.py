"""Small parsing helpers shared by the stages: hardened XML and typed HTML attributes."""

from __future__ import annotations

from typing import Any

from bs4 import Tag
from lxml import etree


def parse_xml(text: str) -> etree._Element:
    """Parse XML without expanding entities or touching the network.

    Raises:
        etree.XMLSyntaxError: the text is not well-formed XML.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.fromstring(text.encode("utf-8"), parser=parser)


def attr(tag: Any, name: str) -> str | None:
    """A tag attribute as one string (bs4 can return a list for class-like attributes)."""
    if not isinstance(tag, Tag):
        return None
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value


def xml_local_name(tag: object) -> str:
    """Element name without its namespace; "" for comments and processing instructions."""
    return etree.QName(tag).localname if isinstance(tag, str) else ""
