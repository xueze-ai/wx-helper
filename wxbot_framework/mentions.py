"""真实群艾特名单来自微信 msgsource，不凭可见昵称猜测目标。"""
import re
from xml.etree import ElementTree

from .account import validate_account


def parse_at_members(source):
    if not isinstance(source, str) or len(source) > 4095 or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        return ()
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        return ()
    node = root if root.tag == "atuserlist" else root.find(".//atuserlist")
    if node is None or not node.text:
        return ()
    result = []
    for value in node.text.split(","):
        value = value.strip()
        try:
            validate_account(value)
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def strip_at_labels(text):
    return re.sub(r"@[^@\r\n\u2005]+\u2005", "", text).strip()
